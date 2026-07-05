const { app, BrowserWindow, dialog, Menu, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const PORT = Number(process.env.INFINITE_CANVAS_PORT || 3000);
const HOST = '127.0.0.1';

let mainWindow = null;
let backendProcess = null;

function appRoot() {
  return app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
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
  const env = {
    ...process.env,
    INFINITE_CANVAS_BASE_DIR: app.getPath('userData'),
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

  backendProcess = spawn(command, args, {
    cwd,
    env,
    windowsHide: true,
    stdio: app.isPackaged ? 'ignore' : 'inherit',
  });

  backendProcess.on('exit', (code, signal) => {
    backendProcess = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend-exited', { code, signal });
    }
  });

  backendProcess.on('error', (error) => {
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
