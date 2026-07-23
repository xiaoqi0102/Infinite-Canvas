const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/canvas.js', 'utf8');
const helpersStart = source.indexOf('function canvasVideoPendingTasksForNode');
const helpersEnd = source.indexOf('async function createCanvasImageTask', helpersStart);
const failureStart = source.indexOf('function failCanvasVideoTask');
const failureEnd = source.indexOf('function completeCanvasImageTask', failureStart);

assert.ok(
    helpersStart >= 0 && helpersEnd > helpersStart && failureStart >= 0 && failureEnd > failureStart,
    '无法定位普通画布视频状态处理函数',
);

const logs = [];
const refreshed = [];
const output = {id:'output-1', type:'output', _pending:[]};
const generator = {id:'video-1', type:'video', running:true};
const sandbox = {
    nodes:[generator, output],
    findPendingTask:taskId => {
        const pending = output._pending.find(item => item.canvasTaskId === taskId);
        return pending ? {out:output, pending} : null;
    },
    nowMs:() => 2000,
    extractUpstreamTaskId:() => '',
    providerIdForPending:() => 'custom-api-2',
    addGenerationLog:entry => logs.push(entry),
    refreshRunNodes:(gen, out) => refreshed.push([gen?.id, out?.id]),
    scheduleSave:() => {},
    tr:key => key,
};

vm.runInNewContext(source.slice(helpersStart, helpersEnd), sandbox);
vm.runInNewContext(source.slice(failureStart, failureEnd), sandbox);

function pendingTask(overrides={}) {
    return {
        id:'pending-1',
        canvasTaskId:'canvas_video_failed',
        canvasTaskType:'online-video',
        cascadeTargetId:'video-1',
        startedAt:1000,
        run:{node:{id:'video-1'}},
        ...overrides,
    };
}

output._pending = [pendingTask()];
sandbox.failCanvasVideoTask(
    'canvas_video_failed',
    '视频生成任务失败：MODERATION_ERROR；task_failed',
    {
        status:'failed',
        upstream_task_id:'vid_failed',
        provider_id:'custom-api-2',
        request_details:{attempts:[]},
    },
);

assert.equal(output._pending.length, 0);
assert.equal(generator.running, false);
assert.equal(generator.runStatus, 'failed');
assert.equal(generator._cascadeFailed, true);
assert.match(generator.runError, /MODERATION_ERROR/);

output._pending = [pendingTask({id:'pending-2'})];
generator.running = true;
sandbox.failCanvasVideoTask('canvas_video_failed', '临时网络错误');

assert.equal(output._pending.length, 1);
assert.equal(output._pending[0].failed, true);
assert.equal(output._pending[0].recoverTaskId, 'canvas_video_failed');
assert.equal(generator.running, true);

assert.match(
    source,
    /if\(pending\?\.failed && pending\.recoverTaskId\)[\s\S]*?if\(opts\.cascade\) throw err;[\s\S]*?return;/,
    '一键运行必须继续抛出可恢复视频错误',
);
assert.match(
    source,
    /failCanvasVideoTask\(taskId, message, data\);/,
    '手动查询确认失败后必须走统一终态清理',
);
assert.match(
    source,
    /p\.canvasTaskType === 'online-video'[\s\S]*?\(!p\.failed \|\| p\.recoverTaskId\)/,
    '旧画布的视频失败 pending 必须重新查询本地任务',
);

assert.ok(logs.length >= 2);
assert.ok(refreshed.length >= 2);
console.log('canvas video terminal state ok');
