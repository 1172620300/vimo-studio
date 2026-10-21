"""机检门禁（借鉴 manju sd-qc / sd-feed 六道闸门）。

每条规则返回 Issue(level, code, segment, message)：
- P0 = 阻断（会导致生成失败或明显错误，必须修）
- P1 = 应修（影响质量，自动返修时会带上）
- P2 = 提示
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..kernel.project import Project
from .base import Stage

ENGINEERING_WORDS = ["提示词", "参考图", "镜头编号", "@图片", "@Image", "分镜稿", "资产编号", "本段", "上一段", "下一段",
                     "如图所示", "prompt", "storyboard"]

SAFETY_REWRITE = {
    "鲜血": "气浪", "流血": "踉跄", "血溅": "火花迸散", "血迹": "污痕", "喷血": "气息一滞", "吐血": "气息一滞",
    "断肢": "被震飞", "砍断": "震开", "割喉": "掠过颈侧", "尸体": "倒地不起的人", "血泊": "尘土",
}

_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’…—\-（）()【】《》·~]")


@dataclass
class Issue:
    level: str
    code: str
    segment: str
    message: str


def norm(text: str) -> str:
    return _PUNCT.sub("", text or "")


def speech_units(text: str, lang: str) -> float:
    if lang == "en":
        return len(re.findall(r"[A-Za-z']+", text or ""))
    return len(norm(text))


def parse_range(t: str) -> tuple[float, float] | None:
    m = re.match(r"\s*([\d.]+)\s*[-~到至]\s*([\d.]+)\s*s?", str(t))
    return (float(m.group(1)), float(m.group(2))) if m else None


def script_lines(script: dict) -> list[tuple[str, str]]:
    return [(b.get("speaker", ""), b.get("text", "")) for sc in script.get("scenes", [])
            for b in sc.get("blocks", []) if b.get("type") in ("dialogue", "vo") and b.get("text")]


def check_storyboard(board: dict, script: dict, bible: dict, brief: dict, max_refs: int = 9) -> list[Issue]:
    issues: list[Issue] = []
    chars = {c["id"] for c in bible.get("characters", [])}
    scenes = {s["id"] for s in bible.get("scenes", [])}
    lang, cps = brief.get("language", "zh"), float(brief.get("cps", 4.5))
    lo, hi = brief.get("segment_min", 4), brief.get("segment_max", 15)
    all_dialogue = []
    total = 0.0
    for seg in board.get("segments", []):
        sid = seg.get("id", "?")
        d = float(seg.get("duration") or 0)
        total += d
        if not lo <= d <= hi:
            issues.append(Issue("P0", "DUR", sid, f"段时长 {d}s 超出模型范围 [{lo},{hi}]"))
        if seg.get("scene_id") not in scenes:
            issues.append(Issue("P0", "SCN", sid, f"未知场景 {seg.get('scene_id')}"))
        unknown = [c for c in seg.get("characters", []) if c not in chars]
        if unknown:
            issues.append(Issue("P1", "CHR", sid, f"未知角色 {unknown}（将不挂参考图）"))
        if len(seg.get("characters", [])) + 1 > max_refs:
            issues.append(Issue("P0", "REF", sid, f"参考图超过上限 {max_refs}"))
        shots = seg.get("shots") or []
        if not shots:
            issues.append(Issue("P0", "SHOT", sid, "没有镜头"))
            continue
        units = 0.0
        fixed = 0
        end_prev = 0.0
        for sh in shots:
            rng = parse_range(sh.get("t", ""))
            if not rng:
                issues.append(Issue("P1", "TIME", sid, f"镜头时间格式错误: {sh.get('t')}"))
            else:
                if abs(rng[0] - end_prev) > 0.6:
                    issues.append(Issue("P2", "TIME", sid, f"镜头时间不连续: {sh.get('t')}"))
                end_prev = rng[1]
            if not sh.get("size") or not sh.get("camera"):
                issues.append(Issue("P1", "CAM", sid, f"镜头 {sh.get('t')} 缺景别或运镜"))
            if str(sh.get("camera", "")).startswith("固定"):
                fixed += 1
            for dl in sh.get("dialogue") or []:
                units += speech_units(dl.get("text", ""), lang)
                all_dialogue.append(norm(dl.get("text", "")))
        if shots and abs(end_prev - d) > 1.0:
            issues.append(Issue("P2", "TIME", sid, f"镜头总长 {end_prev}s 与段时长 {d}s 不一致"))
        if units > d * cps + 1:
            issues.append(Issue("P1", "CPS", sid, f"台词 {int(units)} {'词' if lang == 'en' else '字'} 超过 {d}s×{cps} 的语速上限，念不完会被截断，请拆段"))
        if len(shots) >= 3 and fixed / len(shots) > 0.5:
            issues.append(Issue("P2", "CAM", sid, "固定镜头占比过高"))
    # 台词完整性：剧本每句台词都必须出现在分镜里
    joined = "|".join(all_dialogue)
    for speaker, text in script_lines(script):
        key = norm(text)
        if key and key not in joined and key[: max(6, len(key) // 2)] not in joined:
            issues.append(Issue("P1", "LINE", f"E{script.get('no')}", f"台词丢失：{speaker}「{text}」"))
    target = float(script.get("target_seconds") or brief.get("episode_seconds", 60))
    if total and abs(total - target) / target > 0.35:
        issues.append(Issue("P2", "LEN", f"E{script.get('no')}", f"全集分镜总时长 {total:.0f}s 偏离目标 {target:.0f}s"))
    return issues


def check_prompt(seg_id: str, prompt: str, n_refs: int, max_chars: int) -> list[Issue]:
    issues = []
    if len(prompt) > max_chars:
        issues.append(Issue("P1", "LEN", seg_id, f"提示词 {len(prompt)} 字超过 {max_chars}"))
    for w in ENGINEERING_WORDS:
        if w.lower() in prompt.lower():
            issues.append(Issue("P2", "ENG", seg_id, f"含工程语言「{w}」"))
    for n in {int(x) for x in re.findall(r"图(\d+)", prompt)}:
        if n < 1 or n > n_refs:
            issues.append(Issue("P0", "NUM", seg_id, f"引用了不存在的 图{n}（共 {n_refs} 张）"))
    return issues


def safety_rewrite(text: str) -> str:
    for k in sorted(SAFETY_REWRITE, key=len, reverse=True):
        text = text.replace(k, SAFETY_REWRITE[k])
    return text


def summarize(issues: list[Issue]) -> dict:
    return {
        "P0": sum(i.level == "P0" for i in issues),
        "P1": sum(i.level == "P1" for i in issues),
        "P2": sum(i.level == "P2" for i in issues),
        "issues": [asdict(i) for i in issues],
    }


class QCStage(Stage):
    """出稿前总机检：分镜 + 投喂提示词。P0 存在则阻断付费生成。"""

    name, produces, title = "qc", "qc", "机检门禁"

    async def run(self, project: Project, req: dict) -> dict:
        brief, bible = project.load("brief"), project.load("bible")
        scripts = {s["no"]: s for s in project.load("scripts")}
        feed = project.load("feed")
        max_chars = int(self.config.get("max_prompt_chars", 2000))
        issues: list[Issue] = []
        for board in project.load("storyboards"):
            issues += check_storyboard(board, scripts.get(board["no"], {}), bible, brief)
        for item in feed["segments"]:
            issues += check_prompt(item["id"], item["prompt"], len(item["refs"]), max_chars)
        report = summarize(issues)
        project.save(self.produces, report)
        await self.progress(project, f"机检完成 P0={report['P0']} P1={report['P1']} P2={report['P2']}")
        if report["P0"] and self.config.get("block_on_p0", True):
            raise RuntimeError("机检存在 P0 阻断项，已停止付费生成：" +
                               "；".join(f"{i['segment']} {i['message']}" for i in report["issues"] if i["level"] == "P0")[:500])
        return report
