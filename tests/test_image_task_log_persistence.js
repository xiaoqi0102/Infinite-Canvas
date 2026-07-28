const assert = require('node:assert/strict');
const fs = require('node:fs');

const canvasSource = fs.readFileSync('static/js/canvas.js', 'utf8');
const smartSource = fs.readFileSync('static/js/smart-canvas.js', 'utf8');

const classicLogStart = canvasSource.indexOf('function addGenerationLog');
const classicLogEnd = canvasSource.indexOf('function renderCanvasLog', classicLogStart);
assert.ok(classicLogStart >= 0 && classicLogEnd > classicLogStart, '无法定位普通画布日志函数');
assert.match(
    canvasSource.slice(classicLogStart, classicLogEnd),
    /scheduleSave\(\)/,
    '普通画布日志每次状态更新后必须调度保存',
);

const classicRunStart = canvasSource.indexOf('async function runGenerator(');
const classicRunEnd = canvasSource.indexOf('async function runGeneratorLegacy', classicRunStart);
assert.ok(classicRunStart >= 0 && classicRunEnd > classicRunStart, '无法定位普通画布图片生成函数');
assert.match(
    canvasSource.slice(classicRunStart, classicRunEnd),
    /taskInfos\[0\]\?\.task_id[\s\S]*?addGenerationLog\([\s\S]*?status:taskInfos\[0\]\.status \|\| 'queued'/,
    '普通画布图片任务提交后必须立即生成可持久化的排队日志',
);

const smartRunStart = smartSource.indexOf('async function runApiGeneration');
const smartRunEnd = smartSource.indexOf('async function runRunningHubGeneration', smartRunStart);
assert.ok(smartRunStart >= 0 && smartRunEnd > smartRunStart, '无法定位智能画布图片提交函数');
assert.match(
    smartSource.slice(smartRunStart, smartRunEnd),
    /updateSmartTaskGenerationLog\(logContext, taskIds\[0\]/,
    '智能画布图片任务提交后必须立即生成可持久化的排队日志',
);

assert.match(
    smartSource,
    /kind:'image',[\s\S]*?logRun:runLog,[\s\S]*?logStartedAt:runLogStart/,
    '智能画布图片 pending 必须保存日志上下文以便刷新后继续更新同一条记录',
);

console.log('image task log persistence ok');
