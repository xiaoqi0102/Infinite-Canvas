# 图片接口插件

`plugins.image_plugins` 是 Infinite Canvas 的独立图片协议适配层。当前包含
`aicost-image` 模式，用于隔离 aicost.xyz 与通用 OpenAI/Gemini 图片实现之间的协议差异。

## 存在理由

aicost 的 GPT Image 接口同时包含同步结果和异步任务，图片编辑要求特定 multipart
字段；Gemini 图片模型又使用原生 `generateContent` 请求。它的长连接还需要绕过系统代理，
否则上游可能已经生成并计费，但客户端在收到响应前被本地代理断开。把这些行为放在插件中，
可以避免继续扩张 `main.py` 的供应商分支，并保证其他平台仍沿用原有网络设置。

## 核心职责

- 精确识别 `aicost.xyz` 和 `www.aicost.xyz` 官方主机名。
- 使用内部 `httpx.AsyncClient(trust_env=False)` 直连上游。
- 支持 GPT Image 文生图 `/v1/images/generations`。
- 支持 GPT Image 图片编辑 `/v1/images/edits`。
- 支持 Gemini 图片模型 `/v1beta/models/{model}:generateContent`。
- 解析 URL、base64、Gemini `inlineData` 和文本中的图片 URL。
- 轮询 `/v1/images/generations/{task_id}`，并在异常中保留上游任务 ID。
- 对创建请求执行“结果未知时不重试”策略，避免重复扣费。

## 非职责

- 不保存生成图片；宿主继续使用统一的图片落盘逻辑。
- 不读取 API Key 配置文件；鉴权头由宿主传入。
- 不决定供应商配置或 UI 展示；宿主根据 `aicost-image` 模式分发。
- 不把任意用户路径直接当作文件打开；本地路径必须由宿主回调解析。
- 不自动重试断连或 5xx 创建请求。

## 依赖关系

运行时只依赖标准库和项目已有的 `httpx`。插件不导入 FastAPI、Pydantic 或 `main.py`，
避免循环依赖。宿主需要注入：

- `resolve_local_path(url)`：把受控的 `/assets/`、`/output/` 地址解析为本地文件。
- `content_type_for_path(path)`：返回文件 MIME 类型。

## 快速使用

```python
from plugins.image_plugins import generate_aicost_image

image, raw = await generate_aicost_image(
    {
        "model": "gpt-image-2",
        "prompt": "雨夜中的未来城市街景",
        "size": "1536x864",
        "reference_images": [],
    },
    base_url="https://www.aicost.xyz/v1",
    headers={"Authorization": "Bearer <由宿主提供>"},
    resolve_local_path=resolve_local_path,
    content_type_for_path=content_type_for_path,
    request_timeout=3600.0,
    poll_timeout=3600.0,
)
```

公开接口：

- `AICOST_IMAGE_REQUEST_MODE = "aicost-image"`
- `AICostImageProtocolError`
- `is_aicost_image_official_provider(provider)`
- `generate_aicost_image(request, ...)`
- `query_aicost_image_task(task_id, ...)`

`generate_aicost_image` 返回 `(首张规范化图片, 最终原始响应)`。规范化图片格式为：

```python
{"type": "url", "value": "https://..."}
{"type": "b64", "value": "...", "mime_type": "image/png"}
```

## 目录结构

```text
image_plugins/
├── __init__.py
├── aicost.py
├── README.md
└── DESIGN.md
```

设计取舍和安全边界见 [DESIGN.md](DESIGN.md)。

协议测试位于 [tests/test_aicost_image_plugin.py](../../tests/test_aicost_image_plugin.py)。
