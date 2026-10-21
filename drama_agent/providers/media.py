"""图像 / 视频能力接缝：沧元算力网关实现（契约见沧元 Skill 与 /docs/models/{model}）。

- 图像：POST {base}/v1/images/generations  -> data[0].url | data[0].b64_json
- 视频：POST {base}/v1/videos              -> task_id（异步）
        GET  {base}/v1/videos/{task_id}     -> status=completed 时取 video_url 或 data[0].url
  视频请求只用参数页列出的字段：model / prompt / duration / aspect_ratio / resolution / reference_image_urls。
  参考图必须是可匿名访问的 HTTPS URL（见 media_urls.py）；不 GET /content；已受理的任务超时只轮询、不重提交。
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from ..kernel.config import env, env_bool, env_float
from ..kernel.context import Context, Plugin
from ..video_models import caps, clamp_duration, pick_resolution

log = logging.getLogger("drama.media")


class GenerationError(RuntimeError):
    pass


class TaskPending(GenerationError):
    """任务已受理但客户端等待超时：保留 task_id，下次续跑时继续轮询，不重新提交。"""

    def __init__(self, task_id: str, message: str):
        super().__init__(message)
        self.task_id = task_id


async def download(url: str, out: Path, headers: dict | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as c:
        async with c.stream("GET", url, headers=headers or {}) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
    tmp.replace(out)
    return out


def _status(obj: dict) -> str:
    s = obj.get("status")
    if not s and isinstance(obj.get("data"), dict):
        s = obj["data"].get("status")
    return str(s or "").lower()


def _result_url(obj: dict) -> str | None:
    """契约：completed 时返回 video_url 或 data[0].url；原样使用，不改写。"""
    if isinstance(obj.get("video_url"), str):
        return obj["video_url"]
    data = obj.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("url"):
        return data[0]["url"]
    if isinstance(data, dict):
        return _result_url(data)
    return None


DONE = {"completed", "succeeded", "success"}
FAILED = {"failed", "failure", "error", "cancelled", "canceled", "expired"}


class CangyuanImage:
    def __init__(self, base_url: str, api_key: str, model: str, *, size_map: dict, quality: str | None,
                 concurrency: int = 3, timeout: float = 300):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.size_map = size_map
        self.quality = quality
        self.timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)

    async def generate(self, prompt: str, out: Path, *, aspect: str = "1:1", retries: int = 3,
                       model: str | None = None, size: str | None = None, quality: str | None = None) -> Path:
        body: dict[str, Any] = {"model": model or self.model, "prompt": prompt, "n": 1,
                                "size": size or self.size_map.get(aspect, "1024x1024")}
        selected_quality = quality if quality is not None else self.quality
        if selected_quality:
            body["quality"] = selected_quality
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last: Exception | None = None
        async with self._sem:
            for attempt in range(retries):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as c:
                        r = await c.post(f"{self.base_url}/v1/images/generations", json=body, headers=headers)
                    if r.status_code >= 400:
                        raise GenerationError(f"图像生成 HTTP {r.status_code}: {r.text[:300]}")
                    item = (r.json().get("data") or [{}])[0]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if item.get("b64_json"):
                        out.write_bytes(base64.b64decode(item["b64_json"]))
                        return out
                    if item.get("url"):
                        return await download(item["url"], out)
                    raise GenerationError(f"图像响应无结果: {str(r.json())[:300]}")
                except (httpx.HTTPError, GenerationError) as e:
                    last = e
                    log.warning("图像生成失败(%d/%d): %s", attempt + 1, retries, e)
                    await asyncio.sleep(3 * (attempt + 1))
        raise GenerationError(str(last))


class CangyuanVideo:
    def __init__(self, base_url: str, api_key: str, model: str, *, endpoint: str = "/v1/videos",
                 poll_interval: float = 8, poll_max: int = 300, resolution: str = "720p",
                 extra_body: dict | None = None, concurrency: int = 2):
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self.model = model
        self.endpoint = "/" + endpoint.strip("/")
        self.poll_interval = poll_interval
        self.poll_max = poll_max            # 默认 8s × 300 ≈ 40 分钟（契约建议视频等待 ≥30 分钟）
        self.resolution = resolution
        self.extra_body = extra_body or {}
        self._sem = asyncio.Semaphore(concurrency)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def build_body(self, prompt: str, duration: int, ratio: str, ref_urls: list[str], *,
                   model: str | None = None, resolution: str | None = None) -> dict:
        model = model or self.model
        c = caps(model)
        refs = list(ref_urls)[: c["max_refs"]]
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "duration": clamp_duration(model, duration),
            "aspect_ratio": ratio,
            "resolution": pick_resolution(model, resolution or self.resolution),
        }
        if c["api"] == "ark":
            # 按秒计费线路：必须带方舟格式的 content（只放文本）；参考图仍走 reference_image_urls
            # （2026-10-20 实测：参考图放进 content 时网关不会拉取，放 reference_image_urls 会拉取）
            body["content"] = [{"type": "text", "text": prompt}]
            body["generate_audio"] = True
            body["watermark"] = False
        if refs:
            body["reference_image_urls"] = refs
        body.update(self.extra_body)
        return body

    async def submit(self, body: dict) -> str:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(self.base_url + self.endpoint, json=body, headers=self._headers)
        if r.status_code >= 400:
            raise GenerationError(f"视频提交 HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        inner = data.get("data")
        task_id = data.get("task_id") or data.get("id")
        if not task_id and isinstance(inner, dict):
            task_id = inner.get("task_id") or inner.get("id")
        if not task_id:
            raise GenerationError(f"视频提交未返回任务 id: {str(data)[:300]}")
        return str(task_id)

    async def wait(self, task_id: str, on_progress=None) -> dict:
        url = f"{self.base_url}{self.endpoint}/{task_id}"
        async with httpx.AsyncClient(timeout=30) as c:
            for _ in range(self.poll_max):
                await asyncio.sleep(self.poll_interval)
                try:
                    r = await c.get(url, headers=self._headers)
                    if r.status_code == 429:
                        await asyncio.sleep(self.poll_interval)
                        continue
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("轮询失败，稍后重试: %s", e)
                    continue
                st = _status(data)
                if on_progress:
                    await on_progress(st, data.get("progress"))
                if st in DONE:
                    return data
                if st in FAILED:
                    raise GenerationError(f"视频任务失败（task {task_id}）: "
                                          f"{str(data.get('fail_reason') or data.get('error') or data)[:400]}")
        raise TaskPending(task_id, f"视频任务等待超时，续跑时会继续轮询 task {task_id}")

    async def generate(self, prompt: str, out: Path, *, duration: int, ratio: str, refs: list[str],
                       model: str | None = None, resolution: str | None = None, on_progress=None,
                       task_id: str | None = None,
                       on_task: Callable[[str | None], Awaitable[None]] | None = None, retries: int = 2) -> Path:
        """生成一段视频。refs 为参考图 HTTPS URL。

        - task_id：上次已受理但未取回的任务，直接继续轮询（不重复提交）
        - on_task(task_id)：任务受理后回调，供调用方持久化；完成或失败后回调 None
        - 只有「提交失败」会重试；已受理的任务不会因为超时被重新提交
        """
        async with self._sem:
            if not task_id:
                body = self.build_body(prompt, duration, ratio, refs, model=model, resolution=resolution)
                last: Exception | None = None
                for attempt in range(retries):
                    try:
                        task_id = await self.submit(body)
                        break
                    except (httpx.HTTPError, GenerationError) as e:
                        last = e
                        log.warning("视频提交失败(%d/%d): %s", attempt + 1, retries, e)
                        if "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                            break            # 4xx（参数/权限/通道）重试无意义
                        await asyncio.sleep(5 * (attempt + 1))
                if not task_id:
                    raise GenerationError(str(last))
                log.info("视频任务已受理 %s -> %s（%s · %ss · %s）", out.name, task_id, body["model"],
                         body["duration"], body["resolution"])
                if on_task:
                    await on_task(task_id)
            try:
                data = await self.wait(task_id, on_progress)
            except TaskPending:
                raise
            except GenerationError:
                if on_task:
                    await on_task(None)
                raise
            url = _result_url(data)
            if not url:
                raise GenerationError(f"任务完成但没有结果 URL（task {task_id}）: {str(data)[:300]}")
            path = await download(url, out)      # 结果 URL 可能过期，完成后立即下载
            if on_task:
                await on_task(None)
            return path


class CangyuanMediaPlugin(Plugin):
    """注册 image + video 两个服务。ENABLE_PAID_GENERATION=false 时拒绝加载（防止误花钱）。"""

    name = "media-cangyuan"

    def apply(self, ctx: Context, config: dict) -> None:
        if not env_bool("ENABLE_PAID_GENERATION", False):
            raise RuntimeError("ENABLE_PAID_GENERATION=false：已禁止付费生成。请改用 mock profile 或打开该开关。")
        base = (config.get("base_url") or env("CANGYUAN_BASE_URL", "https://direct-api.cangyuansuanli.cn")).rstrip("/")
        img_cfg = config.get("image", {})
        vid_cfg = config.get("video", {})
        ctx.provide("image", CangyuanImage(
            base.removesuffix("/v1"), img_cfg.get("api_key") or env("CANGYUAN_API_KEY"),
            img_cfg.get("model") or env("CANGYUAN_IMAGE_MODEL", "gpt-image-2"),
            size_map=img_cfg.get("size_map", {"1:1": "1024x1024", "9:16": "1024x1536", "16:9": "1536x1024",
                                              "3:4": "1024x1536", "4:3": "1536x1024"}),
            quality=img_cfg.get("quality"),
            concurrency=int(img_cfg.get("concurrency", 3)),
        ))
        endpoint = vid_cfg.get("endpoint") or env("CANGYUAN_VIDEO_ENDPOINT", "/v1/videos")
        if endpoint.startswith("http"):
            from urllib.parse import urlparse
            endpoint = urlparse(endpoint).path
        ctx.provide("video", CangyuanVideo(
            base, vid_cfg.get("api_key") or env("CANGYUAN_VIDEO_API_KEY") or env("CANGYUAN_API_KEY"),
            vid_cfg.get("model") or env("CANGYUAN_VIDEO_MODEL", "sd17-seedance-2.5-4"),
            endpoint=endpoint,
            poll_interval=env_float("CANGYUAN_POLL_INTERVAL", 8),
            poll_max=int(env_float("CANGYUAN_POLL_MAX_ATTEMPTS", 300)),
            resolution=vid_cfg.get("resolution") or env("CANGYUAN_VIDEO_RESOLUTION", "720p"),
            extra_body=vid_cfg.get("extra_body"),
            concurrency=int(vid_cfg.get("concurrency", 2)),
        ))
