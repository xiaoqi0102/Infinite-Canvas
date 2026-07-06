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

The desktop app stores writable user data in an `InfiniteCanvas_Data` folder next to the installation directory, not inside it. For example, if the app is installed at `D:\Apps\Infinite Canvas`, user data is stored at `D:\Apps\InfiniteCanvas_Data`. Electron passes this path to the backend through `INFINITE_CANVAS_USER_DATA_DIR`.

If the parent directory of the installation directory cannot create or write to the sibling data folder, Electron falls back to an `InfiniteCanvas_Data` folder under Electron's system user data directory. The legacy `INFINITE_CANVAS_BASE_DIR` variable is still passed with the same value for compatibility.

On startup, Electron also copies missing files from the old in-install `InfiniteCanvas_Data` folder into the new sibling folder when that old folder exists. The backend keeps static files and built-in workflows read-only from the packaged app, while user data such as API settings, canvases, uploads, custom workflows, history, and outputs are written under `InfiniteCanvas_Data`.

The desktop launcher appends startup diagnostics to `desktop.log` inside the active `InfiniteCanvas_Data` folder. The log records the selected data directory, install/resource paths, backend command, port, backend exit code, and startup errors. This is the first file to check when a packaged client starts with the wrong data directory or fails before the UI opens.

The NSIS installer sets `deleteAppDataOnUninstall` to `false`, so uninstalling the desktop app does not delete Electron's system app data. The primary user data is outside the install directory in the sibling `InfiniteCanvas_Data` folder and should also be preserved across reinstalls and updates.
