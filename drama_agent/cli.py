"""命令行入口。

  python -m drama_agent run "被赶出家门的真千金三年后归来" --episodes 1 --seconds 60
  python -m drama_agent run "..." --mode prompts          # 只出导演稿/提示词，不花生成费
  python -m drama_agent run --script 剧本.docx             # 上传剧本：按「第N集」分集，保留原台词
  python -m drama_agent resume <项目ID> [--force render]
  python -m drama_agent status <项目ID>
  python -m drama_agent list
  python -m drama_agent serve --host 0.0.0.0 --port 8000
  python -m drama_agent doctor
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .kernel.project import Project
from .pipeline import Pipeline, build_context


def _setup_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    ap = argparse.ArgumentParser(prog="drama_agent", description="短剧一键生成")
    ap.add_argument("--profile", default=None, help="config/profiles 下的 profile 名（default / mock / llm-only）")
    ap.add_argument("--patch", action="append", default=[], help="额外的 YAML patch 文件，可多次指定")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="一句话创意 / 上传剧本 → 成片")
    r.add_argument("idea", nargs="?")
    r.add_argument("--script", help="剧本文件（.txt / .md / .docx），按「第N集」自动分集")
    r.add_argument("--episodes", type=int)
    r.add_argument("--seconds", type=int, dest="episode_seconds", help="每集时长（秒）")
    r.add_argument("--segment", type=int, dest="segment_seconds", help="单段视频时长（秒）")
    r.add_argument("--ratio", choices=["9:16", "16:9", "1:1", "3:4", "4:3"])
    r.add_argument("--style", help="画风，如 真人写实电影感 / 国漫3D / 日系二次元动漫；默认 auto")
    r.add_argument("--genre")
    r.add_argument("--track", choices=["domestic", "overseas", "explainer"])
    r.add_argument("--mode", choices=["full", "prompts"])
    r.add_argument("--continuity", choices=["refs", "last_frame"])

    rs = sub.add_parser("resume", help="断点续跑")
    rs.add_argument("project_id")
    rs.add_argument("--force", action="append", default=[], help="强制重跑的阶段名，可多次指定")

    st = sub.add_parser("status")
    st.add_argument("project_id")
    sub.add_parser("list")
    sv = sub.add_parser("serve", help="启动 HTTP 服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("doctor", help="检查环境与 API 连通性")
    sm = sub.add_parser("smoke", help="用真实接口各生成 1 张图 + 1 段 4 秒视频，验证付费链路（会产生少量费用）")
    sm.add_argument("--skip-video", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "doctor":
        from .doctor import main as doctor
        doctor()
        return 0
    if a.cmd == "serve":
        import os

        import uvicorn
        if a.profile:
            os.environ["DRAMA_PROFILE"] = a.profile
        uvicorn.run("drama_agent.server.app:app", host=a.host, port=a.port)
        return 0
    if a.cmd == "list":
        for pid in Project.list():
            s = Project.open(pid).status()
            print(f"{pid}  {s['state']:8s}  {s['request'].get('idea', '')[:40]}")
        return 0
    if a.cmd == "status":
        print(json.dumps(Project.open(a.project_id).status(), ensure_ascii=False, indent=2))
        return 0

    ctx = build_context(a.profile, a.patch)
    if a.cmd == "smoke":
        return asyncio.run(_smoke(ctx, a.skip_video))
    pipe = Pipeline(ctx)
    if a.cmd == "run":
        req = {k: v for k, v in vars(a).items()
               if k not in ("cmd", "profile", "patch", "script") and v is not None}
        if a.script:
            from pathlib import Path

            from .script_import import extract_text
            src = Path(a.script)
            req["script_text"] = extract_text(src.name, src.read_bytes())
            req.setdefault("title", src.stem)
        elif not a.idea:
            ap.error("请提供创意，或用 --script 指定剧本文件")
        project = Project.create(req)
        print(f"项目 ID：{project.id}\n目录：{project.dir}")
        force: list[str] = []
    else:
        project = Project.open(a.project_id)
        force = a.force
    try:
        final = asyncio.run(pipe.run(project, force=force))
    except Exception as e:  # noqa: BLE001
        print(f"\n失败：{e}\n修复后可执行：python -m drama_agent resume {project.id}")
        return 1
    print("\n完成：", json.dumps(final, ensure_ascii=False, indent=2))
    print(f"产物目录：{project.dir}")
    return 0


async def _smoke(ctx, skip_video: bool) -> int:
    import time

    from .kernel.project import data_root
    out = data_root() / "smoke"
    t0 = time.time()
    img = await ctx.get("image").generate("一只橘猫坐在窗台上看雨，电影感，柔光，画面中不要出现任何文字", out / "smoke.png",
                                          aspect="9:16")
    print(f"图像 OK：{img}（{time.time() - t0:.0f}s）")
    if skip_video:
        return 0
    t0 = time.time()

    async def prog(status, pct):
        print(f"  视频状态：{status} {pct or ''}")

    vid = await ctx.get("video").generate(
        "【画幅风格】9:16 竖屏 · 电影感\n【时间轴分镜】0-5s｜近景｜缓推：窗台上的橘猫转头看向镜头，眨眼。\n【音效】雨声",
        out / "smoke.mp4", duration=5, ratio="9:16", refs=[], on_progress=prog)   # 参考图需公网 HTTPS URL，冒烟测试不带
    print(f"视频 OK：{vid}（{time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
