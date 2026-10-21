"""零成本 mock 提供方：本地跑通整条流水线 / CI 测试用。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

from ..kernel.context import Context, Plugin


def _png(path: Path, w: int, h: int, rgb: tuple[int, int, int]) -> Path:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _color(seed: str) -> tuple[int, int, int]:
    d = hashlib.md5(seed.encode()).digest()
    return d[0], d[1], d[2]


class MockImage:
    async def generate(self, prompt: str, out: Path, *, aspect: str = "1:1", **_) -> Path:
        w, h = {"9:16": (288, 512), "16:9": (512, 288)}.get(aspect, (384, 384))
        return _png(out.with_suffix(".png") if out.suffix != ".png" else out, w, h, _color(prompt))


class MockVideo:
    async def generate(self, prompt: str, out: Path, *, duration: int, ratio: str, refs, on_progress=None, **_) -> Path:
        w, h = {"9:16": (360, 640), "16:9": (640, 360)}.get(ratio, (480, 480))
        r, g, b = _color(prompt)
        out.parent.mkdir(parents=True, exist_ok=True)
        if on_progress:
            await on_progress("in_progress", 50)
        if shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-f", "lavfi", "-i", f"color=c=0x{r:02x}{g:02x}{b:02x}:s={w}x{h}:d={duration}:r=24",
                   "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                   "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode(errors="ignore")[:300])
        else:
            out.write_bytes(b"")
        return out


class MockLLM:
    """返回结构正确的示例数据，仅用于测试流水线连通性。"""

    async def chat(self, messages, **_):
        return "ok"

    async def chat_json(self, system: str, user: str, **_):
        from . import mock_data
        return json.loads(json.dumps(mock_data.respond(system, user), ensure_ascii=False))


class MockMediaPlugin(Plugin):
    name = "media-mock"

    def apply(self, ctx: Context, config: dict) -> None:
        ctx.provide("image", MockImage())
        ctx.provide("video", MockVideo())


class MockLLMPlugin(Plugin):
    name = "llm-mock"

    def apply(self, ctx: Context, config: dict) -> None:
        ctx.provide("llm", MockLLM())
