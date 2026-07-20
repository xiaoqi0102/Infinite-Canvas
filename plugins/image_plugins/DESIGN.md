# 图片接口插件设计

## 设计目标

- 用独立模块承载 aicost 图片协议，减少 `main.py` 中的供应商细节。
- 同时覆盖 GPT Image 和 Gemini 图片模型的文生图、参考图和异步任务。
- 绕过环境代理，解决长请求被本机代理提前断开的问题。
- 在可能已经计费的情况下停止自动重试，并保留上游任务 ID。
- 保持返回格式与 Infinite Canvas 现有图片保存链路兼容。

## 非目标

- 不实现运行时插件发现或动态加载。
- 不改变其他图片供应商的代理、超时或重试策略。
- 不负责本地任务持久化、历史记录、文件保存和 UI。
- 不支持接口文档之外的 aicost 图片模型或任意 Gemini 尺寸。

## 架构

```text
main.py
  │  请求、鉴权头、路径解析回调
  ▼
plugins.image_plugins
  ▼
aicost.py
  ├─ 请求校验与模型分发
  ├─ GPT generations / edits
  ├─ Gemini generateContent
  ├─ 同步响应归一化
  └─ 异步任务查询与轮询
        │
        ▼
  httpx.AsyncClient(trust_env=False)
```

插件不反向导入宿主。路径解析和 MIME 判断通过回调注入，自定义异常由宿主转换为
FastAPI `HTTPException`。

## 方案选择

### 采用静态插件包

沿用 `plugins.video_plugins` 的静态聚合导出模式，而不是增加 `importlib` 扫描。
静态导入可被 PyInstaller 自动分析，无需维护 `hiddenimports`，也避免插件加载失败延迟到运行时。

### 插件拥有 HTTP 客户端

若宿主创建客户端，容易再次继承 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量。
插件内部固定 `trust_env=False`，把代理隔离作为协议适配的一部分；该设置不影响其他平台。

### 创建请求不重试

POST 断连或 5xx 不能证明上游没有执行任务。自动重试可能生成两张图片并重复扣费，
因此直接返回带语义的错误。唯一例外是 `/images/edits` 明确以 400、415 或 422
拒绝 `image[]` 字段时，改用 `image` 字段提交一次；响应必须同时包含图片字段拒绝特征。

### 轮询与创建分离

`generate_aicost_image` 可提交并等待；`query_aicost_image_task` 只查询已有任务，绝不创建。
任务 ID 在 URL 中使用 `urllib.parse.quote(..., safe="")` 编码，并附着在轮询错误上，
供宿主保存和恢复查询。

## 关键决策

| 决策 | 理由 | 影响 |
| --- | --- | --- |
| 模式名固定为 `aicost-image` | 与通用 OpenAI 模式明确区分 | 后端和设置页面需识别该值 |
| 官方域名精确匹配 | 防止 `aicost.xyz.evil.test` 被误识别 | 自定义镜像需手动选择模式 |
| GPT 请求固定 `n=1`、`quality=auto`、`moderation=auto` | 遵循 aicost 文档 | 多图由宿主并发控制 |
| Gemini 尺寸采用文档映射 | 原生接口需要 `imageSize` 与 `aspectRatio` | 非标准尺寸会返回 400 |
| 返回规范化图片加原始响应 | 兼容统一保存并保留 usage/task 元数据 | 插件不写磁盘 |
| 查询成功但无图片立即失败 | 避免已完成任务永久停留在 running | 错误携带任务 ID 便于排查 |

## 安全模型

### 威胁模型

- 恶意 Base URL 伪装为 aicost 官方域名。
- 非法本地路径导致读取用户未授权文件。
- 超大 base64 或图片文件造成内存压力。
- 手工设置 multipart `Content-Type` 导致 boundary 错误。
- API Key 被写入日志或异常文本。
- 未编码任务 ID 改变轮询 URL 路径。

### 安全边界与措施

- 自动识别仅比较 URL 解析后的完整 hostname；不使用子串匹配。
- 插件只打开 `resolve_local_path` 回调返回且确实存在的文件。
- 单张参考图限制为 30 MB，数量限制为 20，并校验 `image/*` MIME。
- multipart 请求移除宿主的 `Content-Type`，由 `httpx` 生成 boundary。
- 插件不记录请求头、API Key、图片二进制或 base64 内容。
- 任务 ID 进行完整路径段编码。
- 上游请求使用显式超时，并固定 `trust_env=False`。

信任边界：宿主负责鉴权头的来源和本地路径回调的目录约束；插件信任这两个注入值，
但不信任请求中的模型、尺寸、参考图 URL、任务 ID 或上游响应。

### 已知风险

- 宿主若传入不安全的路径解析回调，插件无法独立判断文件是否位于用户数据目录。
- 同步 POST 在响应丢失后可能已计费；由于上游未提供幂等键，插件只能停止重试并提示风险。
- 轮询 GET 的瞬时传输错误会继续等待到轮询超时，不会无限重试。

## 已知限制

- 仅支持文档列出的 `gpt-image-2` 和两个 Gemini 图片预览模型。
- 参考图必须是 data URL，或能由宿主解析为本地文件；插件不下载任意远程参考图。
- 当前对外只返回首张规范化图片；原始响应仍保留全部图片，宿主可继续提取。
- 测试集中在仓库 [tests/test_aicost_image_plugin.py](../../tests/test_aicost_image_plugin.py)，
  不在插件目录内单设 `tests` 子目录。

## 变更历史

### 2026-07-20

- 新增 `aicost-image` 插件模式。
- 支持 GPT Image、Gemini、同步结果和异步轮询。
- 固定直连上游并加入避免重复扣费的提交策略。
