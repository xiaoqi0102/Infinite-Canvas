// 覆盖普通画布图片节点「换图后仍显示旧图」的回归场景：
// 节点上传云端后 url 会变成云端地址、本地原件留在 originalLocalUrl，
// 此时再换新图若不清理派生地址，节点缩略图会继续渲染旧图，
// 并且旧图的云端链接仍会被 cloudUploadLinkForCanvasRef 命中，导致拿旧图去生成视频。
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

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'canvas.js'), 'utf8');
const nodes = [];
const sandbox = {
  URL,
  Number,
  Set,
  String,
  CANVAS_UPLOAD_MAX: 20,
  nodes,
  importLocalImages: async () => [{url: '/assets/input/ai_ref_new.png', name: '新图.png', kind: 'image'}],
  pushUndo: () => {},
  outputImageName: url => String(url || '').split('/').pop() || 'image',
  render: () => {},
  scheduleSave: () => {},
  window: {
    location: {
      origin: 'http://127.0.0.1:3000',
      href: 'http://127.0.0.1:3000/static/canvas.html',
    },
  },
  escapeAttr: value => String(value || '').replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
};
vm.createContext(sandbox);
vm.runInContext([
  'function isCloudHostedMediaUrl(url){ return /^https?:\\/\\//i.test(String(url || "")); }',
  sourceBetween(source, 'function canvasPreferredMediaUrl', 'function loadCanvasOriginalImageDimensions'),
  sourceBetween(source, 'function cloudUploadLinkForCanvasRef', 'function cloudUploadSourceUrlForCanvasRef'),
  sourceBetween(source, 'const IMAGE_NODE_DERIVED_URL_KEYS', 'function allowImageNodeDropEvent'),
  sourceBetween(source, 'function measureCanvasOriginalImageNodes', '\nfunction render(){'),
].join('\n'), sandbox);

const LOCAL_OLD = '/assets/input/ai_ref_old.png';
const CLOUD_OLD = 'https://files.sudashuiapi.com/proxy/uploads/20260727/old.png';
const LOCAL_NEW = '/assets/input/ai_ref_new.png';

// 1) 上传云端后仍用本地原件渲染，是必须保留的既有能力（云端链接有效期很短）。
const node = {id: 'img1', type: 'image', url: LOCAL_OLD, name: '旧图.png', mediaKind: 'image'};
node.originalLocalUrl = node.url;
node.url = CLOUD_OLD;
node.natural_w = 832;
node.natural_h = 1216;
assert.equal(sandbox.canvasPreferredMediaUrl(node), LOCAL_OLD);
assert.equal(sandbox.canvasRemoteMediaUrl(node), CLOUD_OLD);

// 2) 换新图后，派生地址必须一起失效，否则节点继续渲染旧图。
sandbox.applyImageNodeMedia(node, {url: LOCAL_NEW, name: '新图.png', mediaKind: 'image'});
assert.equal(node.url, LOCAL_NEW);
assert.equal(node.name, '新图.png');
assert.equal(node.originalLocalUrl, undefined);
assert.equal(sandbox.canvasPreferredMediaUrl(node), LOCAL_NEW);
assert.equal(sandbox.canvasRemoteMediaUrl(node), '');
assert.match(sandbox.canvasPreviewImgHtml(node, 768), /data-original-src="\/assets\/input\/ai_ref_new\.png"/);

// 3) 旧图尺寸不能残留，否则新图按旧比例显示。
assert.equal(node.natural_w, undefined);
assert.equal(node.natural_h, undefined);

// 4) 旧图的云端链接不再被当前节点命中，避免用旧图去生成视频。
const videoNode = {tempShLinks: [{source: LOCAL_OLD, url: CLOUD_OLD}]};
const staleRef = {url: LOCAL_OLD, originalLocalUrl: LOCAL_OLD};
assert.notEqual(sandbox.cloudUploadLinkForCanvasRef(videoNode, staleRef), null);
const freshRef = {url: node.url, originalLocalUrl: node.originalLocalUrl || ''};
assert.equal(sandbox.cloudUploadLinkForCanvasRef(videoNode, freshRef), null);

// 5) 图片编辑器保存会显式带上新尺寸，这类调用要保留传入值。
sandbox.applyImageNodeMedia(node, {
  url: '/assets/input/ai_ref_new_crop.png',
  name: '新图_crop.png',
  mediaKind: 'image',
  naturalW: 512,
  naturalH: 768,
});
assert.equal(node.natural_w, 512);
assert.equal(node.natural_h, 768);

// 6) 清空节点同样要丢弃派生地址。
node.originalLocalUrl = LOCAL_OLD;
sandbox.applyImageNodeMedia(node, {url: '', name: '空白图片', mediaKind: 'image'});
assert.equal(node.url, '');
assert.equal(node.originalLocalUrl, undefined);
assert.equal(node.natural_w, undefined);

// 7) 实际执行 Electron 拖拽本地路径对应的 localPaths 分支，防止调用点退回直接赋值。
(async () => {
  const droppedNode = {
    id: 'img-local-path',
    type: 'image',
    url: CLOUD_OLD,
    originalLocalUrl: LOCAL_OLD,
    remoteUrl: CLOUD_OLD,
    name: '旧图.png',
    mediaKind: 'image',
    natural_w: 832,
    natural_h: 1216,
  };
  nodes.push(droppedNode);
  await sandbox.applyImageDropPayloadToNode(droppedNode.id, {type: 'localPaths', localPaths: ['D:/素材/新图.png']});
  assert.equal(droppedNode.url, LOCAL_NEW);
  assert.equal(droppedNode.name, '新图.png');
  assert.equal(droppedNode.originalLocalUrl, undefined);
  assert.equal(droppedNode.remoteUrl, undefined);
  assert.equal(droppedNode.natural_w, undefined);
  assert.equal(sandbox.canvasPreferredMediaUrl(droppedNode), LOCAL_NEW);

  // 8) 旧图尺寸测量即使晚于换图返回，也不能把旧尺寸写回新图。
  let resolveOldDimensions;
  sandbox.loadCanvasOriginalImageDimensions = () => new Promise(resolve => { resolveOldDimensions = resolve; });
  const measuringNode = {
    id: 'img-measuring',
    type: 'image',
    url: LOCAL_OLD,
    name: '旧图.png',
    mediaKind: 'image',
  };
  nodes.push(measuringNode);
  const fakeRoot = {
    querySelectorAll: () => [{
      dataset: {originalSrc: LOCAL_OLD},
      closest: () => ({dataset: {id: measuringNode.id}}),
    }],
  };
  sandbox.measureCanvasOriginalImageNodes(fakeRoot);
  assert.equal(measuringNode._naturalSizeLoading, LOCAL_OLD);
  sandbox.applyImageNodeMedia(measuringNode, {url: LOCAL_NEW, name: '新图.png', mediaKind: 'image'});
  resolveOldDimensions({w: 832, h: 1216});
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(measuringNode.url, LOCAL_NEW);
  assert.equal(measuringNode.natural_w, undefined);
  assert.equal(measuringNode.natural_h, undefined);

  console.log('canvas image node replace tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
