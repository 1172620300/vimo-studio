"""MockLLM 的固定响应：按 system prompt 的特征识别是哪个阶段。"""
from __future__ import annotations

import json
import re


def respond(system: str, user: str) -> dict:
    if "剧本拆解" in system:
        m = re.search(r"分集列表：(\[.*?\])\n", user)
        eps = json.loads(m.group(1)) if m else [{"no": 1, "title": ""}]
        bible = respond("开发圣经 总编剧", f"恰好 {len(eps)} 项")
        bible["episodes"] = [{"no": e["no"], "title": e["title"] or f"第{e['no']}集", "hook": "", "beats": [],
                              "cliffhanger": ""} for e in eps]
        return bible
    if "剧本结构化" in system or "小说改编" in system:
        return respond("正式剧本编剧", user)
    if "开发圣经" in system and "总编剧" in system:
        n = int(re.search(r"恰好 (\d+) 项", user).group(1)) if re.search(r"恰好 (\d+) 项", user) else 1
        return {
            "title": "逆光归来", "logline": "被赶出家门的真千金三年后携神秘身份归来。", "genre": "都市逆袭",
            "tone": "爽感", "audience": "女频", "selling_points": ["打脸", "身份反转"],
            "style": {"name": "真人写实电影感", "prefix": "冷青灰低饱和 · 单侧硬光 · 35mm 浅景深", "reason": "都市题材"},
            "characters": [
                {"id": "C1", "name": "林晚", "role": "主角", "gender": "女", "age": "24岁", "appearance": "长发及腰，杏眼",
                 "costume": "黑色西装套裙", "personality": "冷静", "voice": "清冷"},
                {"id": "C2", "name": "林薇", "role": "反派", "gender": "女", "age": "23岁", "appearance": "大波浪卷发",
                 "costume": "粉色礼服", "personality": "骄纵", "voice": "尖细"},
            ],
            "scenes": [{"id": "S1", "name": "林家宴会厅", "description": "水晶吊灯，长桌", "time_of_day": "夜", "lighting": "暖金色"}],
            "props": [],
            "episodes": [{"no": i + 1, "title": f"第{i + 1}集", "hook": "宴会上被泼酒", "beats": ["受辱", "反击"],
                          "cliffhanger": "神秘人到场"} for i in range(n)],
        }
    if "正式剧本编剧" in system:
        return {"no": 1, "title": "归来", "scenes": [{"scene_id": "S1", "characters": ["C1", "C2"], "blocks": [
            {"type": "action", "text": "林薇端起红酒泼向林晚。"},
            {"type": "dialogue", "speaker": "C2", "emotion": "讥讽", "text": "你也配回林家？"},
            {"type": "dialogue", "speaker": "C1", "emotion": "冷笑", "text": "这杯酒，我记下了。"},
            {"type": "action", "text": "大门推开，一排黑衣保镖鱼贯而入。"},
        ]}]}
    if "五席圆桌" in system:
        return {"scores": {"运营": 17, "编剧": 16, "台词": 16, "导演": 16, "合规": 19}, "total": 84,
                "notes": ["开场可以更狠"], "revised_script": None}
    if "分镜导演" in system:
        return {"no": 1, "segments": [
            {"id": "x", "duration": 6, "scene_id": "S1", "characters": ["C1", "C2"],
             "blocking": "C1 立于画面左侧面向右，C2 站右侧", "sfx": "宴会嘈杂声，泼酒声", "transition_out": "硬切",
             "shots": [
                 {"t": "0-3s", "size": "中景", "camera": "缓推", "action": "C2 泼酒",
                  "dialogue": [{"speaker": "C2", "emotion": "讥讽", "text": "你也配回林家？"}]},
                 {"t": "3-6s", "size": "近景", "camera": "固定", "action": "C1 擦去酒渍",
                  "dialogue": [{"speaker": "C1", "emotion": "冷笑", "text": "这杯酒，我记下了。"}]}]},
            {"id": "y", "duration": 5, "scene_id": "S1", "characters": ["C1"],
             "blocking": "C1 居中，背后大门", "sfx": "推门声", "transition_out": "硬切",
             "shots": [{"t": "0-5s", "size": "全景", "camera": "缓拉", "action": "保镖入场", "dialogue": []}]},
        ]}
    return {}
