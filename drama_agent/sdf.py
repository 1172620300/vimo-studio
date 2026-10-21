"""short-drama-factory 接入层：改编方法论注入 + 单集 JSON → 标准排版 → 官方机检。

- adapt_references()：拼接改编所需的方法论原文（网文改编流水线 / 黄金3秒钩子库 / 断章公式 / 台词去AI味），追加到改编提示词
- render_episode()：把本项目的单集 JSON 渲染成 short-drama-factory 的单集标准排版（templates/episode-format.md）
- check_episode()：调用上游 validate_episode.validate()（漫剧档 manju），返回 FAIL / WARN 列表
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

from .vendor.short_drama_factory import validate_episode as _v

_PKG = "drama_agent.vendor.short_drama_factory"
ADAPT_REFS = ["webnovel-adaptation.md", "golden-3s-hook-library.md", "cliffhanger-master-formulas.md",
              "dialogue-doctor-anti-ai.md"]


@lru_cache(maxsize=1)
def adapt_references() -> str:
    root = resources.files(_PKG).joinpath("references")
    parts = [f"<!-- short-drama-factory/references/{n} -->\n{root.joinpath(n).read_text(encoding='utf-8')}" for n in ADAPT_REFS]
    return "\n\n".join(parts)


def _clean(s: str) -> str:
    return " ".join(str(s or "").split())


def render_episode(script: dict, bible: dict, seconds: int, *, next_no: int | None = None) -> str:
    """单集 JSON → 标准排版文本（漫剧档）。人物用姓名，场景用圣经场景名。"""
    chars = {c["id"]: c.get("name", c["id"]) for c in bible.get("characters", [])}
    scenes = {s["id"]: s for s in bible.get("scenes", [])}
    card = script.get("card") or {}
    no = script.get("no", 1)
    out = [f"第【{no}】集：【{_clean(script.get('title')) or '未命名'}】（漫剧档）"]
    for key, label in [("hook_type", "黄金前3秒钩子"), ("conflict_form", "冲突形态"), ("emotion_flow", "情绪流变"),
                       ("props", "本集物证"), ("skeptic", "质疑者配角"), ("obsession", "本集执念"),
                       ("rival_wants", "对手要什么"), ("protagonist_goal", "本集主角主动目标"),
                       ("change", "本集改变了什么"), ("settlement", "本集结算")]:
        if card.get(key):
            out.append(f"【{label}】：{_clean(card[key])}")
    out += [f"【本集时长】：{seconds} 秒", ""]
    for sc in script.get("scenes", []):
        meta = scenes.get(sc.get("scene_id"), {})
        out.append(f"[场景]：{_clean(meta.get('name') or sc.get('scene_id'))}·{_clean(meta.get('time_of_day') or '日')}")
        who = "、".join(chars.get(c, c) for c in sc.get("characters", []))
        if who:
            out.append(f"[人物]：{who}")
        out.append("")
        for b in sc.get("blocks", []):
            text = _clean(b.get("text"))
            if not text:
                continue
            if b.get("type") == "action":
                out.append(f"△ {text}")
            elif b.get("type") == "vo":
                spk = b.get("speaker", "")
                out.append(f"旁白：{text}" if spk in ("旁白", "") or b.get("kind") == "旁白"
                           else f"内心独白（{chars.get(spk, spk)}）：{text}")
            else:
                name = chars.get(b.get("speaker"), b.get("speaker") or "群演")
                emo = _clean(b.get("emotion"))
                out.append(f"{name}（{emo}）：{text}" if emo else f"{name}：{text}")
        out.append("")
    cut = _clean(card.get("cliffhanger"))
    unlock = f"（黑屏：下滑立即解锁第{next_no}集）" if next_no else "（黑屏：全剧终）"
    out.append(f"【本集断章卡点】：{cut or '画面停格在悬念处。'}{unlock}")
    return "\n".join(out)


def fix_hints(fails: list[str]) -> list[str]:
    """把机检 FAIL 翻译成可执行的返修提示（词表直接取自上游脚本，保持与机检口径一致）。"""
    hints = []
    if any(f.startswith("E4") for f in fails):
        hints.append("E4 判定口径：本集前 3 行正文（动作或台词）合起来必须出现以下冲突信号之一："
                     + "、".join(s for s in _v.CONFLICT_SIGNALS if s != "！?")
                     + "；行动坚持型还可用：" + "、".join(_v.ACTION_SIGNALS[:12])
                     + "；且不得出现：" + "、".join(_v.OPEN_BAN_WORDS) + "。请把第一个动作改成冲突顶点画面。")
    if any(f.startswith(("E12", "E13", "E15")) for f in fails):
        hints.append("独白/旁白：内心独白 ≤1 处 ≤20 字，旁白 ≤1 处 ≤12 字；不得相邻、不得在前 3 行、不得是最后一个 block。")
    if any(f.startswith("E3") for f in fails):
        hints.append("场景数 ≤2：合并场景或删掉次要场景。")
    if any(f.startswith("E6") for f in fails):
        hints.append("card.cliffhanger 必须填写断章画面停格描述。")
    return hints


def check_episode(text: str) -> dict:
    fails, warns, net = _v.validate(text, (), "manju")
    return {"fails": list(fails), "warns": list(warns), "net_chars": net, "hints": fix_hints(list(fails))}
