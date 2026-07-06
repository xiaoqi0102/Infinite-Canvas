const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { autoUpdater } = require('electron-updater');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const PORT = Number(process.env.INFINITE_CANVAS_PORT || 3000);
const HOST = '127.0.0.1';
const USER_DATA_DIR_NAME = 'InfiniteCanvas_Data';
const CLIENT_UPDATE_CHECK_DELAY_MS = Number(process.env.INFINITE_CANVAS_UPDATE_DELAY_MS || 15000);

let mainWindow = null;
let backendProcess = null;
let clientUpdateState = 'idle';
let manualUpdateCheckPending = false;
let installPromptVisible = false;
let lastDownloadProgressLog = 0;
let downloadedUpdateInfo = null;

function appRoot() {
  return app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
}

function installRoot() {
  return app.isPackaged ? path.dirname(process.resourcesPath) : appRoot();
}

function samePath(left, right) {
  return path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();
}

function canWriteDirectory(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    const probe = path.join(dir, `.write-test-${process.pid}-${Date.now()}`);
    fs.writeFileSync(probe, 'ok');
    fs.unlinkSync(probe);
    return true;
  } catch (_) {
    return false;
  }
}

function copyMissingRecursive(source, target) {
  if (!fs.existsSync(source)) return;
  const stat = fs.statSync(source);
  if (stat.isDirectory()) {
    if (fs.existsSync(target) && !fs.statSync(target).isDirectory()) return;
    fs.mkdirSync(target, { recursive: true });
    for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
      copyMissingRecursive(path.join(source, entry.name), path.join(target, entry.name));
    }
    return;
  }
  if (stat.isFile() && !fs.existsSync(target)) {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(source, target);
  }
}

function migrateLegacyInstallData(targetDir) {
  const legacyDir = path.join(installRoot(), USER_DATA_DIR_NAME);
  if (samePath(legacyDir, targetDir) || !fs.existsSync(legacyDir)) return;
  try {
    copyMissingRecursive(legacyDir, targetDir);
  } catch (_) {
    // 数据目录迁移失败不应阻断应用启动；后端仍会使用新的可写目录。
  }
}

function appendRuntimeLog(dataRoot, event, details = {}) {
  try {
    fs.mkdirSync(dataRoot, { recursive: true });
    const detailText = Object.entries(details)
      .filter(([, value]) => value !== undefined && value !== null && value !== '')
      .map(([key, value]) => `${key}=${String(value)}`)
      .join(' ');
    const line = `[${new Date().toISOString()}] ${event}${detailText ? ` ${detailText}` : ''}\n`;
    fs.appendFileSync(path.join(dataRoot, 'desktop.log'), line, 'utf8');
  } catch (_) {}
}

function appendClientUpdateLog(event, details = {}) {
  if (!app.isPackaged) return;
  try {
    appendRuntimeLog(userDataRoot(), `client-update-${event}`, details);
  } catch (_) {}
}

function dialogParentWindow() {
  return mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
}

function showDesktopDialog(options) {
  const parent = dialogParentWindow();
  return parent ? dialog.showMessageBox(parent, options) : dialog.showMessageBox(options);
}

function updateVersion(info) {
  return info && info.version ? String(info.version) : '';
}

function userDataRoot() {
  if (!app.isPackaged) {
    return appRoot();
  }
  const installSiblingDataDir = path.join(path.dirname(installRoot()), USER_DATA_DIR_NAME);
  if (canWriteDirectory(installSiblingDataDir)) {
    migrateLegacyInstallData(installSiblingDataDir);
    return installSiblingDataDir;
  }
  const fallbackDir = path.join(app.getPath('userData'), USER_DATA_DIR_NAME);
  fs.mkdirSync(fallbackDir, { recursive: true });
  migrateLegacyInstallData(fallbackDir);
  return fallbackDir;
}

function backendExecutable() {
  const exeName = process.platform === 'win32' ? 'infinite-canvas-backend.exe' : 'infinite-canvas-backend';
  return path.join(process.resourcesPath, 'backend', exeName);
}

function pythonExecutable(root) {
  const bundled = path.join(root, 'python', process.platform === 'win32' ? 'python.exe' : 'bin/python3');
  if (fs.existsSync(bundled)) return bundled;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function waitForServer(timeoutMs = 45000) {
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    const probe = () => {
      const socket = net.createConnection({ host: HOST, port: PORT });
      socket.once('connect', () => {
        socket.end();
        resolve();
      });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() - startedAt > timeoutMs) {
          reject(new Error(`Backend did not start within ${Math.round(timeoutMs / 1000)} seconds.`));
          return;
        }
        setTimeout(probe, 500);
      });
    };

    probe();
  });
}

function startBackend() {
  const root = appRoot();
  const dataRoot = userDataRoot();
  appendRuntimeLog(dataRoot, 'desktop-start', {
    packaged: app.isPackaged,
    appRoot: root,
    installRoot: installRoot(),
    resourcesPath: app.isPackaged ? process.resourcesPath : '',
    userDataRoot: dataRoot,
    port: PORT,
  });
  const env = {
    ...process.env,
    INFINITE_CANVAS_USER_DATA_DIR: dataRoot,
    INFINITE_CANVAS_BASE_DIR: dataRoot,
    INFINITE_CANVAS_PORT: String(PORT),
    INFINITE_CANVAS_SKIP_STATIC_SYNC: '1',
    PYTHONIOENCODING: 'utf-8',
  };

  let command;
  let args;
  let cwd;

  if (app.isPackaged && fs.existsSync(backendExecutable())) {
    command = backendExecutable();
    args = [];
    cwd = path.dirname(command);
  } else {
    command = pythonExecutable(root);
    args = [path.join(root, 'main.py')];
    cwd = root;
  }

  appendRuntimeLog(dataRoot, 'backend-spawn', {
    command,
    args: args.join(' '),
    cwd,
  });

  backendProcess = spawn(command, args, {
    cwd,
    env,
    windowsHide: true,
    stdio: app.isPackaged ? 'ignore' : 'inherit',
  });

  backendProcess.on('exit', (code, signal) => {
    appendRuntimeLog(dataRoot, 'backend-exit', { code, signal });
    backendProcess = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend-exited', { code, signal });
    }
  });

  backendProcess.on('error', (error) => {
    appendRuntimeLog(dataRoot, 'backend-error', { message: error.message });
    dialog.showErrorBox('Infinite Canvas 启动失败', error.message);
  });
}

function installDownloadedClientUpdate(info) {
  appendClientUpdateLog('install-now', { version: updateVersion(info) });
  stopBackend();
  setTimeout(() => {
    autoUpdater.quitAndInstall(false, true);
  }, 300);
}

async function promptInstallDownloadedClientUpdate(info) {
  if (installPromptVisible) return;
  installPromptVisible = true;
  try {
    const version = updateVersion(info);
    const result = await showDesktopDialog({
      type: 'info',
      buttons: ['重启并安装', '稍后'],
      defaultId: 0,
      cancelId: 1,
      title: '客户端更新已下载',
      message: version ? `Infinite Canvas ${version} 已下载完成。` : 'Infinite Canvas 客户端更新已下载完成。',
      detail: '重启后会安装新版客户端。用户数据保存在安装目录同级的 InfiniteCanvas_Data，不会被安装包覆盖。',
    });
    if (result.response === 0) {
      installDownloadedClientUpdate(info);
    } else {
      appendClientUpdateLog('install-deferred', { version });
    }
  } finally {
    installPromptVisible = false;
  }
}

async function promptDownloadClientUpdate(info) {
  const version = updateVersion(info);
  const result = await showDesktopDialog({
    type: 'info',
    buttons: ['下载更新', '稍后'],
    defaultId: 0,
    cancelId: 1,
    title: '发现客户端更新',
    message: version ? `发现 Infinite Canvas 客户端新版本 ${version}。` : '发现 Infinite Canvas 客户端新版本。',
    detail: '这会从 GitHub Release 下载桌面安装包。网页里的“一键更新”仍保留用于源项目 main.py、VERSION 和 static 更新提醒。',
  });
  if (result.response !== 0) {
    clientUpdateState = 'idle';
    appendClientUpdateLog('download-deferred', { version });
    return;
  }
  clientUpdateState = 'downloading';
  lastDownloadProgressLog = 0;
  appendClientUpdateLog('download-start', { version });
  try {
    await autoUpdater.downloadUpdate();
  } catch (error) {
    clientUpdateState = 'idle';
    appendClientUpdateLog('download-error', { message: error.message });
    await showDesktopDialog({
      type: 'error',
      buttons: ['知道了'],
      title: '客户端下载失败',
      message: '无法下载客户端更新。',
      detail: error.message,
    });
  }
}

function configureClientUpdater() {
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;

  autoUpdater.on('checking-for-update', () => {
    appendClientUpdateLog('checking-for-update', { version: app.getVersion() });
  });

  autoUpdater.on('update-available', (info) => {
    clientUpdateState = 'prompting-download';
    manualUpdateCheckPending = false;
    appendClientUpdateLog('update-available', { version: updateVersion(info) });
    promptDownloadClientUpdate(info).catch((error) => {
      clientUpdateState = 'idle';
      appendClientUpdateLog('prompt-error', { message: error.message });
    });
  });

  autoUpdater.on('update-not-available', (info) => {
    clientUpdateState = 'idle';
    appendClientUpdateLog('update-not-available', { version: updateVersion(info) });
    if (manualUpdateCheckPending) {
      showDesktopDialog({
        type: 'info',
        buttons: ['知道了'],
        title: '客户端已是最新',
        message: '当前客户端已是最新版本。',
        detail: `当前版本：${app.getVersion()}`,
      }).catch(() => {});
    }
    manualUpdateCheckPending = false;
  });

  autoUpdater.on('download-progress', (progress) => {
    const now = Date.now();
    const percent = Math.round(progress.percent || 0);
    if (percent >= 100 || now - lastDownloadProgressLog > 30000) {
      lastDownloadProgressLog = now;
      appendClientUpdateLog('download-progress', {
        percent,
        transferred: progress.transferred,
        total: progress.total,
      });
    }
  });

  autoUpdater.on('update-downloaded', (info) => {
    clientUpdateState = 'downloaded';
    manualUpdateCheckPending = false;
    downloadedUpdateInfo = info;
    appendClientUpdateLog('update-downloaded', { version: updateVersion(info) });
    promptInstallDownloadedClientUpdate(info).catch((error) => {
      appendClientUpdateLog('install-prompt-error', { message: error.message });
    });
  });

  autoUpdater.on('error', (error) => {
    const wasManual = manualUpdateCheckPending;
    clientUpdateState = 'idle';
    manualUpdateCheckPending = false;
    appendClientUpdateLog('update-error', { message: error.message });
    if (wasManual) {
      showDesktopDialog({
        type: 'error',
        buttons: ['知道了'],
        title: '客户端更新检查失败',
        message: '无法检查客户端更新。',
        detail: error.message,
      }).catch(() => {});
    }
  });
}

function checkForClientUpdates(options = {}) {
  const manual = !!options.manual;
  if (!app.isPackaged) {
    appendClientUpdateLog('skip-dev-mode', { manual });
    if (manual) {
      showDesktopDialog({
        type: 'info',
        buttons: ['知道了'],
        title: '开发模式不检查客户端更新',
        message: '客户端自动更新只在打包安装版中启用。',
      }).catch(() => {});
    }
    return;
  }

  if (clientUpdateState === 'checking') {
    manualUpdateCheckPending = manualUpdateCheckPending || manual;
    return;
  }
  if (clientUpdateState === 'prompting-download') {
    return;
  }
  if (clientUpdateState === 'downloading') {
    if (manual) {
      showDesktopDialog({
        type: 'info',
        buttons: ['知道了'],
        title: '客户端更新下载中',
        message: '客户端更新正在下载，请稍候。',
      }).catch(() => {});
    }
    return;
  }
  if (clientUpdateState === 'downloaded') {
    promptInstallDownloadedClientUpdate(downloadedUpdateInfo || {}).catch(() => {});
    return;
  }

  clientUpdateState = 'checking';
  manualUpdateCheckPending = manual;
  autoUpdater.checkForUpdates().catch((error) => {
    const wasManual = manualUpdateCheckPending;
    clientUpdateState = 'idle';
    manualUpdateCheckPending = false;
    appendClientUpdateLog('check-error', { message: error.message });
    if (wasManual) {
      showDesktopDialog({
        type: 'error',
        buttons: ['知道了'],
        title: '客户端更新检查失败',
        message: '无法检查客户端更新。',
        detail: error.message,
      }).catch(() => {});
    }
  });
}

function scheduleClientUpdateCheck() {
  if (!app.isPackaged) {
    appendClientUpdateLog('disabled', { reason: 'not-packaged' });
    return;
  }
  setTimeout(() => checkForClientUpdates({ manual: false }), CLIENT_UPDATE_CHECK_DELAY_MS);
}

function registerClientUpdateIpc() {
  ipcMain.handle('client-update:check', (event) => {
    if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents) {
      appendClientUpdateLog('ipc-denied');
      return { ok: false, error: 'unauthorized' };
    }
    checkForClientUpdates({ manual: true });
    return {
      ok: true,
      packaged: app.isPackaged,
      state: clientUpdateState,
      version: app.getVersion(),
    };
  });
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1100,
    minHeight: 720,
    autoHideMenuBar: true,
    backgroundColor: '#111111',
    title: 'Infinite Canvas',
    icon: path.join(__dirname, '..', 'static', 'images', 'logo.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  await mainWindow.loadURL(`http://${HOST}:${PORT}/`);
}

function stopBackend() {
  if (!backendProcess) return;
  const child = backendProcess;
  backendProcess = null;
  child.kill();
}

app.whenReady().then(async () => {
  try {
    configureClientUpdater();
    registerClientUpdateIpc();
    startBackend();
    await waitForServer();
    await createWindow();
    scheduleClientUpdateCheck();
  } catch (error) {
    dialog.showErrorBox('Infinite Canvas 启动失败', error.message);
    app.quit();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('before-quit', stopBackend);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
