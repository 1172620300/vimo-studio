"""阶段（Stage）= 流水线上的一个插件。

每个阶段把自己注册为服务 `stage:<name>`；流水线按 profile 里的 stages 顺序依次调用。
阶段产物写入 project artifacts，已存在则跳过（断点续跑）。
"""
from __future__ import annotations

from importlib import resources
from typing import Any

from ..kernel.context import Context, Plugin
from ..kernel.project import Project


def load_prompt(name: str) -> str:
    return resources.files("drama_agent.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


class Stage(Plugin):
    """子类需设置 name / produces，实现 run()。"""

    name = "stage"
    produces: str = ""          # 产物名（artifacts/<produces>.json）
    paid: bool = False          # 是否调用付费的图像/视频生成；mode=prompts 时跳过
    title: str = ""             # 中文名，用于进度展示

    def apply(self, ctx: Context, config: dict) -> None:
        self.ctx = ctx
        self.config = config
        ctx.provide(f"stage:{self.name}", self)

    def done(self, project: Project) -> bool:
        return bool(self.produces) and project.has(self.produces)

    async def run(self, project: Project, req: dict) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    async def progress(self, project: Project, message: str, **data: Any) -> None:
        evt = project.append("stage/progress", {"stage": self.name, "message": message, **data})
        await self.ctx.emit("stage/progress", project, evt)


def selected_episodes(req: dict) -> set[int] | None:
    """按集出片：request.render_episodes 为本次允许出视频的集号；None 表示全部。"""
    eps = req.get("render_episodes")
    return {int(x) for x in eps} if eps else None


def selected_segments(feed: dict, req: dict) -> list[dict]:
    sel = selected_episodes(req)
    return [s for s in feed.get("segments", []) if sel is None or int(s["episode"]) in sel]


def char_map(bible: dict) -> dict[str, dict]:
    return {c["id"]: c for c in bible.get("characters", [])}


def scene_map(bible: dict) -> dict[str, dict]:
    return {s["id"]: s for s in bible.get("scenes", [])}


def speaker_name(bible: dict, speaker_id: str) -> str:
    c = char_map(bible).get(speaker_id)
    return c["name"] if c else speaker_id
