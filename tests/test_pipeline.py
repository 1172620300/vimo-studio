import asyncio
import json

import pytest

from drama_agent.kernel.config import load_config
from drama_agent.kernel.context import Context, Plugin
from drama_agent.kernel.project import Project
from drama_agent.pipeline import Pipeline, build_context
from drama_agent.providers.llm import extract_json
from drama_agent.stages.feed import compile_segment
from drama_agent.stages.qc import check_prompt, check_storyboard, safety_rewrite


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAMA_DATA_DIR", str(tmp_path))
    return tmp_path


BIBLE = {"style": {"name": "写实", "prefix": "冷调"},
         "characters": [{"id": "C1", "name": "林晚", "gender": "女", "age": "24", "costume": "黑西装"}],
         "scenes": [{"id": "S1", "name": "宴会厅", "time_of_day": "夜", "lighting": "暖光"}]}
BRIEF = {"ratio": "9:16", "language": "zh", "cps": 4.5, "segment_min": 4, "segment_max": 15, "episode_seconds": 10}


def seg(**kw):
    base = {"id": "E1-01", "duration": 10, "scene_id": "S1", "characters": ["C1"], "blocking": "C1 居中",
            "shots": [{"t": "0-10s", "size": "近景", "camera": "缓推", "action": "C1 抬头",
                       "dialogue": [{"speaker": "C1", "emotion": "冷", "text": "我回来了"}]}]}
    base.update(kw)
    return base


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的，结果如下：{"a": {"b": "x}"}} 以上') == {"a": {"b": "x}"}}


def test_qc_catches_lost_line_and_speech_rate():
    script = {"no": 1, "scenes": [{"blocks": [{"type": "dialogue", "speaker": "C1", "text": "我回来了"},
                                              {"type": "dialogue", "speaker": "C1", "text": "这一句被丢掉了啊"}]}]}
    issues = check_storyboard({"segments": [seg()]}, script, BIBLE, BRIEF)
    assert any(i.code == "LINE" for i in issues)
    long = "很" * 80
    issues = check_storyboard({"segments": [seg(shots=[{"t": "0-10s", "size": "近景", "camera": "缓推", "action": "",
                                                        "dialogue": [{"speaker": "C1", "text": long}]}])]},
                              {"no": 1, "scenes": []}, BIBLE, BRIEF)
    assert any(i.code == "CPS" for i in issues)
    issues = check_storyboard({"segments": [seg(duration=30, scene_id="S9")]}, {"no": 1, "scenes": []}, BIBLE, BRIEF)
    assert {"DUR", "SCN"} <= {i.code for i in issues if i.level == "P0"}


def test_feed_local_numbering_matches_refs():
    assets = {"S1": {"name": "宴会厅", "kind": "scene", "file": "images/S1.png"},
              "C1": {"name": "林晚", "kind": "character", "file": "images/C1.png"}}
    out = compile_segment(seg(), BIBLE, BRIEF, assets)
    assert [r["asset"] for r in out["refs"]] == ["S1", "C1"]
    assert "图1：宴会厅" in out["prompt"] and "图2：林晚" in out["prompt"]
    assert "C1" not in out["prompt"]  # 角色 ID 已替换为姓名
    assert not [i for i in check_prompt(out["id"], out["prompt"], len(out["refs"]), 2000) if i.level == "P0"]


def test_safety_rewrite():
    assert "血" not in safety_rewrite("他喷血倒地，鲜血四溅")


def test_context_plugin_unload():
    ctx = Context()

    class P(Plugin):
        name = "p"

        def apply(self, c, cfg):
            c.provide("x", 1)
            c.on("e", lambda: None)

    ctx.plugin(P())
    assert ctx.get("x") == 1
    ctx.unload("p")
    assert not ctx.has("x")


def test_profile_patch_by_id():
    cfg = load_config("mock")
    uses = [p["use"] for p in cfg["plugins"]]
    assert ".providers.mock:MockLLMPlugin" in uses
    assert ".providers.llm:OpenAICompatLLMPlugin" not in uses


def test_mock_pipeline_end_to_end_and_resume():
    ctx = build_context("mock")
    pipe = Pipeline(ctx)
    p = Project.create({"idea": "真千金归来", "episodes": 2, "episode_seconds": 20})
    final = asyncio.run(pipe.run(p))
    assert len(final["episodes"]) == 2
    assert (p.dir / "episodes" / "E1.mp4").stat().st_size > 0
    assert p.status()["state"] == "done"
    n = len(p.events())
    asyncio.run(pipe.run(p))
    skipped = [e for e in p.events(n) if e["type"] == "stage/skip"]
    assert len(skipped) == len(ctx.config["stages"])


def test_prompts_mode_skips_paid_stages():
    pipe = Pipeline(build_context("mock"))
    p = Project.create({"idea": "真千金归来", "mode": "prompts"})
    asyncio.run(pipe.run(p))
    assert (p.dir / "director.md").exists()
    assert not (p.dir / "segments").exists()
    started = {e["data"]["stage"] for e in p.events() if e["type"] == "stage/start"}
    assert not started & {"asset_images", "render", "assemble"}


SCRIPT = """人物：林晚（真千金）、林薇（假千金）

第一集 宴会泼酒
林家宴会厅，夜。
林薇（讥讽）：你也配回林家？
林晚：这杯酒，我记下了。

第2集：保镖入场
大门推开，一排黑衣保镖鱼贯而入。
林晚：从今天起，这里我说了算。
"""


def test_split_episodes_and_estimate():
    from drama_agent.script_import import cn_to_int, split_episodes
    assert cn_to_int("十二") == 12 and cn_to_int("二十") == 20 and cn_to_int("３") == 3
    eps = split_episodes(SCRIPT)
    assert [(e["no"], e["title"]) for e in eps] == [(1, "宴会泼酒"), (2, "保镖入场")]
    assert "人物" in eps[0]["preface"] and "林薇" in eps[0]["text"]
    assert all(15 <= e["seconds"] <= 600 for e in eps)
    assert len(split_episodes("没有分集标题的一段剧本内容。\n林晚：你好。")) == 1


def test_extract_text_docx_and_gbk():
    import io
    import zipfile
    from drama_agent.script_import import extract_text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body><w:p><w:r><w:t>第1集</w:t></w:r></w:p><w:p><w:r><w:t>林晚：</w:t></w:r><w:r><w:t>你好</w:t></w:r></w:p>"
                   "</w:body></w:document>")
    assert extract_text("a.docx", buf.getvalue()) == "第1集\n林晚：你好"
    assert extract_text("a.txt", "第1集\r\n林晚：你好".encode("gb18030")) == "第1集\n林晚：你好"


def test_script_upload_pipeline_keeps_episodes_and_skips_rewrite():
    pipe = Pipeline(build_context("mock"))
    p = Project.create({"script_text": SCRIPT, "title": "逆光", "mode": "prompts"})
    asyncio.run(pipe.run(p))
    brief = p.load("brief")
    assert brief["source"] == "script" and brief["episodes"] == 2
    assert [e["no"] for e in p.load("bible")["episodes"]] == [1, 2]
    assert [s["no"] for s in p.load("scripts")] == [1, 2]
    assert all(s.get("target_seconds") for s in p.load("scripts"))
    assert "上传剧本" in (p.dir / "director.md").read_text(encoding="utf-8")
    assert not (p.dir / "segments").exists()


FORMAT_CASES = {
    "bracket_cn": ("【第一集】重生\n林晚睁开眼。\n林晚：我回来了。\n\n【第二集】打脸\n宴会厅。\n林薇：你！\n\n【第十集】大结局\n婚礼。\n林晚：结束了。",
                   "episode", [1, 2, 10]),
    "ep_en": ("EP01 The Return\nINT. HALL - NIGHT\nLIN: I'm back.\n\nEP02 Payback\nEXT. STREET - DAY\nWEI: No!",
              "episode", [1, 2]),
    "hua": ("第1话\n林晚：你好。\n第2话\n林薇：再见。", "episode", [1, 2]),
    "later_batch": ("第11集 反转\n林晚：是你。\n\n第12集 真相\n林薇：不可能。", "episode", [11, 12]),
    "scene_dash": ("人物表：林晚、林薇\n\n1-1 夜 内 宴会厅\n林薇：你也配？\n1-2 夜 外 门口\n保镖入场。\n"
                   "2-1 日 内 办公室\n林晚：开会。\n2-2日内 走廊\n林薇跟上。\n3-1 夜 外 天台\n林晚：结束了。",
                   "scene", [1, 2, 3]),
    "scene_per_ep_header": ("第1集第1场 夜 内 宴会厅\n林薇：你也配？\n第1集第2场 夜 外 门口\n保镖入场。\n"
                            "第2集第1场 日 内 办公室\n林晚：开会。", "episode", [1, 2]),
    "numbered": ("1\n林家宴会厅，夜。林薇端起酒杯泼向林晚，宾客哗然。\n林晚：这杯酒我记下了。\n\n"
                 "2\n大门推开，一排黑衣保镖鱼贯而入，全场安静下来。\n林晚：从今天起我说了算。", "number", [1, 2]),
    "toc": ("目录\n第1集 归来\n第2集 打脸\n\n第1集 归来\n林晚：我回来了。\n\n第2集 打脸\n林薇：你！", "episode", [1, 2]),
    "none": ("林家宴会厅，夜。\n林薇：你也配回林家？\n林晚：这杯酒，我记下了。我们第一集的时候就说过。", "none", [1]),
}


@pytest.mark.parametrize("name", list(FORMAT_CASES))
def test_detect_episode_formats(name):
    from drama_agent.script_import import analyze, split_episodes
    text, fmt, nos = FORMAT_CASES[name]
    a = analyze(text)
    assert a["format"] == fmt, a
    assert [e["no"] for e in a["episodes"]] == nos
    eps = split_episodes(text)
    if name == "scene_dash":
        assert eps[0]["text"].startswith("1-1 夜 内 宴会厅") and "1-2" in eps[0]["text"]
        assert "人物表" in eps[0]["preface"]
    if name == "scene_per_ep_header":
        assert "第1集第2场" in eps[0]["text"]
    if name == "toc":
        assert eps[0]["title"] == "归来" and "我回来了" in eps[0]["text"]
    if name == "bracket_cn":
        assert [e["title"] for e in eps] == ["重生", "打脸", "大结局"]


def test_chapters_regroup_to_target_episodes():
    from drama_agent.script_import import analyze, split_episodes
    text = "\n\n".join(f"第{n}章：标题{n}\n\n" + "\n\n".join(f"林晚：第{n}章第{k}句台词。" for k in range(i % 3 + 2))
                       for i, n in enumerate("一二三四五六七八九十"))
    a = analyze(text)
    assert a["format"] == "chapter" and len(a["episodes"]) == 10
    eps = split_episodes(text, target=4)
    assert len(eps) == 4 and eps[0]["sources"][0] == "第一章"
    assert "第一章：标题一" in eps[0]["text"]                       # 合并后保留章节标题
    flat = [s for e in eps for s in e["sources"]]
    assert flat == [f"第{n}章" for n in "一二三四五六七八九十"]       # 连续、不重不漏
    more = split_episodes("林晚：你好。\n\n林薇：再见。\n\n林晚：走吧。\n\n林薇：好的。", target=3)
    assert len(more) == 3                                             # 没有标记时按段落拆分


def test_novel_chapters_default_to_adaptation():
    novel = "\n\n".join(f"第{n}章：标题{n}\n\n" + "林晚走进大厅。\n\n林晚：我回来了。\n\n" * 30 for n in "一二三")
    pipe = Pipeline(build_context("mock"))
    p = Project.create({"script_text": novel, "mode": "prompts", "episodes": 2})
    asyncio.run(pipe.run(p))
    brief = p.load("brief")
    assert brief["adapt"] is True and brief["episodes"] == 2 and brief["episode_seconds"] == 60
    assert all(s["target_seconds"] == 60 for s in p.load("scripts"))
    p2 = Project.create({"script_text": novel, "mode": "prompts", "adapt": False})
    asyncio.run(pipe.run(p2))
    assert p2.load("brief")["adapt"] is False


def test_sdf_render_and_check():
    from drama_agent.sdf import adapt_references, check_episode, render_episode
    assert "网文→短剧改编流水线" in adapt_references() and "断章" in adapt_references()
    bible = {"characters": [{"id": "C1", "name": "林小凡"}, {"id": "C2", "name": "阎王"}],
             "scenes": [{"id": "S1", "name": "阎王殿", "time_of_day": "夜"}]}
    script = {"no": 1, "title": "猝死入职", "card": {"hook_type": "生死绝境", "emotion_flow": "猝死 ➔ 入职 ➔ 反杀 ➔ 绝命断章",
                                                     "cliffhanger": "生死簿上，林小凡的名字开始发光。"},
              "scenes": [{"scene_id": "S1", "characters": ["C1", "C2"], "blocks": [
                  {"type": "action", "text": "特写：林小凡猛地睁眼，脚下是翻滚的血色忘川，铁链勒住脖子。"},
                  {"type": "dialogue", "speaker": "C2", "emotion": "俯身逼近｜冷笑·低语", "text": "加班猝死？阳寿已尽，跪下画押。"},
                  {"type": "dialogue", "speaker": "C1", "emotion": "扯开铁链｜一字一顿", "text": "先把加班费结了再说。"},
                  {"type": "action", "text": "阎王的判官笔啪地折断。"},
                  {"type": "dialogue", "speaker": "C2", "emotion": "厉声", "text": "放肆！你可知这是何处？"},
                  {"type": "dialogue", "speaker": "C1", "emotion": "掏出简历｜轻声", "text": "知道。所以我来面试。"}]}]}
    text = render_episode(script, bible, 60, next_no=2)
    assert "[场景]：阎王殿·夜" in text and "阎王（俯身逼近｜冷笑·低语）：" in text and "下滑立即解锁第2集" in text
    rep = check_episode(text)
    assert not [f for f in rep["fails"] if f.startswith(("E3", "E6", "E12"))], rep
    script["card"]["cliffhanger"] = ""
    script["scenes"][0]["blocks"].append({"type": "vo", "speaker": "C1", "text": "我" * 30})
    assert any(f.startswith("E12") or f.startswith("E13") for f in check_episode(render_episode(script, bible, 60))["fails"])


def test_render_episode_by_episode(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DRAMA_PROFILE", "mock")
    monkeypatch.delenv("DRAMA_API_TOKEN", raising=False)
    from drama_agent.server import app as srv

    def wait(c, pid):
        for _ in range(200):
            s = c.get(f"/api/projects/{pid}").json()
            if not s["running"] and s["state"] in ("done", "failed", "paused"):
                return s
            time.sleep(0.05)
        raise AssertionError("timeout")

    with TestClient(srv.app) as c:
        pid = c.post("/api/projects", json={"idea": "真千金归来", "episodes": 2, "episode_seconds": 20,
                                            "mode": "prompts"}).json()["id"]
        s = wait(c, pid)
        assert s["state"] == "done" and not s["final"]
        assert c.post(f"/api/projects/{pid}/render", json={"episodes": [9]}).status_code == 400
        r = c.post(f"/api/projects/{pid}/render", json={"episodes": [2]}).json()
        assert r["selected"] == [2]
        s = wait(c, pid)
        assert s["state"] == "done", s["error"]
        assert [e["no"] for e in s["final"]["episodes"]] == [2]
        assert all(bool(g["video"]) == (g["episode"] == 2) for g in s["segments"])
        c.post(f"/api/projects/{pid}/render", json={"episodes": [1]})
        s = wait(c, pid)
        assert [e["no"] for e in s["final"]["episodes"]] == [1, 2]
        assert "render_episodes" not in Project.open(pid).load("request")   # 全部集都已选，回到"全部"


def test_video_body_follows_cangyuan_contract():
    from drama_agent.providers.media import CangyuanVideo
    v = CangyuanVideo("https://direct-api.cangyuansuanli.cn/v1", "k", "sd17-seedance-2.0-4", resolution="720p")
    assert v.base_url == "https://direct-api.cangyuansuanli.cn"           # 去掉结尾 /v1
    b = v.build_body("p", 4, "9:16", ["https://x/a.png"])
    assert b == {"model": "sd17-seedance-2.0-4", "prompt": "p", "duration": 5, "aspect_ratio": "9:16",
                 "resolution": "720p", "generate_audio": True, "watermark": False,
                 "content": [{"type": "text", "text": "p"}], "reference_image_urls": ["https://x/a.png"]}
    b = v.build_body("p", 6, "9:16", ["https://x/a.png"], model="sd17-seedance-2.0")
    assert b["reference_image_urls"] == ["https://x/a.png"] and "content" not in b      # 按条线路保持原格式
    b = v.build_body("p", 40, "9:16", [], model="sd17-seedance-2.5-4", resolution="1080p")
    assert b["duration"] == 30 and b["resolution"] == "1080p" and b["content"] == [{"type": "text", "text": "p"}]
    assert v.build_body("p", 8, "9:16", [], resolution="1080p")["resolution"] == "720p"   # 2.0-4 不支持 1080p → 默认


def test_video_timeout_keeps_task_and_does_not_resubmit(tmp_path):
    from drama_agent.providers.media import CangyuanVideo, TaskPending
    v = CangyuanVideo("http://x", "k", "sd17-seedance-2.5-4", poll_interval=0, poll_max=2)
    calls = {"submit": 0}
    saved = []

    async def submit(body):
        calls["submit"] += 1
        return "task-1"

    async def wait_timeout(task_id, on_progress=None):
        raise TaskPending(task_id, "timeout")

    async def on_task(t):
        saved.append(t)

    v.submit, v.wait = submit, wait_timeout
    with pytest.raises(TaskPending):
        asyncio.run(v.generate("p", tmp_path / "a.mp4", duration=5, ratio="9:16", refs=[], on_task=on_task))
    assert calls["submit"] == 1 and saved == ["task-1"]                    # 已受理的任务被保存，没有重提交

    async def wait_ok(task_id, on_progress=None):
        assert task_id == "task-1"
        return {"status": "completed", "video_url": "http://x/v.mp4"}

    async def fake_download(url, out, headers=None):
        out.write_bytes(b"x")
        return out

    import drama_agent.providers.media as media
    v.wait = wait_ok
    orig, media.download = media.download, fake_download
    try:
        asyncio.run(v.generate("p", tmp_path / "a.mp4", duration=5, ratio="9:16", refs=[], task_id="task-1",
                               on_task=on_task))
    finally:
        media.download = orig
    assert calls["submit"] == 1 and saved[-1] is None                      # 续跑只轮询旧任务


def test_signed_media_urls(monkeypatch):
    import time
    from drama_agent import media_urls
    monkeypatch.setenv("DRAMA_MEDIA_SECRET", "s3cret")
    assert media_urls.public_url("p1", "images/C1.png") is None            # 未配置公网地址
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://drama.example.com/")
    u = media_urls.public_url("p1", "images/C1.png")
    assert u.startswith("https://drama.example.com/media/p1/images/C1.png?exp=")
    exp, sig = int(u.split("exp=")[1].split("&")[0]), u.split("sig=")[1]
    assert media_urls.verify("p1", "images/C1.png", exp, sig)
    assert not media_urls.verify("p2", "images/C1.png", exp, sig)          # 换项目无效
    assert not media_urls.verify("p1", "images/C1.png", int(time.time()) - 1, sig)
    assert media_urls.public_url("p1", "artifacts/brief.json") is None     # 只开放图片
    assert media_urls.public_url("p1", "images/../.env") is None


def test_fit_duration_trims_long_clip(tmp_path):
    import shutil
    import subprocess
    from drama_agent.stages.render import fit_duration, probe_seconds
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg 未安装")
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=6",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "6", "-c:v", "libx264", "-c:a", "aac",
                    str(clip)], check=True)
    assert asyncio.run(fit_duration(clip, 4.0)) is True
    assert abs(asyncio.run(probe_seconds(clip)) - 4.0) < 0.3
    assert asyncio.run(fit_duration(clip, 4.0)) is False   # 已经合适，不再处理


def test_render_retries_transient_and_face_rejection(monkeypatch):
    from drama_agent.providers.media import GenerationError
    from drama_agent.stages.render import is_face_rejected, is_transient, strip_ref_tags
    assert is_transient("视频任务失败（task x）: 生成服务响应超时，请先查询已有任务状态")
    assert is_face_rejected("参考素材中的人脸或人物信息未通过审核，请更换")
    assert strip_ref_tags("图1：殿\n图2：林晚（女）；图3：林薇", keep=1) == "图1：殿\n林晚（女）；林薇"

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://drama.example.com")
    monkeypatch.setenv("DRAMA_MEDIA_SECRET", "s")
    ctx = build_context("mock")
    pipe = Pipeline(ctx)
    p = Project.create({"idea": "真千金归来", "episodes": 1, "episode_seconds": 20, "mode": "prompts"})
    asyncio.run(pipe.run(p))
    real = ctx.get("video")
    calls = []

    class Flaky:
        model, resolution = "sd17-seedance-2.0-4", "480p"

        async def generate(self, prompt, out, *, refs, **kw):
            calls.append((out.stem, len(refs), "图2" in prompt))
            n = sum(1 for c in calls if c[0] == out.stem)
            if out.stem.endswith("01") and n == 1:
                raise GenerationError("视频任务失败: 生成服务响应超时")
            if out.stem.endswith("01") and n == 2:
                raise GenerationError("视频任务失败: 参考素材中的人脸或人物信息未通过审核")
            return await real.generate(prompt, out, refs=refs, **kw)

    ctx.provide("video", Flaky())
    req = p.load("request")
    p.save("request", {**req, "mode": "full"})
    p.save("brief", {**p.load("brief"), "mode": "full"})
    asyncio.run(pipe.run(p))
    first = [c for c in calls if c[0].endswith("01")]
    assert [c[1] for c in first] == [3, 3, 1]          # 超时重提一次（同参考图）→ 人脸未过审改为只用场景图
    assert first[0][2] and not first[2][2]             # 降级后提示词里不再有「图2」
    assert (p.dir / "segments" / "E1-01.mp4").exists()


def test_resume_does_not_skip_unrendered_selected_episode():
    from drama_agent.stages.render import AssembleStage, RenderStage
    pipe = Pipeline(build_context("mock"))
    p = Project.create({"idea": "真千金归来", "episodes": 2, "episode_seconds": 20, "mode": "full",
                        "render_episodes": [1]})
    asyncio.run(pipe.run(p))
    assert [e["no"] for e in p.load("final")["episodes"]] == [1]
    p.save("request", {**p.load("request"), "render_episodes": [1, 2]})   # 追加第 2 集
    render = pipe.ctx.get("stage:render")
    assert not render.done(p) and not pipe.ctx.get("stage:assemble").done(p)
    asyncio.run(pipe.run(p))                                               # 续跑：第 2 集必须真的生成
    assert [e["no"] for e in p.load("final")["episodes"]] == [1, 2]
    assert render.done(p)
