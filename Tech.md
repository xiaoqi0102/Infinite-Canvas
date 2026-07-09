# 源项目更新合并技术规格

## 技术环境

- 操作系统按 Windows 11 / PowerShell 执行。
- Git 远程：
  - `origin=https://github.com/xiaoqi0102/Infinite-Canvas.git`
  - `upstream=https://github.com/hero8152/Infinite-Canvas.git`
- Node.js 项目使用现有 `package-lock.json`，后续如需安装依赖使用 `npm`，不切换包管理器。
- Python 项目执行检查时使用项目内 `venv`。若不存在，执行 `python -m venv venv` 后再使用 `.\venv\Scripts\python.exe`。

## 依赖约束

本次不新增依赖。

现有 Node 依赖以 `package.json` 和 `package-lock.json` 为准：

- `electron`: `^39.2.7`
- `electron-builder`: `^26.0.12`
- `electron-updater`: `^6.8.9`

现有 Python 依赖由 `requirements.txt` 声明，未锁定精确版本：

- `fastapi`
- `uvicorn`
- `requests`
- `pydantic`
- `python-multipart`
- `httpx`
- `pillow`

Python 依赖版本以当前项目 `venv` 实际安装版本为准；本次合并不改变依赖版本策略。

## 执行命令规划

### 1. 准备隔离环境

```powershell
git status --short --branch
if (-not (Test-Path .\venv\Scripts\python.exe)) { python -m venv venv }
```

如后续检查提示 Python 依赖缺失，再按项目声明安装：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Node 依赖如缺失，再按锁文件安装：

```powershell
npm ci
```

### 2. 保存当前静态 HTML 未提交改动

```powershell
git stash push -m "codex/upstream-merge-prep-20260709" -- static/*.html
git status --short --branch
```

该 stash 只保存当前会阻塞合并的静态 HTML 改动，`prd.md`、`Design.md`、`Tech.md` 继续留在工作区作为本次维护文档。

### 3. 创建隔离合并分支

```powershell
git switch -c codex/merge-upstream-20260709 origin/main
```

如果分支名已存在，使用 `codex/merge-upstream-20260709-2`。

### 4. 合并源项目更新

```powershell
git merge upstream/main
```

预期会出现冲突，至少涉及：

- `main.py`
- `static/js/smart-canvas.js`
- `static/index.html`
- 多个 `static/*.html`

### 5. 解冲突顺序

1. `main.py`
2. `static/js/smart-canvas.js`
3. `static/gpt-chat.html`
4. `static/index.html`
5. 其他 `static/*.html`
6. `VERSION`
7. `static/update-notes.json`
8. 文档文件

## 关键合并规则

### `main.py`

必须合并上游新增修复：

- 灵境 API provider 判断与视频接口处理。
- 即梦 CLI 多参考图处理。
- 视频返回字段 `detail` 收集。
- 上游最新更新说明相关版本行为。

必须保留本地能力：

- 视频生成本地任务化。
- 终态错误识别。
- WebDAV 云同步。
- 用户数据目录迁移与拆分。
- 自定义工作流写入用户数据目录。

### `static/js/smart-canvas.js`

必须合并上游来源比例尺寸修复：

- `customRatioKey`
- `applySourceRatioToSettings(prefix)`
- `sourceImageRatioLabel(prefix)`
- `apiImageSize(currentRatio, value, currentCustomRatio, '')`

同时保留本地视频任务 pending、恢复和循环节点 UI 修复。

### `static/*.html`

缓存版本号统一由合并结果决定。不得因为冲突删除：

- `cloud-sync` 页面入口。
- `frame-cloud-sync` iframe。
- `client-update-btn` 客户端更新入口。
- 本地新增脚本和样式引用。

## 验证标准

### Git 检查

```powershell
git status --short --branch
git diff --name-only --diff-filter=U
git diff --check
```

### 冲突标记检查

```powershell
Select-String -Path main.py,static\*.html,static\js\*.js,static\js\i18n\*.js,electron\*.js -Pattern '<<<<<<<|=======|>>>>>>>'
```

### Python 检查

```powershell
.\venv\Scripts\python.exe -m py_compile main.py
.\venv\Scripts\python.exe -c "import fastapi, uvicorn, requests, pydantic, multipart, httpx, PIL; print('imports ok')"
```

### JavaScript 检查

```powershell
node --check electron\main.js
node --check scripts\build-backend.cjs
node --check scripts\sync-electron-version.cjs
Get-ChildItem -Recurse -File static\js,electron -Include *.js | ForEach-Object { node --check $_.FullName }
```

### 关键补丁存在性检查

```powershell
Select-String -Path main.py -Pattern "canvas-video-tasks|video_request_mode|CLOUD_SYNC_SCHEMA|USER_DATA_ROOT|INFINITE_CANVAS_USER_DATA_DIR|USER_WORKFLOW_DIR"
Select-String -Path static\index.html -Pattern "cloud-sync|frame-cloud-sync|client-update-btn"
Select-String -Path electron\main.js,package.json -Pattern "electron-updater|ModelScope|source-fallback|deleteAppDataOnUninstall|InfiniteCanvas_Data"
```

## 技术债与演进规划

- 当前 Python 依赖未精确锁定版本，后续可引入锁文件或离线包校验策略，降低环境漂移风险。
- 静态 HTML 里大量 `?v=` 缓存版本号容易造成重复冲突，后续可考虑统一生成或集中管理缓存版本。
- 上游和本地长期在 `main.py` 中叠加视频 provider 逻辑，后续可拆分 provider 适配层，降低每次合并冲突成本。
- `UPSTREAM_MERGE_GUIDE.md` 内容较长，后续可将高风险文件检查拆成脚本化验证，减少人工漏检。
