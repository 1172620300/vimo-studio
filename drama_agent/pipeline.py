"""流水线编排：按 profile 的 stages 顺序驱动阶段插件，发出事件，支持断点续跑。

事件（同时写入 project events.jsonl 并在 ctx 上广播，供 SSE / 日志 / 通知插件订阅）：
  pipeline/start · stage/start · stage/progress · stage/skip · stage/end · pipeline/end · pipeline/error
"""
from __future__ import annotations

import logging
import time
import traceback

from .kernel.config import load_config
from .kernel.context import Context
from .kernel.project import Project

log = logging.getLogger("drama.pipeline")


def build_context(profile: str | None = None, patches: list[str] | None = None) -> Context:
    cfg = load_config(profile, patches)
    ctx = Context(config=cfg)
    ctx.load_plugins(cfg.get("plugins", []))
    return ctx


class Pipeline:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.stages: list[str] = ctx.config.get("stages", [])

    async def _emit(self, project: Project, type_: str, data: dict) -> None:
        evt = project.append(type_, data)
        await self.ctx.emit(type_, project, evt)

    async def run(self, project: Project, *, force: list[str] | None = None) -> dict:
        req = project.load("request", {})
        mode = req.get("mode") or self.ctx.config.get("defaults", {}).get("mode", "full")
        force = set(force or [])
        stages = [s for s in self.stages if not (mode == "prompts" and self.ctx.get(f"stage:{s}").paid)]
        await self._emit(project, "pipeline/start", {"stages": stages, "mode": mode, "profile": self.ctx.config.get("profile")})
        current = None
        try:
            for name in stages:
                current = name
                stage = self.ctx.get(f"stage:{name}")
                if name not in force and stage.done(project):
                    await self._emit(project, "stage/skip", {"stage": name, "title": stage.title})
                    await self._emit(project, "stage/end", {"stage": name, "title": stage.title, "skipped": True})
                    continue
                t0 = time.time()
                await self._emit(project, "stage/start", {"stage": name, "title": stage.title})
                await self.ctx.emit("stage/before", project, name)
                await stage.run(project, req)
                await self.ctx.emit("stage/after", project, name)
                await self._emit(project, "stage/end", {"stage": name, "title": stage.title, "seconds": round(time.time() - t0, 1)})
            final = project.load("final") if mode != "prompts" else {"director": "director.md"}
            await self._emit(project, "pipeline/end", {"final": final, "mode": mode})
            return final or {}
        except Exception as e:  # noqa: BLE001
            log.error("流水线失败 [%s] %s", current, e)
            await self._emit(project, "pipeline/error", {"stage": current, "error": str(e)[:1000],
                                                         "trace": traceback.format_exc()[-2000:]})
            raise
