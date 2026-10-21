"""L0~L2 写作层：立项锁定 → 开发圣经 → 分集剧本 → 五席圆桌审核。"""
from __future__ import annotations

import asyncio
import json

from ..kernel.project import Project
from ..script_import import split_episodes
from ..sdf import adapt_references, check_episode, render_episode
from .base import Stage, load_prompt

# 视频模型时长约束（秒）。可在 profile 的 intake.config.models 覆盖。
MODEL_LIMITS = {
    "seedance-2.0": {"min": 4, "max": 15},
    "seedance-2.5": {"min": 4, "max": 15},
    "default": {"min": 4, "max": 10},
}

DEFAULTS = {
    "episodes": 1,
    "episode_seconds": 60,
    "segment_seconds": 10,
    "ratio": "9:16",
    "style": "auto",
    "genre": "auto",
    "track": "domestic",      # domestic 国内剧 / overseas 海外剧 / explainer 解说
    "language": "zh",
    "mode": "full",           # full 一键成片 / prompts 只出导演稿与提示词（不花生成费）
    "continuity": "refs",     # refs 参考图锁定 / last_frame 末帧承接（段间串行）
    "review_threshold": 75,
}


class IntakeStage(Stage):
    """〇 立项锁定：一键模式下所有锁都有默认值，不打断用户；只做校验与归一化。"""

    name, produces, title = "intake", "brief", "立项锁定"

    async def run(self, project: Project, req: dict) -> dict:
        defaults = {**DEFAULTS, **self.config.get("defaults", {})}
        brief = {**defaults, **{k: v for k, v in req.items() if v not in (None, "") and k != "script_text"}}
        script_text = str(req.get("script_text") or "").strip()
        brief["source"] = "script" if script_text else "idea"
        if script_text:
            # 上传剧本：集数由剧本决定；未指定每集时长时按剧本估算
            cps = 2.5 if brief["track"] == "overseas" else float(self.config.get("cps", 4.5))
            # 指定集数时按集数合并/拆分（如 16 章小说 → 10 集），否则按剧本格式识别
            eps = split_episodes(script_text, cps, int(req["episodes"]) if req.get("episodes") else None)
            # 改编模式：小说（第N章）默认开启，按目标时长重写；剧本默认保留原文
            adapt = req.get("adapt")
            brief["adapt"] = (eps[0].get("format") == "chapter") if adapt in (None, "", "auto") else bool(adapt)
            if brief["adapt"] and not req.get("episode_seconds"):
                req = {**req, "episode_seconds": self.config.get("adapt_seconds", 60)}
            if req.get("episode_seconds"):
                for ep in eps:
                    ep["seconds"] = int(req["episode_seconds"])
            project.save("source_script", eps)
            brief["episodes"] = len(eps)
            brief["episode_seconds"] = max(e["seconds"] for e in eps)
            brief.setdefault("idea", brief.get("title") or eps[0]["text"][:60])
        elif not str(brief.get("idea", "")).strip():
            raise ValueError("缺少创意 idea")
        max_eps = int(self.config.get("max_episodes", 20))
        if brief["source"] == "script" and brief["episodes"] > max(max_eps, 100):
            raise ValueError(f"剧本共 {brief['episodes']} 集，超过单次上限，请分批上传")
        if brief["source"] == "idea":
            brief["episodes"] = max(1, min(int(brief["episodes"]), max_eps))
        brief["episode_seconds"] = max(15, min(int(brief["episode_seconds"]), 600))
        if brief["ratio"] not in ("9:16", "16:9", "1:1", "3:4", "4:3"):
            raise ValueError(f"不支持的画幅: {brief['ratio']}")
        family = "seedance-2.5" if "2.5" in str(brief.get("video_model", "")) else "seedance-2.0"
        lim = {**MODEL_LIMITS, **self.config.get("models", {})}.get(family, MODEL_LIMITS["default"])
        brief["segment_min"], brief["segment_max"] = lim["min"], lim["max"]
        brief["segment_seconds"] = max(lim["min"], min(int(brief["segment_seconds"]), lim["max"]))
        brief["cps"] = 2.5 if brief["track"] == "overseas" else float(self.config.get("cps", 4.5))
        if brief["track"] == "overseas" and brief["language"] == "zh":
            brief["language"] = "en"
        project.save(self.produces, brief)
        return brief


class BibleStage(Stage):
    name, produces, title = "bible", "bible", "开发圣经"

    async def run(self, project: Project, req: dict) -> dict:
        brief = project.load("brief")
        llm = self.ctx.get("llm")
        lang = "英语（北美口语）" if brief["language"] == "en" else "中文"
        track_note = {
            "domestic": "国内竖屏短剧（抖音/快手/红果）",
            "overseas": "海外短剧（ReelShort/DramaBox 风格，人物用英文名，台词用英语）",
            "explainer": "解说叙事类（以旁白 VO 为主，画面为情景再现）",
        }.get(brief["track"], "竖屏短剧")
        if brief.get("source") == "script":
            return await self._from_script(project, brief, lang, track_note)
        user = (
            f"创意：{brief['idea']}\n"
            f"赛道：{track_note}\n台词语言：{lang}\n"
            f"集数：{brief['episodes']} 集，每集约 {brief['episode_seconds']} 秒\n"
            f"画幅：{brief['ratio']}\n题材：{brief['genre']}\n画风：{brief['style']}\n"
            f"episodes 数组必须恰好 {brief['episodes']} 项。"
        )
        bible = await llm.chat_json(load_prompt("bible"), user)
        if not bible.get("characters") or not bible.get("scenes"):
            raise ValueError("开发圣经缺少角色或场景")
        bible["episodes"] = (bible.get("episodes") or [])[: brief["episodes"]]
        project.save(self.produces, bible)
        await self.progress(project, f"《{bible.get('title', '')}》{len(bible['characters'])} 角色 / {len(bible['scenes'])} 场景")
        return bible

    async def _from_script(self, project: Project, brief: dict, lang: str, track_note: str) -> dict:
        """剧本拆解：从上传的剧本中提炼角色 / 场景 / 画风 / 分集节拍。"""
        eps = project.load("source_script")
        full = "\n\n".join(([eps[0]["preface"]] if eps[0].get("preface") else [])
                           + [f"【第{e['no']}集 {e['title']}】\n{e['text']}" for e in eps])
        user = (
            f"赛道：{track_note}\n台词语言：{lang}\n画幅：{brief['ratio']}\n画风：{brief['style']}\n"
            f"分集列表：{json.dumps([{'no': e['no'], 'title': e['title']} for e in eps], ensure_ascii=False)}\n"
            f"episodes 数组必须恰好 {len(eps)} 项，no 与分集列表一致。\n\n剧本全文：\n{full}"
        )
        bible = await self.ctx.get("llm").chat_json(load_prompt("bible_import"), user, temperature=0.4)
        if not bible.get("characters") or not bible.get("scenes"):
            raise ValueError("剧本拆解失败：未识别出角色或场景")
        got = {int(e.get("no", 0)): e for e in bible.get("episodes") or [] if str(e.get("no", "")).isdigit()}
        bible["episodes"] = [{**got.get(e["no"], {}), "no": e["no"], "title": got.get(e["no"], {}).get("title") or e["title"]}
                             for e in eps]
        project.save(self.produces, bible)
        await self.progress(project, f"剧本拆解《{bible.get('title', '')}》{len(eps)} 集 / "
                                     f"{len(bible['characters'])} 角色 / {len(bible['scenes'])} 场景")
        return bible


class ScriptStage(Stage):
    name, produces, title = "script", "scripts", "分集剧本"

    async def run(self, project: Project, req: dict) -> list:
        brief, bible = project.load("brief"), project.load("bible")
        llm = self.ctx.get("llm")
        system = load_prompt("script")
        bible_brief = {k: bible.get(k) for k in ("title", "logline", "genre", "tone", "characters", "scenes", "props")}
        source = {e["no"]: e for e in project.load("source_script", [])} if brief.get("source") == "script" else {}
        adapt = bool(source) and bool(brief.get("adapt"))
        if source:
            system = load_prompt("script_adapt") + "\n\n" + adapt_references() if adapt else load_prompt("script_import")
        lang_en = brief["language"] == "en"

        async def one(ep: dict) -> dict:
            cached = project.load(f"script_E{ep['no']}")
            if cached:
                return cached
            if adapt:
                return await adapt_one(ep)
            if source:
                src = source[ep["no"]]
                user = (f"开发圣经：\n{json.dumps(bible_brief, ensure_ascii=False)}\n\n"
                        f"第 {ep['no']} 集原始剧本：\n{src['text']}")
                s = await llm.chat_json(system, user, temperature=0.2)
                s["no"], s["target_seconds"] = ep["no"], src["seconds"]
                s.setdefault("title", ep.get("title", ""))
                project.save(f"script_E{ep['no']}", s)
                await self.progress(project, f"第 {ep['no']} 集剧本结构化完成")
                return s
            user = (
                f"开发圣经：\n{json.dumps(bible_brief, ensure_ascii=False)}\n\n"
                f"本集：{json.dumps(ep, ensure_ascii=False)}\n"
                f"目标时长：{brief['episode_seconds']} 秒；台词语言：{'英语' if brief['language'] == 'en' else '中文'}。"
            )
            s = await llm.chat_json(system, user)
            s["no"] = ep["no"]
            project.save(f"script_E{ep['no']}", s)
            await self.progress(project, f"第 {ep['no']} 集剧本完成")
            return s

        async def adapt_one(ep: dict) -> dict:
            """小说改编（short-drama-factory 漫剧档）：改编 → 渲染标准排版 → 官方机检 → 返修（最多 2 轮）。"""
            src, sec = source[ep["no"]], int(source[ep["no"]]["seconds"])
            # 时长倒推：(时长 - 留白 5s) × 4.5 字/s，再留 20% 给动作行
            budget = sec * 2 if lang_en else int((sec - 5) * 4.5 * 0.8)
            next_no = ep["no"] + 1 if ep["no"] < max(source) else None
            user = (f"开发圣经：\n{json.dumps(bible_brief, ensure_ascii=False)}\n\n"
                    f"本集节拍（来自开发圣经）：{json.dumps(ep, ensure_ascii=False)}\n"
                    f"本集为第 {ep['no']}/{len(source)} 集{'（终集，断章用情绪落地或季终悬念）' if not next_no else ''}。\n"
                    f"目标时长：{sec} 秒（漫剧档）；台词+独白+旁白总量 ≤ {budget} {'词' if lang_en else '字'}；"
                    f"台词语言：{'英语' if lang_en else '中文'}。\n\n第 {ep['no']} 集小说原文：\n{src['text']}")
            s = await llm.chat_json(system, user, temperature=0.6)
            report: dict = {}
            for rnd in range(3):
                used = sum(len(b.get("text", "").split()) if lang_en else len(b.get("text", ""))
                           for sc in s.get("scenes", []) for b in sc.get("blocks", []) if b.get("type") in ("dialogue", "vo"))
                report = {} if lang_en else check_episode(render_episode(s, bible, sec, next_no=next_no))
                problems = list(report.get("fails", []))
                if used > budget * 1.15:
                    problems.append(f"时长超标：台词+独白+旁白共 {used} 字，超出 {budget} 字上限（{sec} 秒）")
                if not problems or rnd == 2:
                    break
                await self.progress(project, f"第 {ep['no']} 集机检未通过（{len(problems)} 项），返修第 {rnd + 1} 轮")
                s = await llm.chat_json(system, user + f"\n\n你上一版：{json.dumps(s, ensure_ascii=False)}\n\n"
                                        "short-drama-factory 机检发现以下问题，请修正后输出完整的新版 JSON"
                                        "（超时就删次要台词与动作，保留主线、结算与断章）：\n"
                                        + "\n".join(f"- {p}" for p in problems + report.get("hints", [])),
                                        temperature=0.4)
            s["no"], s["target_seconds"] = ep["no"], sec
            s.setdefault("title", ep.get("title", ""))
            s["sdf_check"] = {"fails": report.get("fails", []), "warns": report.get("warns", []), "dialogue_chars": used}
            project.save(f"script_E{ep['no']}", s)
            project.save_text(f"episodes_script/E{ep['no']}.md", render_episode(s, bible, sec, next_no=next_no))
            verdict = "通过" if not report.get("fails") else f"仍有 {len(report['fails'])} 项 FAIL"
            await self.progress(project, f"第 {ep['no']} 集改编完成（{sec}s · 台词 {used} 字 · 机检{verdict}）")
            return s

        scripts = await asyncio.gather(*(one(ep) for ep in bible["episodes"]))
        project.save(self.produces, list(scripts))
        return list(scripts)


class ReviewStage(Stage):
    """五席圆桌审核：低于阈值自动返修一次（可配置轮数）。"""

    name, produces, title = "review", "review", "圆桌审核"

    async def run(self, project: Project, req: dict) -> dict:
        brief, bible, scripts = project.load("brief"), project.load("bible"), project.load("scripts")
        llm = self.ctx.get("llm")
        threshold = int(brief.get("review_threshold", 75))
        rounds = int(self.config.get("max_rounds", 1))
        system = load_prompt("review")
        report = {}
        # 上传的剧本默认只评分不改写（尊重原作），可用 review_rewrite=true 允许自动返修
        rewrite = brief.get("source") != "script" or bool(brief.get("review_rewrite"))

        async def one(script: dict) -> tuple[int, dict, dict]:
            cur, history = script, []
            for _ in range(rounds):
                user = (f"阈值：{threshold}\n角色：{json.dumps(bible['characters'], ensure_ascii=False)}\n"
                        f"剧本：{json.dumps(cur, ensure_ascii=False)}")
                r = await llm.chat_json(system, user, temperature=0.3)
                history.append({k: r.get(k) for k in ("scores", "total", "notes")})
                revised = r.get("revised_script")
                if not rewrite or int(r.get("total") or 0) >= threshold or not isinstance(revised, dict) or not revised.get("scenes"):
                    break
                revised["no"], revised["target_seconds"] = cur["no"], cur.get("target_seconds")
                cur = revised
            return cur["no"], cur, {"rounds": history}

        results = await asyncio.gather(*(one(s) for s in scripts))
        new_scripts = []
        for no, script, rep in sorted(results, key=lambda x: x[0]):
            new_scripts.append(script)
            report[f"E{no}"] = rep
            last = rep["rounds"][-1] if rep["rounds"] else {}
            await self.progress(project, f"第 {no} 集评审 {last.get('total', '-')} 分")
        project.save("scripts", new_scripts)
        project.save(self.produces, report)
        return report
