# WebDAV 云同步 API 配置改动详解

本文档记录当前项目中“WebDAV 云同步、API 平台配置同步、API Key 明文同步、手动上传/下载、自动上传、本地备份恢复、本地 JSON 手动导入/导出”相关改动。目标是方便多设备使用 Infinite Canvas 时，不必在每台设备上重复填写 API Key、模型列表、RunningHub/火山等平台配置。

## 1. 背景与目标

原先 API 设置主要依赖本机文件：

1. API 平台、请求地址、模型列表等非密钥配置保存到 `data/api_providers.json`。
2. API Key、RunningHub 钱包 Key、火山 AK/SK 等敏感值写入 `API/.env`。
3. 新设备重新安装或换电脑后，需要重新填写 API Key、请求地址和模型列表，容易遗漏或配错。

本次改动后的目标：

- 在 `更多设置 -> 云同步` 下新增独立云同步模块，不再嵌入 `API 设置` 添加/编辑页面。
- 通过 WebDAV 上传/下载 API 配置快照。
- 默认支持坚果云 WebDAV，也支持自定义 WebDAV。
- 同步范围限定为 API 平台配置、模型列表和 API 设置相关 Key，不同步画布、素材、历史记录或完整数据库。
- 上传/下载均由用户手动触发；可选开启“保存 API 配置后自动上传”。
- 独立提供本地 JSON 手动导入/导出，方便不用 WebDAV 时做离线备份或迁移。
- 云端文件按用户选择使用明文 JSON，界面显示明确风险提示。
- 从云端下载覆盖本机配置前，自动备份本机 `data/api_providers.json` 和 `API/.env`。

## 2. 文件总览

| 文件 | 作用 |
| --- | --- |
| `main.py` | 新增云同步配置读写、WebDAV 请求、同步包导出/导入、本地备份、WebDAV 同步和本地 JSON 导入/导出接口。 |
| `static/index.html` | 在“更多设置”中新增 `云同步` 入口，注册 `cloud-sync` iframe 路由，并更新相关页面版本号。 |
| `static/cloud-sync.html` | 独立 WebDAV 云同步页面，承载同步配置、测试连接、上传/下载和本地手动导入/导出操作。 |
| `static/js/cloud-sync.js` | 读取/保存同步配置、测试连接、上传、下载、本地 JSON 导入/导出、下载/导入后广播平台配置刷新。 |
| `static/api-settings.html` | 移除嵌入式云同步面板，保留 API 平台添加/编辑的纯净界面。 |
| `static/js/api-settings.js` | API 保存成功后读取云同步自动上传开关，必要时后台触发上传；收到云同步下载广播后刷新平台配置。 |
| `static/css/api-settings.css` | 云同步面板样式、状态提示、开关、明暗主题和响应式布局。 |
| `static/js/i18n/common.js` | 新增侧边栏 `云同步` 入口文案。 |
| `static/js/i18n/api-settings.js` | 云同步相关中英文文案。 |
| `static/js/i18n.js` | 更新 i18n loader 版本，确保新文案不会被旧缓存挡住。 |

## 3. 对外接口

### 3.1 读取云同步配置

```http
GET /api/cloud-sync/config
```

用途：

- 页面加载时读取本机保存的 WebDAV 配置。
- 后端不会返回完整密码，只返回是否保存过密码和密码遮罩。

响应示例：

```json
{
  "config": {
    "method": "webdav",
    "provider": "jianguoyun",
    "base_url": "https://dav.jianguoyun.com/dav/",
    "username": "user@example.com",
    "remote_root": "infinite-canvas-sync",
    "profile": "default",
    "auto_sync": false,
    "has_password": true,
    "password_preview": "••••••••abcd",
    "last_sync_at": 1783310000000,
    "last_upload_at": 1783310000000,
    "last_download_at": 0,
    "remote_file": "infinite-canvas-sync/default/api-settings.json"
  }
}
```

### 3.2 保存云同步配置

```http
PUT /api/cloud-sync/config
Content-Type: application/json
```

请求体：

```json
{
  "method": "webdav",
  "provider": "jianguoyun",
  "base_url": "https://dav.jianguoyun.com/dav/",
  "username": "user@example.com",
  "password": "webdav-app-password",
  "remote_root": "infinite-canvas-sync",
  "profile": "default",
  "auto_sync": true
}
```

规则：

- `provider` 支持 `jianguoyun` 和 `custom`。
- `provider=jianguoyun` 且 `base_url` 为空时，自动使用 `https://dav.jianguoyun.com/dav/`。
- `password` 为空时保留本机已保存密码，避免页面回填时误清空密码。
- `remote_root` 和 `profile` 会清理危险字符、去掉首尾斜杠，防止写到意外路径。

本地保存位置：

```text
data/cloud_sync_config.json
```

该文件在 `data/` 下，默认不会进入 Git。

### 3.3 测试 WebDAV 连接

```http
POST /api/cloud-sync/test
Content-Type: application/json
```

请求体同保存配置接口。

行为：

- 使用当前表单中的 WebDAV 地址、账号、密码测试连接。
- 使用 `PROPFIND` 请求服务器根地址。
- 不创建远程目录，不上传文件。

成功响应：

```json
{
  "ok": true,
  "remote_file": "infinite-canvas-sync/default/api-settings.json"
}
```

常见失败：

- `400`：缺少服务器地址、账号或密码。
- `502`：WebDAV 返回失败，例如 401、403、404、网络超时等。

### 3.4 上传 API 配置到云端

```http
POST /api/cloud-sync/upload
```

行为：

1. 读取本机 `data/api_providers.json`。
2. 读取本机 `API/.env` 中属于 API 设置范围的 env key。
3. 生成同步包 JSON。
4. 通过 `MKCOL` 创建远程目录：

```text
{remote_root}/{profile}/
```

5. 通过 `PUT` 上传到：

```text
{remote_root}/{profile}/api-settings.json
```

成功响应：

```json
{
  "ok": true,
  "remote_file": "infinite-canvas-sync/default/api-settings.json",
  "exported_at": 1783310000000,
  "env_count": 6,
  "provider_count": 4,
  "config": {}
}
```

### 3.5 从云端下载 API 配置

```http
POST /api/cloud-sync/download
```

行为：

1. 从 WebDAV 下载 `{remote_root}/{profile}/api-settings.json`。
2. 校验 `schema` 必须为 `infinite-canvas.api-sync.v1`。
3. 备份本机配置：

```text
data/cloud_sync_backups/{YYYYMMDD-HHMMSS}/api_providers.json
data/cloud_sync_backups/{YYYYMMDD-HHMMSS}/.env
```

4. 用云端 `providers` 覆盖本机 API 平台配置。
5. 用云端 `env` 覆盖 API 设置范围内的 env key。
6. 调用 `reload_env_globals()`，让新 Key 和模型列表不重启也生效。
7. 返回最新 public providers，前端立即刷新页面状态。

成功响应：

```json
{
  "ok": true,
  "remote_file": "infinite-canvas-sync/default/api-settings.json",
  "imported_at": 1783310000000,
  "backup_dir": "D:/canvas/Infinite-Canvas/data/cloud_sync_backups/20260706-154500",
  "env_count": 6,
  "provider_count": 4,
  "providers": [],
  "config": {}
}
```

### 3.6 导出本地 API 备份 JSON

```http
GET /api/cloud-sync/export
```

行为：

1. 读取本机 `data/api_providers.json`。
2. 读取本机 `API/.env` 中属于 API 设置范围的 env key。
3. 生成与 WebDAV 云端文件相同 schema 的 JSON。
4. 通过 `Content-Disposition` 作为附件下载。

默认文件名：

```text
infinite-canvas-api-settings-{YYYYMMDD-HHMMSS}.json
```

导出的 JSON 同样明文包含完整 API Key，只适合保存在可信位置。

### 3.7 导入本地 API 备份 JSON

```http
POST /api/cloud-sync/import
Content-Type: application/json
```

请求体就是 `GET /api/cloud-sync/export` 或 WebDAV 云端 `api-settings.json` 的完整 JSON 内容。

行为：

1. 校验 `schema` 必须为 `infinite-canvas.api-sync.v1`。
2. 覆盖前自动备份当前 `data/api_providers.json` 和 `API/.env`。
3. 导入 `providers` 到本机 API 平台配置。
4. 导入 `env` 到 API 设置同步范围内的 env key。
5. 调用 `reload_env_globals()`，让新 Key 不重启也生效。

成功响应：

```json
{
  "ok": true,
  "imported_at": 1783310000000,
  "backup_dir": "D:/canvas/Infinite-Canvas/data/cloud_sync_backups/20260706-160500",
  "env_count": 6,
  "provider_count": 4,
  "providers": []
}
```

## 4. 云端同步包格式

云端文件固定名：

```text
api-settings.json
```

默认远程路径：

```text
infinite-canvas-sync/default/api-settings.json
```

同步包示例：

```json
{
  "schema": "infinite-canvas.api-sync.v1",
  "app": "Infinite Canvas",
  "app_version": "2026.06.03",
  "exported_at": 1783310000000,
  "providers": [
    {
      "id": "modelscope",
      "name": "ModelScope",
      "base_url": "https://api-inference.modelscope.cn/v1",
      "protocol": "openai",
      "image_request_mode": "openai",
      "video_request_mode": "openai-videos-generations",
      "image_models": ["Tongyi-MAI/Z-Image-Turbo"],
      "chat_models": [],
      "video_models": [],
      "model_protocols": {},
      "ms_loras": [],
      "rh_apps": [],
      "rh_workflows": []
    }
  ],
  "env": {
    "MODELSCOPE_API_KEY": "完整 key",
    "RUNNINGHUB_API_KEY": "完整 key",
    "RUNNINGHUB_WALLET_API_KEY": "完整 key",
    "ARK_API_KEY": "完整 key",
    "VOLCENGINE_ACCESS_KEY_ID": "完整 AK",
    "VOLCENGINE_SECRET_ACCESS_KEY": "完整 SK",
    "API_PROVIDER_CUSTOM_API_KEY": "完整 key"
  }
}
```

注意：

- `providers` 不直接保存 Key。
- Key 统一保存在 `env` 字段。
- 当前设计为明文 JSON，云端文件可以直接看到完整 Key。

## 5. env 同步范围

同步范围不是整个 `API/.env`，而是 API 设置相关 key。

固定同步 key：

```text
COMFLY_API_KEY
COMFLY_BASE_URL
MODELSCOPE_API_KEY
MODELSCOPE_CHAT_MODELS
RUNNINGHUB_API_KEY
RUNNINGHUB_WALLET_API_KEY
ARK_API_KEY
VOLCENGINE_ACCESS_KEY_ID
VOLCENGINE_SECRET_ACCESS_KEY
IMAGE_MODELS
CHAT_MODELS
VIDEO_MODELS
```

动态同步 key：

```text
API_PROVIDER_{PROVIDER_ID}_KEY
```

例如自定义平台 ID 为 `custom_api`，则 key 为：

```text
API_PROVIDER_CUSTOM_API_KEY
```

下载覆盖规则：

- 云端同步包里存在的 key 会写入本机 `API/.env`。
- 云端同步包里不存在、但属于同步范围的本机 key 会被清空。
- 不属于 API 设置范围的其他 env 不会被主动同步。

## 6. 前端交互流程

云同步模块位于：

```text
更多设置 -> 云同步
```

字段：

| 字段 | 说明 |
| --- | --- |
| 服务商 | `坚果云` 或 `自定义 WebDAV`。 |
| WebDAV 服务器地址 | 默认坚果云为 `https://dav.jianguoyun.com/dav/`。 |
| WebDAV 账号 | 坚果云通常填写账号邮箱。 |
| WebDAV 密码 | 建议使用第三方应用密码，不要使用登录密码。 |
| 远程根目录 | 默认 `infinite-canvas-sync`。 |
| 同步配置名 | 默认 `default`，可用于多套配置。 |
| 自动上传 | 开启后，保存 API 配置成功会自动上传到 WebDAV。 |

按钮：

| 按钮 | 行为 |
| --- | --- |
| 测试连接 | 用当前表单测试 WebDAV 连接，不创建目录。 |
| 保存配置 | 保存 WebDAV 地址、账号、密码、目录和自动上传开关。 |
| 上传到云端 | 先保存同步配置，再上传当前 API 设置快照。 |
| 从云端下载 | 二次确认后下载并覆盖本机 API 配置和 Key。 |
| 选择备份 JSON | 手动选择本地备份 JSON，确认后导入并覆盖本机 API 配置和 Key。 |
| 导出 API 备份 | 手动导出当前 API 设置快照到本地 JSON 文件。 |

### 6.1 自动上传

`static/js/api-settings.js` 不再渲染云同步表单，只在 `saveProviders()` 成功后调用：

```js
queueCloudSyncAutoUpload();
```

该函数会重新读取 `/api/cloud-sync/config` 的 `auto_sync` 开关，只有开关为 `true` 时才会触发上传。

自动上传特点：

- 不阻塞本地保存。
- 失败只在 API 设置页状态栏提示，不回滚本地 API 设置。
- 只自动上传，不自动下载，避免旧设备启动时误覆盖新设备配置。

### 6.2 下载后的页面刷新

在独立云同步页下载成功后前端会：

1. 调用 `broadcastStudioApiChange('providers-changed')`，并标记 `source: "cloud-sync"`。
2. 主页面把变更广播给画布、API 设置等 iframe。
3. 已打开的 API 设置页收到来自云同步的广播后重新调用 `loadProviders()`。
4. 云同步页显示“云端 API 配置已恢复”。

### 6.3 手动导入/导出

云同步页顶部新增“手动导入导出”模块：

- `导出 API 备份`：调用 `/api/cloud-sync/export`，下载当前本机 API 设置快照。
- `选择备份 JSON`：读取用户选择的 JSON 文件，二次确认后调用 `/api/cloud-sync/import`。
- 导入成功后同样广播 `providers-changed`，让画布和 API 设置页重新读取平台配置。
- 导入前后端会自动备份当前配置，备份目录仍是 `data/cloud_sync_backups/{YYYYMMDD-HHMMSS}/`。

## 7. WebDAV 请求细节

当前后端使用项目已有的 `requests` 依赖，不新增 Python 包。

使用的方法：

| 方法 | 用途 |
| --- | --- |
| `PROPFIND` | 测试 WebDAV 地址和认证是否可用。 |
| `MKCOL` | 上传前递归创建远程目录。 |
| `PUT` | 上传 `api-settings.json`。 |
| `GET` | 下载 `api-settings.json`。 |

目录已存在时，`MKCOL` 返回 `405` 也视为成功。

上传目标 URL 由后端拼接和 URL encode，避免中文目录或空格导致请求失败。

## 8. 安全边界与风险

当前实现是为了方便多设备迁移配置，安全边界如下：

- 云端同步包明文保存完整 API Key。
- 本机 WebDAV 密码保存在 `data/cloud_sync_config.json` 中，文件位于被 Git 忽略的 `data/` 目录。
- 前端不会回显完整 WebDAV 密码，只显示遮罩。
- 前端不会回显完整 API Key，仍沿用原有 Key 预览逻辑。
- 从云端下载前有确认框，并且后端会先备份本机配置。

建议：

1. WebDAV 密码使用应用专用密码。
2. 不要把 `data/cloud_sync_config.json`、`API/.env`、云端 `api-settings.json` 发给别人。
3. 公共网盘、共享目录、团队 WebDAV 不适合放这份明文同步包。
4. 如果以后需要更高安全性，可以把 `env` 字段改成用户密码加密后的密文。

## 9. 常见问题与排查

### 9.1 测试连接失败 401 / 403

原因：

- 账号错误。
- 密码不是 WebDAV 应用密码。
- WebDAV 服务未开启。

处理：

- 坚果云请在“安全选项”生成第三方应用密码。
- 确认服务器地址是 `https://dav.jianguoyun.com/dav/`。

### 9.2 上传失败 404

可能原因：

- WebDAV 服务器根地址不正确。
- 自定义 WebDAV 不允许在当前路径下创建目录。

处理：

- 先点“测试连接”。
- 自定义 WebDAV 可以把服务器地址填到有写权限的目录。

### 9.3 上传失败 405

如果发生在 `MKCOL` 阶段，目录已存在时 405 会被视为成功，不应报错。

如果发生在 `PUT` 阶段，说明当前 WebDAV 路径可能不允许写文件。

### 9.4 下载失败：云端文件不是有效 JSON

原因：

- `api-settings.json` 被手动编辑坏了。
- 远程路径指向了其他文件。
- WebDAV 服务返回了 HTML 错误页但状态码异常。

处理：

- 到 WebDAV 里检查 `{remote_root}/{profile}/api-settings.json`。
- 删除损坏文件后重新上传。

### 9.5 下载后某些 Key 被清空

这是当前覆盖规则导致的预期行为：

- 云端同步包代表完整 API 设置快照。
- 同步范围内、本机存在但云端不存在的 Key 会被清空。

处理：

- 在配置完整的设备上重新上传。
- 或从 `data/cloud_sync_backups/` 找回下载前的 `.env`。

### 9.6 页面没有看到云同步入口

可能原因：

- 浏览器或 Electron 缓存了旧版 `index.html` 或 `cloud-sync.html`。

本次已经更新：

- `static/api-settings.html` 中的 `api-settings.css` / `api-settings.js` 版本号。
- `static/cloud-sync.html` 中的 `cloud-sync.js` 版本号。
- `static/js/i18n.js` 内部版本号。
- `static/index.html` 中 API 设置和云同步 iframe 的版本号。

如果仍然看不到，可强制刷新页面或重启客户端，然后展开左侧“更多设置”。

## 10. 验证清单

本次实现后已跑过：

```bat
python -m py_compile main.py
node --check .\static\js\api-settings.js
node --check .\static\js\cloud-sync.js
node --check .\static\js\i18n.js
node --check .\static\js\i18n\common.js
node --check .\static\js\i18n\api-settings.js
git diff --check
python -c "import main; c=main.public_cloud_sync_config(); print(c['provider'], c['remote_file'], c['has_password'])"
npm run build:win
```

预期：

- Python 编译无错误。
- 前端 JS 语法检查无错误。
- `git diff --check` 没有实际空白错误，仅 Windows 下可能提示 LF/CRLF。
- 默认云同步配置输出：

```text
jianguoyun infinite-canvas-sync/default/api-settings.json False
```

- Windows 客户端安装包可以重新构建成功。

## 11. 后续可选增强

当前版本优先保证简单可用，后续可以继续增强：

1. 增加用户密码加密云端 `env` 字段。
2. 增加“仅预览云端配置，不立即覆盖”的下载预览。
3. 增加多份云端快照历史，而不是固定覆盖 `api-settings.json`。
4. 增加同步 ComfyUI、提示词库等轻量配置的选项。
5. 增加 WebDAV 连接日志导出，方便用户反馈失败原因。
