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

function createSandbox() {
  const sandbox = {
    URL,
    window: {
      location: {
        origin: 'http://127.0.0.1:3000',
        href: 'http://127.0.0.1:3000/static/canvas.html',
      },
      StudioVideoApi: {
        isPublicHttpUrl: value => /^https?:\/\//i.test(String(value || '')),
      },
    },
    escapeAttr: value => String(value || '').replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
    escapeHtml: value => String(value || '').replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
  };
  vm.createContext(sandbox);
  return sandbox;
}

{
  const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'canvas.js'), 'utf8');
  const sandbox = createSandbox();
  const helpers = [
    'function isCloudHostedMediaUrl(url){ return /^https?:\\/\\//i.test(String(url || "")); }',
    sourceBetween(source, 'function canvasPreferredMediaUrl', 'function loadCanvasOriginalImageDimensions'),
  ].join('\n');
  vm.runInContext(helpers, sandbox);

  const item = {
    url: 'https://cdn.example.test/result.png',
    originalLocalUrl: '/api/storage-files/generated/result.png',
  };
  assert.equal(sandbox.canvasPreferredMediaUrl(item), item.originalLocalUrl);
  assert.equal(sandbox.canvasRemoteMediaUrl(item), item.url);
  const html = sandbox.canvasPreviewImgHtml(item, 256);
  assert.match(html, /data-original-src="\/api\/storage-files\/generated\/result\.png"/);
  assert.match(html, /data-remote-src="https:\/\/cdn\.example\.test\/result\.png"/);
}

{
  const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'smart-canvas.js'), 'utf8');
  const sandbox = createSandbox();
  const helpers = [
    'function isCloudHostedMediaUrl(url){ return /^https?:\\/\\//i.test(String(url || "")); }',
    sourceBetween(source, 'function smartPreferredMediaUrl', 'function loadSmartOriginalImageDimensions'),
    sourceBetween(source, 'function smartVideoPreviewHtml', 'function smartVideoPlayerHtml'),
    sourceBetween(source, 'function proxiedMediaUrl', 'function safeExportFileName'),
  ].join('\n');
  vm.runInContext(helpers, sandbox);

  const item = {
    url: 'https://cdn.example.test/result.mp4',
    originalLocalUrl: '/api/storage-files/generated/result.mp4',
  };
  assert.equal(sandbox.smartPreferredMediaUrl(item), item.originalLocalUrl);
  assert.equal(sandbox.smartRemoteMediaUrl(item), item.url);
  assert.equal(sandbox.displayMediaUrl(item), item.originalLocalUrl);
  const html = sandbox.smartVideoPreviewHtml(item, 256);
  assert.match(html, /data-original-src="\/api\/storage-files\/generated\/result\.mp4"/);
  assert.match(html, /data-remote-src="https:\/\/cdn\.example\.test\/result\.mp4"/);
}

console.log('media display fallback tests passed');
