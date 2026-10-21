"""上传剧本：文本提取（txt / md / docx）、按剧本格式自动分集、时长估算。

分集识别是确定性的（免费）：依次尝试常见的分集格式，选出最符合剧本实际写法的一种：
  1. 集标题：第1集 / 第一集 / 【第01集】/ 第3话 / 第3回 / EP01 / Episode 1 / 集数：1
  2. 场次编号：1-1 夜 内 宴会厅 / 1.2 日 外 街道 / 第1集第2场（同一集号的场次归为一集）
  3. 数字标题：独占一行的 1 / 01. / 1、 / （1）（仅在没有更明确的标记时使用）
都没有则整部视为 1 集。结构化交给 bible/script 阶段的 LLM。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

MAX_CHARS = 200_000
HEADING_MAX = 40         # 标题行最大长度，避免把台词里的「第一集」误判为分集

_NUM = r"[0-9０-９零〇一二两三四五六七八九十百]+"
_CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_LEAD = r"^[ \t#*>【\[（(「]*"
_DIALOGUE_RE = re.compile(r"^\s*[^\s：:（(]{1,12}\s*(?:[（(][^）)]{0,12}[）)])?\s*[：:]\s*(.+)$")


@dataclass
class _Format:
    key: str
    label: str
    regex: re.Pattern
    keep_heading: bool      # 标题行是否保留在正文里（场次行含日/夜、内/外、地点，需要保留）
    min_eps: int            # 至少识别出几集才认为是这种格式


FORMATS = [
    _Format("episode", "集标题（第N集 / EP N）", re.compile(
        _LEAD + rf"(?:第\s*(?P<n>{_NUM})\s*[集话話回]|(?:EP|Ep|ep|E|Episode|EPISODE)\s*[.\-_ ]?\s*(?P<n2>\d+)(?![\d\-.])"
        rf"|集\s*数?\s*[:：]\s*(?P<n3>{_NUM}))(?P<rest>.*)$", re.M), False, 1),
    _Format("chapter", "章节标题（第N章）", re.compile(
        _LEAD + rf"第\s*(?P<n>{_NUM})\s*[章节節](?P<rest>.*)$", re.M), False, 2),
    _Format("scene", "场次编号（集-场，如 1-1）", re.compile(
        _LEAD + rf"(?P<n>\d{{1,3}})\s*[-－–—.．_]\s*\d{{1,3}}(?![\d.])(?P<rest>(?:[\s、，,）)】\]一-鿿].*)?)$", re.M), True, 2),
    _Format("number", "数字标题（独占一行的序号）", re.compile(
        r"^[ \t#*]*[（(【\[]?(?P<n>\d{1,3})[）)】\]]?\s*[.．、:：]?\s*(?P<rest>[^：:\n]{0,20})$", re.M), False, 2),
]


def cn_to_int(s: str) -> int:
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch == "百":
            total += (cur or 1) * 100
            cur = 0
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        else:
            cur = _CN_NUM.get(ch, 0)
    return total + cur


def extract_text(filename: str, data: bytes) -> str:
    """把上传文件转成纯文本。支持 .docx 与各种编码的纯文本（utf-8 / gb18030）。"""
    name = (filename or "").lower()
    if name.endswith(".docx") or data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml")
        except (zipfile.BadZipFile, KeyError) as e:
            raise ValueError("无法解析 .docx 文件") from e
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paras = []
        for p in ElementTree.fromstring(xml).iter(f"{ns}p"):
            paras.append("".join(t.text or "" for t in p.iter(f"{ns}t")))
        text = "\n".join(paras)
    elif name.endswith(".doc"):
        raise ValueError("暂不支持旧版 .doc，请另存为 .docx 或 .txt")
    else:
        for enc in ("utf-8-sig", "gb18030", "big5"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法识别文本编码，请另存为 UTF-8")
    text = re.sub(r"\r\n?", "\n", text).replace("　", " ")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_CHARS:
        raise ValueError(f"剧本过长（{len(text)} 字），上限 {MAX_CHARS} 字，请分批上传")
    return text


def _headings(text: str, fmt: _Format) -> list[tuple[int, int, int, str]]:
    """返回 [(行首位置, 行尾位置, 集号, 标题余下部分)]，只保留像标题的短行。"""
    out = []
    for m in fmt.regex.finditer(text):
        line = m.group(0).strip()
        if not line or len(line) > HEADING_MAX:
            continue
        raw = m.group("n") or (m.groupdict().get("n2") or "") or (m.groupdict().get("n3") or "")
        no = cn_to_int(raw)
        if no <= 0:
            continue
        out.append((m.start(), m.end(), no, (m.group("rest") or "").strip()))
    return out


def _plausible(heads: list[tuple[int, int, int, str]], fmt: _Format, text: str) -> bool:
    """集号应当从小号开始、整体递增；数字标题格式要求严格 1..n 且每节有一定篇幅。"""
    nos = [h[2] for h in heads]
    runs = [n for i, n in enumerate(nos) if i == 0 or n != nos[i - 1]]   # 场次格式：同一集连续多行合并
    distinct = sorted(set(runs))
    if len(distinct) < fmt.min_eps:
        return False
    if fmt.key != "episode" and distinct[0] > 3:   # 集标题允许从第 N 集开始（分批上传）
        return False
    rising = sum(1 for a, b in zip(runs, runs[1:]) if b > a)
    if len(runs) > 1 and rising / (len(runs) - 1) < 0.8:
        return False
    if fmt.key == "number":
        if runs != list(range(1, len(runs) + 1)):
            return False
        bodies = [heads[i + 1][0] - h[1] if i + 1 < len(heads) else len(text) - h[1] for i, h in enumerate(heads)]
        if min(bodies) < 30:
            return False
    return True


def detect_format(text: str) -> tuple[_Format | None, list[tuple[int, int, int, str]]]:
    for fmt in FORMATS:
        heads = _headings(text, fmt)
        # 目录行（标题下没有正文）不参与判断
        real = [h for i, h in enumerate(heads)
                if text[h[1]:heads[i + 1][0] if i + 1 < len(heads) else len(text)].strip()]
        if real and _plausible(real, fmt, text):
            return fmt, heads
    return None, []


def estimate_seconds(text: str, cps: float = 4.5) -> int:
    """粗估一集的成片时长：台词按语速、其余动作/场景行每行约 2 秒。"""
    talk, other = 0, 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _DIALOGUE_RE.match(line)
        if m:
            talk += len(re.sub(r"[（(][^）)]*[）)]|\s|[，。！？、…—,.!?\"“”'‘’]", "", m.group(1)))
        else:
            other += 1
    sec = talk / cps + other * 2
    return int(max(15, min(600, round(sec / 5) * 5)))


def _units(text: str) -> tuple[str, list[dict], str]:
    """按识别出的格式切成单元（集 / 章 / 场次归并后的集）。返回 (格式, 单元列表, 序言)。"""
    fmt, heads = detect_format(text)
    if not fmt:
        return "none", [{"no": 1, "title": "", "heading": "", "text": text.strip()}], ""
    # 「第1集第2场 日 内」这类每场都带集号的写法：标题行是场次信息，需保留在正文里
    keep = fmt.keep_heading or any(a[2] == b[2] for a, b in zip(heads, heads[1:])
                                   if text[a[1]:b[0]].strip())
    groups: dict[int, dict] = {}
    for i, (start, end, no, rest) in enumerate(heads):
        stop = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        body = text[start if keep else end:stop].strip()
        title = "" if keep else rest.strip(" ：:·-—】]）)*#")
        g = groups.setdefault(no, {"no": no, "title": title, "heading": text[start:end].strip(), "parts": []})
        if not g["title"] and title:
            g["title"], g["heading"] = title, text[start:end].strip()
        if body:
            g["parts"].append(body)
    units = [{"no": no, "title": g["title"], "heading": "" if keep else g["heading"],
              "text": "\n\n".join(g["parts"]).strip()} for no, g in sorted(groups.items())]
    units = [u for u in units if u["text"]]          # 去掉目录里的空标题
    if not units:
        return "none", [{"no": 1, "title": "", "heading": "", "text": text.strip()}], ""
    return fmt.key, units, text[: heads[0][0]].strip()


def _split_unit(u: dict) -> list[dict]:
    """把一个单元在最接近一半篇幅的段落处切成两半（用于目标集数多于识别单元数时）。"""
    paras = [x for x in re.split(r"\n\s*\n", u["text"]) if x.strip()]
    if len(paras) < 2:
        paras = [x for x in u["text"].splitlines() if x.strip()]
    if len(paras) < 2:
        return [u]
    total, acc, cut = sum(map(len, paras)), 0, 1
    for k, para in enumerate(paras[:-1], 1):
        acc += len(para)
        if acc >= total / 2:
            cut = k
            break
    a, b = "\n\n".join(paras[:cut]), "\n\n".join(paras[cut:])
    return [{**u, "text": a, "part": f"{u.get('part', '')}上"}, {**u, "text": b, "heading": "", "part": f"{u.get('part', '')}下"}]


def _partition(sizes: list[int], k: int) -> list[int]:
    """把连续的单元分成 k 组，使各组篇幅尽量均衡（最小化平方偏差）。返回每组的起始下标。"""
    n = len(sizes)
    pre = [0]
    for x in sizes:
        pre.append(pre[-1] + x)
    mean = pre[-1] / k
    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0
    for g in range(1, k + 1):
        for j in range(g, n - (k - g) + 1):
            for i in range(g - 1, j):
                c = cost[g - 1][i] + (pre[j] - pre[i] - mean) ** 2
                if c < cost[g][j]:
                    cost[g][j], back[g][j] = c, i
    starts, j = [], n
    for g in range(k, 0, -1):
        j = back[g][j]
        starts.append(j)
    return starts[::-1]


def _regroup(units: list[dict], target: int) -> list[dict]:
    units = list(units)
    while len(units) < target:                     # 单元不够：把最长的单元对半切
        big = max(range(len(units)), key=lambda i: len(units[i]["text"]))
        parts = _split_unit(units[big])
        if len(parts) == 1:
            break
        units[big:big + 1] = parts
    starts = _partition([len(u["text"]) for u in units], min(target, len(units))) + [len(units)]
    groups = []
    for a, b in zip(starts, starts[1:]):
        members = units[a:b]
        texts = [(u["heading"] + "\n\n" + u["text"]) if u["heading"] else u["text"] for u in members]
        labels = []
        for u in members:
            lab = (u["heading"].split("：")[0].split(":")[0].strip() if u["heading"] else f"#{u['no']}") + \
                (f"（{u['part']}）" if u.get("part") else "")
            if lab not in labels:
                labels.append(lab)
        groups.append({"title": members[0]["title"], "text": "\n\n".join(texts), "sources": labels})
    return groups


def split_episodes(text: str, cps: float = 4.5, target: int | None = None) -> list[dict]:
    """按自动识别出的剧本格式分集；指定 target 时把识别出的单元（集 / 章 / 场）合并或拆分成 target 集。

    每集：{no, title, text, chars, seconds, format, sources}；第一集可能带 preface（人物表、梗概等）。
    """
    fmt, units, preface = _units(text)
    if target and target != len(units):
        eps = [{"no": i, **g} for i, g in enumerate(_regroup(units, target), 1)]
    else:
        eps = [{"no": u["no"], "title": u["title"], "text": u["text"],
                "sources": [u["heading"].split("：")[0].split(":")[0].strip()] if u["heading"] else []} for u in units]
    for e in eps:
        e.update(chars=len(e["text"]), seconds=estimate_seconds(e["text"], cps), format=fmt)
    if preface:
        eps[0]["preface"] = preface
    return eps


def analyze(text: str, cps: float = 4.5, target: int | None = None) -> dict:
    fmt, units, _ = _units(text)
    eps = split_episodes(text, cps, target)
    label = next((f.label for f in FORMATS if f.key == fmt), "未发现分集标记")
    return {"chars": len(text), "format": fmt, "format_label": label, "units": len(units),
            "target": target, "episodes": [{k: e[k] for k in ("no", "title", "chars", "seconds", "sources")} for e in eps]}


