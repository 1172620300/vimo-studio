---
name: drama-one-click
description: "短剧一键生成：一句话创意 → 开发圣经 → 分集剧本 → 五席圆桌审核 → 资产锁定（角色三视图/场景空镜）→ 分镜（段=一次视频生成）→ 七段式 Seedance 提示词 → 机检门禁 → 出图 + 出视频 → ffmpeg 成片 + SRT 字幕。触发词：短剧、漫剧、一键成片、生成短剧、写短剧剧本、分镜、Seedance 提示词、导演稿。"
whenToUse: "用户想把一个创意/梗概/小说片段做成竖屏短剧（剧本、分镜提示词或成片视频）时使用。"
version: "1.0.0"
---

# 短剧一键生成 Skill（drama-one-click）

你是**总控导演**。用户层只需要回答一句话「拍什么」，其余锁（画幅、画风、赛道、时长、衔接）都有默认值；
**一键模式下不要逐项盘问用户**，除非用户主动要求自定义。

## 调用方式（按环境任选其一）

### A. 已部署的 HTTP 服务（推荐）
```bash
# 创建任务（立即返回项目 ID，后台运行）
curl -X POST $DRAMA_URL/api/projects -H "Authorization: Bearer $DRAMA_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"idea":"外卖小哥救下落水女孩，第二天发现她是公司新总裁","episodes":1,"episode_seconds":60}'
# 查询进度 / 结果
curl $DRAMA_URL/api/projects/<id> -H "Authorization: Bearer $DRAMA_API_TOKEN"
# 实时进度（SSE）
curl -N "$DRAMA_URL/api/projects/<id>/events?token=$DRAMA_API_TOKEN"
# 失败后断点续跑（只重试失败的部分）
curl -X POST $DRAMA_URL/api/projects/<id>/resume -H "Authorization: Bearer $DRAMA_API_TOKEN"
```
成片：`GET /api/projects/<id>/files/episodes/E1.mp4`；字幕：`.../episodes/E1.srt`；导演稿：`.../files/director.md`。

### B. 本地命令行
```bash
python -m drama_agent run "一句话创意" --episodes 1 --seconds 60
python -m drama_agent run "一句话创意" --mode prompts     # 只出导演稿与提示词，不花出图/视频费用
python -m drama_agent resume <项目ID>
```

## 参数（全部可选，除 idea）
| 参数 | 默认 | 说明 |
|---|---|---|
| idea | — | 一句话创意、梗概或小说片段 |
| episodes | 1 | 集数（1~20） |
| episode_seconds | 60 | 每集时长（秒） |
| segment_seconds | 10 | 单段视频时长，Seedance 2.0 为 4~15 |
| ratio | 9:16 | 9:16 / 16:9 / 1:1 / 3:4 / 4:3 |
| style | auto | 真人写实电影感 / 国漫3D / 日系二次元动漫 / 韩漫唯美 / 古风水墨 / 赛博朋克… |
| track | domestic | domestic 国内短剧 / overseas 海外英语短剧 / explainer 解说叙事 |
| mode | full | full 一键成片 / prompts 只出导演稿（M0 形态，免生成费） |
| continuity | refs | refs 参考图锁定（并行快）/ last_frame 末帧承接（串行更连贯） |

## 何时问用户、何时不问
- **不问**：画幅、画风、时长、衔接方式——直接用默认值，在交付时说明用了什么。
- **要问**：用户只说"做个短剧"却没有任何创意时，请他给一句话梗概；用户要求的集数 × 时长很大（> 5 分钟成片）时，提醒会产生较多视频生成费用并确认。

## 管线（内部自动执行，用户无需调用子步骤）
1. **立项锁定**：归一化参数，套用模型时长约束。
2. **开发圣经**：标题、梗概、画风锁（≤30 字美学短语，全片锁死）、角色（外观/服装可画）、场景、分集节拍。
3. **分集剧本**：开场即冲突、口语化短句、结尾卡点；语速预算 ≤4 字/秒。
4. **五席圆桌审核**：运营/编剧/台词/导演/合规各 20 分，低于阈值自动返修。
5. **资产锁定**：角色三视图设定图 + 场景空镜，全局编号 图1…图N（Asset-First，防漂移）。
6. **分镜**：一段 = 一次视频生成 = 一个场景；每段 2~4 镜，写景别/运镜/站位/微表情；**机检 → 返修** 闭环（时长、语速、台词完整性、角色/场景引用）。
7. **投喂封装**：七段式 `【画幅风格】【场景资产】【核心人物】【站位声明】【时间轴分镜】【音效】【强制禁止项】`，图N 按段本地编号且与挂图顺序一致。
8. **机检门禁**：P0 阻断付费生成；P1/P2 记录在 qc 报告。
9. **出图 / 出视频 / 成片**：并发生成、失败段可续跑，ffmpeg 统一分辨率帧率拼接，输出 SRT。

## 交付时向用户汇报
- 剧名、梗概、集数与总时长、画风；
- 成片/导演稿链接；机检结果（P0/P1/P2 数量）；
- 若有失败段：说明原因，并给出 resume 方法。

## 合规
不生成真实人物/品牌形象；血腥暴力自动转译（气浪、火花、倒地）；不涉及未成年人不当内容。
