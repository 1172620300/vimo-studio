"""L2 资产层（Asset-First）：先锁角色/场景/道具，再进分镜，防止跨镜漂移。

- assets：根据开发圣经确定性地生成资产清单与出图提示词（免费）
- asset_images：调用图像模型出图（付费；mode=prompts 时跳过 → 交付形态 M0/M1）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..kernel.project import Project
from .base import Stage, selected_episodes, selected_segments

NO_TEXT = "画面中不要出现任何文字、水印、字幕、logo"


def character_prompt(c: dict, style: dict) -> str:
    return (f"{style.get('name', '')}风格，{style.get('prefix', '')}。角色设定图：同一角色的正面、侧面、背面全身三视图并排，"
            f"右上角附面部特写。纯白背景，均匀柔光，站姿自然。角色：{c.get('gender', '')}，{c.get('age', '')}，"
            f"{c.get('appearance', '')}；服装：{c.get('costume', '')}。气质：{c.get('personality', '')}。{NO_TEXT}。")


def scene_prompt(s: dict, style: dict) -> str:
    return (f"{style.get('name', '')}风格，{style.get('prefix', '')}。场景概念空镜，画面中没有任何人物。"
            f"{s.get('name', '')}：{s.get('description', '')}。时间：{s.get('time_of_day', '')}，光线：{s.get('lighting', '')}。"
            f"构图留出人物活动空间，透视清晰。{NO_TEXT}。")


def prop_prompt(p: dict, style: dict) -> str:
    return (f"{style.get('name', '')}风格。道具设定图，单个物体居中，纯白背景，产品级布光。"
            f"{p.get('name', '')}：{p.get('description', '')}。{NO_TEXT}。")


class AssetsStage(Stage):
    name, produces, title = "assets", "assets", "资产锁定"

    async def run(self, project: Project, req: dict) -> dict:
        bible, brief = project.load("bible"), project.load("brief")
        style = bible.get("style", {})
        items = []
        for c in bible.get("characters", []):
            items.append({"id": c["id"], "kind": "character", "name": c["name"], "aspect": "16:9",
                          "prompt": character_prompt(c, style)})
        for s in bible.get("scenes", []):
            items.append({"id": s["id"], "kind": "scene", "name": s["name"], "aspect": brief["ratio"],
                          "prompt": scene_prompt(s, style)})
        if self.config.get("include_props", False):
            for p in bible.get("props", []):
                items.append({"id": p["id"], "kind": "prop", "name": p["name"], "aspect": "1:1",
                              "prompt": prop_prompt(p, style)})
        for i, it in enumerate(items, 1):
            it["ref"] = f"图{i}"          # 全局资产编号（接线单用）；投喂时会按段重新本地编号
            it["file"] = None
        assets = {"mode": "M0", "items": items}
        project.save(self.produces, assets)
        return assets


class AssetImagesStage(Stage):
    name, produces, title, paid = "asset_images", "asset_images", "资产出图", True

    def done(self, project: Project) -> bool:
        """所选集用到的资产图都已存在才算完成。"""
        assets, feed = project.load("assets"), project.load("feed")
        if not assets or not project.has(self.produces):
            return False
        req = project.load("request", {}) or {}
        needed = ({r["asset"] for s in selected_segments(feed, req) for r in s.get("refs", [])}
                  if feed and selected_episodes(req) else {it["id"] for it in assets["items"]})
        files = {it["id"]: it.get("file") for it in assets["items"]}
        return all(files.get(a) and (project.dir / files[a]).is_file() for a in needed if a in files)

    async def run(self, project: Project, req: dict) -> dict:
        assets = project.load("assets")
        image = self.ctx.get("image")
        done = 0
        items = assets["items"]
        if selected_episodes(req) and project.has("feed"):
            # 按集出片：只画所选集用到的角色/场景，已画过的会跳过
            needed = {r["asset"] for seg in selected_segments(project.load("feed"), req) for r in seg.get("refs", [])}
            items = [it for it in items if it["id"] in needed or it.get("file")]

        async def one(it: dict) -> None:
            nonlocal done
            out = project.path("images", f"{it['id']}.png")
            if not out.exists() or out.stat().st_size == 0:
                await image.generate(it["prompt"], out, aspect=it["aspect"])
            it["file"] = str(Path("images") / out.name)
            done += 1
            await self.progress(project, f"资产图 {done}/{len(items)}：{it['name']}", file=it["file"])

        await asyncio.gather(*(one(it) for it in items))
        assets["mode"] = "M2"
        project.save("assets", assets)
        if project.has("feed"):  # 出图后刷新导演稿里的接线单
            from .feed import director_markdown
            project.save_text("director.md", director_markdown(project.load("bible"), project.load("brief"),
                                                               assets, project.load("feed")))
        project.save(self.produces, {"count": sum(1 for it in assets["items"] if it.get("file"))})
        return assets
