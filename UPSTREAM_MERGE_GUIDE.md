# 上游更新合并指南

本文是从 `upstream/main` 合并更新时的当前操作清单。历次合并记录、逐文件旧冲突案例和完整补丁地图见 [`docs/archive/UPSTREAM_MERGE_GUIDE_FULL.md`](docs/archive/UPSTREAM_MERGE_GUIDE_FULL.md)；归档仅用于追溯。

## 远程与分支

- `origin` 是可推送 Fork；`upstream` 只用于获取源项目，禁止推送或移除 `pushurl=DISABLED`。
- 上游合并必须在 `codex/merge-upstream-YYYYMMDD` 一类隔离分支进行，不在 `main` 直接解冲突。
- 上游 merge commit 只包含合并结果；本地修复、文档和版本元数据按独立目的分别提交。

```powershell
git status --short --branch
git fetch origin --prune
git fetch upstream --prune
git switch -c codex/merge-upstream-YYYYMMDD origin/main
git merge upstream/main
```

## 必须保护的本地能力

| 能力 | 关键位置 | 维护文档 |
| --- | --- | --- |
| 视频本地任务、恢复、退避与终态错误 | `main.py`、两条画布脚本、`plugins/video_plugins/` | `VIDEO_GENERATION_POLLING_CHANGES.md` |
| WebDAV 配置/Key 同步与本地备份 | `main.py`、`static/js/api-settings.js`、云同步页面 | `WEBDAV_CLOUD_SYNC_CHANGES.md` |
| 内置资源与用户数据分离、旧数据非覆盖迁移 | `main.py`、`electron/main.js` | `ELECTRON_DESKTOP.md` |
| `VERSION` 驱动的桌面版本与安装包名称 | `VERSION`、`package.json`、`scripts/` | `ELECTRON_DESKTOP.md` |
| GitHub 主源、ModelScope 兜底的客户端更新 | `electron/main.js`、preload、更新 UI | `ELECTRON_DESKTOP.md` |

同时保留 `static/index.html` 中的 `cloud-sync`、`frame-cloud-sync`、客户端更新入口及页面注册。

## 解冲突方法

1. 先列出冲突文件，不机械选择 ours/theirs。
2. 按功能判断双方语义；上游新行为与本地能力可共存时进行组合。
3. 先处理配置与小模块，再处理 `main.py`、两条画布脚本、`static/index.html` 和 `electron/main.js`。
4. HTML 中的 `?v=` 仅用于缓存失效；必须保留完整的 iframe、脚本和功能入口。
5. 新供应商协议继续位于插件层，避免把协议细节重新内联到 `main.py`。
6. 每解决一组冲突就检索相关符号、API、DOM ID、消息事件和两条画布调用点。

高风险文件：

- `main.py`
- `static/js/canvas.js`
- `static/js/smart-canvas.js`
- `static/js/api-settings.js`
- `static/index.html`
- `electron/main.js`
- `package.json`

## 合并后检查

```powershell
git diff --name-only --diff-filter=U
git diff --check
Select-String -Path main.py,static\*.html,static\js\*.js,static\js\i18n\*.js,electron\*.js -Pattern '<<<<<<<|=======|>>>>>>>'
.\venv\Scripts\python.exe -m py_compile main.py
.\venv\Scripts\python.exe -m unittest discover tests
Get-ChildItem tests\test_*.js | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "JS test failed: $($_.Name)" }
}
node static\js\i18n\validate-i18n.js
```

根据冲突范围补做：

- 视频任务提交、刷新恢复、终态失败和供应商协议冒烟测试。
- WebDAV 上传/下载、同步前备份、Key 范围与 `providers-changed` 刷新。
- Electron 数据目录选择、旧数据迁移、受限 IPC 和双源更新。
- iframe 页面入口、脚本加载及 `?v=` 缓存参数完整性。

## 提交前确认

- 工作树不存在未解决冲突或无关改动。
- `git diff` 中所有高风险文件都按语义复核。
- 合并后的测试与手工验证结果已记录。
- merge commit 不混入后续业务修复。
- 版本或发布任务以 `VERSION` 第一行为唯一版本来源。

只有需要复盘某次历史合并或旧冲突方案时，才读取完整归档。
