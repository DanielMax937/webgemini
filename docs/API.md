# Web Gemini API

通过浏览器自动化与 **Gemini Web** 及 **X 上的 Grok**（`https://x.com/i/grok`）交互的 FastAPI 服务，支持文本对话、**Gemini Deep Research**（独立异步接口）、Veo3 视频生成、图片生成、音乐生成。

## 数据持久化

Chat 任务（job_id、prompt、text、gemini_url 等）持久化到 PostgreSQL。需配置环境变量并初始化：

```bash
export PGDATABASE=webgemini PGHOST=localhost PGUSER=caoxiaopeng
./scripts/init-db.sh
```

## 基础信息

- **默认端口**: 8200
- **Swagger 文档**: http://127.0.0.1:8200/docs
- **OpenAPI JSON**: http://127.0.0.1:8200/openapi.json
- **健康检查**: http://127.0.0.1:8200/health

## 端点

### POST /chat

提交聊天任务（异步）。

**Request body**:
```json
{
  "prompt": "string",
  "tool": "string (optional)",
  "attachments": ["local/file/path"]
}
```

**Response**: `{ "job_id": "xxx", "status": "queued" }`

### GET /chat/{job_id}

查询聊天任务状态。

**Response**: `{ "job_id", "status", "text", "images", "error", "gemini_url" }`

- `gemini_url`: 返回结果时当前 Gemini 页面 URL，便于追溯对话来源

### POST /deepresearch

提交 **Gemini Deep Research** 任务（异步），请求形态与 `POST /image` 一致：`multipart/form-data`。

- **`prompt`**（string，必填）：研究问题或指令。
- **`images`**（file[]，**可选**）：参考图。可省略或 0 张。若提供，最多 **5** 张；单张不超过 **10MB**；类型：`image/png`、`image/jpeg`、`image/gif`、`image/webp`。

**Response**: `{ "job_id": "xxx", "status": "queued" }`（与 `POST /chat` 相同字段名）。

**任务墙钟超时**：从任务获得并发槽并开始执行起，默认 **3600 秒**（1 小时），由环境变量 `WG_DEEP_RESEARCH_TASK_TIMEOUT_S` 控制（见下方「Deep Research 环境变量」）。

### GET /deepresearch/{job_id}

查询 Deep Research 任务状态，**响应 JSON 与** `GET /chat/{job_id}` **完全相同**：`job_id`、`status`、`text`、`images`、`error`、`gemini_url`。

- 轮询直至 `status` 为 `completed` 或 `failed`；`completed` 时 `text` 为最终抽取的正文（可能很长）。

#### Deep Research 自动化流程（摘要）

服务端在已登录的 Gemini Web 中自动完成大致顺序：

1. 选择 **Deep Research** 工具并发送 `prompt`（及可选参考图）。
2. 等待并点击 **开始研究** / `Start research` 等首次确认。
3. 在超时内轮询页面是否出现计划确认链接（`WG_DEEP_RESEARCH_LINK_MARKERS`）；若出现则点击二次执行确认；若超时未出现则记录告警并继续后续步骤。
4. 轮询直到主对话中出现 **Copy** 按钮（表示本轮回复已就绪），超时默认 **3600 秒**（`WG_DEEP_RESEARCH_MAX_POLL_S`）。
5. 在发送区非加载态时点击 **Share & Export**（或中文「分享 / 导出」），再在菜单中点击 **Copy contents**（或「复制内容」），通过系统剪贴板读取完整报告正文并写入任务的 `text`。
6. 若 Share/Export 未找到或 **Copy contents** 未点到，则回退为：点击助手消息的 **Copy** 按钮并读剪贴板，再不行则走 DOM 抽取（与普聊一致；受 `WG_USE_DOM_EXTRACTION` 影响）。

调试落盘（可选）：开启时会在 `outputs/deep_research_layout_logs/<job_id>/` 写入轮询阶段的 HTML 与 probe（由 `WG_DEEP_RESEARCH_BODY_LOG` 等控制）。服务默认日志级别为 **INFO**，详细步骤已降为 **DEBUG**，排障时可提高日志级别。

#### Deep Research 相关环境变量

与 `src/web_gemini/concurrency.py` 模块注释保持一致（节选）：

| 变量 | 含义（默认） |
|------|----------------|
| `WG_DEEP_RESEARCH_TASK_TIMEOUT_S` | 任务墙钟超时秒数（3600） |
| `WG_DEEP_RESEARCH_MAX_POLL_S` | 等待主对话 **Copy** 按钮出现的上限秒数（3600） |
| `WG_DEEP_RESEARCH_CONFIRM_TIMEOUT_S` / `WG_DEEP_RESEARCH_CONFIRM_POLL_S` | 首次「开始研究」确认（120s / 2s） |
| `WG_DEEP_RESEARCH_PLAN_LINK_TIMEOUT_S` / `WG_DEEP_RESEARCH_PLAN_LINK_POLL_S` | 等待计划确认链接（600s / 2s） |
| `WG_DEEP_RESEARCH_LINK_MARKERS` | 链接检测用逗号分隔子串 |
| `WG_DEEP_RESEARCH_EXEC_CONFIRM_TIMEOUT_S` / `WG_DEEP_RESEARCH_EXEC_CONFIRM_POLL_S` | 二次执行确认（120s / 2s） |
| `WG_DEEP_RESEARCH_EXPORT_CLICK` | 设为 `0`/`false` 跳过 Share & Export |
| `WG_DEEP_RESEARCH_EXPORT_WAIT_NOT_SPINNING_S` / `WG_DEEP_RESEARCH_EXPORT_SPIN_POLL_S` | 导出前等待发送区非加载（300s / 2s） |
| `WG_DEEP_RESEARCH_EXPORT_POST_CLICK_WAIT_S` | 点击 Share & Export 后等待秒数（2） |
| `WG_DEEP_RESEARCH_COPY_CONTENTS_CLICK` | 设为 `0`/`false` 跳过菜单内 **Copy contents** |
| `WG_DEEP_RESEARCH_COPY_CONTENTS_TIMEOUT_S` / `WG_DEEP_RESEARCH_COPY_CONTENTS_POLL_S` | 等待 **Copy contents** 菜单项（45s / 0.5s） |
| `WG_DEEP_RESEARCH_BODY_LOG` / `…_INTERVAL_S` / `…_MAX_BYTES` | 轮询阶段是否写 HTML、节流间隔、单文件最大字节 |
| `WG_USE_DOM_EXTRACTION` | `1`/`true` 时优先 DOM 抽取（普聊路径；Deep Research 在剪贴板路径失败时仍会尝试） |

### POST /grok/chat

向 **Grok（X / `https://x.com/i/grok`）** 提交聊天任务，异步执行，**请求体与** `POST /chat` **相同**。

**Request body**（与 `/chat` 一致）:
```json
{
  "prompt": "string",
  "tool": "string (optional, 当前 Grok Web 集成会忽略)",
  "attachments": ["local/file/path"]
}
```

- `attachments`：当前 Grok 集成**不执行上传**（会记日志）；需要附件时请用 `POST /chat`（Gemini）。

**Response**: `{ "job_id": "xxx", "status": "queued" }`

### GET /grok/chat/{job_id}

查询 Grok 聊天任务状态，**响应字段与** `GET /chat/{job_id}` **相同**。

- `gemini_url`：此处存放任务完成时浏览器中的 **X 页面 URL**（字段名与 Gemini 接口一致，便于客户端复用）。

#### Grok 自动化行为说明

- **前置**：Web Gemini 启动的 Chrome（CDP）需能打开 `https://x.com/i/grok`，且 **X 账号已登录**；否则无法对话。
- **提交消息**：Grok 输入框为多行，单独按 Enter 只会换行；服务端在 `grok.py` 中使用 **Ctrl+Enter（Control+Enter）** 提交 prompt，与 X Web 一致。
- **模式（Auto / Fast / Expert）**：当前 **HTTP API 不切换** 模式，沿用用户在页面里已选或默认项；需在 Grok 页手动选模式后再跑任务，或后续扩展自动化。

### POST /video

提交 Veo3 视频生成任务（`multipart/form-data`）。

- **`prompt`**（string，必填）：文本提示。
- **`images`**（file[]，**可选**）：参考图。可**省略该字段**，或上传 **0 张**（空数组语义）。若提供，最多 **5** 张；单张不超过 **10MB**；类型：`image/png`、`image/jpeg`、`image/gif`、`image/webp`。

**Response**: `{ "job_id": "...", "status": "queued" }`

### GET /video/{job_id}

查询视频任务状态。`video_url`、`local_path` 在完成后返回。

### POST /image

提交图片生成任务（`multipart/form-data`）。

- **`prompt`**（string，必填）：文本提示。
- **`images`**（file[]，**可选**）：参考图。可**省略该字段**，或上传 **0 张**。若提供，最多 **5** 张；单张不超过 **10MB**；类型：`image/png`、`image/jpeg`、`image/gif`、`image/webp`。无参考图时仅按 `prompt` 走「Create image」流程。

- **超时**：从任务**开始执行**（获得并发槽并启动浏览器流水线）起，默认 **5 分钟** 内须完成取图，否则任务标记为 `failed`。可通过环境变量 `WG_IMAGE_TASK_TIMEOUT_S`（秒）调整。

**Response**: `{ "job_id": "...", "status": "queued" }`

### GET /image/{job_id}

查询图片任务状态。

### POST /music

提交音乐生成任务（`multipart/form-data`）。

- **`prompt`**（string，必填）：文本提示。
- **`images`**（file[]，**可选**）：参考图。可**省略该字段**，或上传 **0 张**。若提供，最多 **5** 张；单张不超过 **10MB**；类型：`image/png`、`image/jpeg`、`image/gif`、`image/webp`。生成完成后通过 Gemini 的 "Download track" 按钮下载音频到本地。

**Response**: `{ "job_id": "...", "status": "queued" }`

### GET /music/{job_id}

查询音乐任务状态。`audio_url`、`local_path` 在完成后返回。`local_path` 为本地保存的音频文件路径。

### GET /metrics

返回并发槽占用与队列长度，便于监控。

**Response** 示例：`{ "active_slots": 0, "queued_tasks": 0, "max_concurrent": 10 }`

### GET /health

健康检查。`{ "status": "ok" }`
