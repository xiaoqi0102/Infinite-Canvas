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

const canvasSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'canvas.js'), 'utf8');
const smartSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'smart-canvas.js'), 'utf8');

{
  const sandbox = {
    trf(key, values) {
      const prefixes = {
        'canvas.mentionRefLabel.image': '图',
        'canvas.mentionRefLabel.video': '视频',
        'canvas.mentionRefLabel.audio': '音频',
      };
      return `${prefixes[key]}${values.n}`;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    sourceBetween(canvasSource, 'function promptMentionLabel', 'function resolveGeneratorRequestInputs'),
    sandbox,
  );
  assert.deepEqual(
    Array.from(sandbox.promptLabelledRefs([
      {kind: 'image'},
      {kind: 'video'},
      {kind: 'image'},
      {kind: 'audio'},
    ]), item => item.label),
    ['图1', '视频1', '图2', '音频1'],
  );
}

{
  const appended = [];
  const sandbox = {
    document: {
      createElement: () => ({
        className: '',
        dataset: {},
        innerHTML: '',
        title: '',
        draggable: false,
        setAttribute() {},
        querySelector: () => null,
        addEventListener() {},
      }),
    },
    resolveGeneratorRequestInputs: () => ({refs: [{url: 'shared-image', name: '共享图片', kind: 'image'}]}),
    promptLabelledRefs: refs => refs.map(ref => ({ref, label: '图1'})),
    promptRequestRefFromSource: (_source, ref) => ref,
    promptMentionRefsEqual: (left, right) => left.url === right.url,
    uniqueValues: values => Array.from(new Set(values)),
    isMissingAssetUrl: () => false,
    canvasPreviewImgHtml: () => '<img>',
    generatorInputConnection: () => null,
    escapeHtml: value => String(value),
    escapeAttr: value => String(value),
    tr: key => key,
    refreshIcons() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    sourceBetween(canvasSource, 'function renderImageInputList', 'function renderVideoImageInputs'),
    sandbox,
  );
  const list = {
    innerHTML: '',
    appendChild: item => appended.push(item),
  };
  sandbox.renderImageInputList(list, {id: 'generator'}, [
    {id: 'single', label: '单图', preview: 'shared-image', refs: [{url: 'shared-image', name: '共享图片', kind: 'image'}]},
    {id: 'group', label: '分组', preview: 'shared-image', refs: [{url: 'shared-image', name: '共享图片', kind: 'image'}]},
  ]);
  assert.equal(appended.length, 2);
  assert.ok(appended.every(item => item.innerHTML.includes('图1')));
  assert.ok(appended.every(item => !item.innerHTML.includes('图2')));
}

{
  let undoCount = 0;
  const synced = [];
  const sandbox = {
    window: {},
    tempLink: null,
    portPoint: () => ({x: 0, y: 0}),
    nodes: [{id: 'source-a'}, {id: 'source-b'}, {id: 'source-invalid'}, {id: 'target'}],
    selected: new Set(['source-a', 'source-b', 'source-invalid']),
    connections: [{id: 'existing', from: 'source-a', to: 'target'}],
    nearestPort: () => ({closest: () => ({dataset: {id: 'target'}})}),
    canConnect: (fromId, toId) => fromId !== 'source-invalid' && fromId !== toId,
    pushUndo: () => { undoCount += 1; },
    uid: prefix => `${prefix}-${undoCount}-${synced.length}`,
    syncLatestGeneratedOutputToConnection: (fromId, toId) => synced.push([fromId, toId]),
    syncGeneratorInputs() {},
    scheduleSave() {},
    render() {},
    renderLinks() {},
    screenToWorld: () => ({x: 0, y: 0}),
    CANVAS_GENERATOR_TYPES: [],
    openLinkCreateMenu() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    sourceBetween(canvasSource, 'function startLink', 'function nearestPort'),
    sandbox,
  );
  sandbox.startLink({stopPropagation() {}}, 'source-a', 'out');
  sandbox.window.onmouseup({clientX: 10, clientY: 10});
  assert.equal(undoCount, 1);
  assert.deepEqual(synced, [['source-b', 'target']]);
  assert.deepEqual(
    Array.from(sandbox.connections, connection => [connection.from, connection.to]),
    [['source-a', 'target'], ['source-b', 'target']],
  );
}

{
  const sandbox = {
    nodes: [
      {id: 'group', type: 'smart-group', items: ['member']},
      {id: 'member', type: 'smart-image'},
      {id: 'standalone', type: 'smart-image'},
    ],
    isSmartGroupNode: node => node?.type === 'smart-group',
    smartGroupMembers: group => sandbox.nodes.filter(node => group.items?.includes(node.id)),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    sourceBetween(smartSource, 'function topLevelSmartConnectionSourceIds', 'function isEditableTarget'),
    sandbox,
  );
  assert.deepEqual(
    Array.from(sandbox.topLevelSmartConnectionSourceIds(['group', 'member', 'standalone', 'standalone'])),
    ['group', 'standalone'],
  );
}

{
  const connected = [];
  const nodeElement = {
    dataset: {id: 'loop-target'},
    getBoundingClientRect: () => ({left: 0, width: 100}),
  };
  const portElement = {
    dataset: {port: 'in'},
    closest: selector => selector === '.image-node' ? nodeElement : null,
  };
  const hitElement = {
    closest: selector => selector === '.node-port' ? portElement : selector === '.image-node' ? nodeElement : null,
  };
  const sandbox = {
    document: {elementFromPoint: () => hitElement},
    connectInputNode: (fromId, toId) => {
      connected.push([fromId, toId]);
      return true;
    },
    commitPendingUndo() {},
    discardPendingUndo() {},
    render() {},
    scheduleSave() {},
    screenToWorld: () => ({x: 0, y: 0}),
    createImageNodeAt: () => ({id: 'new-node'}),
    undoSuppressed: false,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    sourceBetween(smartSource, 'function handlePortDrop', 'function pickMediaForSmartNode'),
    sandbox,
  );
  sandbox.handlePortDrop(
    {fromId: 'loop-source', fromPort: 'out', sourceIds: ['loop-source', 'loop-source-2'], moved: true},
    {clientX: 25, clientY: 10},
  );
  assert.deepEqual(connected, [
    ['loop-source', 'loop-target'],
    ['loop-source-2', 'loop-target'],
  ]);
}

{
  const sandbox = {
    smartLoopContext: {},
    SMART_REFERENCE_IMAGE_MAX: 8,
    collectPromptParts: () => [
      {type: 'text', text: '比较'},
      {type: 'image', url: 'video-url', name: '片段', kind: 'video'},
      {type: 'text', text: '和'},
      {type: 'image', url: 'image-url', name: '图片', kind: 'image'},
    ],
    blockedInputRefKeys: () => new Set(),
    inputRefKey: item => item.url,
    defaultReferenceImagesFor: () => [{url: 'image-url', name: '图片', kind: 'image'}],
    uniqueReferenceImages: items => items.filter((item, index) => items.findIndex(other => other.url === item.url) === index),
    isSmartGroupNode: () => false,
    textForNode: () => '',
    inputPromptTextFor: () => '',
    settings: {engine: 'api'},
    rhDefaultPromptSuggestion: () => '',
    tr: key => key,
    trf(key, values) {
      const prefixes = {
        'canvas.mentionRefLabel.image': '图',
        'canvas.mentionRefLabel.video': '视频',
        'canvas.mentionRefLabel.audio': '音频',
      };
      return `${prefixes[key]}${values.n}`;
    },
    mediaKindForItem: item => item.kind || 'image',
  };
  vm.createContext(sandbox);
  vm.runInContext(
    [
      sourceBetween(smartSource, 'function smartMentionRefLabel', 'function smartMentionLabelsForNode'),
      sourceBetween(smartSource, 'function originalPromptTextFromParts', 'function outgoingConnectionsFor'),
    ].join('\n'),
    sandbox,
  );
  const request = sandbox.buildPromptRequest({id: 'target'});
  assert.match(request.prompt, /图1：图片/);
  assert.match(request.prompt, /视频1：片段/);
  assert.match(request.prompt, /比较视频1和图1/);
  assert.doesNotMatch(request.prompt, /图2/);
}

{
  let nextId = 0;
  const sandbox = {
    nodes: [
      {id: 'image-a', type: 'image', url: '/assets/a.png'},
      {id: 'image-b', type: 'image', url: '/assets/b.png'},
      {id: 'generator', type: 'generator'},
    ],
    selected: new Set(['image-a', 'image-b']),
    connections: [
      {id: 'direct-a', from: 'image-a', to: 'generator'},
      {id: 'direct-b', from: 'image-b', to: 'generator'},
    ],
    nodeBounds: () => ({x: 10, y: 20, w: 300, h: 200}),
    uid: prefix => `${prefix}-${++nextId}`,
    CANVAS_GENERATOR_TYPES: ['generator'],
    canConnect: (fromId, toId) => fromId.startsWith('grp-') && toId === 'generator',
    syncGeneratorInputs() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    [
      sourceBetween(canvasSource, 'function handoffExistingInputsToGroup', 'function updateGroupMembership'),
      sourceBetween(canvasSource, 'function connectSelectionToGenerator', 'function pushUndo'),
    ].join('\n'),
    sandbox,
  );
  sandbox.connectSelectionToGenerator('images', 'generator');
  const group = sandbox.nodes.find(node => node.type === 'group');
  assert.ok(group);
  assert.deepEqual(Array.from(group.items), ['image-a', 'image-b']);
  assert.deepEqual(
    Array.from(sandbox.connections, connection => [connection.from, connection.to]),
    [[group.id, 'generator']],
  );
}

console.log('canvas reference labels and batch connect tests passed');
