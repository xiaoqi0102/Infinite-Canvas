# Electron Desktop Packaging

This project can be packaged as a Windows desktop client with Electron.

## Development

Run:

```bat
启动Electron开发版.bat
```

The Electron main process starts the local FastAPI backend and opens a desktop window.

## Build Installer

Run:

```bat
打包Electron桌面版.bat
```

The build flow is:

1. PyInstaller packages `main.py` plus `static/`, `workflows/`, and `VERSION` into `dist/infinite-canvas-backend/`.
2. electron-builder packages the Electron shell and embeds the backend folder as an extra resource.
3. The Windows installer is written to `release/`.

Current installer output:

```text
release/Infinite Canvas Setup 2026.6.3.exe
```

## Runtime Data

The desktop app stores writable data in Electron's user data directory instead of the install directory. Electron passes this path to the backend through `INFINITE_CANVAS_BASE_DIR`.

The backend keeps static files read-only from the packaged app, while user data such as API settings, canvases, uploads, and outputs are written under the user data directory.
