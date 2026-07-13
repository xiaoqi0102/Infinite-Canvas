const { app, BrowserWindow, dialog, ipcMain, net: electronNet, shell } = require('electron');
const { autoUpdater } = require('electron-updater');
const { Provider, parseUpdateInfo, resolveFiles } = require('electron-updater/out/providers/Provider');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const nodeNet = require('node:net');
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
let clientUpdateProgressWindow = null;
let latestClientUpdateProgressPayload = null;
let clientUpdateProgressVersion = '';
let clientUpdateProgressWindowReady = false;
let clientUpdateDownloadFailureHandled = false;
let lastDownloadProgressUiUpdate = 0;
let pendingClientUpdatePrompt = null;
let clientUpdateDownloadApproved = false;

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

function normalizeClientUpdateSource(source) {
  return source === 'modelscope' ? 'modelscope' : 'github';
}

function clientUpdateSourceConfig(source) {
  return source === 'modelscope' ? clientUpdateModelScopeConfig() : CLIENT_UPDATE_GITHUB_CONFIG;
}

function clientUpdateSourceOrder(preferred = 'github') {
  preferred = normalizeClientUpdateSource(preferred);
  const sources = ['github', 'modelscope'];
  return [preferred, ...sources.filter((source) => source !== preferred)];
}

function setClientUpdateSource(source) {
  source = normalizeClientUpdateSource(source);
  activeClientUpdateSource = source;
  autoUpdater.setFeedURL(clientUpdateSourceConfig(source));
  appendClientUpdateLog('source-selected', {
    source,
    label: clientUpdateSourceLabel(source),
  });
}

function clientUpdateModelScopeFileUrl(relativePath) {
  const config = clientUpdateModelScopeConfig();
  const apiBaseUrl = trimSlashes(config.apiBaseUrl || 'https://modelscope.cn/api/v1/studio');
  const url = new URL(`${apiBaseUrl}/${trimSlashes(config.owner)}/${trimSlashes(config.repo)}/repo`);
  url.searchParams.set('Revision', String(config.revision || 'master'));
  url.searchParams.set('FilePath', joinUrlPath(config.directory, relativePath));
  return url.toString();
}

function clientUpdateConnectivityTargets() {
  const githubRepo = `${CLIENT_UPDATE_GITHUB_CONFIG.owner}/${CLIENT_UPDATE_GITHUB_CONFIG.repo}`;
  const modelScopeConfig = clientUpdateModelScopeConfig();
  const modelScopeRepo = `${trimSlashes(modelScopeConfig.owner)}/${trimSlashes(modelScopeConfig.repo)}`;
  return [
    {
      id: 'github-release-api',
      name: 'GitHub Release 更新信息',
      url: `https://api.github.com/repos/${githubRepo}/releases/latest`,
      source: 'github',
      required: true,
    },
    {
      id: 'github-latest-yml',
      name: 'GitHub 安装包元数据',
      url: `https://github.com/${githubRepo}/releases/latest/download/latest.yml`,
      source: 'github',
      required: true,
    },
    {
      id: 'github-release-page',
      name: 'GitHub Release 页面',
      url: `https://github.com/${githubRepo}/releases/latest`,
      source: 'github',
      required: false,
    },
    {
      id: 'modelscope-latest-yml',
      name: 'ModelScope 安装包元数据',
      url: clientUpdateModelScopeFileUrl('latest.yml'),
      source: 'modelscope',
      required: true,
    },
    {
      id: 'modelscope-space-page',
      name: 'ModelScope 空间页面',
      url: `https://modelscope.cn/studios/${modelScopeRepo}`,
      source: 'modelscope',
      required: false,
    },
    {
      id: 'modelscope-home',
      name: 'ModelScope 主页',
      url: 'https://modelscope.cn/',
      source: 'modelscope',
      required: false,
    },
    {
      id: 'google-connectivity',
      name: 'Google 连通性',
      url: 'https://www.google.com/generate_204',
      source: 'reference',
      required: false,
    },
  ];
}

async function probeClientUpdateConnectivity(targetId) {
  const target = clientUpdateConnectivityTargets().find((item) => item.id === targetId);
  if (!target) throw new Error('unknown-target');
  const startedAt = Date.now();
  const result = {
    ...target,
    ok: false,
    status: 0,
    elapsed_ms: 0,
    error: '',
    timed_out: false,
  };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 6000);
  try {
    const response = await electronNet.fetch(target.url, {
      method: 'GET',
      redirect: 'follow',
      signal: controller.signal,
      headers: { 'User-Agent': 'Infinite-Canvas-Desktop-Updater' },
    });
    result.status = response.status;
    result.ok = response.status >= 200 && response.status < 400;
    if (!result.ok) result.error = `HTTP ${response.status} ${response.statusText || ''}`.trim();
    try {
      if (response.body) await response.body.cancel();
    } catch (_) {}
  } catch (error) {
    if (error && error.name === 'AbortError') {
      result.timed_out = true;
      result.error = '连接超时（超过 6s）';
    } else {
      result.error = clientUpdateErrorMessage(error);
    }
  } finally {
    clearTimeout(timeout);
    result.elapsed_ms = Date.now() - startedAt;
  }
  appendClientUpdateLog('connectivity-probe', {
    target: target.id,
    source: target.source,
    ok: result.ok,
    status: result.status,
    elapsedMs: result.elapsed_ms,
  });
  return result;
}

function clientUpdateErrorMessage(error) {
  return error && error.message ? error.message : String(error || 'Unknown update error');
}

function clientUpdateReleaseNotes(info) {
  const notes = info && info.releaseNotes;
  if (Array.isArray(notes)) {
    return notes
      .map((item) => (typeof item === 'string' ? item : item && item.note))
      .filter(Boolean)
      .map(String);
  }
  if (typeof notes === 'string') {
    return notes
      .replace(/<[^>]+>/g, ' ')
      .split(/\r?\n/)
      .map((item) => item.replace(/^[-*•\s]+/, '').trim())
      .filter(Boolean);
  }
  return [];
}

function resolveClientUpdatePrompt(action = 'later', source = '') {
  const pending = pendingClientUpdatePrompt;
  pendingClientUpdatePrompt = null;
  if (pending) {
    pending.resolve({
      action: action === 'download' ? 'download' : 'later',
      source: normalizeClientUpdateSource(source || pending.source),
    });
  }
}

function showClientUpdateAvailablePrompt(info, source) {
  if (!mainWindow || mainWindow.isDestroyed()) return Promise.resolve('later');
  resolveClientUpdatePrompt('later');
  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const payload = {
    requestId,
    version: updateVersion(info),
    currentVersion: app.getVersion(),
    source,
    sourceLabel: clientUpdateSourceLabel(source),
    fallbackSource: source === 'github' ? 'modelscope' : 'github',
    fallbackSourceLabel: source === 'github' ? 'ModelScope' : 'GitHub',
    releaseNotes: clientUpdateReleaseNotes(info).slice(0, 8),
    connectivityTargets: clientUpdateConnectivityTargets(),
  };
  mainWindow.show();
  mainWindow.focus();
  return new Promise((resolve) => {
    pendingClientUpdatePrompt = { requestId, source, resolve };
    mainWindow.webContents.send('client-update:available', payload);
  });
}

function resetClientUpdateFallbackState() {
  clientUpdateFallbackSources = [];
  clientUpdateFailureHandled = false;
  clientUpdateReportFailures = false;
  lastClientUpdateError = null;
  activeClientUpdateAttemptId = 0;
  clientUpdateDownloadApproved = false;
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

function clampProgressPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(100, number));
}

function formatClientUpdateBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = number;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 || size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
}

function clientUpdateProgressHtml() {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>正在下载客户端更新</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f8fafc;
      color: #111827;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      overflow: hidden;
      user-select: none;
    }
    .wrap {
      min-height: 100vh;
      padding: 20px 22px 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }
    .status {
      margin: 0 0 5px;
      font-size: 15px;
      line-height: 1.35;
      font-weight: 800;
      color: #0f172a;
    }
    .source,
    .detail {
      margin: 0;
      color: #64748b;
      font-size: 12px;
      line-height: 1.45;
      font-weight: 650;
    }
    .percent {
      flex: 0 0 auto;
      min-width: 58px;
      text-align: right;
      color: #2563eb;
      font-size: 22px;
      line-height: 1;
      font-weight: 900;
      font-variant-numeric: tabular-nums;
    }
    .track {
      width: 100%;
      height: 10px;
      overflow: hidden;
      border-radius: 999px;
      background: #e2e8f0;
      box-shadow: inset 0 1px 2px rgba(15, 23, 42, .12);
    }
    .bar {
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #2563eb, #38bdf8);
      transition: width .18s ease-out;
    }
    .meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      color: #475569;
      font-size: 12px;
      line-height: 1.45;
      font-weight: 750;
      font-variant-numeric: tabular-nums;
    }
    .speed {
      flex: 0 0 auto;
      color: #2563eb;
    }
    body.is-indeterminate .bar {
      width: 46%;
      animation: indeterminate 1.15s ease-in-out infinite alternate;
    }
    body.is-done .bar {
      background: linear-gradient(90deg, #16a34a, #22c55e);
    }
    body.is-done .percent,
    body.is-done .speed {
      color: #16a34a;
    }
    @keyframes indeterminate {
      from { transform: translateX(-70%); }
      to { transform: translateX(126%); }
    }
    @media (prefers-reduced-motion: reduce) {
      .bar { transition: none; animation: none !important; transform: none !important; }
    }
  </style>
</head>
<body class="is-indeterminate">
  <div class="wrap">
    <div class="head">
      <div>
        <p class="status" id="status">正在准备下载客户端更新</p>
        <p class="source" id="source">下载源：-</p>
      </div>
      <div class="percent" id="percent">--%</div>
    </div>
    <div class="track" aria-hidden="true">
      <div class="bar" id="bar"></div>
    </div>
    <div class="meta">
      <span id="size">正在连接下载源...</span>
      <span class="speed" id="speed">正在获取速度...</span>
    </div>
    <p class="detail" id="detail">可关闭此窗口，下载会继续；完成后会再次询问是否重启并安装。</p>
  </div>
  <script>
    function setText(id, value) {
      var element = document.getElementById(id);
      if (element) element.textContent = value || '';
    }
    window.__setClientUpdateProgress = function(payload) {
      payload = payload || {};
      var percent = Math.max(0, Math.min(100, Number(payload.percent) || 0));
      var indeterminate = !!payload.indeterminate;
      var done = payload.status === 'done';
      document.body.classList.toggle('is-indeterminate', indeterminate && !done);
      document.body.classList.toggle('is-done', done);
      setText('status', payload.statusText || '正在下载客户端更新');
      setText('source', payload.sourceText || '下载源：-');
      setText('percent', indeterminate && !done ? '--%' : (payload.percentText || Math.round(percent) + '%'));
      setText('size', payload.sizeText || '');
      setText('speed', payload.speedText || '');
      setText('detail', payload.detailText || '可关闭此窗口，下载会继续；完成后会再次询问是否重启并安装。');
      var bar = document.getElementById('bar');
      if (bar && !indeterminate) bar.style.width = percent + '%';
    };
  </script>
</body>
</html>`;
}

function clientUpdateProgressDataUrl() {
  return `data:text/html;charset=utf-8,${encodeURIComponent(clientUpdateProgressHtml())}`;
}

function buildClientUpdateProgressPayload(progress = {}, options = {}) {
  const source = options.source || downloadedUpdateSource || activeClientUpdateSource;
  const version = options.version !== undefined ? options.version : clientUpdateProgressVersion;
  const percent = clampProgressPercent(progress.percent);
  const total = Number(progress.total) || 0;
  const transferred = Number(progress.transferred) || 0;
  const bytesPerSecond = Number(progress.bytesPerSecond) || 0;
  const totalKnown = total > 0;
  const done = options.status === 'done';
  const indeterminate = !done && !totalKnown && percent <= 0;
  const statusText =
    options.statusText ||
    (done
      ? '客户端下载完成'
      : version
        ? `正在下载 Infinite Canvas ${version}`
        : '正在下载 Infinite Canvas 客户端更新');
  const sizeText = totalKnown
    ? `${formatClientUpdateBytes(transferred)} / ${formatClientUpdateBytes(total)}`
    : transferred > 0
      ? `已下载 ${formatClientUpdateBytes(transferred)}`
      : '正在连接下载源...';

  return {
    status: options.status || 'downloading',
    statusText,
    sourceText: `下载源：${clientUpdateSourceLabel(source)}`,
    percent: done ? 100 : percent,
    percentText: done ? '100%' : `${Math.round(percent)}%`,
    sizeText: done ? '下载已完成' : sizeText,
    speedText: done ? '完成' : bytesPerSecond > 0 ? `${formatClientUpdateBytes(bytesPerSecond)}/s` : '正在获取速度...',
    detailText: options.detailText || '可关闭此窗口，下载会继续；完成后会再次询问是否重启并安装。',
    indeterminate,
  };
}

function setClientUpdateTaskbarProgress(payload) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (!payload || payload.status === 'done') {
    mainWindow.setProgressBar(-1);
    return;
  }
  if (payload.indeterminate) {
    mainWindow.setProgressBar(2);
    return;
  }
  mainWindow.setProgressBar(payload.percent / 100);
}

function sendClientUpdateProgressToWindow(force = false) {
  const target = clientUpdateProgressWindow;
  if (!target || target.isDestroyed() || !latestClientUpdateProgressPayload) return;
  if (!clientUpdateProgressWindowReady) return;
  const now = Date.now();
  const complete =
    latestClientUpdateProgressPayload.percent >= 100 || latestClientUpdateProgressPayload.status === 'done';
  if (!force && !complete && now - lastDownloadProgressUiUpdate < 180) return;
  lastDownloadProgressUiUpdate = now;
  const script = `window.__setClientUpdateProgress(${JSON.stringify(latestClientUpdateProgressPayload)});`;
  target.webContents.executeJavaScript(script).catch(() => {});
}

function updateClientUpdateProgressWindow(progress = {}, options = {}) {
  latestClientUpdateProgressPayload = buildClientUpdateProgressPayload(progress, options);
  setClientUpdateTaskbarProgress(latestClientUpdateProgressPayload);
  sendClientUpdateProgressToWindow(!!options.force);
}

function showClientUpdateProgressWindow(info, source = activeClientUpdateSource) {
  clientUpdateProgressVersion = updateVersion(info);
  if (clientUpdateProgressWindow && !clientUpdateProgressWindow.isDestroyed()) {
    clientUpdateProgressWindow.focus();
    updateClientUpdateProgressWindow({ percent: 0, transferred: 0, total: 0 }, { source, force: true });
    return;
  }

  clientUpdateProgressWindow = new BrowserWindow({
    width: 480,
    height: 230,
    resizable: false,
    maximizable: false,
    minimizable: false,
    closable: true,
    fullscreenable: false,
    parent: dialogParentWindow(),
    modal: false,
    show: false,
    title: '正在下载客户端更新',
    backgroundColor: '#f8fafc',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  const progressWindow = clientUpdateProgressWindow;
  clientUpdateProgressWindowReady = false;

  progressWindow.on('closed', () => {
    if (clientUpdateState === 'downloading') {
      appendClientUpdateLog('progress-window-closed', { source });
    }
    if (clientUpdateProgressWindow === progressWindow) {
      clientUpdateProgressWindow = null;
      clientUpdateProgressWindowReady = false;
    }
  });
  progressWindow.webContents.once('did-finish-load', () => {
    if (clientUpdateProgressWindow !== progressWindow) return;
    clientUpdateProgressWindowReady = true;
    sendClientUpdateProgressToWindow(true);
  });
  progressWindow.once('ready-to-show', () => {
    if (clientUpdateProgressWindow === progressWindow && !progressWindow.isDestroyed()) {
      progressWindow.show();
    }
  });
  progressWindow.loadURL(clientUpdateProgressDataUrl()).catch(() => {});
  updateClientUpdateProgressWindow({ percent: 0, transferred: 0, total: 0 }, { source, force: true });
}

function closeClientUpdateProgressWindow(delayMs = 0) {
  setClientUpdateTaskbarProgress(null);
  const target = clientUpdateProgressWindow;
  clientUpdateProgressWindow = null;
  clientUpdateProgressWindowReady = false;
  latestClientUpdateProgressPayload = null;
  clientUpdateProgressVersion = '';
  lastDownloadProgressUiUpdate = 0;
  if (!target || target.isDestroyed()) {
    return;
  }
  const close = () => {
    if (!target.isDestroyed()) target.destroy();
  };
  if (delayMs > 0) {
    setTimeout(close, delayMs);
  } else {
    close();
  }
}

function completeClientUpdateProgressWindow(info, source = downloadedUpdateSource) {
  clientUpdateProgressVersion = updateVersion(info) || clientUpdateProgressVersion;
  updateClientUpdateProgressWindow(
    { percent: 100, transferred: 1, total: 1 },
    {
      source,
      status: 'done',
      statusText: '客户端下载完成',
      detailText: '正在准备安装确认窗口...',
      force: true,
    },
  );
  closeClientUpdateProgressWindow(650);
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
      const socket = nodeNet.createConnection({ host: HOST, port: PORT });
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
  if (phase === 'download' && clientUpdateDownloadFailureHandled) {
    appendClientUpdateLog('duplicate-download-error-ignored', { source, phase, message });
    return true;
  }
  lastClientUpdateError = error;
  appendClientUpdateLog('source-error', { source, phase, message });
  if (phase === 'download') {
    clientUpdateDownloadFailureHandled = true;
    closeClientUpdateProgressWindow();
  }

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

async function startClientUpdateDownload(info, source = activeClientUpdateSource) {
  const version = updateVersion(info);
  clientUpdateState = 'downloading';
  clientUpdateReportFailures = true;
  downloadedUpdateSource = source;
  clientUpdateDownloadFailureHandled = false;
  lastDownloadProgressLog = 0;
  appendClientUpdateLog('download-start', { version, source });
  showClientUpdateProgressWindow(info, source);
  try {
    await autoUpdater.downloadUpdate();
  } catch (error) {
    appendClientUpdateLog('download-error', { source, message: error.message });
    handleClientUpdateSourceFailure(error, 'download');
  }
}

async function promptDownloadClientUpdate(info, source = activeClientUpdateSource) {
  const version = updateVersion(info);
  const response = await showClientUpdateAvailablePrompt(info, source);
  if (response.action !== 'download') {
    clientUpdateState = 'idle';
    manualUpdateCheckPending = false;
    resetClientUpdateFallbackState();
    appendClientUpdateLog('download-deferred', { version, source });
    return;
  }
  const selectedSource = normalizeClientUpdateSource(response.source || source);
  clientUpdateDownloadApproved = true;
  clientUpdateReportFailures = true;
  appendClientUpdateLog('source-user-selected', {
    discoveredSource: source,
    selectedSource,
    version,
  });
  if (selectedSource !== source) {
    clientUpdateState = 'idle';
    lastClientUpdateError = null;
    clientUpdateFallbackSources = clientUpdateSourceOrder(selectedSource);
    checkNextClientUpdateSource();
    return;
  }
  await startClientUpdateDownload(info, source);
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
    const downloadFlow = clientUpdateDownloadApproved
      ? startClientUpdateDownload(info, source)
      : promptDownloadClientUpdate(info, source);
    downloadFlow.catch((error) => {
      clientUpdateState = 'idle';
      resetClientUpdateFallbackState();
      appendClientUpdateLog('prompt-error', { message: error.message });
    });
  });

  autoUpdater.on('update-not-available', (info) => {
    const source = activeClientUpdateSource;
    if (clientUpdateDownloadApproved) {
      appendClientUpdateLog('approved-source-update-not-available', {
        source,
        version: updateVersion(info),
        fallbackSources: clientUpdateFallbackSources.join(','),
      });
      if (clientUpdateFallbackSources.length > 0) {
        lastClientUpdateError = new Error(`${clientUpdateSourceLabel(source)} 未提供已发现的客户端版本。`);
        checkNextClientUpdateSource();
        return;
      }
      const error = new Error('用户选择的下载源及备用源均未提供已发现的客户端版本。');
      clientUpdateState = 'idle';
      manualUpdateCheckPending = false;
      resetClientUpdateFallbackState();
      showClientUpdateFailureDialog(error, 'download').catch(() => {});
      return;
    }
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
    if (clientUpdateState !== 'downloading') return;
    updateClientUpdateProgressWindow(progress, { source: downloadedUpdateSource });
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
    clientUpdateDownloadFailureHandled = false;
    const source = downloadedUpdateSource;
    resetClientUpdateFallbackState();
    completeClientUpdateProgressWindow(info, source);
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
    if (clientUpdateState === 'downloading') {
      handleClientUpdateSourceFailure(error, 'download');
      return;
    }
    const shouldReport = manualUpdateCheckPending || clientUpdateReportFailures;
    clientUpdateState = 'idle';
    manualUpdateCheckPending = false;
    resetClientUpdateFallbackState();
    if (shouldReport) {
      showClientUpdateFailureDialog(error).catch(() => {});
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
      showClientUpdateProgressWindow({ version: clientUpdateProgressVersion }, downloadedUpdateSource);
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
  ipcMain.handle('client-update:respond', (event, payload = {}) => {
    if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents) {
      appendClientUpdateLog('response-ipc-denied');
      return { ok: false, error: 'unauthorized' };
    }
    if (!pendingClientUpdatePrompt || payload.requestId !== pendingClientUpdatePrompt.requestId) {
      return { ok: false, error: 'stale-request' };
    }
    const source = normalizeClientUpdateSource(payload.source);
    resolveClientUpdatePrompt(payload.action, source);
    return { ok: true };
  });
  ipcMain.handle('client-update:probe-connectivity', async (event, payload = {}) => {
    if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents) {
      appendClientUpdateLog('connectivity-ipc-denied');
      return { ok: false, error: 'unauthorized' };
    }
    if (!pendingClientUpdatePrompt || payload.requestId !== pendingClientUpdatePrompt.requestId) {
      return { ok: false, error: 'stale-request' };
    }
    try {
      return await probeClientUpdateConnectivity(String(payload.targetId || ''));
    } catch (error) {
      return { ok: false, error: clientUpdateErrorMessage(error) };
    }
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
  mainWindow.on('closed', () => {
    resolveClientUpdatePrompt('later');
    mainWindow = null;
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
