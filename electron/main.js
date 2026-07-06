const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { autoUpdater } = require('electron-updater');
const { Provider, parseUpdateInfo, resolveFiles } = require('electron-updater/out/providers/Provider');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const PORT = Number(process.env.INFINITE_CANVAS_PORT || 3000);
const HOST = '127.0.0.1';
const USER_DATA_DIR_NAME = 'InfiniteCanvas_Data';
const CLIENT_UPDATE_CHECK_DELAY_MS = Number(process.env.INFINITE_CANVAS_UPDATE_DELAY_MS || 15000);
const CLIENT_UPDATE_GITHUB_CONFIG = {
  provider: 'github',
  owner: 'xiaoqi0102',
  repo: 'Infinite-Canvas',
  releaseType: 'release',
};

let mainWindow = null;
let backendProcess = null;
let clientUpdateState = 'idle';
let manualUpdateCheckPending = false;
let installPromptVisible = false;
let lastDownloadProgressLog = 0;
let downloadedUpdateInfo = null;
let downloadedUpdateSource = 'github';
let activeClientUpdateSource = 'github';
let activeClientUpdateAttemptId = 0;
let clientUpdateFallbackSources = [];
let clientUpdateFailureHandled = false;
let clientUpdateReportFailures = false;
let lastClientUpdateError = null;

function trimSlashes(value) {
  return String(value || '').replace(/^\/+|\/+$/g, '');
}

function joinUrlPath(...parts) {
  return parts
    .map((part) => trimSlashes(part))
    .filter(Boolean)
    .join('/');
}

function formatVersionLike(version, template) {
  const versionParts = String(version || '').split('.');
  const templateParts = String(template || '').split('.');
  if (versionParts.length !== templateParts.length) return String(version || '');
  return templateParts
    .map((part, index) => {
      const value = versionParts[index] || '';
      if (/^\d+$/.test(value) && /^\d+$/.test(part) && part.length > value.length) {
        return value.padStart(part.length, '0');
      }
      return value;
    })
    .join('.');
}

function versionTokenFromPath(filePath) {
  const matches = String(filePath || '').match(/\d+(?:\.\d+){2}/g);
  return matches ? matches[matches.length - 1] : '';
}

class ModelScopeClientUpdateProvider extends Provider {
  constructor(configuration, updater, runtimeOptions) {
    super({ ...runtimeOptions, isUseMultipleRangeRequest: false });
    this.configuration = configuration;
    this.baseUrl = new URL('https://modelscope.cn/');
    this.apiBaseUrl = trimSlashes(configuration.apiBaseUrl || 'https://modelscope.cn/api/v1/studio');
    this.owner = trimSlashes(configuration.owner || 'xiaoqi0102');
    this.repo = trimSlashes(configuration.repo || 'Infinite-Canvas');
    this.revision = String(configuration.revision || 'master');
    this.directory = trimSlashes(configuration.directory || 'desktop-release');
  }

  get channel() {
    return this.getDefaultChannelName();
  }

  get isUseMultipleRangeRequest() {
    return false;
  }

  modelScopeFileUrl(relativePath) {
    const filePath = joinUrlPath(this.directory, relativePath);
    const url = new URL(`${this.apiBaseUrl}/${this.owner}/${this.repo}/repo`);
    url.searchParams.set('Revision', this.revision);
    url.searchParams.set('FilePath', filePath);
    return url;
  }

  pathToModelScopeUrl(filePath) {
    const value = String(filePath || '').trim();
    if (/^https?:\/\//i.test(value)) return value;
    return this.modelScopeFileUrl(value).toString();
  }

  async getLatestVersion() {
    const channelFile = `${this.channel}.yml`;
    const channelUrl = this.modelScopeFileUrl(channelFile);
    return parseUpdateInfo(await this.httpRequest(channelUrl), channelFile, channelUrl);
  }

  resolveFiles(updateInfo) {
    return resolveFiles(updateInfo, this.baseUrl, (filePath) => this.pathToModelScopeUrl(filePath));
  }

  getBlockMapFiles(baseUrl, oldVersion, newVersion) {
    const filePath = baseUrl.searchParams.get('FilePath') || '';
    const newPath = filePath ? `${filePath}.blockmap` : '';
    const versionToken = versionTokenFromPath(filePath);
    const newToken = versionToken || newVersion;
    const oldToken = versionToken ? formatVersionLike(oldVersion, versionToken) : oldVersion;
    const oldPath = filePath ? `${filePath.split(newToken).join(oldToken)}.blockmap` : '';
    return [
      oldPath ? this.modelScopeFileUrl(oldPath) : baseUrl,
      newPath ? this.modelScopeFileUrl(newPath) : baseUrl,
    ];
  }
}

function clientUpdateModelScopeConfig() {
  return {
    provider: 'custom',
    updateProvider: ModelScopeClientUpdateProvider,
    apiBaseUrl: process.env.INFINITE_CANVAS_CLIENT_UPDATE_MODELSCOPE_API_ROOT || 'https://modelscope.cn/api/v1/studio',
    owner: process.env.INFINITE_CANVAS_CLIENT_UPDATE_MODELSCOPE_OWNER || 'xiaoqi0102',
    repo: process.env.INFINITE_CANVAS_CLIENT_UPDATE_MODELSCOPE_REPO || 'Infinite-Canvas',
    revision: process.env.INFINITE_CANVAS_CLIENT_UPDATE_MODELSCOPE_REVISION || 'master',
    directory: process.env.INFINITE_CANVAS_CLIENT_UPDATE_MODELSCOPE_DIR || 'desktop-release',
  };
}

function clientUpdateSourceLabel(source) {
  return source === 'modelscope' ? 'ModelScope' : 'GitHub';
}

function clientUpdateSourceConfig(source) {
  return source === 'modelscope' ? clientUpdateModelScopeConfig() : CLIENT_UPDATE_GITHUB_CONFIG;
}

function clientUpdateSourceOrder(preferred = 'github') {
  const sources = ['github', 'modelscope'];
  return [preferred, ...sources.filter((source) => source !== preferred)];
}

function setClientUpdateSource(source) {
  activeClientUpdateSource = source;
  autoUpdater.setFeedURL(clientUpdateSourceConfig(source));
  appendClientUpdateLog('source-selected', {
    source,
    label: clientUpdateSourceLabel(source),
  });
}

function clientUpdateErrorMessage(error) {
  return error && error.message ? error.message : String(error || 'Unknown update error');
}

function resetClientUpdateFallbackState() {
  clientUpdateFallbackSources = [];
  clientUpdateFailureHandled = false;
  clientUpdateReportFailures = false;
  lastClientUpdateError = null;
  activeClientUpdateAttemptId = 0;
}

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

function showClientUpdateFailureDialog(error, phase = 'check') {
  const message = clientUpdateErrorMessage(error);
  const attemptedSources = clientUpdateSourceOrder('github').map(clientUpdateSourceLabel).join('、');
  const isDownload = phase === 'download';
  return showDesktopDialog({
    type: 'error',
    buttons: ['知道了'],
    title: isDownload ? '客户端下载失败' : '客户端更新检查失败',
    message: isDownload ? '无法下载客户端更新。' : '无法检查客户端更新。',
    detail: `已尝试更新源：${attemptedSources}\n最后错误：${message}`,
  });
}

function installDownloadedClientUpdate(info, source = downloadedUpdateSource) {
  appendClientUpdateLog('install-now', { version: updateVersion(info), source });
  stopBackend();
  setTimeout(() => {
    autoUpdater.quitAndInstall(false, true);
  }, 300);
}

async function promptInstallDownloadedClientUpdate(info, source = downloadedUpdateSource) {
  if (installPromptVisible) return;
  installPromptVisible = true;
  try {
    const version = updateVersion(info);
    const sourceLabel = clientUpdateSourceLabel(source);
    const result = await showDesktopDialog({
      type: 'info',
      buttons: ['重启并安装', '稍后'],
      defaultId: 0,
      cancelId: 1,
      title: '客户端更新已下载',
      message: version ? `Infinite Canvas ${version} 已下载完成。` : 'Infinite Canvas 客户端更新已下载完成。',
      detail: `下载源：${sourceLabel}\n重启后会安装新版客户端。用户数据保存在安装目录同级的 InfiniteCanvas_Data，不会被安装包覆盖。`,
    });
    if (result.response === 0) {
      installDownloadedClientUpdate(info, source);
    } else {
      appendClientUpdateLog('install-deferred', { version, source });
    }
  } finally {
    installPromptVisible = false;
  }
}

function checkNextClientUpdateSource() {
  const source = clientUpdateFallbackSources.shift();
  if (!source) {
    const shouldReport = manualUpdateCheckPending || clientUpdateReportFailures;
    const error = lastClientUpdateError || new Error('All client update sources failed.');
    clientUpdateState = 'idle';
    manualUpdateCheckPending = false;
    resetClientUpdateFallbackState();
    if (shouldReport) {
      showClientUpdateFailureDialog(error).catch(() => {});
    }
    return;
  }

  setClientUpdateSource(source);
  clientUpdateState = 'checking';
  clientUpdateFailureHandled = false;
  activeClientUpdateAttemptId += 1;
  const attemptId = activeClientUpdateAttemptId;
  appendClientUpdateLog('check-start', {
    source,
    attemptId,
    version: app.getVersion(),
    fallbackSources: clientUpdateFallbackSources.join(','),
  });
  autoUpdater.checkForUpdates().catch((error) => {
    handleClientUpdateSourceFailure(error, 'check', source, attemptId);
  });
}

function handleClientUpdateSourceFailure(
  error,
  phase = 'check',
  expectedSource = activeClientUpdateSource,
  expectedAttemptId = activeClientUpdateAttemptId,
) {
  if (
    phase === 'check' &&
    (expectedSource !== activeClientUpdateSource || expectedAttemptId !== activeClientUpdateAttemptId)
  ) {
    appendClientUpdateLog('stale-error-ignored', {
      source: expectedSource,
      activeSource: activeClientUpdateSource,
      attemptId: expectedAttemptId,
      activeAttemptId: activeClientUpdateAttemptId,
      message: clientUpdateErrorMessage(error),
    });
    return true;
  }
  if (phase === 'check' && clientUpdateFailureHandled) return true;
  if (phase === 'check') clientUpdateFailureHandled = true;

  const source = activeClientUpdateSource;
  const message = clientUpdateErrorMessage(error);
  lastClientUpdateError = error;
  appendClientUpdateLog('source-error', { source, phase, message });

  if (clientUpdateFallbackSources.length > 0) {
    const nextSource = clientUpdateFallbackSources[0];
    appendClientUpdateLog('source-fallback', {
      from: source,
      to: nextSource,
      phase,
      message,
    });
    checkNextClientUpdateSource();
    return true;
  }

  const shouldReport = manualUpdateCheckPending || clientUpdateReportFailures || phase === 'download';
  clientUpdateState = 'idle';
  manualUpdateCheckPending = false;
  resetClientUpdateFallbackState();
  if (shouldReport) {
    showClientUpdateFailureDialog(error, phase).catch(() => {});
  }
  return false;
}

async function promptDownloadClientUpdate(info, source = activeClientUpdateSource) {
  const version = updateVersion(info);
  const sourceLabel = clientUpdateSourceLabel(source);
  const result = await showDesktopDialog({
    type: 'info',
    buttons: ['下载更新', '稍后'],
    defaultId: 0,
    cancelId: 1,
    title: '发现客户端更新',
    message: version ? `发现 Infinite Canvas 客户端新版本 ${version}。` : '发现 Infinite Canvas 客户端新版本。',
    detail: `这会从 ${sourceLabel} 下载桌面安装包。网页里的“一键更新”仍保留用于源项目 main.py、VERSION 和 static 更新提醒。`,
  });
  if (result.response !== 0) {
    clientUpdateState = 'idle';
    manualUpdateCheckPending = false;
    resetClientUpdateFallbackState();
    appendClientUpdateLog('download-deferred', { version, source });
    return;
  }
  clientUpdateState = 'downloading';
  clientUpdateReportFailures = true;
  downloadedUpdateSource = source;
  lastDownloadProgressLog = 0;
  appendClientUpdateLog('download-start', { version, source });
  try {
    await autoUpdater.downloadUpdate();
  } catch (error) {
    appendClientUpdateLog('download-error', { source, message: error.message });
    handleClientUpdateSourceFailure(error, 'download');
  }
}

function configureClientUpdater() {
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  setClientUpdateSource('github');

  autoUpdater.on('checking-for-update', () => {
    appendClientUpdateLog('checking-for-update', { source: activeClientUpdateSource, version: app.getVersion() });
  });

  autoUpdater.on('update-available', (info) => {
    const source = activeClientUpdateSource;
    clientUpdateState = 'prompting-download';
    manualUpdateCheckPending = false;
    clientUpdateFailureHandled = false;
    appendClientUpdateLog('update-available', { source, version: updateVersion(info) });
    promptDownloadClientUpdate(info, source).catch((error) => {
      clientUpdateState = 'idle';
      resetClientUpdateFallbackState();
      appendClientUpdateLog('prompt-error', { message: error.message });
    });
  });

  autoUpdater.on('update-not-available', (info) => {
    const source = activeClientUpdateSource;
    const previousError = lastClientUpdateError;
    const shouldReport = manualUpdateCheckPending || clientUpdateReportFailures;
    clientUpdateState = 'idle';
    appendClientUpdateLog('update-not-available', { source, version: updateVersion(info) });
    if (shouldReport) {
      showDesktopDialog({
        type: 'info',
        buttons: ['知道了'],
        title: previousError ? '备用更新源未发现新版本' : '客户端已是最新',
        message: previousError ? '备用更新源当前没有可下载的新客户端版本。' : '当前客户端已是最新版本。',
        detail: `当前版本：${app.getVersion()}\n检查源：${clientUpdateSourceLabel(source)}${
          previousError ? `\n上一个源错误：${clientUpdateErrorMessage(previousError)}` : ''
        }`,
      }).catch(() => {});
    }
    manualUpdateCheckPending = false;
    resetClientUpdateFallbackState();
  });

  autoUpdater.on('download-progress', (progress) => {
    const now = Date.now();
    const percent = Math.round(progress.percent || 0);
    if (percent >= 100 || now - lastDownloadProgressLog > 30000) {
      lastDownloadProgressLog = now;
      appendClientUpdateLog('download-progress', {
        source: downloadedUpdateSource,
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
    downloadedUpdateSource = activeClientUpdateSource;
    const source = downloadedUpdateSource;
    resetClientUpdateFallbackState();
    appendClientUpdateLog('update-downloaded', { source, version: updateVersion(info) });
    promptInstallDownloadedClientUpdate(info, source).catch((error) => {
      appendClientUpdateLog('install-prompt-error', { message: error.message });
    });
  });

  autoUpdater.on('error', (error) => {
    appendClientUpdateLog('update-error', {
      source: activeClientUpdateSource,
      state: clientUpdateState,
      message: clientUpdateErrorMessage(error),
    });
    if (clientUpdateState === 'checking') {
      return;
    }
    if (clientUpdateState !== 'downloading') {
      const shouldReport = manualUpdateCheckPending || clientUpdateReportFailures;
      clientUpdateState = 'idle';
      manualUpdateCheckPending = false;
      resetClientUpdateFallbackState();
      if (shouldReport) {
        showClientUpdateFailureDialog(error).catch(() => {});
      }
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

  const [firstSource, ...fallbackSources] = clientUpdateSourceOrder('github');
  clientUpdateFallbackSources = fallbackSources;
  clientUpdateReportFailures = manual;
  lastClientUpdateError = null;
  manualUpdateCheckPending = manual;
  clientUpdateFallbackSources.unshift(firstSource);
  checkNextClientUpdateSource();
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
