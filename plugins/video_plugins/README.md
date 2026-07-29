# 视频协议插件

`plugins/video_plugins/` 存放与通用 OpenAI 视频接口不兼容的供应商协议适配。每个供应商一个模块，由 `__init__.py` 静态导出，并在 `main.py` 显式注册。

## 模块

| 模块 | `video_request_mode` | 主要接口 |
| --- | --- | --- |
| `aicost.py` | `aicost-video` | `/v1/videos` |
| `geeknow.py` | `geeknow-v1-videos` | `/v1/videos` |
| `megabyai.py` | `megabyai-v1-videos` | `/v1/videos` |
| `meai.py` | `meai-v1-videos` | `/v1/videos` |
| `sudashui.py` | `sudashui-video-generations` | `/v1/video/generations` |
| `tudou.py` | `tudou-video` | 按模型使用 `/v1/videos` 或 `/v1/videos/generations` |
| `common.py` | — | Base URL、下载 URL、公网请求与脱敏日志工具 |

## 职责边界

插件负责：

- 校验供应商协议参数并构造请求。
- 提交任务、解析状态、执行安全的状态轮询。
- 解析结果 URL 和供应商业务错误。
- 通过进度回调报告上游任务 ID、请求日志和最新状态。

插件不得：

- 读取 API Key 或全局 provider 配置。
- 自行解析任意本地路径或扩大用户数据目录权限。
- 直接写入输出目录。
- 在创建请求结果不确定时自动重发，避免重复扣费。

宿主通过回调提供受控素材公网化、本地路径解析、内容类型识别和视频落盘能力。服务重启恢复只允许查询已持久化的上游任务 ID，不得重新创建任务。

详细安全边界见 [DESIGN.md](DESIGN.md)，视频任务主链路见根目录 [VIDEO_GENERATION_POLLING_CHANGES.md](../../VIDEO_GENERATION_POLLING_CHANGES.md)。

## 新增插件检查清单

1. 在模块内定义独立的 `*_VIDEO_REQUEST_MODE`、协议异常、创建和恢复函数。
2. 在 `__init__.py` 导出，并在 `main.py` 注册模式、官方 hostname、提交、恢复和轮询间隔。
3. 在 `static/js/video-api-utils.js` 补齐双画布共用的参数和素材约束。
4. 在 API 设置页及 i18n 增加协议选项。
5. 更新 `tests/test_video_plugin_urls.py`、`tests/test_video_api_utils.js` 和长期维护文档。
6. 验证 Base URL 的根域、尾斜杠、尾部 `/v1`、重复 `/v1` 都只生成一份版本路径。
