# Drama Agent · 短剧一键生成

一句话创意 → 剧本 → 资产 → 分镜 → Seedance 视频 → 成片（MP4 + SRT）。
提供 **HTTP API + 网页 + 命令行 + Agent Skill（`SKILL.md`）** 四种入口，可直接部署到服务器。

## 架构

参考了两个项目：

- **[deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)** 的工程架构：
  - **一切皆插件**：LLM / 图像 / 视频 是可替换的「能力接缝」，每个流水线阶段也是插件（`ctx.provide("stage:xxx")`）。
  - **事件总线**：`stage/start`、`stage/progress`、`pipeline/end` 等事件；日志、Webhook、SSE 都只是订阅者。
  - **追加式日志**：每个项目一份 `events.jsonl` + `artifacts/*.json`（唯一真相），可**断点续跑**。
  - **Profile 分层配置**：`default → 子 profile（extends）→ --patch → .env`，可以按插件 id 替换（`replace` / `disable` / `plugin_config`）。
- **[manju-laoli-skill](https://github.com/lixiaoxiao9888-create/manju-laoli-skill)**（MIT）的短剧方法论，精简后用在各阶段：
  总控 + 分层引擎、Asset-First 资产锁定、五席圆桌审核、语速与台词完整性闸门、七段式投喂格式、图N 编号接线单、合规转译。

```
                ┌──────────── Context（服务注册 + 事件总线）────────────┐
 profile.yaml → │  llm      image      video     ← 可替换的能力接缝         │
                │  stage:intake → bible → script → review → assets       │
                │  → storyboard ⇄ qc返修 → feed（七段式）→ qc门禁         │
                │  → asset_images → render → assemble     ← 付费阶段      │
                │  观察者：console-log / webhook / SSE                     │
                └──────────────────────────────────────────────────────────┘
                      项目目录：events.jsonl + artifacts/ + images/ + segments/ + episodes/
```

| 阶段 | 对应 manju 引擎 | 产物 |
|---|---|---|
| intake 立项锁定 | 总控〇节六道锁（一键模式全有默认值） | `brief` |
| bible 开发圣经 | sd-script-engine（来源登记/IP/开发圣经） | `bible` |
| script 分集剧本 | sd-script-engine（分集/正式剧本） | `scripts` |
| review 圆桌审核 | 五席圆桌 + 反捧杀，低分自动返修 | `review` |
| assets 资产锁定 | sd-asset-engine（三清单、图N 编号） | `assets` |
| storyboard 分镜 | sd-storyboard / sd-decision，**机检→返修闭环** | `storyboards` |
| feed 投喂封装 | sd-feed-engine（七段式、接线单） | `feed`、`director.md` |
| qc 机检门禁 | sd-qc-engine（P0 阻断付费生成） | `qc` |
| asset_images / render / assemble | 出图 / Seedance 出视频 / ffmpeg 成片 | `images/` `segments/` `episodes/` |

## 快速开始（本地）

```bash
pip install -r requirements.txt
cp .env.example .env        # 填写密钥（已有 .env 可跳过）
python -m drama_agent doctor                     # 检查配置与接口连通性
python -m drama_agent --profile mock run "测试"   # 零成本跑通整条流水线
python -m drama_agent smoke                      # 真实接口出 1 张图 + 1 段 4 秒视频（少量费用）
python -m drama_agent run "外卖小哥救下落水女孩，第二天发现她是公司新总裁" --episodes 1 --seconds 60
python -m drama_agent serve --port 8000          # 打开 http://127.0.0.1:8000
```

### 上传剧本出片

网页选「上传剧本」，拖入 `.txt / .md / .docx`（GBK 编码也可以），或者直接粘贴剧本文本。系统会根据剧本的写法自动识别分集，并按台词量估算每集时长。支持的格式（按优先级）：集标题（`第1集` `【第一集】` `第3话` `EP01` `集数：1`，可以从第 N 集开始）、场次编号（`1-1 夜 内 宴会厅`，同一集号的场次归为一集）、独占一行的数字序号；章节标题（`第一章：…`，小说体）；目录行会自动忽略。填写「集数」时，会把识别出的集/章按篇幅均衡地合并成指定集数（例如 16 章 → 10 集，章节保持连续、不拆散）；集数多于识别出的单元时，在段落处对半拆分；都识别不到时整部按 1 集处理。网页上会显示「按哪种格式识别出几集」。之后的流程：

1. **剧本拆解**：LLM 从剧本里提炼角色、场景、画风，形成开发圣经。
2. **剧本结构化**：逐集转成标准 JSON，**原台词一字不改**；圆桌审核默认只评分、不改写。
3. 资产 → 分镜 → 提示词 → 机检，与创意模式共用同一套流程。
4. 交付方式默认是「先出分镜，确认后生成视频」：免费阶段跑完后，在网页点「确认并生成视频」，才会调用付费的出图和视频接口。

**小说转短剧改编**：识别为「第N章」小说时默认开启（网页「改编方式」可手动切换）。每集默认 60 秒，可在「每集时长」修改。改编方法论来自 [short-drama-factory](https://github.com/lixiaoxiao9888-create/short-drama-factory)（MIT，作者：老李），内置在 `drama_agent/vendor/short_drama_factory/`：
- 网文改编流水线、黄金前 3 秒六母型钩子、五大断章公式、台词去 AI 味规则，原文注入改编提示词；
- 按漫剧档（60～90 秒）规格写：1 集 1 个关系质变节点，场景 ≤2，对白行 ≥4，神态短语用「画面词｜语气词」，独白和旁白有配额；
- 每集改编完先渲染成该 skill 的单集标准排版，再用它的 `validate_episode.py`（manju 档）机检，FAIL 或超时长会自动返修，最多 2 轮。结果写入 `episodes_script/E{n}.md`，网页「概览」里能看到每集机检结果。

**按集出视频**：分镜和提示词就绪后，可以只生成某几集，比如先出第 1 集看效果，满意再继续。网页「概览」里每集都有「生成本集视频」按钮；API 为 `POST /api/projects/{id}/render`，传 `{"episodes":[1]}`，不传表示全部剩余集，可以多次调用。只为所选集出图和生成视频，已生成的图和视频段会跳过，已出片的集会保留。

命令行：`python -m drama_agent run --script 剧本.docx --mode prompts`
API：`POST /api/script/analyze`（`{"filename","data":base64}` 或 `{"text"}`，返回文本与分集识别结果）；`POST /api/projects` 传 `script_text`；`POST /api/projects/{id}/resume` 传 `{"mode":"full"}` 即可从分镜阶段切到出视频。

Profile：`default`（真实 LLM + 真实出图/视频）、`llm-only`（真实 LLM，图像/视频为 mock）、`mock`（全部 mock）。

常用参数：`--mode prompts` 只出导演稿与提示词（不花生成费）；`--continuity last_frame` 用上一段末帧承接下一段（更连贯，但段间串行）；`--style 国漫3D`；`--track overseas` 海外英语剧。

失败后续跑：`python -m drama_agent resume <项目ID>`，已完成的阶段和已生成的视频段会跳过；`--force storyboard` 可强制重跑某阶段。

## 部署到服务器

### Docker（推荐）
```bash
scp -r drama_agent/ user@server:/opt/        # 连同 .env 一起
cd /opt/drama_agent
# .env 里务必设置 DRAMA_API_TOKEN=<长随机串>
docker compose up -d --build
```
服务只监听 `127.0.0.1:8000`，用 `deploy/nginx.conf` 反代并配 HTTPS（SSE 已关闭缓冲）。

### 不用 Docker
```bash
sudo apt install ffmpeg python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/drama-agent.service /etc/systemd/system/ && sudo systemctl enable --now drama-agent
```

## HTTP API

所有接口需要 `Authorization: Bearer $DRAMA_API_TOKEN`（或 `?token=`，用于浏览器视频/SSE）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 创建任务：`{"idea": "...", "episodes": 1, "episode_seconds": 60, "ratio": "9:16", "style": "auto", "mode": "full"}` |
| GET | `/api/projects` | 项目列表 |
| GET | `/api/projects/{id}` | 状态、进度、圣经、机检、成片 |
| GET | `/api/projects/{id}/events` | SSE 实时事件 |
| POST | `/api/projects/{id}/resume` | 断点续跑 |
| POST | `/api/projects/{id}/cancel` | 取消 |
| GET | `/api/projects/{id}/artifacts/{name}` | 阶段产物 JSON（bible / scripts / storyboards / feed / qc …） |
| GET | `/api/projects/{id}/files/{path}` | 文件：`episodes/E1.mp4`、`episodes/E1.srt`、`director.md` |

## 扩展

新增或替换任何部分都只需写一个插件并在 profile 里登记，不改内核：

```python
# my_plugins.py
from drama_agent.kernel.context import Plugin
class FeishuNotify(Plugin):
    name = "feishu"
    def apply(self, ctx, config):
        async def done(project, evt): ...   # 推送飞书
        ctx.on("pipeline/end", done)
```
```yaml
# config/profiles/prod.yaml
extends: default
plugins_append:
  - use: my_plugins:FeishuNotify
replace:
  .providers.media:CangyuanMediaPlugin: {use: my_media:KlingPlugin, config: {...}}
plugin_config:
  .stages.storyboard:StoryboardStage: {max_repair_rounds: 3}
```

视频网关字段名不同时（如参考图字段叫 `reference_images`），在 `default.yaml` 的 `video.field_map` / `video.extra_body` 里改，无需改代码。

## 目录

```
drama_agent/
  kernel/      config.py（分层配置）context.py（插件/服务/事件）project.py（事件日志与产物）
  providers/   llm.py（OpenAI 兼容）media.py（苍源 图像/视频）mock.py
  stages/      writing.py assets.py storyboard.py feed.py qc.py render.py
  prompts/     bible.md script.md review.md storyboard.md（规则库）
  plugins/     logging.py webhook.py
  server/      app.py（FastAPI）
  cli.py doctor.py pipeline.py
config/profiles/  default.yaml mock.yaml llm-only.yaml
web/index.html    网页
deploy/           nginx.conf drama-agent.service
SKILL.md          Agent 技能入口
```

## 致谢

短剧方法论参考 [漫剧老李 AIGC 创作全流程 Skill](https://github.com/lixiaoxiao9888-create/manju-laoli-skill)（MIT License）；
插件化架构参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（MIT License）。
