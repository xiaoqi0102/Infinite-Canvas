# Infinite Canvas 项目协作指南

## 适用范围

- 本文件位于仓库根目录，适用于整个仓库；子目录若后续新增更具体的 `AGENTS.md`，以离目标文件最近的规则为准。
- 所有文本、代码注释、文档、提交信息和交付说明优先使用简体中文与 UTF-8 编码。
- 开发和验证默认面向 Windows 11 与 PowerShell，同时不得无故破坏仓库现有的 macOS、Linux、Chrome 扩展和 Photoshop UXP 支持。

## 项目结构与事实来源

- `main.py`：FastAPI 单体后端，包含 HTTP API、WebSocket、第三方模型适配、任务轮询、文件与用户数据管理；当前约 1.8 万行，是最高风险文件。
- `static/`：无前端框架和打包器的原生 HTML/CSS/JavaScript。`static/index.html` 是 iframe 外壳；`static/js/canvas.js`、`static/js/smart-canvas.js` 均为超大脚本，修改前必须检索完整调用链。
- `electron/`：Electron 桌面外壳。主进程负责启动后端、选择用户数据目录和客户端更新；`preload.js` 只暴露受限 IPC，必须保持 `contextIsolation: true`、`nodeIntegration: false`。
- `scripts/`、`build/backend.spec`：版本同步、PyInstaller 后端打包和 Electron 构建流程。
- `workflows/`：随应用发布的内置 ComfyUI 工作流；自定义工作流运行时写入用户数据目录，不得混入内置目录。
- `tools/`、`CLI/`：Chrome 素材导入扩展、Photoshop UXP 插件、第三方 CLI 安装与登录脚本，修改时需遵守各自 README 中的接口和脚本加载顺序。

事实来源优先级：

1. 当前工作树中的代码、配置和锁文件。
2. 根目录 `VERSION`、`package.json`、`package-lock.json`、`requirements.txt`。
3. 与目标功能直接相关的维护文档。
4. 历史需求或发布记录。

`prd.md`、`Design.md`、`Tech.md` 记录过具体的上游合并和旧版本发布任务，其中的分支状态、文件数量和版本号可能已过期；不得把这些历史状态当作当前事实。版本任务以 `VERSION` 第一行作为项目版本唯一来源，`npm run sync:desktop-version` 负责同步 Electron semver、锁文件版本和安装包文件名。

## 修改前必须完成的检索

- 先运行 `git status --short --branch`，识别并保护用户已有改动。不得覆盖、格式化、暂存、回滚或提交与当前任务无关的文件。
- 优先使用 `rg` / `rg --files` 检索目标符号、API 路径、DOM ID、i18n key、配置字段、调用方、文档和验证脚本；不要只阅读局部片段就修改超大文件。
- 必须同时检查相关代码、配置、Markdown 文档、构建脚本、依赖声明和现有验证方式。若存在多个行为差异明显的方案或仍有关键不确定性，先向用户说明权衡并请求选择。
- 涉及第三方 API、SDK、CLI 或发布平台且本地资料不足时，查阅对应官方最新文档并核对废弃项；不要仅凭历史文档猜测接口。

按任务优先阅读以下文档：

- 上游合并或高风险回归：`UPSTREAM_MERGE_GUIDE.md`。
- 视频任务、轮询或接口模式：`VIDEO_GENERATION_POLLING_CHANGES.md`。
- WebDAV、API Key 或 provider 同步：`WEBDAV_CLOUD_SYNC_CHANGES.md`。
- Electron、数据目录、构建、发布或客户端更新：`ELECTRON_DESKTOP.md`。
- 用户运行方式：`新手运行与使用教程.md`、`MAC-使用说明.md`。
- CLI、Chrome 扩展、Photoshop 插件：对应目录的 `README.md`。

## 依赖与运行环境

- Node.js 必须沿用 `package-lock.json` 和 `npm`，不得切换到 pnpm/yarn。需要干净安装时使用 `npm ci`；仅在项目既有脚本明确需要时使用 `npm install`。
- Python 开发、检查和打包必须使用项目 `venv`：`.\venv\Scripts\python.exe`。若不存在，执行 `python -m venv venv`；依赖缺失时再运行 `.\venv\Scripts\python.exe -m pip install -r requirements.txt`。
- 根目录 `python/` 是便携运行时，`packages/` 是离线 wheel 集合；除非任务明确涉及分发运行时或离线包，不要修改或用它们替代开发 `venv`。
- 不擅自新增依赖。确需新增时，先说明名称、精确版本、用途、来源和影响，并同步依赖声明/锁文件；Python 动态导入或额外数据文件还需检查 `build/backend.spec` 与后端打包脚本。
- 默认不启动开发服务器。确需验证时应短暂启动、告知访问地址并在完成后关闭。
- 当前 `main.py` 直接运行时固定监听 `3000` 端口；`INFINITE_CANVAS_PORT` 目前只被 Electron 侧读取，不能假设它会改变后端监听端口。
- 直接启动 `main.py` 会在启动阶段同步 `static/*.html` 的 `?v=` 缓存参数。若只是诊断或冒烟测试且不希望产生静态文件改动，先设置：

```powershell
$env:INFINITE_CANVAS_SKIP_STATIC_SYNC = '1'
.\venv\Scripts\python.exe main.py
```

## 运行数据与生成物边界

- 不得把运行时用户数据当源码修改或提交，包括 `API/`、`data/`、`assets/`、`output/`、`history.json`、`global_config.json`、日志和更新备份；已被跟踪的种子文件只有在任务明确要求时才可修改。
- 不读取、输出或提交 `API/.env`、API Key、WebDAV 密码、访问令牌及其他用户秘密；诊断时只检查必要的键名、是否存在或脱敏结果。
- 不得手工编辑或提交 `node_modules/`、`venv/`、`dist/`、`release/`、`build/backend/`、`__pycache__/` 等依赖、缓存或构建产物。
- `build/backend.spec` 和 `build/icon.ico` 是受跟踪的构建输入，不是可删除的中间产物。
- 必须保持应用资源与用户数据分离：`APP_ROOT`/`WORKFLOW_DIR` 读取内置资源，`USER_DATA_ROOT`/`USER_WORKFLOW_DIR` 写入用户文件。
- Electron 打包版的主用户数据目录应为安装目录同级 `InfiniteCanvas_Data`；仅在同级目录不可写时回退到 Electron 系统 userData。不得让更新、覆盖安装或卸载流程删除该目录。

## 代码修改规则

### 后端 `main.py`

- 优先在现有相邻职责区域内做最小改动，不进行无关的大范围格式化、排序或重构。
- 新增/修改请求体应沿用 Pydantic 模型；接口错误使用明确的 HTTP 状态和 `detail`，文件读写显式使用 UTF-8，JSON 中文内容使用 `ensure_ascii=False`。
- 文件路径必须经过既有安全路径和根目录约束，禁止允许用户输入逃逸到 `USER_DATA_ROOT` 或允许列表之外。
- 网络调用必须设置超时，并保持现有错误转换、重试、轮询退避和终态错误判定。
- 修改 provider、视频或任务逻辑时，必须检查普通画布与智能画布两条前端链路，避免只修一侧。

### 静态前端 `static/`

- 保持现有原生 DOM、全局函数/状态和脚本加载模式，不擅自引入框架、构建工具或新依赖。
- 修改 HTML 元素 ID、全局函数、消息事件、API 路径或数据结构时，必须检索所有 iframe、父子窗口通信和调用点。
- 面向用户的新文案必须同时提供中英文。优先复用 `StudioI18n`、`data-i18n*`、`tr()`/`trf()`/`tf()`，并在 `static/js/i18n/*.js` 中补齐 zh/en 条目。
- 不要直接编辑 `static/vendor/` 中的第三方镜像文件，除非任务明确要求升级镜像；升级时同步更新 `static/vendor/MANIFEST.md`。
- `?v=` 是缓存失效参数。只有在相关资源确实变更并且任务需要时统一刷新，不能因版本冲突删除脚本、样式或 iframe 入口。

### Electron 与构建

- IPC 只通过受限 preload 暴露，主进程 handler 必须校验调用来源和输入；不要把 Node/Electron 能力直接暴露给网页。
- 保持 GitHub Release 为 `electron-builder` 主发布源，ModelScope 为 `electron/main.js` 的运行时兜底源。
- 保持 `deleteAppDataOnUninstall: false`、`allowToChangeInstallationDirectory: true` 和后端 `extraResources` 配置。
- 只有版本或发布任务才运行 `npm run sync:desktop-version`；该命令可能修改 `package.json` 和 `package-lock.json`。
- 未经用户明确要求，不构建安装包、不创建 Release、不上传产物。

## 必须保护的本地能力

涉及上游合并或高风险文件时，不能机械选择某一侧文本，必须按语义保留以下能力：

- 视频生成本地任务化：`POST /api/canvas-video-tasks`、`GET /api/canvas-video-tasks/{task_id}`、任务持久化、重启恢复、退避轮询和终态错误识别。
- `video_request_mode` 对 `/v1/videos/generations` 与 `/v1/video/generations` 的兼容。
- WebDAV API 配置同步、同步前本地备份、Key 同步范围和前端 `providers-changed` 刷新。
- `APP_ROOT` 与 `USER_DATA_ROOT` 分离、自定义工作流写入用户目录、旧用户数据非覆盖迁移。
- Electron 安装包级更新的 GitHub 优先/ModelScope 兜底、受限 IPC、下载进度和用户数据保护。
- `static/index.html` 中的 `cloud-sync`、`frame-cloud-sync`、客户端更新入口及相关页面注册。

高风险文件包括：`main.py`、`static/js/canvas.js`、`static/js/smart-canvas.js`、`static/js/api-settings.js`、`static/index.html`、`electron/main.js`、`package.json`。修改这些文件后应执行更完整的检索和冒烟验证。

## 验证要求

仓库当前没有正式的单元测试目录、CI、ESLint、Prettier、Ruff 或类型检查配置；不得声称这些检查已通过。按变更范围执行以下既有验证，并如实报告未运行项。

基础检查：

```powershell
git diff --check
git diff --name-only --diff-filter=U
Select-String -Path main.py,static\*.html,static\js\*.js,static\js\i18n\*.js,electron\*.js -Pattern '<<<<<<<|=======|>>>>>>>'
```

Python 后端变更：

```powershell
.\venv\Scripts\python.exe -m py_compile main.py
.\venv\Scripts\python.exe -c "import fastapi, uvicorn, requests, pydantic, multipart, httpx, PIL; print('imports ok')"
```

JavaScript/Electron 变更：

```powershell
node --check electron\main.js
node --check electron\preload.js
node --check scripts\build-backend.cjs
node --check scripts\sync-electron-version.cjs
Get-ChildItem -Recurse -File static\js,electron -Include *.js | ForEach-Object {
    node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "JS syntax check failed: $($_.FullName)" }
}
```

i18n 或用户文案变更：

```powershell
node static\js\i18n\validate-i18n.js
```

版本同步任务：

```powershell
npm run sync:desktop-version
```

仅验证/构建后端包：

```powershell
npm run build:backend
```

完整 Windows 安装包构建：

```powershell
npm run build:win
```

`build:win` 已自动执行版本同步和后端打包，不要在它之前重复运行前两条命令。构建命令耗时且会生成大量忽略文件，只在任务明确需要构建/发布时执行。前端交互、视频 pending 恢复、WebDAV、Electron 用户数据和客户端更新没有自动化覆盖，相关修改必须依据维护文档执行最小必要的手工冒烟测试。

## Git 仓库与远程关系

- 本地项目源目录：`E:\Infinite-Canvas`。
- `origin` 是维护者自己的 Fork，可拉取和推送：`https://github.com/xiaoqi0102/Infinite-Canvas.git`。本地 `main` 跟踪 `origin/main`，它也是默认集成与推送目标。
- `upstream` 是源项目仓库，只用于拉取和合并：`https://github.com/hero8152/Infinite-Canvas.git`。不得向 `upstream` 推送，也不得删除或覆盖当前的 `pushurl=DISABLED` 防护。
- 比较远程状态前先刷新引用：普通任务至少执行 `git fetch origin --prune`；涉及源项目同步时还要执行 `git fetch upstream --prune`。不得把本地远程跟踪分支的旧提交差异写成长期事实。
- 当前本地 `main` 跟踪 `origin/main`，因此在 `main` 上执行无参数 `git pull` 只会同步 Fork，不会同步 `upstream/main`；其他分支必须先检查各自 upstream 配置。不得使用 `git pull upstream main` 直接在当前 `main` 集成源项目。

## 分支、提交与推送规则

- 每完成一项独立修改并通过对应验证后，必须只暂存该项修改涉及的明确文件并立即创建提交，不得把无关改动混入提交。
- 修改或实现请求仅自动授权上述暂存与提交，不构成推送授权。完成提交后必须停留在本地，只有用户后续明确输入推送指令时才可推送；未经明确要求，不推送、不打 tag、不创建 Release、不删除远程分支、不改写历史。
- 普通功能或维护任务优先从最新 `origin/main` 创建 `codex/<类型>-<简短描述>` 分支。上游同步必须使用 `codex/merge-upstream-YYYYMMDD` 一类隔离分支，不在 `main` 上直接解冲突。
- 上游 merge commit 只包含 `upstream/main` 合并结果；本地业务修复、冲突后的额外调整、文档维护和版本/发布元数据按独立逻辑目的分别提交。
- 默认提交信息格式为 `<type>: <简体中文摘要>`。可用类型：`feat`、`fix`、`docs`、`refactor`、`test`、`chore`、`build`、`ci`、`perf`、`merge`、`revert`。摘要必须说明具体结果，不使用“修复bug”“更新文件”等含糊表述。
- 只使用 `git add -- <明确文件路径...>` 暂存当前任务文件，默认禁止 `git add .` 和 `git add -A`。若同一文件混有用户改动，只有能安全审查分块时才使用 `git add -p`，否则先向用户确认。
- 提交前必须核对：

```powershell
git status --short --branch
git diff
git diff --cached --name-status
git diff --cached
git diff --cached --check
git diff --name-only --diff-filter=U
```

- 提交前还必须运行与变更范围匹配的项目验证。必要检查失败时停止，不得提交或推送；提交后使用 `git show --stat --oneline HEAD` 复核范围。
- 当用户明确要求“推送”但没有指定目标分支时，默认将已完成并通过验证的任务分支合并回本地 `main`，再推送到 Fork 的 `origin/main`。默认保留任务分支中的原子提交，不 squash、不 rebase；是否产生额外 merge commit 由分支拓扑决定。
- 任务分支成功合并回本地 `main` 后，必须先完成合并后验证；验证通过后使用 `git branch -d <task-branch>` 删除该本地任务分支。合并失败、验证失败或分支未完全合并时不得删除，不得使用 `-D` 强制删除；删除远程分支仍须用户另行明确授权。
- 默认推送流程：

```powershell
git fetch origin --prune
git switch main
git status --short --branch
git log --left-right --graph --oneline main...origin/main
# main 仅落后 origin/main 时：
git merge --ff-only origin/main
# 合并已经验证的任务分支：
git merge <task-branch>
# 在 main 上再次执行必要验证并核对待推送提交：
git log --oneline origin/main..main
git push origin main
```

- 执行上述流程前，工作树和暂存区必须完全干净，并核对所有未跟踪文件；`git status --porcelain` 应无输出。若存在用户改动，不自动 stash、不带着脏工作区切换或合并，应停止并请求用户处理或授权使用独立 worktree。
- `git log --oneline origin/main..main` 的输出必须只包含本任务已审查、已批准的提交；若包含本地 `main` 原有的无关提交，停止推送并向用户确认，不能一并带入 `origin/main`。
- 若 `main` 与 `origin/main` 已分叉、合并出现冲突、检查失败或无法安全切换，停止并向用户报告，不擅自 rebase、强推或丢弃改动。
- 只有用户明确指定推送任务分支、创建 PR 或使用其他目标时，才偏离默认的 `origin/main` 流程。不得向 `upstream` 推送；不得使用 `--force` 或 `--force-with-lease`，除非用户逐项明确批准改写历史。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令处理用户改动。不得 amend、rebase 或 cherry-pick 已共享提交，除非用户明确要求并确认影响。
- 行为、接口、构建或发布流程变化时，同步维护对应长期文档；保留 `LICENSE` 和 README 中的版权/使用限制，不自行改写法律含义。
- 交付时列出实际修改文件、已运行检查、失败或未运行检查、已知限制和剩余风险。
