const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');

const mainSource = fs.readFileSync(path.join(__dirname, '..', 'electron', 'main.js'), 'utf8');

function sourceBetween(start, end) {
  const startIndex = mainSource.indexOf(start);
  const endIndex = mainSource.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `未找到源码起点：${start}`);
  assert.notEqual(endIndex, -1, `未找到源码终点：${end}`);
  return mainSource.slice(startIndex, endIndex);
}

function loadMigrationFunctions(overrides = {}) {
  const sandbox = {
    fs,
    path,
    USER_DATA_DIR_NAME: 'InfiniteCanvas_Data',
    installRoot: () => '',
    ...overrides,
  };
  vm.createContext(sandbox);
  vm.runInContext(sourceBetween('function samePath', 'function appendClientUpdateLog'), sandbox);
  return sandbox;
}

function writeFile(filePath, content) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, content, 'utf8');
}

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'infinite-canvas-electron-migration-'));
try {
  {
    const sourceDir = path.join(tempRoot, 'copy-source');
    const targetDir = path.join(tempRoot, 'copy-target');
    writeFile(path.join(sourceDir, 'existing.json'), 'fallback-newer');
    writeFile(path.join(sourceDir, 'nested', 'missing.json'), 'missing-data');
    writeFile(path.join(targetDir, 'existing.json'), 'sibling-kept');

    const sandbox = loadMigrationFunctions();
    sandbox.copyMissingRecursive(sourceDir, targetDir);

    assert.equal(fs.readFileSync(path.join(targetDir, 'existing.json'), 'utf8'), 'sibling-kept');
    assert.equal(fs.readFileSync(path.join(targetDir, 'nested', 'missing.json'), 'utf8'), 'missing-data');
    assert.equal(sandbox.unsafeMigrationPaths(sourceDir, sourceDir), true);
    assert.equal(sandbox.unsafeMigrationPaths(sourceDir, path.join(sourceDir, 'nested-target')), true);
    assert.equal(sandbox.unsafeMigrationPaths(path.join(sourceDir, 'nested-source'), sourceDir), true);
  }

  {
    const sourceDir = path.join(tempRoot, 'error-source');
    const targetDir = path.join(tempRoot, 'error-target');
    writeFile(path.join(sourceDir, 'data.json'), 'data');
    fs.mkdirSync(targetDir, { recursive: true });
    const failingFs = {
      ...fs,
      copyFileSync() {
        throw new Error('simulated copy failure');
      },
    };
    const sandbox = loadMigrationFunctions({ fs: failingFs });

    assert.doesNotThrow(() => sandbox.migrateDataDirectory(sourceDir, targetDir, 'test-source'));
    const log = fs.readFileSync(path.join(targetDir, 'desktop.log'), 'utf8');
    assert.match(log, /user-data-migration-failed/);
    assert.match(log, /sourceType=test-source/);
    assert.match(log, /simulated copy failure/);
  }

  {
    const installDir = path.join(tempRoot, 'Apps', 'Infinite Canvas');
    const fallbackBase = path.join(tempRoot, 'SystemUserData');
    const calls = [];
    const sandbox = {
      app: { isPackaged: true, getPath: () => fallbackBase },
      appRoot: () => tempRoot,
      canWriteDirectory: () => true,
      fs,
      installRoot: () => installDir,
      migrateDataDirectory: (...args) => calls.push(['fallback', ...args]),
      migrateLegacyInstallData: (...args) => calls.push(['legacy', ...args]),
      path,
      USER_DATA_DIR_NAME: 'InfiniteCanvas_Data',
    };
    vm.createContext(sandbox);
    vm.runInContext(sourceBetween('function userDataRoot', 'function backendExecutable'), sandbox);

    const selected = sandbox.userDataRoot();
    const siblingDir = path.join(path.dirname(installDir), 'InfiniteCanvas_Data');
    assert.equal(selected, siblingDir);
    assert.deepEqual(calls, [
      ['fallback', path.join(fallbackBase, 'InfiniteCanvas_Data'), siblingDir, 'system-user-data-fallback'],
      ['legacy', siblingDir],
    ]);
  }
} finally {
  fs.rmSync(tempRoot, { recursive: true, force: true });
}

console.log('electron data migration tests passed');
