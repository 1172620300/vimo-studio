"""观察者插件：任务完成/失败时 POST 到 webhook（飞书/企微/自建服务等）。"""
from __future__ import annotations

import logging

import httpx

from ..kernel.context import Context, Plugin

log = logging.getLogger("drama.webhook")


class WebhookPlugin(Plugin):
    name = "webhook"

    def apply(self, ctx: Context, config: dict) -> None:
        url = config.get("url")
        if not url:
            return

        async def send(project, evt):
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    await c.post(url, json={"project": project.id, "type": evt["type"], "data": evt["data"]})
            except httpx.HTTPError as e:
                log.warning("webhook 发送失败: %s", e)

        ctx.on("pipeline/end", send)
        ctx.on("pipeline/error", send)
