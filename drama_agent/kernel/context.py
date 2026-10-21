"""微内核：Context = 服务注册表 + 事件总线 + 插件生命周期。

借鉴 deepseek-harness / Cordis 的 "一切皆插件"：
- 插件通过 ctx.provide(name, impl) 贡献服务（如 llm / image / video / stage:script）；
- 插件通过 ctx.on(event, handler) 挂接事件（如 stage/before、stage/after、gate/fail）；
- 所有注册都是可撤销的副作用：插件卸载时自动撤销，不需要给内核打补丁。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("drama")

Handler = Callable[..., Any | Awaitable[Any]]


class Plugin:
    """插件基类。子类设置 name，实现 apply(ctx, config)。"""

    name: str = "plugin"

    def apply(self, ctx: "Context", config: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class _Registration:
    owner: str
    undo: Callable[[], None]


@dataclass
class Context:
    config: dict = field(default_factory=dict)
    _services: dict[str, Any] = field(default_factory=dict)
    _handlers: dict[str, list[tuple[str, Handler]]] = field(default_factory=lambda: defaultdict(list))
    _regs: list[_Registration] = field(default_factory=list)
    _current_owner: str = "core"

    # ---------------- 服务 ----------------
    def provide(self, name: str, impl: Any) -> None:
        prev = self._services.get(name)
        self._services[name] = impl
        owner = self._current_owner

        def undo() -> None:
            if prev is None:
                self._services.pop(name, None)
            else:
                self._services[name] = prev

        self._regs.append(_Registration(owner, undo))

    def get(self, name: str) -> Any:
        if name not in self._services:
            raise KeyError(f"服务未注册: {name}（检查 profile 里是否启用了对应插件）")
        return self._services[name]

    def has(self, name: str) -> bool:
        return name in self._services

    def services(self, prefix: str = "") -> dict[str, Any]:
        return {k: v for k, v in self._services.items() if k.startswith(prefix)}

    # ---------------- 事件 ----------------
    def on(self, event: str, handler: Handler) -> None:
        owner = self._current_owner
        self._handlers[event].append((owner, handler))

        def undo() -> None:
            try:
                self._handlers[event].remove((owner, handler))
            except ValueError:
                pass

        self._regs.append(_Registration(owner, undo))

    async def emit(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """依次调用所有处理器（串行，保证顺序与可拦截性）。"""
        results = []
        for _, h in list(self._handlers.get(event, [])) + list(self._handlers.get("*", [])):
            try:
                r = h(event, *args, **kwargs) if _wants_event_name(h, event) else h(*args, **kwargs)
                if inspect.isawaitable(r):
                    r = await r
                results.append(r)
            except Exception:  # noqa: BLE001 - 观察者异常不应打断主流程
                log.exception("事件处理器异常: %s", event)
        return results

    # ---------------- 插件 ----------------
    def plugin(self, plugin: Plugin, config: dict | None = None) -> None:
        self._current_owner = plugin.name
        try:
            plugin.apply(self, config or {})
        finally:
            self._current_owner = "core"
        log.debug("插件已加载: %s", plugin.name)

    def unload(self, owner: str) -> None:
        for reg in reversed([r for r in self._regs if r.owner == owner]):
            reg.undo()
        self._regs = [r for r in self._regs if r.owner != owner]

    def load_plugins(self, entries: list[dict]) -> None:
        """按 profile 里的 plugins 列表加载：每项 {use: 'module:Class', config: {...}, enabled: bool}。"""
        for entry in entries:
            if entry.get("enabled", True) is False:
                continue
            target = entry["use"]
            mod_name, _, cls_name = target.partition(":")
            if mod_name.startswith("."):
                mod_name = "drama_agent" + mod_name
            cls = getattr(importlib.import_module(mod_name), cls_name)
            self.plugin(cls(), entry.get("config") or {})


def _wants_event_name(h: Handler, event: str) -> bool:
    # 通配处理器 ("*") 会额外收到事件名作为第一个参数
    return getattr(h, "_wildcard", False)


def wildcard(fn: Handler) -> Handler:
    fn._wildcard = True  # type: ignore[attr-defined]
    return fn


def run_sync(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]
