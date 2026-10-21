"""HTTP 服务：一键生成 API + 实时进度（SSE）+ 产物下载 + 简易网页。

环境变量：
  DRAMA_API_TOKEN   访问令牌（公网部署务必设置）。请求头 Authorization: Bearer <token> 或 ?token=<token>
  DRAMA_MAX_JOBS    同时运行的任务数（默认 2）
  DRAMA_PROFILE     使用的 profile（默认 default）
  DRAMA_DATA_DIR    数据目录（默认 ./data）
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ..kernel.config import ROOT, env, load_dotenv
from ..kernel.project import Project
from ..kernel.project import data_root
from ..pipeline import Pipeline, build_context
from ..media_urls import verify as verify_media
from ..script_import import MAX_CHARS, analyze, extract_text
from ..video_models import VIDEO_MODELS, caps, estimate_cost

log = logging.getLogger("drama.server")
load_dotenv()

STATE: dict = {"ctx": None, "pipe": None, "sem": None, "tasks": {}, "generation_tasks": {}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    STATE["ctx"] = build_context(env("DRAMA_PROFILE"))
    STATE["pipe"] = Pipeline(STATE["ctx"])
    STATE["sem"] = asyncio.Semaphore(int(env("DRAMA_MAX_JOBS", "2")))
    if not env("DRAMA_API_TOKEN"):
        log.warning("未设置 DRAMA_API_TOKEN：任何人都能调用付费生成接口！公网部署前务必设置。")
    # 服务重启时，把中断的任务标记为 paused，便于用户 resume
    for pid in Project.list():
        p = Project.open(pid)
        if p.status()["state"] in ("running", "created"):
            p.append("pipeline/paused", {"reason": "服务重启导致中断，可调用 resume 继续"})
    # 独立生成任务同样可恢复查看；服务重启时将未完成任务标为失败，避免页面永久显示生成中。
    for record in _generation_list():
        if record.get("state") in ("queued", "running"):
            record["state"] = "failed"
            record["error"] = "服务重启导致任务中断"
            record["updated_at"] = time.time()
            _generation_save(record)
    yield


app = FastAPI(title="Drama Agent · 短剧一键生成", version="1.0.0", lifespan=lifespan)


def auth(request: Request) -> None:
    token = env("DRAMA_API_TOKEN")
    if not token:
        return
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if not secrets.compare_digest(got, token):
        raise HTTPException(401, "未授权")


class CreateReq(BaseModel):
    idea: str | None = Field(None, min_length=2, max_length=4000, description="一句话创意或故事梗概")
    script_text: str | None = Field(None, min_length=20, max_length=MAX_CHARS, description="上传的完整剧本（有则按剧本生成）")
    title: str | None = Field(None, max_length=100, description="项目名（上传剧本时可用文件名）")
    review_rewrite: bool | None = Field(None, description="上传剧本时是否允许审核环节自动改写（默认否）")
    adapt: bool | None = Field(None, description="小说转短剧改编：按每集时长重写（默认：识别为第N章小说时开启）")
    episodes: int | None = Field(None, ge=1, le=100, description="集数；上传剧本时留空则按剧本格式自动识别")
    episode_seconds: int | None = Field(None, ge=15, le=600)
    segment_seconds: int | None = Field(None, ge=4, le=15)
    ratio: Literal["9:16", "16:9", "1:1", "3:4", "4:3"] | None = None
    style: str | None = Field(None, max_length=50)
    genre: str | None = Field(None, max_length=50)
    track: Literal["domestic", "overseas", "explainer"] | None = None
    mode: Literal["full", "prompts"] | None = None
    continuity: Literal["refs", "last_frame"] | None = None

    @model_validator(mode="after")
    def need_source(self):
        if not self.idea and not self.script_text:
            raise ValueError("请提供创意 idea 或剧本 script_text")
        return self


class AnalyzeReq(BaseModel):
    text: str | None = Field(None, max_length=MAX_CHARS)
    filename: str | None = Field(None, max_length=200)
    data: str | None = Field(None, max_length=MAX_CHARS * 8, description="文件内容 base64")
    episodes: int | None = Field(None, ge=1, le=100, description="目标集数；留空则按剧本格式识别")


class RenderReq(BaseModel):
    episodes: list[int] | None = Field(None, description="要生成视频的集号；留空表示全部剩余集")
    model: str | None = Field(None, max_length=80, description="视频模型（GET /api/video-models 中的 id）")
    resolution: str | None = Field(None, max_length=10, description="清晰度，须在该模型支持的列表内")


class ResumeReq(BaseModel):
    force: list[str] | None = None
    mode: Literal["full", "prompts"] | None = Field(None, description="切换交付方式，例如确认分镜后改为 full 生成视频")


class GenerationReq(BaseModel):
    kind: Literal["image", "video"]
    prompt: str = Field(..., min_length=1, max_length=8000)
    model: str | None = Field(None, max_length=100)
    ratio: Literal["1:1", "9:16", "16:9", "3:4", "4:3"] = "1:1"
    size: str | None = Field(None, max_length=30)
    quality: str | None = Field(None, max_length=30)
    duration: int | None = Field(None, ge=1, le=60)
    resolution: str | None = Field(None, max_length=20)
    reference_images: list[str] = Field(default_factory=list, max_length=9)


def _generation_root() -> Path:
    root = data_root() / "generations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _generation_dir(gid: str) -> Path:
    # IDs are generated locally and this check also protects file endpoints.
    if not gid or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in gid):
        raise HTTPException(404, "生成记录不存在")
    return _generation_root() / gid


def _generation_save(record: dict) -> None:
    d = _generation_dir(record["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "record.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / "record.json")


def _generation_event(record: dict, event_type: str, data: dict | None = None) -> None:
    d = _generation_dir(record["id"])
    d.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "type": event_type, "data": data or {}}
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _generation_load(gid: str) -> dict:
    d = _generation_dir(gid)
    p = d / "record.json"
    if not p.exists():
        raise HTTPException(404, "生成记录不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def _generation_list() -> list[dict]:
    root = _generation_root()
    out = []
    for d in root.iterdir():
        p = d / "record.json"
        if d.is_dir() and p.exists():
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return sorted(out, key=lambda x: x.get("created_at", 0), reverse=True)


def _image_options() -> dict:
    # Profile config is the source of truth; provider attributes keep backwards compatibility
    # with profiles that only specify CANGYUAN_IMAGE_MODEL.
    service = STATE["ctx"].get("image") if STATE.get("ctx") and STATE["ctx"].has("image") else None
    config_models = []
    if STATE.get("ctx"):
        for entry in STATE["ctx"].config.get("plugins", []):
            if "media:CangyuanMediaPlugin" in str(entry.get("use", "")):
                config_models = (entry.get("config", {}).get("image", {}) or {}).get("models", []) or []
                break
    if not config_models:
        is_mock = service is not None and service.__class__.__module__.endswith("providers.mock")
        model = ("mock-image-1" if is_mock else None) or getattr(service, "model", None) or "gpt-image-2"
        size_map = getattr(service, "size_map", {}) or {}
        sizes = sorted(set(size_map.values()) or {"1024x1024", "1024x1536", "1536x1024"})
        config_models = [{"id": model, "label": model, "sizes": sizes, "qualities": ["auto", "high"]}]
    default_model = getattr(service, "model", None) or config_models[0].get("id")
    return {"models": config_models, "default_model": default_model,
            "sizes": sorted({s for m in config_models for s in m.get("sizes", [])}),
            "qualities": sorted({q for m in config_models for q in m.get("qualities", [])}) or ["auto"]}


def _video_options_sync() -> dict:
    service = STATE["ctx"].get("video") if STATE.get("ctx") and STATE["ctx"].has("video") else None
    return {"default_model": getattr(service, "model", None), "default_resolution": getattr(service, "resolution", None)}


def _open(pid: str) -> Project:
    try:
        return Project.open(pid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "项目不存在")


def _start(project: Project, force: list[str] | None = None) -> None:
    if project.id in STATE["tasks"] and not STATE["tasks"][project.id].done():
        raise HTTPException(409, "任务正在运行")

    async def job():
        async with STATE["sem"]:
            try:
                await STATE["pipe"].run(project, force=force)
            except Exception:  # noqa: BLE001 - 错误已写入事件日志
                pass

    STATE["tasks"][project.id] = asyncio.create_task(job())


@app.post("/api/projects", dependencies=[Depends(auth)])
async def create(req: CreateReq):
    project = Project.create(req.model_dump(exclude_none=True))
    _start(project)
    return {"id": project.id, "status_url": f"/api/projects/{project.id}", "events_url": f"/api/projects/{project.id}/events"}


@app.post("/api/script/analyze", dependencies=[Depends(auth)])
async def analyze_script(req: AnalyzeReq):
    """上传文件 → 纯文本 + 分集识别 + 时长估算（免费，不调用模型）。"""
    if req.data is not None:
        try:
            raw = base64.b64decode(req.data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "文件内容不是合法的 base64")
        try:
            text = extract_text(req.filename or "", raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        text = (req.text or "").strip()
    if len(text) < 20:
        raise HTTPException(400, "剧本内容太短")
    return {"text": text if req.data is not None else None, **analyze(text, target=req.episodes)}


@app.get("/api/projects", dependencies=[Depends(auth)])
async def list_projects(limit: int = 50):
    out = []
    for pid in Project.list()[:limit]:
        s = Project.open(pid).status()
        r = s["request"]
        out.append({k: s.get(k) for k in ("id", "state", "stage", "progress", "error")}
                   | {"idea": (r.get("title") or r.get("idea") or "")[:80], "source": "script" if r.get("script_text") else "idea"})
    return out


@app.get("/api/projects/{pid}", dependencies=[Depends(auth)])
async def get_project(pid: str):
    p = _open(pid)
    s = p.status()
    s["running"] = pid in STATE["tasks"] and not STATE["tasks"][pid].done()
    s["bible"] = p.load("bible")
    s["qc"] = p.load("qc")
    s["final"] = p.load("final")
    req = s.pop("request", {})
    s["request"] = {k: v for k, v in req.items() if k != "script_text"} | {"source": "script" if req.get("script_text") else "idea"}
    s["brief"] = p.load("brief")
    s["episodes"] = [{k: e.get(k) for k in ("no", "title", "chars", "seconds")} for e in p.load("source_script", [])]
    s["assets"] = (p.load("assets") or {}).get("items", [])
    s["review"] = p.load("review")
    s["scripts"] = [{"no": x.get("no"), "title": x.get("title"), "target_seconds": x.get("target_seconds"),
                     "card": x.get("card"), "check": x.get("sdf_check"),
                     "md": f"episodes_script/E{x.get('no')}.md" if (p.dir / "episodes_script" / f"E{x.get('no')}.md").exists() else None}
                    for x in (p.load("scripts") or [])]
    feed = p.load("feed") or {}
    done = p.load("render") or p.load("render_partial") or {}
    s["segments"] = [{"id": g["id"], "episode": g["episode"], "duration": g["duration"], "prompt": g["prompt"],
                      "refs": [r["name"] for r in g.get("refs", [])], "video": done.get(g["id"])}
                     for g in feed.get("segments", [])]
    return s


@app.post("/api/projects/{pid}/resume", dependencies=[Depends(auth)])
async def resume(pid: str, body: ResumeReq | list[str] | None = Body(None)):
    p = _open(pid)
    if isinstance(body, list):  # 兼容旧调用：直接传阶段名数组
        body = ResumeReq(force=body)
    body = body or ResumeReq()
    if body.mode:
        if pid in STATE["tasks"] and not STATE["tasks"][pid].done():
            raise HTTPException(409, "任务正在运行")
        for name in ("request", "brief"):
            data = p.load(name)
            if data is not None:
                p.save(name, {**data, "mode": body.mode})
    _start(p, body.force)
    return {"id": pid, "resumed": True, "mode": body.mode}


@app.post("/api/projects/{pid}/render", dependencies=[Depends(auth)])
async def render_episodes(pid: str, body: RenderReq | None = None):
    """按集出视频：在分镜/提示词就绪后，指定若干集出图 → 生成视频 → 合成成片。可多次调用逐集推进。"""
    p = _open(pid)
    if pid in STATE["tasks"] and not STATE["tasks"][pid].done():
        raise HTTPException(409, "任务正在运行，请等当前集完成")
    feed = p.load("feed")
    if not feed:
        raise HTTPException(409, "分镜与提示词尚未就绪")
    all_eps = sorted({int(s["episode"]) for s in feed["segments"]})
    want = sorted(set(body.episodes)) if body and body.episodes else all_eps
    bad = [e for e in want if e not in all_eps]
    if bad:
        raise HTTPException(400, f"不存在的集：{bad}")
    req = p.load("request") or {}
    video = STATE["ctx"].get("video") if STATE["ctx"].has("video") else None
    model = (body.model if body else None) or req.get("video_model") or getattr(video, "model", None)
    if body and body.model and body.model not in await visible_video_models():
        raise HTTPException(400, f"当前令牌不可用的视频模型：{body.model}")
    resolution = (body.resolution if body else None) or req.get("video_resolution") or getattr(video, "resolution", None)
    if model and resolution not in caps(model)["resolutions"]:
        raise HTTPException(400, f"{model} 不支持 {resolution}，可选：{'/'.join(caps(model)['resolutions'])}")
    sel = sorted(set(req.get("render_episodes") or []) | set(want))
    for name in ("request", "brief"):
        data = p.load(name)
        if data is not None:
            data = {**data, "mode": "full", "video_model": model, "video_resolution": resolution}
            if sel == all_eps:
                data.pop("render_episodes", None)
            else:
                data["render_episodes"] = sel
            p.save(name, data)
    p.append("render/requested", {"episodes": want, "model": model, "resolution": resolution})
    _start(p, ["asset_images", "render", "assemble"])
    return {"id": pid, "episodes": want, "selected": sel, "model": model, "resolution": resolution}


@app.post("/api/projects/{pid}/cancel", dependencies=[Depends(auth)])
async def cancel(pid: str):
    t = STATE["tasks"].get(pid)
    if not t or t.done():
        raise HTTPException(409, "任务未在运行")
    t.cancel()
    _open(pid).append("pipeline/paused", {"reason": "用户取消"})
    return {"id": pid, "cancelled": True}


@app.get("/api/projects/{pid}/artifacts/{name}", dependencies=[Depends(auth)])
async def artifact(pid: str, name: str):
    p = _open(pid)
    data = p.load(name) if name.replace("_", "").isalnum() else None
    if data is None:
        raise HTTPException(404, "产物不存在")
    return data


@app.get("/api/projects/{pid}/files/{path:path}", dependencies=[Depends(auth)])
async def file(pid: str, path: str):
    p = _open(pid)
    target = (p.dir / path).resolve()
    if not target.is_relative_to(p.dir.resolve()) or not target.is_file() or "artifacts" in target.parts and target.suffix == ".tmp":
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@app.get("/api/projects/{pid}/events", dependencies=[Depends(auth)])
async def events(pid: str, request: Request, since: int = 0):
    """SSE：实时推送事件日志（断线重连时传 since=已收到条数）。"""
    p = _open(pid)

    async def gen():
        n = since
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            new = p.events(n)
            for e in new:
                e.get("data", {}).pop("trace", None)
                yield f"id: {n}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                n += 1
                if e["type"] in ("pipeline/end", "pipeline/error", "pipeline/paused") and not (
                        pid in STATE["tasks"] and not STATE["tasks"][pid].done()):
                    return
            idle = 0 if new else idle + 1
            if idle and idle % 15 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_MODELS_CACHE: dict = {"at": 0.0, "ids": None}


async def visible_video_models() -> set[str]:
    """能力表 ∩ 当前视频令牌 GET /v1/models 可见的型号（缓存 10 分钟）；mock / 查询失败时返回能力表全集。"""
    import time

    import httpx
    video = STATE["ctx"].get("video") if STATE["ctx"] and STATE["ctx"].has("video") else None
    if not video or not getattr(video, "api_key", None):
        return set(VIDEO_MODELS)
    if _MODELS_CACHE["ids"] is None or time.time() - _MODELS_CACHE["at"] > 600:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{video.base_url}/v1/models", headers={"Authorization": f"Bearer {video.api_key}"})
            r.raise_for_status()
            _MODELS_CACHE.update(at=time.time(), ids={m["id"] for m in r.json().get("data", [])})
        except Exception as e:  # noqa: BLE001
            log.warning("查询 /v1/models 失败，使用能力表全集: %s", e)
            return set(VIDEO_MODELS)
    return set(VIDEO_MODELS) & _MODELS_CACHE["ids"]


@app.get("/api/video-models", dependencies=[Depends(auth)])
async def video_models(pid: str | None = None, episodes: str | None = None):
    """可选视频模型 + 清晰度；传 pid/episodes 时附带该批段落的生成时长与参考价估算。"""
    video = STATE["ctx"].get("video") if STATE["ctx"].has("video") else None
    durations: list[int] = []
    if pid:
        proj = _open(pid)
        feed = proj.load("feed") or {}
        eps = {int(x) for x in episodes.split(",")} if episodes else None
        done = lambda sid: (proj.dir / "segments" / f"{sid}.mp4").is_file()   # noqa: E731 已生成的段不再计费
        durations = [int(s["duration"]) for s in feed.get("segments", [])
                     if (eps is None or int(s["episode"]) in eps) and not done(s["id"])]
    out = []
    for mid in sorted(await visible_video_models()):
        c = caps(mid)
        out.append({**{k: c[k] for k in ("id", "label", "min", "max", "resolutions", "default_resolution", "billing",
                                         "price", "note")},
                    "estimates": {r: estimate_cost(mid, r, durations) for r in c["resolutions"]} if durations else {},
                    "gen_seconds": sum(max(c["min"], min(d, c["max"])) for d in durations) if durations else 0})
    return {"default_model": getattr(video, "model", None), "default_resolution": getattr(video, "resolution", None),
            "models": out, "price_note": "参考价来自沧元文档（2026-10-20），实时价格以沧元模型广场为准；失败通常不计费"}


@app.get("/api/generation-options", dependencies=[Depends(auth)])
async def generation_options():
    """首页图片/视频模式所需的模型与规格选项。"""
    video = _video_options_sync()
    models = []
    service = STATE["ctx"].get("video") if STATE.get("ctx") and STATE["ctx"].has("video") else None
    is_mock = service is not None and service.__class__.__module__.endswith("providers.mock")
    if is_mock:
        models = [{"id": "mock-video-1", "label": "Mock Video", "min": 1, "max": 30,
                   "resolutions": ["480p", "720p"], "default_resolution": "720p", "max_refs": 9}]
        video = {"default_model": "mock-video-1", "default_resolution": "720p"}
    else:
        visible = await visible_video_models()
        for mid in sorted(visible):
            c = caps(mid)
            models.append({k: c[k] for k in ("id", "label", "min", "max", "resolutions", "default_resolution", "max_refs")})
    return {"image": _image_options(), "video": {**video, "models": models}}


async def _run_generation(record: dict) -> None:
    gid = record["id"]
    try:
        record["state"] = "running"
        record["progress"] = 5
        record["updated_at"] = time.time()
        _generation_save(record)
        _generation_event(record, "generation/start", {"kind": record["kind"]})
        d = _generation_dir(gid)
        if record["kind"] == "image":
            image = STATE["ctx"].get("image")
            out = d / "output.png"
            await image.generate(record["prompt"], out, aspect=record["ratio"], model=record.get("model"),
                                 size=record.get("size"), quality=record.get("quality"))
            record["progress"] = 100
        else:
            video = STATE["ctx"].get("video")
            out = d / "output.mp4"

            async def progress(status, value=None):
                record["progress"] = int(value) if isinstance(value, (int, float)) else max(10, record.get("progress", 0))
                record["stage"] = str(status or "running")
                record["updated_at"] = time.time()
                _generation_save(record)
                _generation_event(record, "generation/progress", {"progress": record["progress"], "status": record["stage"]})

            refs = [u for u in record.get("reference_images", []) if isinstance(u, str) and u.startswith("https://")]
            await video.generate(record["prompt"], out, duration=record["duration"] or 8, ratio=record["ratio"], refs=refs,
                                 model=record.get("model"), resolution=record.get("resolution"), on_progress=progress)
            record["progress"] = 100
        record["state"] = "done"
        record["output_file"] = out.name
        record["updated_at"] = time.time()
        _generation_save(record)
        _generation_event(record, "generation/end", {"output_file": out.name})
    except asyncio.CancelledError:
        record["state"] = "failed"
        record["error"] = "任务已取消"
        record["updated_at"] = time.time()
        _generation_save(record)
        _generation_event(record, "generation/error", {"error": record["error"]})
    except Exception as exc:  # noqa: BLE001 - provider errors are exposed in the record
        log.exception("独立生成失败 %s", gid)
        record["state"] = "failed"
        record["error"] = str(exc)[:1000]
        record["updated_at"] = time.time()
        _generation_save(record)
        _generation_event(record, "generation/error", {"error": record["error"]})


@app.post("/api/generations", dependencies=[Depends(auth)])
async def create_generation(req: GenerationReq):
    options = await generation_options()
    if req.kind == "image":
        image = options["image"]
        model = req.model or image["default_model"]
        selected = next((m for m in image["models"] if m.get("id") == model), None)
        if not selected:
            raise HTTPException(400, f"不可用的图片模型：{model}")
        sizes = selected.get("sizes") or image.get("sizes", [])
        if req.size and sizes and req.size not in sizes:
            raise HTTPException(400, f"图片模型不支持尺寸：{req.size}")
        qualities = selected.get("qualities") or image.get("qualities", [])
        if req.quality and qualities and req.quality not in qualities:
            raise HTTPException(400, f"图片模型不支持质量：{req.quality}")
        size = req.size or {"1:1": "1024x1024", "9:16": "1024x1536", "16:9": "1536x1024",
                            "3:4": "1024x1536", "4:3": "1536x1024"}[req.ratio]
        if sizes and size not in sizes:
            size = sizes[0]
        quality = req.quality or (qualities[0] if qualities else None)
        duration = None
        resolution = None
    else:
        model = req.model or options["video"]["default_model"]
        selected = next((m for m in options["video"]["models"] if m.get("id") == model), None)
        if not selected:
            raise HTTPException(400, f"当前令牌不可用的视频模型：{model}")
        if req.ratio not in ("1:1", "9:16", "16:9"):
            raise HTTPException(400, "视频画幅仅支持 1:1、9:16、16:9")
        duration = max(selected["min"], min(req.duration or selected["min"], selected["max"]))
        resolution = req.resolution or selected["default_resolution"]
        if resolution not in selected["resolutions"]:
            raise HTTPException(400, f"视频模型不支持清晰度：{resolution}")
        size = None
        quality = None
    gid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    record = {"id": gid, "kind": req.kind, "prompt": req.prompt.strip(), "model": model, "ratio": req.ratio,
              "size": size, "quality": quality,
              "duration": duration, "resolution": resolution, "reference_images": req.reference_images, "state": "queued", "progress": 0,
              "output_file": None, "error": None, "created_at": time.time(), "updated_at": time.time()}
    _generation_save(record)
    _generation_event(record, "generation/queued", {})
    task = asyncio.create_task(_run_generation(record))
    STATE["generation_tasks"][gid] = task
    return {"id": gid, "status_url": f"/api/generations/{gid}", "events_url": f"/api/generations/{gid}/events"}


@app.get("/api/generations", dependencies=[Depends(auth)])
async def list_generations(limit: int = 50):
    return _generation_list()[:max(1, min(limit, 200))]


@app.get("/api/generations/{gid}", dependencies=[Depends(auth)])
async def get_generation(gid: str):
    record = _generation_load(gid)
    record["running"] = gid in STATE["generation_tasks"] and not STATE["generation_tasks"][gid].done()
    if record.get("output_file"):
        record["file_url"] = f"/api/generations/{gid}/file"
    return record


@app.get("/api/generations/{gid}/events", dependencies=[Depends(auth)])
async def generation_events(gid: str, request: Request, since: int = 0):
    _generation_load(gid)
    path = _generation_dir(gid) / "events.jsonl"

    async def gen():
        n = since
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            new = [json.loads(x) for x in lines[n:] if x.strip()]
            for event in new:
                yield f"id: {n}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                n += 1
            record = _generation_load(gid)
            if record.get("state") in ("done", "failed") and not new:
                return
            idle = 0 if new else idle + 1
            if idle and idle % 15 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/generations/{gid}/file", dependencies=[Depends(auth)])
async def generation_file(gid: str):
    record = _generation_load(gid)
    if not record.get("output_file"):
        raise HTTPException(404, "生成结果尚未就绪")
    target = (_generation_dir(gid) / record["output_file"]).resolve()
    if not target.is_relative_to(_generation_dir(gid).resolve()) or not target.is_file():
        raise HTTPException(404, "生成结果不存在")
    return FileResponse(target)


@app.get("/api/works", dependencies=[Depends(auth)])
async def works(limit: int = 100):
    projects = []
    for pid in Project.list()[:limit]:
        s = Project.open(pid).status()
        r = s["request"]
        projects.append({"id": pid, "type": "project", "kind": "short_drama", "title": (r.get("title") or r.get("idea") or "未命名项目")[:80],
                         "state": s.get("state"), "progress": s.get("progress", 0), "source": "script" if r.get("script_text") else "idea",
                         "created_at": next((e["ts"] for e in Project.open(pid).events() if e["type"] == "project/created"), 0)})
    generations = [{**g, "type": "generation", "title": g.get("prompt", "")[:80],
                    "file_url": f"/api/generations/{g['id']}/file" if g.get("output_file") else None} for g in _generation_list()]
    return sorted(projects + generations, key=lambda x: x.get("created_at", 0), reverse=True)[:max(1, min(limit, 200))]


@app.get("/media/{pid}/{path:path}")
async def media(pid: str, path: str, exp: int = 0, sig: str = ""):
    """给视频模型拉取参考图用的签名链接（不需要访问令牌，过期失效，只开放 images/ 与 segments/*.png）。"""
    if not verify_media(pid, path, exp, sig):
        raise HTTPException(403, "链接无效或已过期")
    target = (_open(pid).dir / path).resolve()
    if not target.is_relative_to(_open(pid).dir.resolve()) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "profile": STATE["ctx"].config.get("profile") if STATE["ctx"] else None}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(ROOT) / "web" / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})
