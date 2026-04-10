# Web Gemini API

通过浏览器自动化与 **Gemini Web** 及 **X 上的 Grok**（`https://x.com/i/grok`）交互的 FastAPI 服务，支持文本对话、Veo3 视频生成、图片生成、音乐生成。

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

### GET /health

健康检查。`{ "status": "ok" }`
