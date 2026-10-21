"""环境自检：检查 .env 是否齐全、各网关是否连通、列出可用模型。绝不打印密钥。

用法: python -m drama_agent.doctor
"""
from __future__ import annotations

import shutil
from urllib.parse import urlparse

import httpx

from .kernel.config import env, load_dotenv

REQUIRED = {
    "LLM": ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"],
    "图像": ["CANGYUAN_BASE_URL", "CANGYUAN_API_KEY", "CANGYUAN_IMAGE_MODEL"],
    "视频": ["CANGYUAN_VIDEO_MODEL"],
}


def _host(url: str | None) -> str:
    if not url:
        return "-"
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}{u.path}"


def _list_models(base: str, key: str) -> list[str]:
    url = base.rstrip("/") + "/models"
    r = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", data if isinstance(data, list) else [])
    return [str(m.get("id", m)) if isinstance(m, dict) else str(m) for m in items]


def main() -> None:
    load_dotenv()
    print("== 环境变量 ==")
    for group, names in REQUIRED.items():
        for n in names:
            print(f"  [{group}] {n:28s} {'已设置' if env(n) else '缺失 ✗'}")
    print(f"  ENABLE_PAID_GENERATION = {env('ENABLE_PAID_GENERATION', 'false')}")
    print(f"  IMAGE_PROVIDER = {env('IMAGE_PROVIDER', '-')}  VIDEO_PROVIDER = {env('VIDEO_PROVIDER', '-')}  LLM_PROVIDER = {env('LLM_PROVIDER', '-')}")
    print(f"  LLM_MODEL = {env('LLM_MODEL', '-')}")
    print(f"  CANGYUAN_IMAGE_MODEL = {env('CANGYUAN_IMAGE_MODEL', '-')}  CANGYUAN_VIDEO_MODEL = {env('CANGYUAN_VIDEO_MODEL', '-')}")
    print(f"  LLM 网关:   {_host(env('LLM_BASE_URL'))}")
    print(f"  图像网关:   {_host(env('CANGYUAN_BASE_URL'))}")
    _ep = env('CANGYUAN_VIDEO_ENDPOINT', '/v1/videos')
    print(f"  视频端点:   {_ep if _ep.startswith('http') else _host(env('CANGYUAN_BASE_URL')) + _ep}")
    print(f"  ffmpeg:     {'OK' if shutil.which('ffmpeg') else '未安装 ✗（无法合成成片）'}")

    print("\n== 连通性 / 模型列表 ==")
    for label, base, key in [
        ("LLM", env("LLM_BASE_URL"), env("LLM_API_KEY")),
        ("苍源", (env("CANGYUAN_BASE_URL") or "").rstrip("/") + "/v1", env("CANGYUAN_API_KEY")),
    ]:
        if not (base and key):
            print(f"  {label}: 跳过（未配置）")
            continue
        try:
            models = _list_models(base, key)
            print(f"  {label}: OK，{len(models)} 个模型")
            for m in models[:80]:
                print(f"     - {m}")
            if label == "苍源":
                vkey = env("CANGYUAN_VIDEO_API_KEY") or key
                vmodels = _list_models(base, vkey) if vkey != key else models
                vm = env("CANGYUAN_VIDEO_MODEL", "sd11-seedance-2.0")
                im = env("CANGYUAN_IMAGE_MODEL", "gpt-image-2")
                if im not in models:
                    print(f"  ⚠ 图像模型 {im} 不在图像密钥可用列表中")
                if vm not in vmodels:
                    sd = [m for m in vmodels if "seedance" in m]
                    print(f"  ⚠ 视频模型 {vm} 不在视频密钥可用列表中，可选：{', '.join(sd) or vmodels[:10]}")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if key and key in msg:
                msg = msg.replace(key, "***")
            print(f"  {label}: 失败 -> {type(e).__name__}: {msg[:200]}")


if __name__ == "__main__":
    main()
