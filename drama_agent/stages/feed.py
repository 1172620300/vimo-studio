"""L3 投喂层：分镜段 → 七段式视频提示词 + 本段参考图（图N 按段本地编号）+ 导演稿 / 接线单。

七段式：【画幅风格】【场景资产】【核心人物】【站位声明】【时间轴分镜】【音效】【强制禁止项】
确定性编译（不经 LLM），保证可审计、可复现。
"""
from __future__ import annotations

from ..kernel.project import Project
from .base import Stage, char_map, scene_map, speaker_name
from .qc import safety_rewrite

RATIO_NAME = {"9:16": "9:16 竖屏", "16:9": "16:9 横屏", "1:1": "1:1 方屏", "3:4": "3:4 竖屏", "4:3": "4:3 横屏"}
FORBID = "画面内文字、字幕、水印、logo；人物换脸或服装改变；多余手指与肢体变形；画面闪烁；无动机的镜头抖动"
FORBID_EN = "on-screen text, subtitles, watermark, logo; face or costume change; extra fingers or limb distortion; flicker"


def compile_segment(seg: dict, bible: dict, brief: dict, assets_by_id: dict, prev_frame: bool = False) -> dict:
    chars, scenes = char_map(bible), scene_map(bible)
    style = bible.get("style", {})
    refs: list[dict] = []

    def add_ref(asset_id: str) -> int | None:
        a = assets_by_id.get(asset_id)
        if not a:
            return None
        refs.append({"asset": asset_id, "file": a.get("file"), "name": a["name"], "kind": a["kind"]})
        return len(refs)

    sc = scenes.get(seg.get("scene_id"), {})
    n_scene = add_ref(seg.get("scene_id"))
    lines = [f"【画幅风格】{RATIO_NAME.get(brief['ratio'], brief['ratio'])} · {style.get('name', '')} · {style.get('prefix', '')}"]
    scene_desc = f"{sc.get('name', '')}，{sc.get('time_of_day', '')}，{sc.get('lighting', '')}"
    lines.append(f"【场景资产】{'图%d：' % n_scene if n_scene else ''}{scene_desc}")

    people = []
    for cid in seg.get("characters", []):
        c = chars.get(cid)
        if not c:
            continue
        n = add_ref(cid)
        tag = f"图{n}：" if n else ""
        people.append(f"{tag}{c['name']}（{c.get('gender', '')}，{c.get('age', '')}，{c.get('costume', '')}）")
    lines.append("【核心人物】" + ("；".join(people) if people else "无"))
    if prev_frame:
        refs.append({"asset": "__prev_frame__", "file": None, "name": "上一段末帧", "kind": "frame"})
        lines.append(f"【衔接】图{len(refs)} 为起始画面，从该构图自然延续")

    blocking = seg.get("blocking", "")
    for cid, c in chars.items():
        blocking = blocking.replace(cid, c["name"])
    lines.append(f"【站位声明】{blocking}")

    lines.append("【时间轴分镜】")
    for sh in seg.get("shots") or []:
        action = sh.get("action", "")
        for cid, c in chars.items():
            action = action.replace(cid, c["name"])
        row = f"{sh.get('t', '')}｜{sh.get('size', '')}｜{sh.get('camera', '')}：{action}"
        dl = [f"【{speaker_name(bible, d.get('speaker', ''))}（{d.get('emotion', '')}）：\"{d.get('text', '')}\"】"
              for d in sh.get("dialogue") or [] if d.get("text")]
        if dl:
            row += " 台词：" + " ".join(dl)
        lines.append(row)
    lines.append(f"【音效】{seg.get('sfx') or '环境声'}；台词由角色本人原声说出，口型同步")
    lines.append(f"【强制禁止项】{FORBID_EN if brief.get('language') == 'en' else FORBID}")
    prompt = safety_rewrite("\n".join(lines))
    return {"id": seg["id"], "duration": seg["duration"], "scene_id": seg.get("scene_id"),
            "transition_out": seg.get("transition_out", "硬切"), "refs": refs, "prompt": prompt}


def director_markdown(bible: dict, brief: dict, assets: dict, feed: dict) -> str:
    out = [f"# 《{bible.get('title', '')}》导演稿", "",
           f"> 素材形态：{assets.get('mode', 'M1')} · 画幅 {brief['ratio']} · 画风 {bible.get('style', {}).get('name', '')} · "
           f"{brief['episodes']} 集 × {brief['episode_seconds']}s"
           + (" · 来源：上传剧本" if brief.get("source") == "script" else ""), "",
           f"**梗概**：{bible.get('logline', '')}", "", "## 角色", ""]
    for c in bible.get("characters", []):
        out.append(f"- **{c['name']}**（{c['id']}·{c.get('role', '')}）{c.get('appearance', '')}；{c.get('costume', '')}")
    out += ["", "## 素材接线单", "", "| 全局编号 | 类型 | 名称 | 文件 | 出图提示词 |", "|---|---|---|---|---|"]
    for it in assets.get("items", []):
        out.append(f"| {it['ref']} | {it['kind']} | {it['name']} | {it.get('file') or '（待生成）'} | {it['prompt'][:80]}… |")
    out += ["", "## 分段提示词（可直接投喂 Seedance）", ""]
    for seg in feed["segments"]:
        mapping = "；".join(f"图{i} = {r['name']}" for i, r in enumerate(seg["refs"], 1)) or "无参考图"
        out += [f"### {seg['id']} · {seg['duration']}s · 出段衔接：{seg['transition_out']}", "",
                f"挂图顺序：{mapping}", "", "```", seg["prompt"], "```", ""]
    return "\n".join(out)


class FeedStage(Stage):
    name, produces, title = "feed", "feed", "提示词封装"

    async def run(self, project: Project, req: dict) -> dict:
        brief, bible, assets = project.load("brief"), project.load("bible"), project.load("assets")
        by_id = {it["id"]: it for it in assets["items"]}
        chain = brief.get("continuity") == "last_frame"
        segs = []
        for board in project.load("storyboards"):
            for i, seg in enumerate(board.get("segments", [])):
                segs.append({**compile_segment(seg, bible, brief, by_id, prev_frame=chain and i > 0),
                             "episode": board["no"]})
        feed = {"segments": segs}
        project.save(self.produces, feed)
        project.save_text("director.md", director_markdown(bible, brief, assets, feed))
        await self.progress(project, f"已封装 {len(segs)} 段提示词，导演稿 director.md")
        return feed
