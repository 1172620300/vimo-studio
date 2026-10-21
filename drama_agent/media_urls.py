"""参考素材的公网签名链接。

沧元视频接口的 reference_image_urls 只接受可匿名访问的 HTTPS URL（不接受本地文件或 base64）。
服务部署在公网（PUBLIC_BASE_URL，如 https://drama.imaideo.xyz）时，为资产图生成带过期时间的 HMAC 签名链接：
  {PUBLIC_BASE_URL}/media/{项目ID}/{相对路径}?exp=<unix秒>&sig=<hex>
链接只能访问 images/ 与 segments/*.png，不带访问令牌，过期即失效。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .kernel.config import env
from .kernel.project import data_root

ALLOWED_PREFIXES = ("images/", "segments/")
ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def _secret() -> bytes:
    s = env("DRAMA_MEDIA_SECRET")
    if s:
        return s.encode()
    f = data_root() / ".media_secret"          # 未配置时生成一次并持久化在数据目录
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip().encode()


def allowed(rel: str) -> bool:
    return rel.startswith(ALLOWED_PREFIXES) and rel.lower().endswith(ALLOWED_SUFFIXES) and ".." not in rel


def sign(project_id: str, rel: str, exp: int) -> str:
    return hmac.new(_secret(), f"{project_id}/{rel}:{exp}".encode(), hashlib.sha256).hexdigest()


def verify(project_id: str, rel: str, exp: int, sig: str) -> bool:
    return allowed(rel) and exp >= time.time() and hmac.compare_digest(sign(project_id, rel, exp), sig or "")


def public_url(project_id: str, rel: str, ttl: int = 6 * 3600) -> str | None:
    """返回公网签名链接；未配置 PUBLIC_BASE_URL（如本机运行）时返回 None。"""
    base = (env("PUBLIC_BASE_URL") or "").rstrip("/")
    rel = rel.replace("\\", "/")
    if not base.startswith("https://") or not allowed(rel):
        return None
    exp = int(time.time()) + ttl
    return f"{base}/media/{project_id}/{rel}?exp={exp}&sig={sign(project_id, rel, exp)}"
