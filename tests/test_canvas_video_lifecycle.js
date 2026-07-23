const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/canvas.js', 'utf8');

function sourceBetween(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.ok(start >= 0 && end > start, `无法定位源码片段：${startMarker}`);
    return source.slice(start, end);
}

async function testParallelRoundsStopAtFirstError() {
    const sandbox = {};
    vm.runInNewContext(
        sourceBetween('async function runLimitedCascadeRounds', 'function canvasRunTypes'),
        sandbox,
    );

    const originalError = new Error('round zero failed');
    const started = [];
    const crossedBoundary = [];
    const reported = [];
    const result = await sandbox.runLimitedCascadeRounds(
        [0, 1, 2, 3],
        2,
        async (round, control) => {
            started.push(round);
            if(round === 0){
                await Promise.resolve();
                throw originalError;
            }
            await new Promise(resolve => setImmediate(resolve));
            control.throwIfFailed();
            crossedBoundary.push(round);
        },
        {onError:error => reported.push(error)},
    );

    assert.equal(result.error, originalError, '必须保留首个原始错误对象');
    assert.deepEqual(reported, [originalError], '首错只应触发一次级联中止');
    assert.deepEqual(started.sort(), [0, 1], '首错后不得再领取新轮次');
    assert.deepEqual(crossedBoundary, [], '已在执行的 worker 必须在下一个边界停止');
}

function createVideoStateSandbox() {
    const generator = {id:'video-1', type:'video', running:false};
    const output = {
        id:'output-1',
        type:'output',
        _pending:[
            {
                id:'pending-1',
                canvasTaskId:'task-1',
                canvasTaskType:'online-video',
                cascadeTargetId:'video-1',
                run:{node:{id:'video-1'}},
            },
        ],
    };
    let saves = 0;
    const refreshed = [];
    const sandbox = {
        nodes:[generator, output],
        tr:key => key,
        isCascadeAbortError:err => Boolean(err?.isCascadeAbort),
        findPendingTask:taskId => {
            const pending = output._pending.find(item => item.canvasTaskId === taskId);
            return pending ? {out:output, pending} : null;
        },
        refreshRunNodes:(gen, out) => refreshed.push([gen?.id, out?.id]),
        scheduleSave:() => { saves += 1; },
        setTimeout,
    };
    vm.runInNewContext(
        sourceBetween('function canvasVideoPendingTasksForNode', 'async function createCanvasImageTask'),
        sandbox,
    );
    return {sandbox, generator, output, refreshed, get saves(){ return saves; }};
}

function testDetachedTaskRemainsTracked() {
    const state = createVideoStateSandbox();
    const pending = state.output._pending[0];
    const detached = state.sandbox.detachCanvasVideoTaskFromCascade('task-1');

    assert.equal(detached, true);
    assert.equal(state.output._pending.length, 1, '停止后不能删除已提交任务');
    assert.equal(pending.cascadeTargetId, undefined, '独立轮询不能继续携带已停止级联的信号');
    assert.equal(pending.canvasTaskStatus, 'polling');
    assert.equal(pending.recoverTaskId, 'task-1');
    assert.equal(state.generator.running, true, '仍有 pending 时节点必须保持占用，避免重复提交');
    assert.equal(state.saves, 1, '分离后的 pending 状态必须持久化');

    const abortError = Object.assign(new Error('stopped'), {isCascadeAbort:true});
    assert.equal(
        state.sandbox.shouldKeepCanvasVideoPending(pending, abortError, {task_id:'task-1'}),
        true,
        '级联停止异常不能触发 pending 删除',
    );
}

function testConcurrentPendingStateAggregation() {
    const state = createVideoStateSandbox();
    state.output._pending.push({
        id:'pending-2',
        canvasTaskId:'task-2',
        canvasTaskType:'online-video',
        run:{node:{id:'video-1'}},
    });

    state.output._pending = state.output._pending.filter(item => item.canvasTaskId !== 'task-1');
    state.sandbox.syncCanvasVideoNodeState(state.generator, {status:'succeeded'});
    assert.equal(state.generator.runStatus, 'running', '一个任务成功不能覆盖同节点其他 pending');
    assert.equal(state.generator.running, true);

    state.sandbox.syncCanvasVideoNodeState(state.generator, {
        status:'failed',
        error:'terminal task failure',
        cascadeFailed:true,
    });
    assert.equal(state.generator.runStatus, 'failed');
    assert.equal(state.generator.running, true, '其他任务仍在运行时节点应继续占用');

    state.output._pending = [];
    state.sandbox.syncCanvasVideoNodeState(state.generator, {status:'succeeded'});
    assert.equal(state.generator.runStatus, 'failed', '后完成的成功任务不能覆盖同批次终态失败');
    assert.equal(state.generator.runError, 'terminal task failure');
    assert.equal(state.generator.running, false);

    state.sandbox.syncCanvasVideoNodeState(state.generator, {status:'removed'});
    assert.equal(state.generator.runStatus, '');
}

async function runPollScenario({response, thrown, cascadeAbort=false}) {
    const calls = {fail:[], detach:0, continue:0};
    const pending = {canvasTaskId:'task-1', cascadeTargetId:'cascade-1'};
    const sandbox = {
        activeCanvasTaskPolls:new Set(),
        findPendingTask:() => ({out:{id:'output-1'}, pending}),
        ensureCascadeActive:() => {
            if(cascadeAbort){
                const error = new Error('stopped');
                error.isCascadeAbort = true;
                throw error;
            }
        },
        cascadeFetch:async () => {
            if(thrown) throw thrown;
            return response;
        },
        cascadeBackendRestartMessage:() => 'missing task',
        missingCanvasVideoTaskData:() => ({status:'failed', status_code:404}),
        responseErrorMessage:async () => 'http error',
        tr:key => key,
        normalizeCanvasTaskError:err => err.message,
        isCascadeAbortError:err => Boolean(err?.isCascadeAbort),
        detachCanvasVideoTaskFromCascade:() => { calls.detach += 1; return true; },
        continueCanvasVideoPollDetached:() => { calls.continue += 1; },
        failCanvasVideoTask:(...args) => calls.fail.push(args),
        refreshNodes:() => {},
        addGenerationLog:() => {},
        nowMs:() => 0,
        sleep:async () => {},
    };
    vm.runInNewContext(
        sourceBetween('async function pollCanvasVideoTask', 'async function waitCanvasVideoTaskResult'),
        sandbox,
    );
    const status = await sandbox.pollCanvasVideoTask('task-1', {cascadeTargetId:'cascade-1'});
    return {status, calls, sandbox};
}

async function testPollTerminalAndRecoverableErrors() {
    const stopped = await runPollScenario({cascadeAbort:true});
    assert.equal(stopped.status, 'aborted');
    assert.equal(stopped.calls.detach, 1);
    assert.equal(stopped.calls.continue, 1, '停止后必须安排脱离级联的独立轮询');
    assert.equal(stopped.sandbox.activeCanvasTaskPolls.size, 0);

    const missing = await runPollScenario({response:{ok:false, status:404}});
    assert.equal(missing.status, 'failed');
    assert.equal(missing.calls.fail.length, 1);
    assert.deepEqual(
        JSON.parse(JSON.stringify(missing.calls.fail[0][2])),
        {status:'failed', status_code:404},
        '404 必须携带明确终态数据进入统一清理',
    );

    const network = await runPollScenario({thrown:new Error('Failed to fetch')});
    assert.equal(network.status, 'failed');
    assert.equal(network.calls.fail.length, 1);
    assert.equal(network.calls.fail[0][2], undefined, '网络异常不能伪装成不可恢复终态');
}

function testPendingDeletePersists() {
    const pending = {id:'pending-1', run:{node:{id:'video-1'}}};
    const output = {id:'output-1', type:'output', _pending:[pending]};
    const generator = {id:'video-1', type:'video'};
    const del = {};
    let saves = 0;
    const wrap = {
        dataset:{pendingId:'pending-1'},
        querySelector:selector => selector === '.output-del' ? del : null,
        addEventListener:() => {},
    };
    const sandbox = {
        nodes:[output, generator],
        syncCanvasVideoNodeState:() => {},
        refreshRunNodes:() => {},
        scheduleSave:() => { saves += 1; },
        refreshNodes:() => {},
    };
    vm.runInNewContext(
        sourceBetween('function bindOutputWrap', 'function outputDomKeyForItem'),
        sandbox,
    );
    sandbox.bindOutputWrap(wrap, output);
    del.onclick({stopPropagation(){}});

    assert.equal(output._pending.length, 0);
    assert.equal(saves, 1, '删除 pending 卡片必须保存画布');

    const imagePending = {id:'pending-image', canvasTaskType:'online-image', run:{node:{id:'image-1'}}};
    const imageOutput = {id:'output-image', type:'output', _pending:[imagePending]};
    const imageGenerator = {id:'image-1', type:'generator', runStatus:'running'};
    const imageDelete = {};
    let videoStateSyncs = 0;
    const imageWrap = {
        dataset:{pendingId:'pending-image'},
        querySelector:selector => selector === '.output-del' ? imageDelete : null,
        addEventListener:() => {},
    };
    sandbox.nodes = [imageOutput, imageGenerator];
    sandbox.syncCanvasVideoNodeState = () => {
        videoStateSyncs += 1;
    };
    sandbox.bindOutputWrap(imageWrap, imageOutput);
    imageDelete.onclick({stopPropagation(){}});
    assert.equal(videoStateSyncs, 0, '删除非视频 pending 不能误改视频任务状态');
}

(async () => {
    await testParallelRoundsStopAtFirstError();
    testDetachedTaskRemainsTracked();
    testConcurrentPendingStateAggregation();
    await testPollTerminalAndRecoverableErrors();
    testPendingDeletePersists();
    console.log('canvas video lifecycle ok');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
