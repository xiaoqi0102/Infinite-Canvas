const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function sourceBetween(source, start, end) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `未找到源码起点：${start}`);
  assert.notEqual(endIndex, -1, `未找到源码终点：${end}`);
  return source.slice(startIndex, endIndex);
}

function response(status, data = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  };
}

async function testCanvasSaveRetry() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'canvas.js'), 'utf8');
  const replies = [response(500), response(200, {canvas: {updated_at: 22}})];
  const scheduled = [];
  const sandbox = {
    JSON,
    Number,
    Promise,
    console: {error() {}},
    setTimeout: fn => {
      scheduled.push(fn);
      return scheduled.length;
    },
    clearTimeout() {},
    fetch: async () => replies.shift(),
    canvas: {id: 'canvas-1', title: 'Canvas', icon: 'image', logs: [], updated_at: 11},
    applyingRemoteCanvas: false,
    savingCanvasNow: false,
    saveCanvasAgain: false,
    localCanvasDirty: true,
    saveRetryDelay: 1000,
    saveRetryTimer: null,
    saveTimer: null,
    nodes: [{id: 'node-1'}],
    connections: [],
    viewport: {x: 0, y: 0, scale: 1},
    lastCanvasUpdatedAt: 11,
    CLIENT_ID: 'test-client',
    currentCanvasTime: null,
    sanitizeConnections() {},
    serializableCanvasNodes: value => value,
    formatCanvasTime: value => String(value),
    setStatus() {},
    tr: key => key,
    loadCanvasList() {},
    applyRemoteCanvasData() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(sourceBetween(source, 'async function saveCanvas(){', 'async function loadConfig(){'), sandbox);

  assert.equal(await sandbox.saveCanvas(), false);
  assert.equal(sandbox.localCanvasDirty, true);
  assert.ok(sandbox.saveRetryTimer);

  assert.equal(await sandbox.saveCanvas(), true);
  assert.equal(sandbox.localCanvasDirty, false);
  assert.equal(sandbox.lastCanvasUpdatedAt, 22);
}

async function testSmartCanvasSaveRetry() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'smart-canvas.js'), 'utf8');
  const replies = [response(503), response(200, {canvas: {updated_at: 31}})];
  const scheduled = [];
  const sandbox = {
    JSON,
    Promise,
    encodeURIComponent,
    setTimeout: fn => {
      scheduled.push(fn);
      return scheduled.length;
    },
    clearTimeout() {},
    fetch: async () => replies.shift(),
    canvasId: 'smart-1',
    canvas: {id: 'smart-1', title: 'Smart', nodes: [], connections: [], logs: [], updated_at: 20},
    canvasSyncInFlight: false,
    smartSaveAgain: false,
    smartCanvasDirty: true,
    smartSaveRetryDelay: 1000,
    smartSaveRetryTimer: null,
    saveTimer: null,
    nodes: [],
    viewport: {x: 0, y: 0, scale: 1},
    canvasDefaultSmartSettings: {},
    initialSmartSettings: {},
    smartClientId: 'test-client',
    savePromptDraftForCurrent() {},
    mediaItemForStorage: value => value,
    stripImageGenerationMeta: value => value,
    settingsForStorage: value => value,
    applyMergedServerCanvas() {},
    toast() {},
    tr: key => key,
  };
  sandbox.canvasForStorage = () => sandbox.canvas;
  vm.createContext(sandbox);
  vm.runInContext(sourceBetween(source, 'async function saveCanvas(){', 'function imageMetaFromNode'), sandbox);

  assert.equal(await sandbox.saveCanvas(), false);
  assert.equal(sandbox.smartCanvasDirty, true);
  assert.ok(sandbox.smartSaveRetryTimer);

  assert.equal(await sandbox.saveCanvas(), true);
  assert.equal(sandbox.smartCanvasDirty, false);
  assert.equal(sandbox.canvas.updated_at, 31);
}

Promise.all([testCanvasSaveRetry(), testSmartCanvasSaveRetry()])
  .then(() => console.log('canvas save resilience tests passed'))
  .catch(error => {
    console.error(error);
    process.exitCode = 1;
  });
