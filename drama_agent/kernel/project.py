"""项目 = 一次短剧生成任务。

借鉴 harness 的 "仅追加会话日志"：所有事实都以事件追加到 events.jsonl，
阶段产物以 JSON 存在 artifacts/ 下（唯一真相），因此任务可以中断后从断点续跑。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ROOT, env

_lock = threading.Lock()


def data_root() -> Path:
    p = Path(env("DRAMA_DATA_DIR", str(ROOT / "data")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_project_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Project:
    id: str

    @classmethod
    def create(cls, request: dict) -> "Project":
        p = cls(new_project_id())
        p.dir.mkdir(parents=True, exist_ok=True)
        p.save("request", request)
        p.append("project/created", {"request": request})
        return p

    @classmethod
    def open(cls, project_id: str) -> "Project":
        if not _ID_RE.match(project_id):
            raise ValueError("非法项目 ID")
        p = cls(project_id)
        if not p.dir.exists():
            raise FileNotFoundError(project_id)
        return p

    @classmethod
    def list(cls) -> list[str]:
        root = data_root() / "projects"
        if not root.exists():
            return []
        return sorted((d.name for d in root.iterdir() if d.is_dir()), reverse=True)

    # ------------- 路径 -------------
    @property
    def dir(self) -> Path:
        return data_root() / "projects" / self.id

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ------------- 事件日志 -------------
    def append(self, type_: str, data: dict | None = None) -> dict:
        evt = {"ts": time.time(), "type": type_, "data": data or {}}
        with _lock, self.path("events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        return evt

    def events(self, since: int = 0) -> list[dict]:
        f = self.dir / "events.jsonl"
        if not f.exists():
            return []
        lines = f.read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[since:] if x.strip()]

    # ------------- 产物 -------------
    def save(self, name: str, obj: Any) -> Path:
        p = self.path("artifacts", f"{name}.json")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return p

    def load(self, name: str, default: Any = None) -> Any:
        p = self.dir / "artifacts" / f"{name}.json"
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def has(self, name: str) -> bool:
        return (self.dir / "artifacts" / f"{name}.json").exists()

    def save_text(self, rel: str, text: str) -> Path:
        p = self.path(rel)
        p.write_text(text, encoding="utf-8")
        return p

    # ------------- 状态 -------------
    def status(self) -> dict:
        st = {"id": self.id, "state": "created", "stage": None, "stages_done": [], "error": None,
              "progress": 0.0, "final": None}
        for e in self.events():
            t, d = e["type"], e["data"]
            if t == "pipeline/start":
                st["state"] = "running"
                st["stages_total"] = d.get("stages", [])
            elif t == "stage/start":
                st["stage"] = d["stage"]
                st["stage_started"] = e["ts"]
                st["stage_message"] = None
            elif t == "stage/progress":
                st["stage_message"] = d.get("message")
            elif t == "stage/end":
                if d["stage"] not in st["stages_done"]:
                    st["stages_done"].append(d["stage"])
            elif t == "pipeline/end":
                st["state"] = "done"
                st["final"] = d.get("final")
            elif t == "pipeline/error":
                st["state"] = "failed"
                st["error"] = d.get("error")
            elif t == "pipeline/paused":
                st["state"] = "paused"
                st["error"] = d.get("reason")
        total = len(st.get("stages_total", [])) or 1
        st["progress"] = round(len(st["stages_done"]) / total, 3)
        st["request"] = self.load("request", {})
        return st
