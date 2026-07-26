const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const canvasSource = fs.readFileSync('static/js/canvas.js', 'utf8');
const smartSource = fs.readFileSync('static/js/smart-canvas.js', 'utf8');

function sourceBetween(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.ok(start >= 0 && end > start, `无法定位源码片段：${startMarker}`);
    return source.slice(start, end);
}

function jsonResponse(data) {
    return {
        ok:true,
        json:async () => data,
    };
}

function testPreflightRunsBeforeTaskCreation() {
    const canvasRun = sourceBetween(
        canvasSource,
        'async function runVideoNode',
        'async function uploadCanvasUrlToComfy',
    );
    assert.ok(
        canvasRun.indexOf('await preflightCanvasVideoMedia') < canvasRun.indexOf('await createCanvasVideoTask'),
        '普通画布必须在创建视频任务前完成素材预检',
    );
    const smartRun = sourceBetween(
        smartSource,
        'async function runApiVideoGeneration',
        'async function runModelscopeGeneration',
    );
    assert.ok(
        smartRun.indexOf('await preflightSmartVideoMedia') < smartRun.indexOf('await createSmartCanvasVideoTask'),
        '智能画布必须在创建视频任务前完成素材预检',
    );
}

async function testCanvasPreflightRefreshesPayloadAndCache() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/expired.png';
    const newUrl = 'https://files.sudashuiapi.com/proxy/uploads/refreshed.png';
    const localUrl = '/assets/material.png';
    const imageNode = {
        id:'image-1',
        type:'image',
        url:oldUrl,
        originalLocalUrl:localUrl,
        mediaKind:'image',
    };
    const videoNode = {
        id:'video-1',
        tempShLinks:[{source:localUrl, url:oldUrl, service:'sudashui', kind:'image'}],
    };
    let requestBody;
    const sandbox = {
        nodes:[imageNode, videoNode],
        window:{StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))}},
        mediaKindForRef:ref => ref?.kind || 'image',
        fetch:async (_url, options) => {
            requestBody = JSON.parse(options.body);
            return jsonResponse({
                refreshed_count:1,
                materials:[{
                    url:newUrl,
                    source:localUrl,
                    service:'sudashui',
                    refreshed:true,
                }],
            });
        },
        responseErrorMessage:async () => '素材检查失败',
        manualVideoUrlForNode:() => '',
        refreshNodes:() => {},
        scheduleSave:() => {},
        setStatus:() => {},
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(canvasSource, 'function isCloudHostedMediaUrl', 'async function uploadCanvasVideosToCloud'),
        sandbox,
    );
    const ref = {
        url:oldUrl,
        originalLocalUrl:localUrl,
        nodeId:imageNode.id,
        kind:'image',
    };
    const prepared = await sandbox.preflightCanvasVideoMedia(videoNode, [ref]);

    assert.deepEqual(requestBody.materials, [{
        url:oldUrl,
        source_url:localUrl,
        kind:'image',
    }]);
    assert.equal(prepared.refs[0].url, newUrl);
    assert.equal(imageNode.url, newUrl);
    assert.equal(imageNode.originalLocalUrl, localUrl);
    assert.deepEqual(
        JSON.parse(JSON.stringify(videoNode.tempShLinks)),
        [{source:localUrl, url:newUrl, expires:'', service:'sudashui', kind:'image'}],
    );
}

async function testSmartPreflightPersistsRefreshedCache() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/expired-smart.png';
    const newUrl = 'https://files.sudashuiapi.com/proxy/uploads/refreshed-smart.png';
    const localUrl = '/output/material.png';
    const sourceNode = {
        id:'source-1',
        images:[{url:oldUrl, originalLocalUrl:localUrl, kind:'image'}],
    };
    const runSettings = {
        videoTempShLinks:[{source:localUrl, url:oldUrl, service:'sudashui', kind:'image'}],
    };
    const settingsOwner = {id:'generator-1', runSettings:{}};
    const sandbox = {
        nodes:[sourceNode, settingsOwner],
        settings:{videoTempShLinks:[]},
        transientSmartCloudLinks:[],
        window:{
            StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))},
            StudioI18n:{lang:() => 'zh'},
        },
        mediaKindForItem:ref => ref?.kind || 'image',
        localDisplayUrlForMediaItem:ref => ref?.originalLocalUrl || ref?.sourceUrl || ref?.url || '',
        smartResponseErrorMessage:async () => '素材检查失败',
        fetch:async () => jsonResponse({
            refreshed_count:1,
            materials:[{
                url:newUrl,
                source:localUrl,
                service:'sudashui',
                refreshed:true,
            }],
        }),
        settingsForStorage:value => JSON.parse(JSON.stringify(value || {})),
        persistActiveSmartSettings:() => {},
        scheduleSave:() => {},
        toast:() => {},
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(smartSource, 'function tempShUploadedUrlFor', 'function applyManualVideoUrlToSmartRef'),
        sandbox,
    );
    const ref = {
        url:oldUrl,
        originalLocalUrl:localUrl,
        nodeId:sourceNode.id,
        imageIndex:0,
        kind:'image',
    };
    const prepared = await sandbox.preflightSmartVideoMedia(
        [ref],
        runSettings,
        {settingsOwner},
    );

    assert.equal(prepared[0].url, newUrl);
    assert.equal(sourceNode.images[0].url, newUrl);
    assert.equal(sourceNode.images[0].originalLocalUrl, localUrl);
    assert.deepEqual(
        JSON.parse(JSON.stringify(runSettings.videoTempShLinks)),
        [{source:localUrl, url:newUrl, expires:'', service:'sudashui', kind:'image'}],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(settingsOwner.runSettings.videoTempShLinks)),
        [{source:localUrl, url:newUrl, expires:'', service:'sudashui', kind:'image'}],
    );
}

async function main() {
    testPreflightRunsBeforeTaskCreation();
    await testCanvasPreflightRefreshesPayloadAndCache();
    await testSmartPreflightPersistsRefreshedCache();
    console.log('video material preflight ok');
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
