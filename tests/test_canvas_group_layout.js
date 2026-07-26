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
const groupSource = sourceBetween(canvasSource, 'const CANVAS_GROUP_LAYOUT', 'function nodeBounds');
const undoSource = sourceBetween(canvasSource, 'function pushUndo', 'function cloneNode');

{
  const sizes = new Map([
    ['a', {w:100, h:80}],
    ['b', {w:120, h:90}],
    ['c', {w:90, h:110}],
    ['d', {w:130, h:70}],
    ['e', {w:80, h:100}],
    ['f', {w:110, h:95}],
    ['g', {w:105, h:85}],
  ]);
  const nodes = [
    {id:'a', type:'image', x:0, y:0},
    {id:'b', type:'image', x:300, y:10},
    {id:'c', type:'image', x:20, y:250},
    {id:'d', type:'image', x:340, y:260},
    {id:'e', type:'image', x:40, y:520},
    {id:'f', type:'image', x:360, y:530},
    {id:'g', type:'image', x:700, y:540},
    {id:'group', type:'group', x:-24, y:-58, w:900, h:800, items:['a','b','c','d','e','f','g']},
  ];
  const sandbox = {
    nodes,
    nodeRect(node) {
      const size = sizes.get(node.id);
      return {x:node.x, y:node.y, w:size.w, h:size.h};
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(groupSource, sandbox);

  assert.equal(sandbox.arrangeCanvasGroupMembers(nodes.at(-1)), true);
  assert.deepEqual(Array.from(nodes.at(-1).items), ['a','b','c','d','e','f','g']);
  assert.deepEqual(
    nodes.slice(0, 7).map(node => [node.id, node.x, node.y]),
    [
      ['a', 0, 0],
      ['b', 154, 0],
      ['c', 298, 0],
      ['d', 0, 134],
      ['e', 154, 134],
      ['f', 298, 134],
      ['g', 0, 258],
    ],
  );
  assert.equal(nodes.at(-1).w, 456);
  assert.equal(nodes.at(-1).h, 425);
}

{
  const nodes = [
    {id:'image', type:'image', x:24, y:58},
    {id:'prompt', type:'prompt', x:308, y:58},
    {id:'group', type:'group', x:0, y:0, w:642, h:420, items:['image','prompt']},
    {id:'generator', type:'generator', x:900, y:0},
    {id:'llm', type:'llm', x:900, y:500},
    {
      id:'ltx',
      type:'ltxDirector',
      x:1300,
      y:0,
      ltxTimelineData:JSON.stringify({
        segments:[{id:'segment', canvasSourceId:'group:image', length:73, guideStrength:0.4}],
        audioSegments:[],
      }),
    },
  ];
  let connections = [
    {id:'existing-link', from:'image', to:'generator'},
    {id:'group-link', from:'group', to:'generator'},
    {id:'group-llm-link', from:'group', to:'llm'},
    {id:'group-ltx-link', from:'group', to:'ltx'},
  ];
  let undoCalls = 0;
  let syncCalls = 0;
  let renderCalls = 0;
  let saveCalls = 0;
  let nextId = 0;
  const selected = new Set(['group']);
  const sandbox = {
    nodes,
    connections,
    selected,
    pushUndo() { undoCalls += 1; },
    canConnect(from, to) {
      return Boolean(
        sandbox.nodes.find(node => node.id === from)
        && sandbox.nodes.find(node => node.id === to),
      );
    },
    uid() { nextId += 1; return `connection-${nextId}`; },
    syncGeneratorInputs() { syncCalls += 1; },
    render() { renderCalls += 1; },
    scheduleSave() { saveCalls += 1; },
    nodeRect(node) { return {x:node.x, y:node.y, w:100, h:100}; },
  };
  vm.createContext(sandbox);
  vm.runInContext(groupSource, sandbox);

  assert.equal(sandbox.ungroupCanvasGroups(['group']), true);
  assert.equal(undoCalls, 1);
  assert.equal(syncCalls, 1);
  assert.equal(renderCalls, 1);
  assert.equal(saveCalls, 1);
  assert.deepEqual(Array.from(sandbox.nodes, node => node.id), ['image','prompt','generator','llm','ltx']);
  assert.deepEqual(Array.from(sandbox.selected), ['image','prompt']);
  assert.deepEqual(
    Array.from(sandbox.connections, connection => [connection.from, connection.to]),
    [
      ['image','generator'],
      ['prompt','generator'],
      ['image','llm'],
      ['image','ltx'],
      ['prompt','ltx'],
    ],
  );
  const ltxTimeline = JSON.parse(sandbox.nodes.find(node => node.id === 'ltx').ltxTimelineData);
  assert.equal(ltxTimeline.segments[0].canvasSourceId, 'image');
  assert.equal(ltxTimeline.segments[0].length, 73);
  assert.equal(ltxTimeline.segments[0].guideStrength, 0.4);
}

{
  let nextId = 0;
  const selected = new Set(['group']);
  const sandbox = {
    canvas:{},
    nodes:[
      {id:'image', type:'image', x:24, y:58},
      {id:'group', type:'group', x:0, y:0, w:300, h:220, items:['image']},
      {id:'generator', type:'generator', x:600, y:0},
    ],
    connections:[{id:'group-link', from:'group', to:'generator'}],
    selected,
    undoStack:[],
    UNDO_MAX:20,
    serializableCanvasNodes() { return sandbox.nodes; },
    canConnect(from, to) {
      return Boolean(
        sandbox.nodes.find(node => node.id === from)
        && sandbox.nodes.find(node => node.id === to),
      );
    },
    uid() { nextId += 1; return `connection-${nextId}`; },
    syncGeneratorInputs() {},
    render() {},
    scheduleSave() {},
    nodeRect(node) { return {x:node.x, y:node.y, w:100, h:100}; },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${undoSource}\n${groupSource}`, sandbox);

  assert.equal(sandbox.ungroupCanvasGroups(['missing']), false);
  assert.equal(sandbox.undoStack.length, 0);
  assert.equal(sandbox.ungroupCanvasGroups(['group']), true);
  assert.equal(sandbox.undoStack.length, 1);
  assert.deepEqual(Array.from(sandbox.nodes, node => node.id), ['image','generator']);
  assert.deepEqual(
    Array.from(sandbox.connections, connection => [connection.from, connection.to]),
    [['image','generator']],
  );

  sandbox.performUndo();
  assert.deepEqual(Array.from(sandbox.nodes, node => node.id), ['image','group','generator']);
  assert.deepEqual(
    Array.from(sandbox.connections, connection => [connection.from, connection.to]),
    [['group','generator']],
  );
  assert.deepEqual(Array.from(sandbox.selected), []);
}

console.log('普通画布分组布局与打散测试通过');
