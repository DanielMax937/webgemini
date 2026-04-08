# Web Gemini — 接口验证与已知问题

> 记录日期：2026-03-26（与当时会话中的联调结论一致）。

## 对外 HTTP 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/chat` | 提交聊天任务（JSON：`prompt`、`tool`、可选 `attachments`） |
| `GET` | `/chat/{job_id}` | 查询聊天任务状态 |
| `POST` | `/video` | 提交视频任务（`multipart/form-data`：`prompt` + 至少一张参考图） |
| `GET` | `/video/{job_id}` | 查询视频任务状态 |
| `POST` | `/image` | 提交图片任务（`prompt` + 至少一张参考图） |
| `GET` | `/image/{job_id}` | 查询图片任务状态 |
| `POST` | `/music` | 提交音乐任务（`prompt`，可选参考图） |
| `GET` | `/music/{job_id}` | 查询音乐任务状态 |

实现见：`src/web_gemini/main.py`。

## 验证结论（当时）

- **HTTP 层**：服务可正常响应；`/health` 等路由可访问；任务提交与轮询接口形态正常。
- **Chrome DevTools（CDP）**：本机 `http://127.0.0.1:9222/json/version` 可访问，说明 **9222 端口与 CDP 绑定** 无问题（与「端口未起」类故障排除）。
- **端到端业务（chat / image / music / video）**：在 Playwright 驱动 Gemini 网页时，出现 **失败或超时**，问题主要归因于 **页面自动化**（选择器、等待、UI 变化），而非「HTTP 服务本身挂了」。

## 碰到的问题（现象与推断）

1. **Playwright 超时 / 选择器失败**  
   - 例如：难以稳定定位 **Tools**、**Gemini 输入框** 等控件。  
   - 推断：Google Gemini 网页 **改版、A/B、语言/区域** 导致 DOM 结构或文案变化，与代码里写死的选择器或流程假设不一致。

2. **登录与会话**  
   - 自动化依赖持久化 Chrome profile（见 `src/web_gemini/chrome_automation/paths.py` → `chrome_profile_dir()`，默认在仓库下 `chrome_data/chrome-profile`）。  
   - 若未登录、会话过期或地区限制，页面行为与「已登录可用」不一致，会表现为超时或找不到元素。

3. **区分问题层级**  
   - **API 进程**：若 `/health` 正常、任务能 `pending`/`running`，但结果 `error` 或长时间卡住，优先查 **浏览器侧日志与 Playwright 步骤**。  
   - **CDP**：`curl` 9222 正常则排除「Chrome 未起」。

## 建议后续工作

- 在失败路径上增加 **更明确的日志**（当前步骤名、截图或 DOM 快照），便于对照线上 UI。  
- 定期 **回归** 与 Gemini 页面 DOM 对齐（选择器、iframe、aria、文案）。  
- 文档化 **最小前置条件**：已登录账号、网络/地区、Chrome 由 `start-bg.sh` 等脚本拉起。

## 调试相关（备忘）

- **CDP 端口**：`9222`（`paths.CDP_PORT` / `CDP_URL`）。  
- **Chrome user-data**：`chrome_data/chrome-profile`（相对 webgemini 仓库根目录），与 `--remote-debugging-port=9222` 搭配使用；历史环境若曾迁移，旧路径可能为 `~/.claude/skills/chrome-automation/chrome-profile`，以当前 `paths.py` 为准。
