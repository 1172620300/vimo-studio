"""视频模型能力表（来源：沧元算力 /docs/seedance 与各模型参数页，2026-10-20 核对）。

api 字段：
  "ark"   —— 按秒计费线路：prompt + content=[{type:text}] + reference_image_urls + aspect_ratio + duration
             + resolution + generate_audio（2026-10-20 实测：缺 content 会 404 fail_to_fetch_task；
             参考图放 content 里网关不拉取，放 reference_image_urls 才拉取）
  缺省    —— prompt + duration + aspect_ratio + resolution + reference_image_urls（sd17-seedance-2.0 实测可用）

只收录读过参数页的型号；运行时再与 GET /v1/models 取交集，只展示当前令牌可见的型号。
价格为文档参考价，实时价格以沧元「模型广场」/ GET /api/pricing 为准。
新增型号：先读它的参数页，再按同样字段补一行。
"""
from __future__ import annotations

VIDEO_MODELS: dict[str, dict] = {
    "sd17-seedance-2.0-4": {
        "label": "Seedance 2.0 · 按秒", "min": 5, "max": 15, "resolutions": ["480p", "720p"], "default_resolution": "720p",
        "max_refs": 9, "billing": "second", "price": {"480p": 0.38, "720p": 0.46}, "api": "ark",
        "note": "原生过人脸；按秒计费；参考图/视频/音频",
    },
    "sd17-seedance-2.5-4": {
        "label": "Seedance 2.5 · 按秒", "min": 5, "max": 30, "resolutions": ["480p", "720p", "1080p"],
        "default_resolution": "720p", "max_refs": 30, "billing": "second", "price": {"480p": 0.5}, "api": "ark",
        "note": "原生过人脸；5–30 秒；支持 1080p",
    },
    "sd17-seedance-2.0": {
        "label": "Seedance 2.0 · 按条", "min": 5, "max": 15, "resolutions": ["720p"], "default_resolution": "720p",
        "max_refs": 9, "billing": "clip", "price": {"720p": 3.75},
        "note": "原生过人脸；固定 720p",
    },
    "sd11-seedance-2.0": {
        "label": "Seedance 2.0 · 首尾帧", "min": 4, "max": 15, "resolutions": ["480p", "720p", "1080p"],
        "default_resolution": "720p", "max_refs": 9, "billing": "clip", "price": {"480p": 4.5, "720p": 9.6, "1080p": 18.9},
        "note": "原生过人脸；支持首尾帧",
    },
}
DEFAULT_CAPS = {"api": "simple", "label": "", "min": 5, "max": 15, "resolutions": ["720p"], "default_resolution": "720p",
                "max_refs": 9, "billing": "unknown", "price": {}, "note": "未收录参数页，按保守默认处理"}


def caps(model: str) -> dict:
    return {**DEFAULT_CAPS, **VIDEO_MODELS.get(model, {}), "id": model}


def clamp_duration(model: str, seconds: int) -> int:
    c = caps(model)
    return max(c["min"], min(int(seconds), c["max"]))


def pick_resolution(model: str, wanted: str | None) -> str:
    c = caps(model)
    return wanted if wanted in c["resolutions"] else c["default_resolution"]


def estimate_cost(model: str, resolution: str, durations: list[int]) -> float | None:
    """按文档参考价估算；该清晰度没有参考价时返回 None。"""
    c = caps(model)
    price = c["price"].get(resolution)
    if price is None:
        return None
    gen = [clamp_duration(model, d) for d in durations]
    return round(sum(gen) * price if c["billing"] == "second" else len(gen) * price, 2)
