const { app, BrowserWindow, dialog, Menu, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const PORT = Number(process.env.INFINITE_CANVAS_PORT || 3000);
const HOST = '127.0.0.1';
const USER_DATA_DIR_NAME = 'InfiniteCanvas_Data';

let mainWindow = null;
let backendProcess = null;

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
    Menu.setApplicationMenu(null);
    startBackend();
    await waitForServer();
    await createWindow();
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
