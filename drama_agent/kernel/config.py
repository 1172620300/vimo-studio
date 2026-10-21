"""分层配置：内置默认 → profile YAML → patch YAML → 环境变量(.env)。

借鉴 deepseek-harness 的 profile/patch 叠加思想：后一层按 key 深度覆盖前一层，
任何一项都可以被上层替换，而无需改代码。
"""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / "config" / "profiles"


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 解析（不覆盖已存在的环境变量，便于服务器上用真实环境变量）。"""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def deep_merge(base: dict, patch: dict) -> dict:
    out = deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def _expand(value: Any) -> Any:
    """支持在 YAML 里写 ${ENV_NAME} 或 ${ENV_NAME:-default}。"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        inner = value[2:-1]
        name, _, default = inner.partition(":-")
        return env(name, default or None)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _patch_plugins(cfg: dict, replace: dict, disable: list, plugin_config: dict) -> None:
    """按插件 id（use 字段）打补丁，类似 harness 的 patch-by-id：
    replace:       {旧use: 新use 或 {use, config}}  —— 替换实现
    disable:       [use, ...]                       —— 禁用
    plugin_config: {use: {...}}                     —— 深度合并该插件的 config
    """
    for entry in cfg.get("plugins", []):
        use = entry.get("use")
        if use in replace:
            new = replace[use]
            if isinstance(new, str):
                entry["use"], entry["config"] = new, {}
            else:
                entry["use"], entry["config"] = new["use"], new.get("config", {})
        if use in disable or entry.get("use") in disable:
            entry["enabled"] = False
        if entry.get("use") in plugin_config:
            entry["config"] = deep_merge(entry.get("config") or {}, plugin_config[entry["use"]])


def load_config(profile: str | None = None, patches: list[str] | None = None) -> dict:
    load_dotenv()
    profile = profile or env("DRAMA_PROFILE", "default")
    cfg: dict = {}
    chain = []
    p = PROFILE_DIR / f"{profile}.yaml"
    # profile 可以通过 extends 继承另一个 profile
    while p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        chain.append(data)
        parent = data.pop("extends", None)
        p = PROFILE_DIR / f"{parent}.yaml" if parent else Path("__none__")
    layers = list(reversed(chain)) + [yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {} for p in patches or []]
    for layer in layers:
        replace = layer.pop("replace", {}) or {}
        disable = layer.pop("disable", []) or []
        plugin_config = layer.pop("plugin_config", {}) or {}
        append = layer.pop("plugins_append", []) or []
        cfg = deep_merge(cfg, layer)
        cfg.setdefault("plugins", []).extend(append)
        _patch_plugins(cfg, replace, disable, plugin_config)
    cfg = _expand(cfg)
    cfg["profile"] = profile
    return cfg
