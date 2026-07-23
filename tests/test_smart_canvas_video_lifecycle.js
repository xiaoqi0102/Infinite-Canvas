const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/smart-canvas.js', 'utf8');

function sourceBetween(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.ok(start >= 0 && end > start, `无法定位源码片段：${startMarker}`);
    return source.slice(start, end);
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return {promise, resolve, reject};
}

async function testPendingFailureSemantics() {
    const transientNode = {
        id:'video-transient',
        running:true,
        queued:true,
        pending:1,
        pendingTasks:[{taskId:'video_network_error', kind:'video'}],
        images:[],
    };
    const terminalNode = {
        id:'video-terminal',
        running:true,
        queued:true,
        pending:1,
        pendingTasks:[{taskId:'video_failed', kind:'video'}],
        images:[],
    };
    const warnings = [];
    const sandbox = {
        nodes:[transientNode],
        console:{warn:(...args) => warnings.push(args)},
        liveSmartNode:node => sandbox.nodes.find(item => item.id === node?.id) || node,
        smartPendingTasks:node => Array.isArray(node?.pendingTasks) ? node.pendingTasks.filter(task => task?.taskId) : [],
        pollSmartCanvasTask:async () => {
            throw new Error('图像任务失败');
        },
        pollSmartCanvasVideoTask:async taskId => {
            if(taskId === 'video_failed'){
                const error = new Error('审核失败');
                error.canvasTaskFailed = true;
                throw error;
            }
            throw new Error('网络暂时不可用');
        },
        updateSmartTaskGenerationLog:() => {},
        providerIdForSmartTask:() => 'provider',
        setNodeJimengPending:() => {},
        resultMediaUrls:value => Array.isArray(value) ? value : [],
        cleanHistoryImages:value => value || [],
        stripImageGenerationMeta:value => value,
        copyMediaSizeFields:(value, target) => ({...target, ...value}),
        nowMs:() => 2000,
        mediaNodeDefaultScale:() => 1,
        addSmartGenerationLog:() => {},
        render:() => {},
        scheduleSave:() => {},
        toast:() => {},
        tr:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween('function removeSmartPendingTask', 'function updateSelectionBox'),
        sandbox
    );

    await assert.rejects(
        sandbox.resumeSmartPendingNode(transientNode),
        error => error.smartPendingPreserved === true && error.message === '网络暂时不可用',
    );
    assert.equal(transientNode.pendingTasks.length, 1);
    assert.equal(transientNode.pendingTasks[0].taskId, 'video_network_error');
    assert.equal(transientNode.pendingTasks[0].recoverTaskId, 'video_network_error');
    assert.equal(transientNode.pendingTasks[0].failed, true);
    assert.equal(transientNode.pending, 1);
    assert.equal(transientNode.running, false);
    assert.equal(transientNode.queued, false);

    sandbox.nodes = [terminalNode];
    await assert.rejects(
        sandbox.resumeSmartPendingNode(terminalNode),
        error => error.canvasTaskFailed === true && error.message === '审核失败',
    );
    assert.equal(terminalNode.pendingTasks, undefined);
    assert.equal(terminalNode.pending, 0);

    const imageNode = {
        id:'image-failed',
        pending:1,
        pendingTasks:[{taskId:'image_failed', kind:'image'}],
        images:[],
    };
    sandbox.nodes = [imageNode];
    const recoveries = sandbox.resumeSmartPendingTasks();
    assert.equal(recoveries.length, 1);
    await Promise.all(recoveries);
    assert.equal(warnings.length, 1, '后台恢复失败应被 catch 消费并记录，而不是产生 unhandledrejection');
}

async function testCascadeStopKeepsSubmittedVideo() {
    const resume = deferred();
    const node = {
        id:'video-output',
        running:true,
        pending:0,
        pendingTasks:[],
        images:[],
    };
    let saveCount = 0;
    const sandbox = {
        settings:{engine:'api', apiKind:'video'},
        isApiLikeEngine:() => true,
        runningHubSelectedModel:() => null,
        runApiVideoGeneration:async () => ({
            taskIds:['video_submitted'],
            providerId:'video-provider',
            model:'video-model',
        }),
        liveSmartNode:value => value,
        smartPendingTasks:value => Array.isArray(value?.pendingTasks) ? value.pendingTasks : [],
        resumeSmartPendingNode:() => resume.promise,
        render:() => {},
        scheduleSave:() => {},
        saveCanvas:async () => {
            saveCount += 1;
        },
        resultMediaUrls:value => Array.isArray(value) ? value : [],
        tr:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween('function smartCascadeAbortError', 'function requestSmartCascadeStop'),
        sandbox
    );
    vm.runInContext(
        sourceBetween('async function generateUrlsForCurrentSettings', 'async function generateComfyUrlsWithSettings'),
        sandbox
    );

    const runState = {stopRequested:false};
    sandbox.ensureSmartCascadeStopSignal(runState);
    const generation = sandbox.generateUrlsForCurrentSettings(
        node,
        '生成视频',
        [],
        sandbox.settings,
        {run:{}, startedAt:1000, runState},
    );
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(saveCount, 1, '已提交任务必须先持久化，再进入长轮询');
    assert.equal(node.pendingTasks.length, 1);
    assert.equal(node.pendingTasks[0].taskId, 'video_submitted');
    sandbox.signalSmartCascadeStop(runState);

    await assert.rejects(
        generation,
        error => error.smartCascadeStopped === true,
    );
    assert.equal(node.pendingTasks.length, 1, '停止级联不能删除已提交的视频任务');
    assert.equal(node.pending, 1);

    resume.reject(new Error('后台轮询后续失败'));
    await new Promise(resolve => setImmediate(resolve));
}

async function testParallelFailureStopsNewRounds() {
    const sandbox = {smartCascadeStopRequested:false};
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween('function smartCascadeAbortError', 'async function runSmartCascade(targetNode=null)'),
        sandbox
    );

    const activeWorker = deferred();
    const started = [];
    const finished = [];
    const runState = {stopRequested:false};
    sandbox.ensureSmartCascadeStopSignal(runState);

    const run = sandbox.runSmartCascadeRoundsWithLimit(
        [0, 1, 2, 3],
        2,
        async round => {
            started.push(round);
            if(round === 0){
                await Promise.resolve();
                throw new Error('首轮失败');
            }
            await activeWorker.promise;
            finished.push(round);
        },
        runState,
    );
    const observedRun = run.then(
        () => ({status:'fulfilled'}),
        error => ({status:'rejected', error}),
    );

    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(started, [0, 1], '首个错误后不应再领取后续轮次');
    let settled = false;
    observedRun.then(() => {
        settled = true;
    });
    await Promise.resolve();
    assert.equal(settled, false, '并行调度器必须等待已启动 worker 安全收束');

    activeWorker.resolve();
    const outcome = await observedRun;
    assert.equal(outcome.status, 'rejected');
    assert.equal(outcome.error.message, '首轮失败');
    assert.deepEqual(finished, [1]);
    assert.equal(runState.stopRequested, true);
}

(async () => {
    await testPendingFailureSemantics();
    await testCascadeStopKeepsSubmittedVideo();
    await testParallelFailureStopsNewRounds();
    console.log('smart canvas video lifecycle ok');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
