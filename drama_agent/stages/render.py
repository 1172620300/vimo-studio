"""渲染层：逐段生成视频（Seedance）→ ffmpeg 合成每集成片 + SRT 字幕。"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from ..kernel.project import Project
from ..media_urls import public_url
from ..providers.media import TaskPending
from ..video_models import clamp_duration
from .base import Stage, selected_segments, speaker_name
from .qc import parse_range

RES = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (960, 960), "3:4": (960, 1280), "4:3": (1280, 960)}


async def _ffmpeg(*args: str) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("服务器未安装 ffmpeg")
    proc = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 失败: " + proc.stderr.decode(errors="ignore")[-500:])


def is_transient(msg: str) -> bool:
    """网关/上游临时故障（任务已终止，可安全重提）。"""
    return any(k in msg for k in ("响应超时", "服务繁忙", "稍后重试", "upstream timeout", "HTTP 502", "HTTP 503", "HTTP 504"))


def is_face_rejected(msg: str) -> bool:
    """参考素材人脸/人物审核未通过。"""
    return "审核" in msg and any(k in msg for k in ("人脸", "人物", "肖像"))


def strip_ref_tags(prompt: str, keep: int) -> str:
    """去掉编号 > keep 的「图N：」标签（场景图恒为图1，角色图从图2起），保留文字描述。"""
    return re.sub(r"图(\d+)：?", lambda m: m.group(0) if int(m.group(1)) <= keep else "", prompt)


async def probe_seconds(video: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    proc = await asyncio.to_thread(subprocess.run, ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                    "-of", "default=nw=1:nk=1", str(video)], capture_output=True)
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return None


async def fit_duration(video: Path, target: float) -> bool:
    """网关按更长时长生成时（如不支持 4 秒），把片段裁回分镜时长，保证字幕与成片时间轴一致。"""
    actual = await probe_seconds(video)
    if not actual or actual <= target + 0.4:
        return False
    tmp = video.with_name(video.stem + "_fit.mp4")
    await _ffmpeg("-i", str(video), "-t", f"{target:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-c:a", "aac", "-b:a", "160k", str(tmp))
    tmp.replace(video)
    return True


async def last_frame(video: Path, out: Path) -> Path:
    await _ffmpeg("-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-update", "1", str(out))
    return out


class RenderStage(Stage):
    name, produces, title, paid = "render", "render", "视频生成", True

    def done(self, project: Project) -> bool:
        """按集出片：所选集的每一段都已有视频文件才算完成（不能只看 render.json 是否存在）。"""
        feed = project.load("feed")
        if not feed or not project.has(self.produces):
            return False
        segs = selected_segments(feed, project.load("request", {}) or {})
        return all((project.dir / "segments" / f"{s['id']}.mp4").is_file() for s in segs)

    async def run(self, project: Project, req: dict) -> dict:
        brief, feed, assets = project.load("brief"), project.load("feed"), project.load("assets")
        files = {it["id"]: it.get("file") for it in assets["items"]}
        video = self.ctx.get("video")
        result = project.load("render_partial", {}) or {}
        failures: dict[str, str] = {}
        segs_sel = selected_segments(feed, req)      # 按集出片：只生成所选集的段
        total = len(segs_sel)
        model = req.get("video_model") or getattr(video, "model", None)
        resolution = req.get("video_resolution") or getattr(video, "resolution", None)
        tasks: dict[str, str] = project.load("video_tasks", {}) or {}   # 已受理未取回的任务，续跑时继续轮询
        warned = False

        async def save_task(seg_id: str, task_id: str | None) -> None:
            if task_id:
                tasks[seg_id] = task_id
            else:
                tasks.pop(seg_id, None)
            project.save("video_tasks", tasks)

        async def one(seg: dict, prev: Path | None) -> Path | None:
            nonlocal warned
            out = project.path("segments", f"{seg['id']}.mp4")
            if out.exists() and out.stat().st_size > 0:
                return out
            # 参考图：沧元只接受可匿名访问的 HTTPS URL → 用服务公网地址签发临时链接
            ref_files: list[str] = []
            for r in seg["refs"]:
                if r["asset"] == "__prev_frame__":
                    if prev and prev.exists():
                        await last_frame(prev, project.path("segments", f"{seg['id']}_start.png"))
                        ref_files.append(f"segments/{seg['id']}_start.png")
                elif files.get(r["asset"]):
                    ref_files.append(files[r["asset"]].replace("\\", "/"))
            refs = [u for u in (public_url(project.id, f) for f in ref_files) if u]
            prompt = seg["prompt"]
            if len(refs) < len(seg["refs"]):
                # 参考图缺失（未出图，或本机运行没有公网地址）：去掉图号，避免模型错配
                if seg["refs"] and not warned and len(ref_files) == len(seg["refs"]):
                    warned = True
                    await self.progress(project, "未配置 PUBLIC_BASE_URL（公网 HTTPS 地址），参考图无法提供给视频模型，"
                                                 "本次按纯文字生成，角色一致性会下降；建议在服务器上生成", level="warn")
                prompt = re.sub(r"图\d+：?", "", prompt)
                refs = []

            async def on_progress(status, pct):
                await self.progress(project, f"{seg['id']} {status} {pct or ''}", segment=seg["id"])

            async def on_task(task_id: str | None) -> None:
                await save_task(seg["id"], task_id)

            try:
                pending = tasks.get(seg["id"])
                gen_sec = clamp_duration(model, seg["duration"]) if model else seg["duration"]
                await self.progress(project, (f"继续轮询 {seg['id']}（task {pending}）" if pending else
                                              f"提交 {seg['id']}（{model} · {resolution} · {gen_sec}s，{len(refs)} 张参考图）"),
                                    segment=seg["id"])
                retried_transient = False
                while True:
                    try:
                        path = await video.generate(prompt, out, duration=seg["duration"], ratio=brief["ratio"],
                                                    refs=refs, model=model, resolution=resolution,
                                                    on_progress=on_progress, task_id=pending, on_task=on_task)
                        break
                    except Exception as e:  # noqa: BLE001
                        if isinstance(e, TaskPending):
                            raise
                        pending, msg = None, str(e)
                        if is_transient(msg) and not retried_transient:
                            # 网关侧超时：任务已终止（failed），重新提交一次不会重复
                            retried_transient = True
                            await self.progress(project, f"{seg['id']} 网关超时，自动重新提交一次", segment=seg["id"],
                                                level="warn")
                            continue
                        scene_first = (len(refs) == len(seg["refs"]) and seg["refs"]
                                       and seg["refs"][0].get("kind") == "scene")
                        if is_face_rejected(msg) and len(refs) > 1 and scene_first:
                            # 角色参考图人脸未过审：只保留场景图（图1），角色改按文字描述生成
                            refs, prompt = refs[:1], strip_ref_tags(prompt, keep=1)
                            await self.progress(project, f"{seg['id']} 角色参考图人脸未通过审核，改为只用场景图重试"
                                                         "（该段角色一致性会略降）", segment=seg["id"], level="warn")
                            continue
                        if is_face_rejected(msg) and refs:
                            refs, prompt = [], strip_ref_tags(prompt, keep=0)
                            await self.progress(project, f"{seg['id']} 参考图未通过审核，改为纯文字重试",
                                                segment=seg["id"], level="warn")
                            continue
                        raise
                if await fit_duration(path, float(seg["duration"])):
                    await self.progress(project, f"{seg['id']} 已裁回 {seg['duration']}s（模型最短 {gen_sec}s）",
                                        segment=seg["id"])
                result[seg["id"]] = str(Path("segments") / path.name)
                project.save("render_partial", result)
                await self.progress(project, f"完成 {seg['id']}（{len(result)}/{total}）", segment=seg["id"], file=result[seg["id"]])
                return path
            except Exception as e:  # noqa: BLE001
                failures[seg["id"]] = str(e)[:300]
                await self.progress(project, f"失败 {seg['id']}: {str(e)[:120]}", segment=seg["id"], level="error")
                return None

        by_ep: dict[int, list[dict]] = defaultdict(list)
        for seg in segs_sel:
            by_ep[seg["episode"]].append(seg)

        async def episode(segs: list[dict]) -> None:
            if brief.get("continuity") == "last_frame":
                prev = None
                for seg in segs:          # 末帧承接：段间必须串行
                    prev = await one(seg, prev)
            else:
                await asyncio.gather(*(one(s, None) for s in segs))

        await asyncio.gather(*(episode(s) for s in by_ep.values()))
        for seg in feed["segments"]:
            out = project.dir / "segments" / f"{seg['id']}.mp4"
            if out.exists() and out.stat().st_size > 0:
                result[seg["id"]] = str(Path("segments") / out.name)
        if failures:
            project.save("render_partial", result)
            raise RuntimeError(f"{len(failures)} 段生成失败（已完成的段已保存，调用 resume 可只重试失败段）："
                               + "; ".join(f"{k}: {v}" for k, v in list(failures.items())[:3]))
        project.save(self.produces, result)
        return result


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(boards_ep: dict, bible: dict) -> str:
    rows, offset, idx = [], 0.0, 1
    for seg in boards_ep.get("segments", []):
        for sh in seg.get("shots") or []:
            rng = parse_range(sh.get("t", "")) or (0, seg["duration"])
            dls = [d for d in sh.get("dialogue") or [] if d.get("text")]
            if not dls:
                continue
            span = (rng[1] - rng[0]) / len(dls)
            for k, d in enumerate(dls):
                st = offset + rng[0] + k * span
                rows.append(f"{idx}\n{_srt_time(st)} --> {_srt_time(st + span)}\n"
                            f"{speaker_name(bible, d.get('speaker', ''))}：{d['text']}\n")
                idx += 1
        offset += float(seg["duration"])
    return "\n".join(rows)


class AssembleStage(Stage):
    name, produces, title, paid = "assemble", "final", "成片合成", True

    def done(self, project: Project) -> bool:
        """所选集都已合成成片才算完成。"""
        feed, final = project.load("feed"), project.load("final")
        if not feed or not final:
            return False
        want = {int(s["episode"]) for s in selected_segments(feed, project.load("request", {}) or {})}
        have = {int(e["no"]) for e in final.get("episodes", []) if (project.dir / e["video"]).is_file()}
        return want <= have

    async def run(self, project: Project, req: dict) -> dict:
        brief, bible, feed, render = project.load("brief"), project.load("bible"), project.load("feed"), project.load("render")
        boards = {b["no"]: b for b in project.load("storyboards")}
        w, h = RES.get(brief["ratio"], (720, 1280))
        fps = int(self.config.get("fps", 24))
        burn = bool(self.config.get("burn_subtitles", False))
        prev = {e["no"]: e for e in (project.load("final") or {}).get("episodes", [])}
        episodes = []
        by_ep: dict[int, list[str]] = defaultdict(list)
        for seg in selected_segments(feed, req):       # 按集出片：只合成所选集
            if seg["id"] not in render:
                raise RuntimeError(f"{seg['id']} 视频段缺失，无法合成第 {seg['episode']} 集")
            by_ep[seg["episode"]].append(str(project.dir / render[seg["id"]]))
        for no, clips in sorted(by_ep.items()):
            out = project.path("episodes", f"E{no}.mp4")
            srt = project.save_text(f"episodes/E{no}.srt", build_srt(boards.get(no, {}), bible))
            inputs, filters = [], []
            for i, clip in enumerate(clips):
                inputs += ["-i", clip]
                filters.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                               f"setsar=1,fps={fps},format=yuv420p[v{i}];"
                               f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}];")
            concat = "".join(f"[v{i}][a{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
            fc = "".join(filters) + concat
            vmap = "[v]"
            if burn:
                sub = str(srt).replace("\\", "/").replace(":", "\\:")
                fc += f";[v]subtitles='{sub}'[vs]"
                vmap = "[vs]"
            try:
                await _ffmpeg(*inputs, "-filter_complex", fc, "-map", vmap, "-map", "[a]",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                              "-movflags", "+faststart", str(out))
            except RuntimeError:
                # 某些段没有音轨：补静音后重试
                fixed = []
                for clip in clips:
                    p = Path(clip).with_name(Path(clip).stem + "_a.mp4")
                    if not p.exists():
                        await _ffmpeg("-i", clip, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest",
                                      "-c:v", "copy", "-c:a", "aac", str(p))
                    fixed.append(str(p))
                inputs = sum((["-i", c] for c in fixed), [])
                await _ffmpeg(*inputs, "-filter_complex", fc, "-map", vmap, "-map", "[a]",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                              "-movflags", "+faststart", str(out))
            episodes.append({"no": no, "video": f"episodes/E{no}.mp4", "srt": f"episodes/E{no}.srt"})
            await self.progress(project, f"第 {no} 集成片完成", file=f"episodes/E{no}.mp4")
        for e in episodes:
            prev[e["no"]] = e
        final = {"title": bible.get("title"), "episodes": [prev[k] for k in sorted(prev)]}
        project.save(self.produces, final)
        return final
