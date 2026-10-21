"""观察者插件：把流水线事件打印到控制台。"""
from __future__ import annotations

import logging

from ..kernel.context import Context, Plugin

log = logging.getLogger("drama")


class ConsoleLogPlugin(Plugin):
    name = "console-log"

    def apply(self, ctx: Context, config: dict) -> None:
        def show(project, evt):
            d = evt["data"]
            t = evt["type"]
            if t == "stage/start":
                log.info("[%s] ▶ %s", project.id, d.get("title") or d["stage"])
            elif t == "stage/end" and not d.get("skipped"):
                log.info("[%s] ✔ %s (%ss)", project.id, d.get("title") or d["stage"], d.get("seconds"))
            elif t == "stage/skip":
                log.info("[%s] ↷ 跳过已完成：%s", project.id, d.get("title") or d["stage"])
            elif t == "stage/progress":
                log.info("[%s]   %s", project.id, d.get("message"))
            elif t == "pipeline/error":
                log.error("[%s] ✘ 失败于 %s：%s", project.id, d.get("stage"), d.get("error"))
            elif t == "pipeline/end":
                log.info("[%s] ★ 完成", project.id)

        for e in ("stage/start", "stage/end", "stage/skip", "stage/progress", "pipeline/error", "pipeline/end"):
            ctx.on(e, show)
