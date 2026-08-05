# 视频生成任务与轮询维护指南

本文只保留当前维护视频链路所需的约束和验证入口。完整接口示例、旧行为说明、供应商接入记录及排障细节见 [`docs/archive/VIDEO_GENERATION_POLLING_CHANGES_FULL.md`](docs/archive/VIDEO_GENERATION_POLLING_CHANGES_FULL.md)；归档用于追溯，不替代当前代码。

## 当前架构

- `POST /api/canvas-video` 仅保留同步兼容；普通画布和智能画布的主流程使用：
  - `POST /api/canvas-video-tasks`
  - `GET /api/canvas-video-tasks/{task_id}`
- 本地任务 ID 与上游任务 ID 必须分离。本地任务持久化后可在刷新或后端重启后恢复查询。
- 已取得上游任务 ID 的任务可以恢复轮询；没有上游任务 ID 的中断任务不得自动重提，避免重复扣费。
- 前端保存 pending 后轮询本地任务；后端负责上游轮询、`retry_after`、退避、临时错误重试和终态判定。
- 余额、额度、账单、支付和明确的上游失败属于终态，必须结束 pending 并展示错误。
- `video_request_mode` 继续兼容 `/v1/videos/generations` 与 `/v1/video/generations`。独立供应商协议由 `plugins/video_plugins/` 实现，不应重新内联到 `main.py`。
- 普通画布与智能画布是两条平行链路，任何任务状态、恢复或错误处理改动都必须同时核对。

## 主要位置

| 范围 | 文件 |
| --- | --- |
| 任务 API、持久化、恢复与错误转换 | `main.py` |
| 普通画布提交、pending、轮询与完成处理 | `static/js/canvas.js` |
| 智能画布提交、恢复、轮询与终态处理 | `static/js/smart-canvas.js` |
| 接口模式设置 | `static/api-settings.html`、`static/js/api-settings.js` |
| 供应商协议 | `plugins/video_plugins/` |
| 旧核心补丁重放 | `tools/patches/video_request_mode_patch.py`、`重放视频接口补丁.bat` |

实现细节以当前代码、插件目录的 `README.md`/`DESIGN.md` 及测试为准。

## 修改检查清单

1. 确认 provider 模式归一化、提交 URL、查询 URL 和下载 URL 使用同一协议规则。
2. 确认创建类 POST 不会因结果未知而自动重放；仅查询请求可按既有策略重试。
3. 确认任务快照不包含秘密或不受控的大响应，并保持既有限量与节流。
4. 确认刷新、停止、404、临时异常、终态失败和完成状态在两条画布链路上一致。
5. 确认重启恢复不会重新提交没有上游任务 ID 的任务。
6. 新供应商优先新增/修改 `plugins/video_plugins/`，并补充协议 URL、请求体、状态与下载测试。

## 验证

按变更范围至少运行直接相关用例：

```powershell
.\venv\Scripts\python.exe -m py_compile main.py
.\venv\Scripts\python.exe -m unittest tests.test_video_plugin_urls
.\venv\Scripts\python.exe -m unittest tests.test_video_material_preflight
.\venv\Scripts\python.exe -m unittest tests.test_video_http_request_logging
node tests\test_video_api_utils.js
node tests\test_canvas_video_lifecycle.js
node tests\test_canvas_video_terminal_state.js
node tests\test_smart_canvas_video_lifecycle.js
node tests\test_smart_canvas_video_terminal_state.js
node tests\test_smart_canvas_live_pending_node.js
```

涉及 provider 配置、前端恢复或跨链路行为时，再运行全部 Python/Node 测试并做最小手工冒烟验证。

## 何时读取归档

仅在以下情况读取完整归档：

- 需要还原某个供应商的历史协议约定。
- 排查旧版本任务记录或补丁脚本重放问题。
- 比较 2026-07-28 容错修复前后的行为。
- 当前代码和测试不足以解释既有兼容分支。
