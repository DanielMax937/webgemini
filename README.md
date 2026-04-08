# web-gemini

FastAPI 服务：通过浏览器自动化操作 **Gemini Web**（`gemini.google.com`）与 **X 上的 Grok**（`https://x.com/i/grok`），支持异步文本对话、Veo3 视频、图片与音乐生成等。

## API 文档

- 仓库内说明：[docs/API.md](docs/API.md)
- 运行后 Swagger：http://127.0.0.1:8200/docs  
- 健康检查：http://127.0.0.1:8200/health

## 启动

```bash
./start-bg.sh   # 启动 Chrome（CDP）+ uvicorn
./stop-bg.sh    # 停止
```

默认端口 **8200**，日志见 `webgemini.log`。

## 主要端点摘要

| 能力 | 端点 |
|------|------|
| Gemini 对话 | `POST /chat`，`GET /chat/{job_id}` |
| Grok（X）对话 | `POST /grok/chat`，`GET /grok/chat/{job_id}` |
| Veo3 视频 / 图片 / 音乐 | `/video`、`/image`、`/music` 及对应 `GET .../{job_id}` |

Grok 与 Gemini 共用 `ChatRequest` 形状；Grok 侧会忽略 `tool`，且不处理附件上传（详见 [docs/API.md](docs/API.md)）。
