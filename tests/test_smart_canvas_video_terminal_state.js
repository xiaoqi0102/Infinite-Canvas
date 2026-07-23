const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/smart-canvas.js', 'utf8');
const pollStart = source.indexOf('async function pollSmartCanvasVideoTask');
const helperEnd = source.indexOf('function finalizeSmartPendingTask', pollStart);

assert.ok(pollStart >= 0 && helperEnd > pollStart, '无法定位智能画布视频任务状态函数');

const sandbox = {
    activeSmartTaskPolls:new Map(),
    fetch:async () => ({
        ok:true,
        json:async () => ({
            status:'failed',
            error:'视频生成任务失败：MODERATION_ERROR；task_failed',
            request_details:{attempts:[]},
        }),
    }),
    smartPendingTasks:node => Array.isArray(node?.pendingTasks) ? node.pendingTasks : [],
    smartResponseErrorMessage:async () => '请求失败',
    tr:key => key,
    sleep:async () => {},
};

vm.runInNewContext(source.slice(pollStart, helperEnd), sandbox);

(async () => {
    await assert.rejects(
        sandbox.pollSmartCanvasVideoTask('canvas_video_failed'),
        error => error.canvasTaskFailed === true
            && error.message.includes('MODERATION_ERROR')
            && error.requestDetails?.attempts?.length === 0,
    );
    assert.equal(sandbox.activeSmartTaskPolls.size, 0);

    const node = {
        running:true,
        queued:true,
        pending:1,
        pendingTasks:[{taskId:'canvas_video_failed', kind:'video'}],
        images:[],
        w:320,
        h:240,
    };
    sandbox.removeSmartPendingTask(node, 'canvas_video_failed');
    assert.equal(node.pending, 0);
    assert.equal(node.running, false);
    assert.equal(node.queued, false);
    assert.equal(node.pendingTasks, undefined);
    assert.equal(node.w, undefined);
    assert.equal(node.h, undefined);

    assert.match(
        source,
        /task\.failed && task\.recoverTaskId && task\.kind !== 'video'/,
        '旧画布中的视频失败任务应重新向后端确认终态',
    );

    console.log('smart canvas video terminal state ok');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
