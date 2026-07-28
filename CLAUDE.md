# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 必读规范

根目录 **`AGENTS.md`** 是本仓库最详尽的协作规范（目录职责、修改规则、运行数据边界、Git 分支/提交/推送流程、验证清单），先读它再动手，本文件只做补充概览。

- 全仓库统一简体中文 + UTF-8（代码注释、文档、提交信息；提交格式 `<type>: <中文摘要>`）。
- Node 侧只用 **npm**（沿用 `package-lock.json`），本仓库不使用 pnpm/yarn，即使全局习惯是 pnpm。
- Python 一律用项目 venv：`.\venv\Scripts\python.exe`；根目录 `python/`（便携运行时）和 `packages/`（离线 wheel）是分发用途，开发不要碰。
- Git：`origin` 是可推送的 Fork；`upstream`（hero8152/Infinite-Canvas）只读，push 已设为 DISABLED，不得恢复。提交后停在本地，用户明确要求才推送。
- 面向用户的文案必须中英双语：走 `StudioI18n` / `tr()` / `trf()` / `data-i18n*`，条目补进 `static/js/i18n/*.js`。

## 常用命令

（默认环境 Windows / PowerShell）

后端开发运行（固定监听 3000 端口）：

```powershell
$env:INFINITE_CANVAS_SKIP_STATIC_SYNC = '1'   # 避免启动时改写 static/*.html 的 ?v= 缓存参数
.\venv\Scripts\python.exe main.py
```

Electron 桌面壳：`npm run desktop`

测试（无 pytest/jest 等运行器，全是标准库/裸脚本；Python 测试必须在仓库根目录运行，它们直接 `import main`）：

```powershell
.\venv\Scripts\python.exe -m unittest discover tests            # 全部 Python 测试
.\venv\Scripts\python.exe -m unittest tests.test_jimeng_models  # 单个 Python 测试
node tests\test_video_api_utils.js                              # 单个 JS 测试（成功时打印 ok）
```

检查（仓库没有 lint / typecheck / CI 配置，用以下代替，不得声称跑过不存在的检查）：

```powershell
.\venv\Scripts\python.exe -m py_compile main.py
node --check electron\main.js          # 对每个改动过的 JS 文件执行
node static\js\i18n\validate-i18n.js   # i18n 条目完整性
```

构建 / 版本（仅在任务明确要求时执行）：

```powershell
npm run sync:desktop-version   # 以 VERSION 首行为唯一版本源，同步 package.json 等
npm run build:backend          # PyInstaller 打包后端到 dist/infinite-canvas-backend
npm run build:win              # 完整 Windows 安装包（已含上面两步，勿重复先跑）
```

## 架构总览

三层结构：**FastAPI 单体后端 + 无框架静态前端 + Electron 桌面壳**。本质是本地 Web 应用（浏览器访问 `http://127.0.0.1:3000/`），桌面版只是把后端打包成 exe 再套 Electron 窗口。

### 后端：`main.py`（约 2.1 万行单文件，最高风险文件）

- 包含全部 HTTP API（约 160 个路由）+ WebSocket + 各第三方模型平台适配（OpenAI 协议 / Gemini / 方舟 / RunningHub / ModelScope / 火山引擎 / 即梦 CLI / 本地 ComfyUI）+ 视频任务轮询持久化 + 文件与用户数据管理。
- 资源双根分离是核心不变量：`APP_ROOT`（内置只读资源，如 `workflows/`）与 `USER_DATA_ROOT`（用户数据：`API/`、`data/`、`output/`、自定义工作流）。写操作永远进用户根，文件路径必须走既有安全校验，不得逃逸。
- 视频生成走本地任务化：`POST /api/canvas-video-tasks` 创建任务，持久化、重启恢复、退避轮询、终态错误识别；`video_request_mode` 兼容 `/v1/videos/generations` 与 `/v1/video/generations` 两种上游接口形态。

### 供应商插件层：`plugins/`

`main.py` 之外的协议适配出口，用于阻止单文件继续膨胀。`image_plugins/`、`video_plugins/` 每个供应商一个模块（aicost、geeknow、megabyai、sudashui、tudou 等），由 `main.py` 顶部显式导入。插件不落盘、不读 API Key、不直接解析本地路径——这些由宿主回调完成，职责边界见各目录 `README.md` / `DESIGN.md`。新增供应商适配优先写成插件，而不是往 `main.py` 加分支。

### 前端：`static/`（原生 HTML/CSS/JS，无框架无打包器）

- `static/index.html` 是 iframe 外壳：每个功能页（`zimage`、`canvas`、`smart-canvas`、`api-settings`、`cloud-sync` 等）是独立 HTML，经 iframe 加载，靠 postMessage / 父子窗口通信协作。改 DOM ID、全局函数、消息事件或 API 路径时，必须检索所有 iframe 和调用点。
- **双画布链路**：`static/js/canvas.js`（普通画布）与 `static/js/smart-canvas.js`（智能画布）是两条平行实现。改 provider、视频或任务逻辑必须两边同步检查——只修一侧是本仓库最常见的回归来源。
- 脚本靠 `<script>` 顺序加载共享全局状态；HTML 里的 `?v=` 参数是缓存失效机制，随版本统一刷新，不要手工单独改。
- 不要直接编辑 `static/vendor/`（第三方镜像，升级须同步 `static/vendor/MANIFEST.md`）。

### 桌面壳：`electron/`

`main.js` 负责启动打包后端（`dist/` 里的 PyInstaller 产物）、选择用户数据目录（安装目录同级 `InfiniteCanvas_Data`，不可写才回退系统 userData）和客户端更新（GitHub Release 为主、ModelScope 兜底）。`preload.js` 只暴露受限 IPC，必须保持 `contextIsolation: true`、`nodeIntegration: false`。

### 外围

`tools/`（Chrome 采集扩展、Photoshop UXP 插件）、`CLI/`（即梦 CLI 安装登录脚本）、`workflows/`（随应用发布的内置 ComfyUI 工作流；用户自定义工作流运行时写入用户数据目录，不得混入内置目录）。

## 按任务读文档

`AGENTS.md` 内有完整映射，最常用的：上游合并读 `UPSTREAM_MERGE_GUIDE.md`；视频任务/轮询读 `VIDEO_GENERATION_POLLING_CHANGES.md`；WebDAV / API Key 同步读 `WEBDAV_CLOUD_SYNC_CHANGES.md`；Electron / 数据目录 / 构建发布读 `ELECTRON_DESKTOP.md`。`prd.md`、`Design.md`、`Tech.md` 是历史记录，其中的分支状态和版本号不可当作当前事实。
