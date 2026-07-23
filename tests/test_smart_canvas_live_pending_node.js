const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/smart-canvas.js', 'utf8');
const helperStart = source.indexOf('function removeSmartPendingTask');
const helperEnd = source.indexOf('async function resumeSmartPendingNode', helperStart);

assert.ok(helperStart >= 0 && helperEnd > helperStart, '无法定位智能画布任务收尾函数');

const liveNodes = [];
const sandbox = {
    liveSmartNode:node => liveNodes.find(item => item.id === node?.id) || node,
    smartPendingTasks:node => Array.isArray(node?.pendingTasks) ? node.pendingTasks : [],
    resultMediaUrls:value => Array.isArray(value) ? value : [value],
    cleanHistoryImages:images => images.filter(item => item?.url),
    stripImageGenerationMeta:item => item,
    copyMediaSizeFields:(sourceItem, target) => ({...target, width:sourceItem?.width, height:sourceItem?.height}),
    nowMs:() => 2000,
    mediaNodeDefaultScale:() => 1,
};

vm.runInNewContext(source.slice(helperStart, helperEnd), sandbox);

const staleNode = {
    id:'smart-output',
    pending:1,
    pendingTasks:[{taskId:'canvas_img_done', kind:'image'}],
    images:[],
    runStartedAt:1000,
};
const liveNode = JSON.parse(JSON.stringify(staleNode));
liveNodes.push(liveNode);

const completed = sandbox.finalizeSmartPendingTask(
    staleNode,
    'canvas_img_done',
    [{url:'/output/online_done.png', width:2048, height:1152}],
    'image',
);

assert.equal(completed, liveNode);
assert.equal(liveNode.pending, 0);
assert.equal(liveNode.pendingTasks, undefined);
assert.equal(liveNode.running, false);
assert.equal(liveNode.images.length, 1);
assert.equal(liveNode.images[0].url, '/output/online_done.png');
assert.equal(liveNode.runElapsedMs, 1000);
assert.equal(staleNode.pending, 1, '旧节点对象不应再作为画布真实状态');

const staleFailedNode = {
    id:'smart-failed-output',
    pending:1,
    pendingTasks:[{taskId:'canvas_img_failed', kind:'image'}],
    images:[],
    running:true,
    queued:true,
    w:320,
    h:240,
};
const liveFailedNode = JSON.parse(JSON.stringify(staleFailedNode));
liveNodes.push(liveFailedNode);

const cleared = sandbox.removeSmartPendingTask(staleFailedNode, 'canvas_img_failed');
assert.equal(cleared, liveFailedNode);
assert.equal(liveFailedNode.pending, 0);
assert.equal(liveFailedNode.pendingTasks, undefined);
assert.equal(liveFailedNode.running, false);
assert.equal(liveFailedNode.queued, false);
assert.equal(liveFailedNode.w, undefined);
assert.equal(liveFailedNode.h, undefined);

assert.match(
    source,
    /outputSlot\s*=\s*await resumeSmartPendingNode\(/,
    '循环输出应继续使用轮询后重新解析的活动节点',
);
assert.match(
    source,
    /pendingNode\s*=\s*await resumeSmartPendingNode\(/,
    '普通生成应继续使用轮询后重新解析的活动节点',
);

console.log('smart canvas live pending node ok');
