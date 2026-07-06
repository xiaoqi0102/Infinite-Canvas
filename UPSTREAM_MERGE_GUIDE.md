# 后续源项目更新合并指导

本文档用于后续从源项目 `upstream/main` 合并更新时，保护本仓库已有的本地改动。重点保护五组补丁集：

1. 视频生成接口与轮询任务化改动，详见 `VIDEO_GENERATION_POLLING_CHANGES.md`。
2. WebDAV 云同步与 API 配置同步改动，详见 `WEBDAV_CLOUD_SYNC_CHANGES.md`。
3. Electron 客户端用户数据持久化改动：所有 API 配置、画布、素材、输出、历史、自定义工作流等用户文件必须写入安装目录同级 `InfiniteCanvas_Data`，不能写入安装目录内部。
4. Electron 客户端构建版本命名改动：Windows 安装包文件名后缀必须来自根目录 `VERSION`，不能回退到旧的 `package.json.version`。
5. Electron 客户端安装包级自动更新改动：打包客户端从 GitHub Release 检查、下载并安装新版本；网页“一键更新”仍保留为源项目更新提醒。

本文件不是用户使用说明，而是合并、解冲突、验证时的工程操作清单。

## 1. 合并前原则

- 永远先在独立分支合并上游，例如 `codex/merge-upstream-main` 或 `codex/merge-upstream-YYYYMMDD`。
- 不要在 `main` 上直接解冲突。
- 不要把“上游合并结果”和“本地新功能开发”混在同一个提交里。
- 合并期间优先保护本地补丁集的行为，而不是机械保留某一边的全部文本。
- 如果上游也修改了同一功能，先判断能否合并双方语义，再决定取舍。
- 静态 HTML 的 `?v=` 版本号冲突通常只是 cache-busting，功能语义以入口、iframe、脚本引用是否完整为准。

推荐基础流程：

```powershell
git fetch upstream --prune
git status --short --branch
git switch -c codex/merge-upstream-YYYYMMDD
git merge upstream/main
```

如果出现冲突，解完后先验证，再提交：

```powershell
git diff --name-only --diff-filter=U
git diff --check
git status
```

## 2. 当前本地补丁集地图

### 2.1 视频任务化补丁

目标：前端不再把长时间视频生成绑定在一个 `/api/canvas-video` 长请求上，而是先创建本地任务，再保存 pending，再轮询本地任务接口。

关键文件：

- `main.py`
- `static/js/canvas.js`
- `static/js/smart-canvas.js`
- `static/api-settings.html`
- `static/js/api-settings.js`
- `tools/patches/video_request_mode_patch.py`
- `重放视频接口补丁.bat`
- `VIDEO_GENERATION_POLLING_CHANGES.md`

必须保留的后端接口：

- `POST /api/canvas-video-tasks`
- `GET /api/canvas-video-tasks/{task_id}`
- `POST /api/canvas-video` 只作为兼容旧入口，不应重新成为前端主流程。

必须保留的后端能力：

- `video_request_mode` 支持 `openai-videos-generations` 和 `openai-video-generations`。
- `/v1/videos/generations` 与 `/v1/video/generations` 都可按配置选择。
- 本地任务 ID 使用 `canvas_video_xxx`，不要和上游任务 ID 混用。
- 后端任务需要持久化，重启后能恢复已经拿到上游任务 ID 的任务。
- 没有上游任务 ID 的任务不要自动重提，避免重复扣费。
- 视频轮询从 5 秒开始，支持 `retry_after`，并逐步退避。
- 余额不足、额度不足、账单、`insufficient_quota`、`Insufficient credits` 等错误是终态失败，不要无限 pending。

关键函数和结构：

- `normalize_video_request_mode`
- `effective_video_request_mode`
- `video_submit_url_candidates`
- `video_task_url_candidates`
- `video_retry_after_seconds`
- `is_video_terminal_error`
- `update_canvas_video_task`
- `run_canvas_video_task`
- `resume_canvas_video_tasks_on_startup`
- `createCanvasVideoTask`
- `pollCanvasVideoTask`
- `waitCanvasVideoTaskResult`
- `completeCanvasVideoTask`
- `failCanvasVideoTask`
- `createSmartCanvasVideoTask`
- `runApiVideoGeneration`
- `pollSmartCanvasVideoTask`
- `resumeSmartPendingNode`
- `querySmartImageTaskNow`
- `isSmartTerminalTaskError`

合并时特别注意：

- `runApiVideoGeneration()` 返回的是任务对象，形如 `{ taskIds, providerId, model, kind:'video' }`，不是视频 URL 数组。
- 智能画布视频必须进入 `pendingTasks + resumeSmartPendingNode()` 路径。
- 不要把 API 视频分支改回“直接 finalize 视频数组”的逻辑。
- 普通画布 pending 中必须保留 `canvasTaskType:'online-video'` 和本地 `canvasTaskId`。

### 2.2 WebDAV 云同步补丁

目标：在独立“云同步”页面中同步 API 平台配置、模型列表和 API Key，支持 WebDAV 上传/下载、本地 JSON 导入/导出、保存 API 设置后自动上传。

关键文件：

- `main.py`
- `static/index.html`
- `static/cloud-sync.html`
- `static/js/cloud-sync.js`
- `static/api-settings.html`
- `static/js/api-settings.js`
- `static/css/api-settings.css`
- `static/js/i18n.js`
- `static/js/i18n/common.js`
- `static/js/i18n/api-settings.js`
- `WEBDAV_CLOUD_SYNC_CHANGES.md`

必须保留的后端接口：

- `GET /api/cloud-sync/config`
- `PUT /api/cloud-sync/config`
- `POST /api/cloud-sync/test`
- `POST /api/cloud-sync/upload`
- `POST /api/cloud-sync/download`
- `GET /api/cloud-sync/export`
- `POST /api/cloud-sync/import`

必须保留的数据边界：

- `CLOUD_SYNC_SCHEMA = "infinite-canvas.api-sync.v1"`
- 云端文件名为 `api-settings.json`。
- 同步包中的 `providers` 不直接保存 API Key。
- API Key 和敏感值统一保存在同步包 `env` 字段。
- 同步范围只覆盖 API 设置相关 env，不要扩展成整个 `API/.env`。
- 下载或导入覆盖本机配置前，必须备份 `data/api_providers.json` 和 `API/.env`。
- WebDAV 密码为空时应保留本机已保存密码，不能误清空。

前端入口必须保留：

- `static/index.html` 中的云同步侧边栏入口。
- `frame-cloud-sync` iframe。
- `PAGE_IDS` 中的 `cloud-sync`。
- 更多设置展开时包含 `cloud-sync`。
- `static/js/api-settings.js` 中 `queueCloudSyncAutoUpload()` 和云同步广播刷新逻辑。

合并时特别注意：

- 上游如果改了首页路由或 iframe 列表，不要丢掉 `cloud-sync`。
- 上游如果改了 API 设置页，不要把云同步重新嵌回 API 设置表单。当前设计是独立页面。
- 上游如果改了 provider schema，导入云同步包后仍要走 `normalize_provider()`、`merge_default_api_providers()`、`save_api_providers()`、`update_env_values()`、`reload_env_globals()` 这些路径。
- 自动上传只在保存 API 配置成功后触发，失败只提示，不回滚本地保存。

### 2.3 桌面端用户数据持久化补丁

目标：打包客户端更新、覆盖安装、卸载重装时，用户文件不被安装目录覆盖或删除。所有用户可写数据进入安装目录同级 `InfiniteCanvas_Data`，应用资源、静态文件、内置工作流和更新目标仍留在应用根目录。

关键文件：

- `main.py`
- `electron/main.js`
- `package.json`
- `package-lock.json`
- `scripts/build-backend.cjs`
- `scripts/sync-electron-version.cjs`
- `打包Electron桌面版.bat`
- `ELECTRON_DESKTOP.md`

必须保留的数据目录策略：

- 打包后若安装在 `D:\Apps\Infinite Canvas`，用户数据目录应为 `D:\Apps\InfiniteCanvas_Data`。
- 不能使用 `D:\Apps\Infinite Canvas\InfiniteCanvas_Data` 作为主目录，因为它仍在安装目录内部。
- 如果安装目录父级不可写，Electron 才回退到系统 userData 下的 `InfiniteCanvas_Data`。
- Electron 必须通过 `INFINITE_CANVAS_USER_DATA_DIR` 把最终目录传给后端。
- `INFINITE_CANVAS_BASE_DIR` 可继续传同一个值作为兼容别名。
- 打包版必须设置 `INFINITE_CANVAS_SKIP_STATIC_SYNC=1`，避免启动时写静态资源。

必须保留的用户数据边界：

- `BASE_DIR` / `APP_ROOT` 仍代表应用根目录。
- `STATIC_DIR` 和内置 `WORKFLOW_DIR` 仍从应用根目录派生。
- `USER_DATA_ROOT` 代表用户数据根目录。
- `API_ENV_FILE`、`DATA_DIR`、`ASSETS_DIR`、`OUTPUT_DIR`、`HISTORY_FILE`、`GLOBAL_CONFIG_FILE` 必须从 `USER_DATA_ROOT` 派生。
- 自定义工作流写入 `USER_WORKFLOW_DIR` / `CUSTOM_WORKFLOW_DIR`，内置工作流继续从应用根目录读取。
- `shared_folders` 的相对路径基准应是 `USER_DATA_ROOT`，不要退回 `BASE_DIR`。

必须保留的迁移与诊断能力：

- 后端启动前运行 `migrate_user_data_from_app_root()`，从旧应用根目录补拷 `API/`、`data/`、`assets/`、`output/`、`history.json`、`global_config.json`、`workflows/custom`、`workflows/自定义`。
- 迁移只补缺，不覆盖目标已有文件。
- 迁移成功写 `.migration_complete.json`；迁移失败写 `.migration_failed.json`，以便下次继续尝试。
- Electron 启动时从旧的安装目录内部 `InfiniteCanvas_Data` 非覆盖补拷到新的同级目录。
- Electron 启动时把诊断日志写到当前 `InfiniteCanvas_Data/desktop.log`，至少记录数据目录、安装/资源路径、后端命令、端口、退出码和启动错误。
- `package.json` 的 NSIS 配置必须保留 `deleteAppDataOnUninstall: false`。

合并时特别注意：

- 不要把 `BASE_DIR` 整体改成用户数据目录；这会导致静态资源、内置工作流、自更新目标混在用户数据里。
- 不要把 `safe_update_target()`、`safe_static_dir()` 等更新目标改到 `USER_DATA_ROOT`。
- 不要让 `os.makedirs(STATIC_DIR)` 或 `os.makedirs(WORKFLOW_DIR)` 在打包版写应用资源目录。
- 不要把自定义工作流上传回应用根目录的 `workflows/custom`。
- 不要删掉 `desktop.log` 诊断日志；打包版 `stdio` 通常不可见，这个日志是用户现场排查的第一入口。

### 2.4 桌面端构建版本命名补丁

目标：每次按项目根目录 `VERSION` 更新后，Windows 安装包文件名必须使用同一个版本后缀，避免 UI 显示新版本但安装包仍叫旧版本。

关键文件：

- `VERSION`
- `package.json`
- `package-lock.json`
- `scripts/build-backend.cjs`
- `scripts/sync-electron-version.cjs`
- `打包Electron桌面版.bat`
- `ELECTRON_DESKTOP.md`

必须保留的构建规则：

- `npm run build:win` 和 `npm run pack:win` 必须先执行 `npm run sync:desktop-version`。
- `npm run build:backend` 必须执行 `node scripts/build-backend.cjs`，不要改回裸 `pyinstaller`。
- `scripts/build-backend.cjs` 必须优先使用项目 `venv\Scripts\python.exe`，并在构建前确认 `requirements.txt` 和 PyInstaller 已安装到该 venv。
- `scripts/sync-electron-version.cjs` 必须读取根目录 `VERSION` 的第一行作为项目版本。
- `package.json.version` 可以使用去掉前导零的 semver 兼容值，例如 `VERSION=2026.07.6` 时写入 `2026.7.6`。
- Windows 安装包文件名必须保留原始 `VERSION` 文本，例如 `release/Infinite-Canvas-Setup-2026.07.6.exe`。
- 安装包前缀使用连字符，确保 `.exe`、`.blockmap` 和 `latest.yml` 引用同一个文件名。
- `build.win.artifactName` 必须由同步脚本维护，不要手动改回 electron-builder 默认命名。
- 构建日志应打印 `Project VERSION`、Electron metadata version 和 expected installer 路径，便于现场确认。

合并时特别注意：

- 上游如果改了 `package.json.version`，合并后要重新运行 `npm run sync:desktop-version`。
- 上游如果改了 `package.json` 的 `scripts` 或 `build.win`，不要丢掉 `sync:desktop-version` 和 `artifactName`。
- 上游如果改了 `build:backend`，不要让后端打包重新使用全局 `pyinstaller`；全局 Python 缺依赖时会生成运行时缺 `httpx` 等模块的坏包。
- 不要只在构建后手动重命名 `.exe`；这会让 `latest.yml` 或其他构建元数据和真实产物名失配。
- 不要把 `artifactName` 改回带空格的安装包名，除非同时验证 `latest.yml` 也引用真实存在的同名文件。
- 如果 `VERSION` 改成非 `MAJOR.MINOR.PATCH` 数字格式，必须先调整同步脚本规则，再打包。

### 2.5 桌面端安装包级自动更新补丁

目标：打包版 Electron 客户端通过 GitHub Release 的 `latest.yml`、`.exe` 和 `.blockmap` 检查、下载并安装新客户端；网页内“一键更新”仍用于源项目 `main.py`、`VERSION`、`static` 更新提醒，不改成安装包更新。

关键文件：

- `electron/main.js`
- `electron/preload.js`
- `static/index.html`
- `static/js/i18n/common.js`
- `package.json`
- `package-lock.json`
- `ELECTRON_DESKTOP.md`

必须保留的客户端更新规则：

- `electron-updater` 必须作为运行时依赖存在。
- `build.publish` 必须指向 `provider=github`、`owner=xiaoqi0102`、`repo=Infinite-Canvas`。
- Electron 自动更新只在 `app.isPackaged` 时启用，开发模式不检查。
- 自动检查应在窗口打开后延迟执行，不阻塞后端启动和首屏。
- 手动入口应保留在网页左侧底部版本号下方的 `检查客户端更新` 按钮中，通过 `electron/preload.js` 暴露的窄 IPC 调用 Electron 主进程。
- 不要把客户端更新入口放回 Electron 原生菜单或 Windows 托盘菜单；用户入口应在 `static/index.html` 侧栏底部。
- 不要复用网页里的 `update-now-btn`；它仍然只服务源项目更新提醒。
- `update-available` 时先询问用户是否下载；`update-downloaded` 后再询问是否重启并安装。
- 重启安装前必须调用 `stopBackend()`，再调用 `autoUpdater.quitAndInstall()`。
- 更新事件必须写入当前 `InfiniteCanvas_Data/desktop.log`，事件名前缀为 `client-update-`。
- `BrowserWindow.webPreferences` 必须保留 `preload: path.join(__dirname, 'preload.js')`、`contextIsolation: true` 和 `nodeIntegration: false`。
- preload 只能暴露 `window.InfiniteCanvasDesktop.checkClientUpdate()` 这类窄接口，不要把通用 `ipcRenderer` 暴露给网页。

合并时特别注意：

- 不要把 `static/index.html` 的“一键更新”改成客户端安装包更新；它是源项目更新提醒。
- 不要删除 `static/index.html` 左侧底部 `client-update-btn`，它应位于 `project-version-badge` 之后、`author-box` 之前。
- 不要删除 `autoUpdater.autoDownload = false`，否则会绕过“下载更新 / 稍后”的用户确认。
- 不要让客户端更新写入或覆盖 `InfiniteCanvas_Data`。
- 发布新客户端时必须上传 `Infinite-Canvas-Setup-<VERSION>.exe`、`.blockmap` 和 `latest.yml` 到同一个 GitHub Release tag。

## 3. 高风险冲突文件处理

### 3.1 `main.py`

这是最高风险文件。上游经常修改模型、平台、任务、接口相关逻辑，本地又在同一文件里维护视频任务化、WebDAV 云同步和用户数据目录拆分。

保留原则：

- 合入上游新增 provider、RunningHub、CLI、模型列表等修复。
- 同时保留视频任务化接口和云同步接口。
- 同时保留应用根目录与用户数据根目录的拆分：`APP_ROOT/BASE_DIR` 用于应用资源，`USER_DATA_ROOT` 用于用户文件。
- 如果上游改了 `normalize_provider()`，必须确认 `video_request_mode`、`rh_apps`、`rh_workflows`、`video_models`、云同步导入的 provider 字段没有丢。
- 如果上游改了异步图片或视频任务逻辑，必须确认本地视频任务持久化和恢复仍在。
- 如果上游改了路径常量，必须确认 `API/.env`、`data/`、`assets/`、`output/`、`history.json`、`global_config.json` 没有重新指回 `BASE_DIR`。
- 如果上游改了工作流管理，必须确认内置工作流从 `WORKFLOW_DIR` 读取，自定义工作流从 `USER_WORKFLOW_DIR` 写入。

检查关键词：

```text
canvas-video-tasks
video_request_mode
effective_video_request_mode
video_retry_after_seconds
is_video_terminal_error
resume_canvas_video_tasks_on_startup
cloud-sync
CLOUD_SYNC_SCHEMA
apply_cloud_sync_payload
public_cloud_sync_config
USER_DATA_ROOT
INFINITE_CANVAS_USER_DATA_DIR
migrate_user_data_from_app_root
USER_WORKFLOW_DIR
workflow_path_for_write
INFINITE_CANVAS_SKIP_STATIC_SYNC
```

### 3.2 `static/js/canvas.js`

普通画布重点保护视频节点任务化和 RunningHub 旧能力。

保留原则：

- 视频节点走 `createCanvasVideoTask()`。
- 视频 pending 使用 `canvasTaskType:'online-video'`。
- 轮询使用 `pollCanvasVideoTask()`，手动恢复使用本地 `canvasTaskId`。
- 如果上游新增 RunningHub 模型 API，要与本地视频 pending 逻辑并存。

检查关键词：

```text
createCanvasVideoTask
pollCanvasVideoTask
waitCanvasVideoTaskResult
completeCanvasVideoTask
failCanvasVideoTask
canvasTaskType:'online-video'
runRhModelNode
```

### 3.3 `static/js/smart-canvas.js`

智能画布是最容易产生语义冲突的前端文件。

保留原则：

- `runApiVideoGeneration()` 只负责提交本地视频任务，并返回任务对象。
- `runGeneration()` 中视频任务应进入统一 pending/resume 分支。
- `resumeSmartPendingNode()` 必须按 `task.kind === 'video'` 调用 `pollSmartCanvasVideoTask()`。
- `querySmartImageTaskNow()` 虽然名字含 Image，但要保留视频手动查询分支。
- 如果上游新增 RunningHub 模型 API，要保留 `runningHubSelectedModel()` 和 `runningHubModelApiSettings()`，并让它走图片任务路径。

检查关键词：

```text
createSmartCanvasVideoTask
runApiVideoGeneration
pollSmartCanvasVideoTask
resumeSmartPendingNode
querySmartImageTaskNow
isSmartTerminalTaskError
runningHubSelectedModel
runningHubModelApiSettings
```

### 3.4 `static/api-settings.html` 与 `static/js/api-settings.js`

保护 API 设置中的视频接口模式和云同步自动上传。

保留原则：

- `videoRequestModeInput` 下拉必须存在。
- `normalizeVideoRequestMode()` 必须保留兼容别名。
- 保存 provider 时必须保存 `video_request_mode`。
- 保存成功后继续调用 `queueCloudSyncAutoUpload()`。
- 收到云同步广播后重新加载 providers。

检查关键词：

```text
videoRequestModeInput
normalizeVideoRequestMode
video_request_mode
queueCloudSyncAutoUpload
providers-changed
cloud-sync
```

### 3.5 `static/index.html`

保护云同步入口。

保留原则：

- 保留侧边栏 “云同步” 按钮。
- 保留侧边栏底部版本号下方的 `检查客户端更新` 按钮；它是桌面安装包更新入口，不是源项目“一键更新”。
- 保留 `frame-cloud-sync`。
- 保留 `PAGE_IDS` 中的 `cloud-sync`。
- 保留更多设置展开逻辑。
- 合并后统一更新 `?v=` 版本号，避免加载旧页面。

检查关键词：

```text
cloud-sync
frame-cloud-sync
PAGE_IDS
client-update-btn
checkClientUpdate
```

### 3.6 静态 HTML 版本号

上游合并后常出现 `?v=` 冲突。处理原则：

- 功能入口优先于版本号本身。
- 解决冲突后，相关 HTML、JS、CSS、i18n 的 `?v=` 应统一刷新到当前版本。
- 不要因为版本号冲突删掉本地 iframe 或脚本引用。

### 3.7 `electron/main.js`

保护桌面端用户数据目录选择、旧目录补拷和运行日志。

保留原则：

- `USER_DATA_DIR_NAME` 必须是 `InfiniteCanvas_Data`。
- `installRoot()` 打包后应返回 `path.dirname(process.resourcesPath)`。
- `userDataRoot()` 打包后优先返回 `path.join(path.dirname(installRoot()), USER_DATA_DIR_NAME)`。
- 只有同级目录不可写时，才回退到 `path.join(app.getPath('userData'), USER_DATA_DIR_NAME)`。
- `migrateLegacyInstallData()` 必须从旧的 `path.join(installRoot(), USER_DATA_DIR_NAME)` 非覆盖补拷。
- `appendRuntimeLog()` 必须写入当前数据目录下的 `desktop.log`。
- 后端环境变量必须包含 `INFINITE_CANVAS_USER_DATA_DIR`、兼容的 `INFINITE_CANVAS_BASE_DIR`、`INFINITE_CANVAS_SKIP_STATIC_SYNC=1`。
- `ipcMain.handle('client-update:check', ...)` 必须保留，且应校验调用方来自当前主窗口后再触发 `checkForClientUpdates({ manual: true })`。
- `BrowserWindow` 必须加载 `electron/preload.js`，不要关闭 `contextIsolation` 或打开 `nodeIntegration`。

检查关键词：

```text
USER_DATA_DIR_NAME
InfiniteCanvas_Data
installSiblingDataDir
migrateLegacyInstallData
appendRuntimeLog
desktop.log
INFINITE_CANVAS_USER_DATA_DIR
INFINITE_CANVAS_SKIP_STATIC_SYNC
client-update:check
preload.js
```

### 3.8 `package.json`

保护打包、卸载行为和安装包版本命名。

保留原则：

- `scripts.sync:desktop-version` 必须存在。
- `build:win` / `pack:win` 必须先执行 `npm run sync:desktop-version`。
- `build:backend` 必须执行 `node scripts/build-backend.cjs`，确保 PyInstaller 使用项目 venv。
- `build.win.artifactName` 必须由 `scripts/sync-electron-version.cjs` 写入并保留原始 `VERSION` 后缀。
- `dependencies.electron-updater` 必须存在。
- `build.publish` 必须指向 `xiaoqi0102/Infinite-Canvas` 的 GitHub Release。
- `build.nsis.deleteAppDataOnUninstall` 必须是 `false`。
- `allowToChangeInstallationDirectory` 必须保留为 `true`，用户可以继续选择安装位置。
- `extraResources` 必须继续把 `dist/infinite-canvas-backend` 打进 `resources/backend`。
- 不要把 `InfiniteCanvas_Data` 放进 `files` 或 `extraResources`，它是运行时用户数据目录，不是打包资源。

检查关键词：

```text
deleteAppDataOnUninstall
allowToChangeInstallationDirectory
extraResources
dist/infinite-canvas-backend
sync:desktop-version
build-backend.cjs
artifactName
electron-updater
publish
```

### 3.9 `ELECTRON_DESKTOP.md`

保护桌面端运行数据和构建版本命名说明。

保留原则：

- 文档必须说明安装包文件名后缀来自根目录 `VERSION`。
- 文档必须说明 `npm run sync:desktop-version` 会同步 Electron 元数据和 `build.win.artifactName`。
- 文档必须说明后端打包通过 `scripts/build-backend.cjs` 使用项目 `venv`，避免漏打 `httpx` 等 Python 依赖。
- 文档必须说明客户端安装包级自动更新依赖 GitHub Release 的 `.exe`、`.blockmap` 和 `latest.yml`。
- 文档必须说明网页“一键更新”仍是源项目更新提醒，不是安装包更新。
- 示例应保持类似 `VERSION=2026.07.6` -> `release/Infinite-Canvas-Setup-2026.07.6.exe`。
- 文档必须明确 `InfiniteCanvas_Data` 位于安装目录同级，不在安装目录内部。
- 示例应保持类似 `D:\Apps\Infinite Canvas` -> `D:\Apps\InfiniteCanvas_Data`。
- 文档必须说明系统 userData 回退条件。
- 文档必须说明旧安装目录内部 `InfiniteCanvas_Data` 会非覆盖补拷。
- 文档必须说明 `desktop.log` 的位置和用途。
- 文档必须说明 NSIS `deleteAppDataOnUninstall: false`。

## 4. 推荐解冲突顺序

1. 先解 `main.py`，确认后端接口、数据结构、用户数据根目录拆分完整。
2. 再解 `electron/main.js`，确认打包版数据目录仍是安装目录同级 `InfiniteCanvas_Data`。
3. 再解 `package.json`，确认 NSIS 卸载和打包资源策略不变。
4. 再解 `static/js/api-settings.js` 与 `static/api-settings.html`，确认配置字段仍会保存。
5. 再解 `static/js/canvas.js`，确认普通画布视频 pending 逻辑。
6. 再解 `static/js/smart-canvas.js`，确认智能画布视频和 RunningHub 模型 API 的分支选择。
7. 再解 `static/index.html`，确认云同步入口不丢。
8. 最后处理 `ELECTRON_DESKTOP.md`、静态 HTML 的 `?v=` 版本号和样式冲突。

## 5. 合并后自动检查

基础 Git 检查：

```powershell
git status --short --branch
git diff --name-only --diff-filter=U
git diff --check
```

Python 检查：

```powershell
.\venv\Scripts\python.exe -c "import fastapi, uvicorn, requests, pydantic, multipart, httpx, PIL; print('imports ok')"
.\venv\Scripts\python.exe -m py_compile main.py
```

JavaScript 语法检查：

```powershell
node --check electron\main.js
Get-ChildItem -Recurse -File static\js,electron -Include *.js | ForEach-Object { node --check $_.FullName }
```

冲突标记检查：

```powershell
Select-String -Path main.py,static\*.html,static\js\*.js,static\js\i18n\*.js,electron\*.js -Pattern '<<<<<<<|=======|>>>>>>>'
```

视频补丁 dry-run：

```powershell
重放视频接口补丁.bat -DryRun -SkipChecks
```

如果 dry-run 显示会改关键文件，先人工审查差异，不要盲目覆盖上游新增逻辑。

云同步轻量检查：

```powershell
.\venv\Scripts\python.exe -c "import main; c=main.public_cloud_sync_config(); print(c['provider'], c['remote_file'], c['has_password'])"
```

预期包含：

```text
jianguoyun infinite-canvas-sync/default/api-settings.json False
```

用户数据目录轻量检查：

```powershell
$env:INFINITE_CANVAS_USER_DATA_DIR = Join-Path $env:TEMP ("InfiniteCanvas_Data_merge_check_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $env:INFINITE_CANVAS_USER_DATA_DIR -Force | Out-Null
Set-Content -LiteralPath (Join-Path $env:INFINITE_CANVAS_USER_DATA_DIR ".migration_complete.json") -Value "{}" -Encoding UTF8
.\venv\Scripts\python.exe -c "import os, main; assert main.USER_DATA_ROOT == os.environ['INFINITE_CANVAS_USER_DATA_DIR']; assert main.DATA_DIR.startswith(main.USER_DATA_ROOT); assert main.API_ENV_FILE.startswith(main.USER_DATA_ROOT); assert main.STATIC_DIR.startswith(main.APP_ROOT); assert main.WORKFLOW_DIR.startswith(main.APP_ROOT); assert main.USER_WORKFLOW_DIR.startswith(main.USER_DATA_ROOT); print('user data paths ok')"
Remove-Item -LiteralPath $env:INFINITE_CANVAS_USER_DATA_DIR -Recurse -Force
Remove-Item Env:\INFINITE_CANVAS_USER_DATA_DIR -ErrorAction SilentlyContinue
```

Electron 和打包配置检查：

```powershell
node --check electron\main.js
node --check scripts\build-backend.cjs
node --check scripts\sync-electron-version.cjs
npm run sync:desktop-version
npm run build:backend
node -e "const p=require('./package.json'); if(p.build.nsis.deleteAppDataOnUninstall !== false) process.exit(1); console.log('nsis uninstall keeps app data')"
node -e "const p=require('./package.json'); if(!p.dependencies || !p.dependencies['electron-updater']) process.exit(1); const pub=Array.isArray(p.build.publish)?p.build.publish[0]:p.build.publish; if(!pub || pub.provider!=='github' || pub.owner!=='xiaoqi0102' || pub.repo!=='Infinite-Canvas') process.exit(1); console.log('electron updater publish config ok')"
$v = (Get-Content -Raw VERSION).Trim()
node -e "const fs=require('fs'); const p=require('./package.json'); const v=fs.readFileSync('VERSION','utf8').trim(); if(!p.build.win.artifactName.includes(v)) process.exit(1); console.log('installer suffix follows VERSION')"
Select-String -Path electron\main.js,ELECTRON_DESKTOP.md,package.json,scripts\build-backend.cjs,scripts\sync-electron-version.cjs -Pattern "InfiniteCanvas_Data|INFINITE_CANVAS_USER_DATA_DIR|deleteAppDataOnUninstall|desktop.log|sync:desktop-version|build-backend|artifactName|httpx|electron-updater|autoUpdater|client-update"
```

## 6. 手工功能验证清单

### 6.1 视频任务化

- API 设置页能看到 “视频：videos” 和 “视频：video”。
- 选择 `视频：video` 后，提交接口应为 `/v1/video/generations`。
- 选择 `视频：videos` 后，提交接口应为 `/v1/videos/generations`。
- 普通画布视频节点运行后，输出节点出现 pending。
- 刷新普通画布后，pending 仍可继续查询。
- 智能画布视频运行后，节点 `pendingTasks` 中 `kind` 为 `video`。
- 智能画布刷新后，视频 pending 可恢复或可手动查询。
- 上游返回余额不足时，任务应进入 failed，不应无限 pending。
- 后端重启后，已有上游任务 ID 的任务可继续查询。
- 没有上游任务 ID 的任务不要自动重提。

### 6.2 WebDAV 云同步

- 左侧 “更多设置” 中能看到 “云同步”。
- 点击后能加载 `static/cloud-sync.html`。
- 保存 WebDAV 配置时，密码为空不会清空已保存密码。
- 测试连接不创建远程目录。
- 上传时会生成 `api-settings.json`。
- 下载或导入前会备份本机 `data/api_providers.json` 与 `API/.env`。
- 下载或导入后，API 设置页能收到 `providers-changed` 并刷新。
- 导出 JSON 能正常下载。
- 导入 JSON 后 API Key 和 provider 列表能生效。

### 6.3 桌面端用户数据持久化

- 安装到 `D:\Apps\Infinite Canvas` 后，首次启动应创建 `D:\Apps\InfiniteCanvas_Data`。
- 不应创建或继续使用 `D:\Apps\Infinite Canvas\InfiniteCanvas_Data` 作为主数据目录。
- 如果旧目录 `D:\Apps\Infinite Canvas\InfiniteCanvas_Data` 已存在，新版启动后应把缺失内容非覆盖补拷到 `D:\Apps\InfiniteCanvas_Data`。
- `D:\Apps\InfiniteCanvas_Data\desktop.log` 应记录 `desktop-start`、`backend-spawn`，退出时记录 `backend-exit`。
- `desktop.log` 中 `userDataRoot=` 应指向同级 `InfiniteCanvas_Data`。
- API 配置保存后，应写入 `D:\Apps\InfiniteCanvas_Data\API\.env`。
- 画布和素材应写入 `D:\Apps\InfiniteCanvas_Data\data`、`assets`、`output`。
- 上传自定义工作流后，应写入 `D:\Apps\InfiniteCanvas_Data\workflows\custom`。
- 内置工作流仍应从安装目录的 `resources/backend/workflows` 或后端应用资源目录读取。
- 覆盖安装新版后，`D:\Apps\InfiniteCanvas_Data` 内容应保留。
- 卸载客户端后，NSIS 不应删除 Electron 系统 appData；同级 `InfiniteCanvas_Data` 也不应被安装器删除。

## 7. 常见问题与处理

### 7.1 视频任务一直 pending

优先检查：

- 后端任务是否有 `upstream_task_id`。
- 前端 pending 是否保存的是本地 `canvas_video_xxx`。
- 错误内容是否是余额不足、额度不足或账单类终态错误。
- `pollCanvasVideoTask()` 或 `pollSmartCanvasVideoTask()` 是否被误删。

### 7.2 智能画布视频一运行就失败

重点检查：

- `runApiVideoGeneration()` 是否返回任务对象。
- `runGeneration()` 是否错误地把返回值当视频数组处理。
- `resumeSmartPendingNode()` 是否按 `kind:'video'` 调用视频轮询。

### 7.3 云同步入口消失

重点检查：

- `static/index.html` 是否包含 `cloud-sync` 按钮。
- 是否有 `frame-cloud-sync` iframe。
- `PAGE_IDS` 是否包含 `cloud-sync`。
- `static/cloud-sync.html` 与 `static/js/cloud-sync.js` 的版本号是否已刷新。

### 7.4 下载云同步后 Key 丢失

这是覆盖式同步的风险点。检查：

- 云端同步包是否包含完整 `env` 字段。
- `apply_cloud_sync_payload()` 是否只清理同步范围内的 env。
- 覆盖前备份目录是否生成。

### 7.5 Git 状态类命令出现全局 ignore 权限警告

当前仓库可使用本地配置规避：

```powershell
git config core.excludesfile "E:/Infinite-Canvas/.git/info/exclude"
```

这只影响当前仓库，不改用户全局 Git 配置。

### 7.6 打包版数据目录出现在安装目录内部

这是需要立即修正的回归。重点检查：

- `electron/main.js` 中 `userDataRoot()` 是否仍使用 `path.dirname(installRoot())`。
- 是否误把目录改成 `path.join(installRoot(), USER_DATA_DIR_NAME)`。
- `desktop.log` 中 `installRoot=` 和 `userDataRoot=` 是否只差一级目录。
- 安装目录父级是否可写；不可写时才允许回退到 Electron 系统 userData。

### 7.7 更新后 API 配置或画布丢失

重点检查：

- `main.py` 中 `API_ENV_FILE`、`DATA_DIR`、`ASSETS_DIR`、`OUTPUT_DIR` 是否仍从 `USER_DATA_ROOT` 派生。
- `migrate_user_data_from_app_root()` 是否被删除或提前返回。
- `.migration_complete.json` 是否在目标数据目录中。
- `desktop.log` 是否显示本次启动使用了错误的数据目录。

## 8. 提交建议

推荐提交拆分：

1. 一个 merge commit，只包含 `upstream/main` 合并结果。
2. 一个本地文档或维护提交，记录本次合并经验和后续指导。

不要在 merge commit 中混入新的业务功能。这样后续回看历史时，可以清楚区分：

- 哪些来自上游。
- 哪些是本地补丁保护。
- 哪些是维护文档。

## 9. 每次合并后的最终确认

提交前至少确认：

- 没有未解决冲突文件。
- 没有 `<<<<<<<`、`=======`、`>>>>>>>`。
- `git diff --check` 通过。
- `main.py` 可以 py_compile。
- `static/js` 与 `electron` 下 JS 可以 `node --check`。
- 视频任务化和 WebDAV 云同步关键关键词仍存在。
- 用户数据持久化关键词仍存在：`InfiniteCanvas_Data`、`INFINITE_CANVAS_USER_DATA_DIR`、`USER_DATA_ROOT`、`USER_WORKFLOW_DIR`、`deleteAppDataOnUninstall`、`desktop.log`。
- `static/index.html` 没有丢 `cloud-sync`。
- `smart-canvas.js` 没有把视频任务对象当视频数组处理。
- 打包版数据目录设计仍是安装目录同级，不是安装目录内部。
- Windows 安装包文件名后缀与根目录 `VERSION` 一致。
- 后端打包使用项目 `venv`，不是全局 `pyinstaller`。
- Electron 安装包级自动更新仍只在打包版启用，并保留网页“一键更新”的源项目更新用途。
- `ELECTRON_DESKTOP.md` 已同步任何新的桌面端数据目录策略。

完成后记录本次合并中遇到的新坑，追加到本文档。
