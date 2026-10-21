"""LLM 能力接缝：OpenAI 兼容 Chat Completions（DeepSeek / 苍源 / 任意 new-api 网关）。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from ..kernel.config import env
from ..kernel.context import Context, Plugin

log = logging.getLogger("drama.llm")


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """容错解析：去掉 ```json 包裹、截取第一个完整 JSON 对象/数组。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("模型输出中没有 JSON")
    opener = t[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise LLMError("JSON 不完整（可能被截断）")


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, *, temperature: float = 0.8,
                 max_tokens: int = 32768, timeout: float = 600, concurrency: int = 4,
                 thinking: str = "auto", extra_body: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = thinking          # auto: 先思考，输出被思考耗尽时自动关思考重试 / enabled / disabled
        self.extra_body = extra_body or {}
        self._sem = asyncio.Semaphore(concurrency)

    async def chat(self, messages: list[dict], *, json_mode: bool = False, temperature: float | None = None,
                   max_tokens: int | None = None, retries: int = 3) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        body.update(self.extra_body)
        last: Exception | None = None
        async with self._sem:
            for attempt in range(retries):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as c:
                        r = await c.post(f"{self.base_url}/chat/completions", json=body,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
                    if r.status_code == 400 and json_mode and "response_format" in r.text:
                        body.pop("response_format", None)  # 部分网关不支持 JSON 模式
                        continue
                    if r.status_code >= 500 or r.status_code == 429:
                        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                    r.raise_for_status()
                    data = r.json()
                    choice = data["choices"][0]
                    content = choice["message"].get("content") or ""
                    if not content.strip() and choice.get("finish_reason") == "length":
                        # 推理模型把输出额度全用在思考上：关闭思考后重试
                        if self.thinking == "auto" and "thinking" not in body:
                            log.warning("输出被思考耗尽，关闭思考后重试")
                            body["thinking"] = {"type": "disabled"}
                            continue
                        raise LLMError("输出被截断且无内容（调大 max_tokens）")
                    return content
                except (httpx.HTTPError, LLMError, KeyError) as e:
                    last = e
                    log.warning("LLM 调用失败(%d/%d): %s", attempt + 1, retries, e)
                    await asyncio.sleep(2 * (attempt + 1))
        raise LLMError(f"LLM 调用多次失败: {last}")

    async def chat_json(self, system: str, user: str, *, temperature: float | None = None, retries: int = 3) -> Any:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last: Exception | None = None
        for _ in range(retries):
            text = await self.chat(messages, json_mode=True, temperature=temperature)
            try:
                return extract_json(text)
            except (LLMError, json.JSONDecodeError) as e:
                last = e
                messages = messages[:2] + [
                    {"role": "assistant", "content": text[-4000:]},
                    {"role": "user", "content": f"上面的输出不是合法 JSON（{e}）。请只输出完整、合法的 JSON，不要任何解释。"},
                ]
        raise LLMError(f"无法得到合法 JSON: {last}")


class OpenAICompatLLMPlugin(Plugin):
    name = "llm-openai-compat"

    def apply(self, ctx: Context, config: dict) -> None:
        base = config.get("base_url") or env("LLM_BASE_URL")
        key = config.get("api_key") or env("LLM_API_KEY")
        model = config.get("model") or env("LLM_MODEL")
        if not (base and key and model):
            raise RuntimeError("LLM 未配置：需要 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
        ctx.provide("llm", OpenAICompatLLM(
            base, key, model,
            temperature=float(config.get("temperature", 0.8)),
            max_tokens=int(config.get("max_tokens", 32768)),
            concurrency=int(config.get("concurrency", 4)),
            thinking=str(config.get("thinking", "auto")),
            extra_body=config.get("extra_body"),
        ))
