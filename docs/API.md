# Web Gemini API

通过浏览器自动化与 Gemini Web 交互的 FastAPI 服务，支持文本对话、Veo3 视频生成、图片生成。

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

**Response**: `{ "job_id": "xxx", "status": "pending" }`

### GET /chat/{job_id}

查询聊天任务状态。

**Response**: `{ "job_id", "status", "text", "images", "error", "gemini_url" }`

- `gemini_url`: 返回结果时当前 Gemini 页面 URL，便于追溯对话来源

### POST /video

提交 Veo3 视频生成任务（Form: prompt + images）。

**Response**: `{ "job_id", "status" }`

### GET /video/{job_id}

查询视频任务状态。`video_url`、`local_path` 在完成后返回。

### POST /image

提交图片生成任务（Form: prompt + images）。

**Response**: `{ "job_id", "status" }`

### GET /image/{job_id}

查询图片任务状态。

### GET /health

健康检查。`{ "status": "ok" }`
