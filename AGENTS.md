# Infinite Canvas 项目协作指南

## 基本约定

- 本文件适用于整个仓库；子目录中的 `AGENTS.md` 优先级更高。
- 默认使用简体中文、UTF-8、Windows 11 和 PowerShell，同时保持现有 macOS、Linux、Chrome 扩展及 Photoshop UXP 兼容性。
- 修改前运行 `git status --short --branch`，保护并排除用户已有改动。

## 项目事实

- `main.py`：FastAPI 单体后端，也是最高风险文件。
- `plugins/image_plugins/`、`plugins/video_plugins/`：供应商协议适配层。插件不落盘、不读取 Key、不解析本地路径；新增供应商优先写成插件，具体边界见目录内 `README.md` 与 `DESIGN.md`。
- `static/`：无构建器的原生 HTML/CSS/JavaScript；`static/index.html` 是 iframe 外壳，`canvas.js` 与 `smart-canvas.js` 是两条主要画布链路。
- `electron/`：桌面外壳；preload 仅暴露受限 IPC，保持 `contextIsolation: true`、`nodeIntegration: false`。
- `workflows/`：随应用发布的内置 ComfyUI 工作流；用户工作流写入用户数据目录。
- `tests/`：Python `unittest` 与独立 Node 断言脚本；Python 测试必须从仓库根目录运行。
- `VERSION` 第一行是版本唯一来源；`npm run sync:desktop-version` 同步 Electron 版本、锁文件和安装包名称。`prd.md`、`Design.md`、`Tech.md` 中的历史状态不可作为当前事实。

按任务阅读维护文档：

- 上游合并：`UPSTREAM_MERGE_GUIDE.md`
- 视频任务与轮询：`VIDEO_GENERATION_POLLING_CHANGES.md`
- WebDAV/provider 同步：`WEBDAV_CLOUD_SYNC_CHANGES.md`
- Electron、数据目录、构建与更新：`ELECTRON_DESKTOP.md`
- CLI、Chrome 扩展、Photoshop 插件：对应目录的 `README.md`

## 环境与数据边界

- Node.js 使用 `npm` 和 `package-lock.json`；Python 使用 `./venv/Scripts/python.exe`。根目录 `python/` 和 `packages/` 属于分发运行时，不用于日常开发。
- `main.py` 直接运行固定监听 `3000`；`INFINITE_CANVAS_PORT` 仅由 Electron 读取。
- 诊断启动后端时设置 `$env:INFINITE_CANVAS_SKIP_STATIC_SYNC = '1'`，避免重写 `static/*.html` 的 `?v=` 参数。
- 不把 `API/`、`data/`、`assets/`、`output/`、日志、Key 或密码作为源码读取、修改或提交。
- 不修改依赖、缓存和构建产物；`build/backend.spec` 与 `build/icon.ico` 是受跟踪的构建输入。
- 保持 `APP_ROOT`/`WORKFLOW_DIR`（内置资源）与 `USER_DATA_ROOT`/`USER_WORKFLOW_DIR`（用户数据）分离。
- Electron 主用户数据目录为安装目录同级 `InfiniteCanvas_Data`，不可写时才回退到系统 userData；更新、覆盖安装和卸载不得删除该目录。

## 代码边界

### 后端

- 在 `main.py` 相邻职责区域做最小修改；新增供应商逻辑优先进入插件层。
- 请求体沿用 Pydantic；文件路径必须受既有根目录约束；网络调用必须设置超时并保持现有错误转换、重试和轮询语义。
- 修改 provider、视频或任务逻辑时，同时检查普通画布与智能画布链路。

### 静态前端

- 保持现有原生 DOM、全局函数和 iframe 通信模式，不引入前端框架或构建工具。
- 修改 DOM ID、全局函数、消息事件、API 路径或数据结构时检索全部调用点。
- 用户文案同时提供中英文，复用 `StudioI18n`、`data-i18n*`、`tr()`/`trf()`/`tf()`，并更新 `static/js/i18n/*.js`。
- 不直接修改 `static/vendor/`；升级镜像时同步 `static/vendor/MANIFEST.md`。
- 仅在资源确实变更时刷新对应 `?v=` 缓存参数。

### Electron 与发布

- IPC 通过受限 preload 暴露，主进程 handler 校验来源和输入。
- 保持 GitHub Release 为主更新源、ModelScope 为运行时兜底源。
- 保持 `deleteAppDataOnUninstall: false`、`allowToChangeInstallationDirectory: true` 和后端 `extraResources`。
- 仅版本或发布任务运行 `npm run sync:desktop-version`；仅在用户明确要求时构建安装包或发布。

## 必须保护的能力

修改高风险文件或合并上游时，按语义保留：

- 视频本地任务 API、持久化、重启恢复、退避轮询及终态错误识别。
- `video_request_mode` 对 `/v1/videos/generations` 和 `/v1/video/generations` 的兼容。
- WebDAV API 配置同步、本地备份、Key 同步范围及 `providers-changed` 刷新。
- 内置资源与用户数据分离、自定义工作流用户目录、旧数据非覆盖迁移。
- Electron 更新的 GitHub/ModelScope 双源、受限 IPC、下载进度及用户数据保护。
- `static/index.html` 的 `cloud-sync`、`frame-cloud-sync`、客户端更新入口及页面注册。

高风险文件：`main.py`、`static/js/canvas.js`、`static/js/smart-canvas.js`、`static/js/api-settings.js`、`static/index.html`、`electron/main.js`、`package.json`。

## 验证

按变更范围运行直接相关检查；仓库没有 pytest、Jest、ESLint、Prettier、Ruff 或类型检查配置。

```powershell
# 基础
git diff --check
git diff --name-only --diff-filter=U

# Python
.\venv\Scripts\python.exe -m py_compile main.py
.\venv\Scripts\python.exe -m unittest discover tests

# JavaScript（单文件示例；按范围执行其他 tests/test_*.js）
node --check <changed-file.js>
node tests\test_video_api_utils.js

# i18n
node static\js\i18n\validate-i18n.js

# 构建（仅任务明确需要时）
npm run build:backend
npm run build:win
```

`build:win` 已包含版本同步和后端打包，不要预先重复执行。前端交互、WebDAV 和客户端更新缺少完整自动化覆盖，相关变更需做最小手工冒烟验证。

## Git 与远程

- `origin` 是可推送 Fork，`main` 跟踪 `origin/main`；`upstream` 仅用于拉取源项目，禁止推送或移除其 `pushurl=DISABLED` 防护。
- 普通任务先 `git fetch origin --prune`，从最新 `origin/main` 创建 `codex/<类型>-<描述>`；上游同步另用 `codex/merge-upstream-YYYYMMDD` 并先刷新 `upstream`。
- 每项独立修改验证后，只暂存明确文件并提交，格式为 `<type>: <简体中文摘要>`；不得混入用户改动。
- 实现请求授权本地提交，但不授权推送、创建 PR、打 tag、发布或改写历史。
- 用户只说“推送”时，将已验证任务分支合并到本地 `main`，确认 `origin/main..main` 仅含本任务提交，再推送 `origin main`；不得推送 `upstream` 或强推。
- 合并回 `main` 后重新验证，通过后用 `git branch -d` 删除已合并的本地任务分支。

交付时列出修改文件、实际运行的检查、失败或未运行项以及剩余风险。
