"""L2 分镜层：剧本 → 段（每段 = 一次视频生成）。内置 "机检 → 返修" 闭环。"""
from __future__ import annotations

import asyncio
import json

from ..kernel.project import Project
from .base import Stage, load_prompt
from .qc import check_storyboard, safety_rewrite


def _render_system(brief: dict) -> str:
    s = load_prompt("storyboard")
    for k, v in {"{min_seg}": brief["segment_min"], "{max_seg}": brief["segment_max"],
                 "{seg}": brief["segment_seconds"], "{cps}": brief["cps"]}.items():
        s = s.replace(k, str(v))
    return s


def _sanitize(board: dict) -> dict:
    """合规转译 + 规范段 ID。"""
    no = board.get("no")
    for i, seg in enumerate(board.get("segments", []), 1):
        seg["id"] = f"E{no}-{i:02d}"
        seg["duration"] = int(round(float(seg.get("duration") or 0)))
        seg["blocking"] = safety_rewrite(seg.get("blocking", ""))
        for sh in seg.get("shots") or []:
            sh["action"] = safety_rewrite(sh.get("action", ""))
    return board


class StoryboardStage(Stage):
    name, produces, title = "storyboard", "storyboards", "分镜"

    async def run(self, project: Project, req: dict) -> list:
        brief, bible, scripts = project.load("brief"), project.load("bible"), project.load("scripts")
        llm = self.ctx.get("llm")
        system = _render_system(brief)
        max_fix = int(self.config.get("max_repair_rounds", 2))
        ctx_bible = {"style": bible.get("style"),
                     "characters": [{k: c.get(k) for k in ("id", "name", "appearance", "costume")} for c in bible["characters"]],
                     "scenes": bible["scenes"]}

        async def one(script: dict) -> dict:
            cached = project.load(f"storyboard_E{script['no']}")
            if cached:
                return cached
            user = (f"圣经：{json.dumps(ctx_bible, ensure_ascii=False)}\n\n"
                    f"本集目标时长：{script.get('target_seconds') or brief['episode_seconds']} 秒；画幅 {brief['ratio']}。\n"
                    f"剧本：{json.dumps(script, ensure_ascii=False)}")
            board = _sanitize({**await llm.chat_json(system, user, temperature=0.5), "no": script["no"]})
            for rnd in range(max_fix):
                issues = [i for i in check_storyboard(board, script, bible, brief) if i.level in ("P0", "P1")]
                if not issues:
                    break
                await self.progress(project, f"第 {script['no']} 集分镜返修第 {rnd + 1} 轮（{len(issues)} 项）")
                fix = (user + f"\n\n你上一版分镜：{json.dumps(board, ensure_ascii=False)}\n\n"
                       "机检发现以下问题，请修正后输出完整的新版分镜 JSON（不要删台词，放不下就拆段）：\n"
                       + "\n".join(f"- [{i.code}] {i.segment}: {i.message}" for i in issues))
                board = _sanitize({**await llm.chat_json(system, fix, temperature=0.3), "no": script["no"]})
            project.save(f"storyboard_E{script['no']}", board)
            await self.progress(project, f"第 {script['no']} 集分镜 {len(board.get('segments', []))} 段")
            return board

        boards = await asyncio.gather(*(one(s) for s in scripts))
        boards = sorted(boards, key=lambda b: b["no"])
        project.save(self.produces, boards)
        return boards
