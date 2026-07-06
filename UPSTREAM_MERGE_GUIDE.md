# 后续源项目更新合并指导

本文档用于后续从源项目 `upstream/main` 合并更新时，保护本仓库已有的本地改动。重点保护两组补丁集：

1. 视频生成接口与轮询任务化改动，详见 `VIDEO_GENERATION_POLLING_CHANGES.md`。
2. WebDAV 云同步与 API 配置同步改动，详见 `WEBDAV_CLOUD_SYNC_CHANGES.md`。

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

## 3. 高风险冲突文件处理

### 3.1 `main.py`

这是最高风险文件。上游经常修改模型、平台、任务、接口相关逻辑，本地又在同一文件里维护视频任务化和 WebDAV 云同步。

保留原则：

- 合入上游新增 provider、RunningHub、CLI、模型列表等修复。
- 同时保留视频任务化接口和云同步接口。
- 如果上游改了 `normalize_provider()`，必须确认 `video_request_mode`、`rh_apps`、`rh_workflows`、`video_models`、云同步导入的 provider 字段没有丢。
- 如果上游改了异步图片或视频任务逻辑，必须确认本地视频任务持久化和恢复仍在。

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
- 保留 `frame-cloud-sync`。
- 保留 `PAGE_IDS` 中的 `cloud-sync`。
- 保留更多设置展开逻辑。
- 合并后统一更新 `?v=` 版本号，避免加载旧页面。

检查关键词：

```text
cloud-sync
frame-cloud-sync
PAGE_IDS
```

### 3.6 静态 HTML 版本号

上游合并后常出现 `?v=` 冲突。处理原则：

- 功能入口优先于版本号本身。
- 解决冲突后，相关 HTML、JS、CSS、i18n 的 `?v=` 应统一刷新到当前版本。
- 不要因为版本号冲突删掉本地 iframe 或脚本引用。

## 4. 推荐解冲突顺序

1. 先解 `main.py`，确认后端接口和数据结构完整。
2. 再解 `static/js/api-settings.js` 与 `static/api-settings.html`，确认配置字段仍会保存。
3. 再解 `static/js/canvas.js`，确认普通画布视频 pending 逻辑。
4. 再解 `static/js/smart-canvas.js`，确认智能画布视频和 RunningHub 模型 API 的分支选择。
5. 再解 `static/index.html`，确认云同步入口不丢。
6. 最后处理静态 HTML 的 `?v=` 版本号和样式冲突。

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
- `static/index.html` 没有丢 `cloud-sync`。
- `smart-canvas.js` 没有把视频任务对象当视频数组处理。

完成后记录本次合并中遇到的新坑，追加到本文档。
