# Claude Code 项目入口

根目录 [`AGENTS.md`](AGENTS.md) 是本仓库唯一的协作规范。开始修改前先读取并遵循它；本文件不重复架构、验证、Git 或安全规则。

Claude Code 使用提示：

- 默认环境为 Windows 11 + PowerShell，文本使用简体中文和 UTF-8。
- Node.js 使用 `npm`；Python 使用 `.\venv\Scripts\python.exe`。
- 只按当前任务读取 `AGENTS.md` 指向的维护文档，不要预加载全部历史资料。
- 需要追溯旧实现或历次合并时，再读取 `docs/archive/`；归档内容不是当前事实来源。
- `main.py`、两条画布脚本和 Electron 主进程属于高风险区域，修改前检索完整调用链。

项目结构：FastAPI 单体后端 + 原生静态前端 + Electron 桌面外壳。
