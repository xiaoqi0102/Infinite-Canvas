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

async function testCanvasCloudUploadRefreshesExpiredLink() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/expired.png';
    const firstUrl = 'https://files.sudashuiapi.com/proxy/uploads/refreshed-1.png';
    const secondUrl = 'https://files.sudashuiapi.com/proxy/uploads/refreshed-2.png';
    const localUrl = '/assets/reference.png';
    const mediaNode = {
        id:'image-1',
        type:'image',
        url:oldUrl,
        originalLocalUrl:localUrl,
        mediaKind:'image',
    };
    const videoNode = {
        id:'video-1',
        type:'video',
        tempShLinks:[{source:localUrl, url:oldUrl, service:'sudashui', kind:'image'}],
    };
    const requests = [];
    const uploadedUrls = [firstUrl, secondUrl];
    const sandbox = {
        nodes:[mediaNode, videoNode],
        window:{StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))}},
        mediaKindForRef:ref => ref?.kind || 'image',
        responseErrorMessage:async () => '上传失败',
        fetch:async (_url, options) => {
            requests.push(JSON.parse(options.body));
            return jsonResponse({url:uploadedUrls.shift(), service:'sudashui'});
        },
        resolveGeneratorRequestInputs:() => ({
            refs:[{
                url:mediaNode.url,
                ...(requests.length ? {} : {originalLocalUrl:mediaNode.originalLocalUrl}),
                nodeId:mediaNode.id,
                kind:'image',
            }],
        }),
        showErrorModal:() => {},
        refreshNodes:() => {},
        scheduleSave:() => {},
        copyTextToClipboard:async () => true,
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(canvasSource, 'function isCloudHostedMediaUrl', 'function applyManualVideoUrlToCanvasRef'),
        sandbox,
    );

    assert.deepEqual(
        JSON.parse(JSON.stringify(await sandbox.uploadCanvasVideosToCloud(videoNode.id))),
        [firstUrl],
    );
    assert.deepEqual(requests[0], {url:localUrl, service:'auto'});
    assert.equal(mediaNode.url, firstUrl);
    assert.equal(mediaNode.originalLocalUrl, localUrl);
    assert.deepEqual(
        JSON.parse(JSON.stringify(videoNode.tempShLinks)),
        [{source:localUrl, url:firstUrl, expires:'', service:'sudashui', kind:'image'}],
    );

    assert.deepEqual(
        JSON.parse(JSON.stringify(await sandbox.uploadCanvasVideosToCloud(videoNode.id))),
        [secondUrl],
    );
    assert.deepEqual(requests[1], {url:localUrl, service:'auto'});
    assert.equal(mediaNode.url, secondUrl);
    assert.deepEqual(
        JSON.parse(JSON.stringify(videoNode.tempShLinks)),
        [{source:localUrl, url:secondUrl, expires:'', service:'sudashui', kind:'image'}],
        '二次上传后必须替换旧缓存，而不是继续复用过期链接',
    );
}

async function testCanvasFailedRefreshFallsBackToLocalSource() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/expired-failed.png';
    const localUrl = '/assets/reference-failed.png';
    const mediaNode = {
        id:'image-failed',
        type:'image',
        url:oldUrl,
        originalLocalUrl:localUrl,
        mediaKind:'image',
    };
    const videoNode = {
        id:'video-failed',
        type:'video',
        tempShLinks:[{source:localUrl, url:oldUrl, service:'sudashui', kind:'image'}],
    };
    let saveCount = 0;
    const sandbox = {
        nodes:[mediaNode, videoNode],
        window:{StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))}},
        mediaKindForRef:ref => ref?.kind || 'image',
        responseErrorMessage:async () => '云端续传失败',
        fetch:async () => ({ok:false}),
        resolveGeneratorRequestInputs:() => ({
            refs:[{url:mediaNode.url, originalLocalUrl:mediaNode.originalLocalUrl, nodeId:mediaNode.id, kind:'image'}],
        }),
        showErrorModal:() => {},
        refreshNodes:() => {},
        scheduleSave:() => { saveCount += 1; },
        copyTextToClipboard:async () => true,
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(canvasSource, 'function isCloudHostedMediaUrl', 'function applyManualVideoUrlToCanvasRef'),
        sandbox,
    );

    await assert.rejects(
        sandbox.uploadCanvasVideosToCloud(videoNode.id),
        /云端续传失败/,
    );
    assert.deepEqual(JSON.parse(JSON.stringify(videoNode.tempShLinks)), []);
    assert.equal(mediaNode.url, localUrl, '续传失败后素材节点必须恢复本地地址');
    assert.equal(mediaNode.originalLocalUrl, localUrl);
    assert.equal(saveCount, 1, '失败后的缓存清理与本地回退必须持久化');
    assert.equal(
        sandbox.applyUploadedUrlToRefs([{url:oldUrl, originalLocalUrl:localUrl, kind:'image'}], videoNode)[0].url,
        localUrl,
        '后续生成不得继续套用已知过期链接',
    );
}

async function testCanvasSourceUrlIsPersistedForLaterRefresh() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/source-url-only.png';
    const localUrl = '/assets/source-url-only.png';
    const mediaNode = {id:'image-source-url', type:'image', url:oldUrl, mediaKind:'image'};
    const videoNode = {id:'video-source-url', type:'video', tempShLinks:[]};
    const requests = [];
    const uploadedUrls = [
        'https://files.sudashuiapi.com/proxy/uploads/source-url-new-1.png',
        'https://files.sudashuiapi.com/proxy/uploads/source-url-new-2.png',
    ];
    const sandbox = {
        nodes:[mediaNode, videoNode],
        window:{StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))}},
        mediaKindForRef:ref => ref?.kind || 'image',
        responseErrorMessage:async () => '上传失败',
        fetch:async (_url, options) => {
            requests.push(JSON.parse(options.body));
            return jsonResponse({url:uploadedUrls.shift(), service:'sudashui'});
        },
        resolveGeneratorRequestInputs:() => ({refs:[{
            url:mediaNode.url,
            ...(mediaNode.originalLocalUrl ? {originalLocalUrl:mediaNode.originalLocalUrl} : {sourceUrl:localUrl}),
            nodeId:mediaNode.id,
            kind:'image',
        }]}),
        showErrorModal:() => {},
        refreshNodes:() => {},
        scheduleSave:() => {},
        copyTextToClipboard:async () => true,
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(canvasSource, 'function isCloudHostedMediaUrl', 'function applyManualVideoUrlToCanvasRef'),
        sandbox,
    );

    await sandbox.uploadCanvasVideosToCloud(videoNode.id);
    assert.equal(mediaNode.originalLocalUrl, localUrl);
    await sandbox.uploadCanvasVideosToCloud(videoNode.id);
    assert.deepEqual(requests, [
        {url:localUrl, service:'auto'},
        {url:localUrl, service:'auto'},
    ]);
}

function testCanvasRefsKeepLocalSource() {
    const normalizedSandbox = {mediaKindForRef:ref => ref?.kind || 'image'};
    vm.createContext(normalizedSandbox);
    vm.runInContext(
        sourceBetween(canvasSource, 'function normalizedPromptMentionRef', 'function normalizedPromptRichTextParts'),
        normalizedSandbox,
    );
    const normalized = normalizedSandbox.normalizedPromptMentionRef({
        url:'https://files.sudashuiapi.com/proxy/uploads/expired.png',
        originalLocalUrl:'/assets/reference.png',
        sourceUrl:'/output/reference.png',
        kind:'image',
    });
    assert.equal(normalized.originalLocalUrl, '/assets/reference.png');
    assert.equal(normalized.sourceUrl, '/output/reference.png');

    const generator = {id:'video-target'};
    const imageNode = {
        id:'image-source',
        type:'image',
        url:'https://files.sudashuiapi.com/proxy/uploads/image-expired.png',
        originalLocalUrl:'/assets/image.png',
        name:'image.png',
    };
    const outputNode = {
        id:'output-source',
        type:'output',
        images:[{
            url:'https://files.sudashuiapi.com/proxy/uploads/output-expired.png',
            originalLocalUrl:'/output/image.png',
            kind:'image',
        }],
    };
    const generatedNode = {id:'generated-source', type:'video'};
    const generatorSandbox = {
        connections:[
            {from:imageNode.id, to:generator.id},
            {from:outputNode.id, to:generator.id},
            {from:generatedNode.id, to:generator.id},
        ],
        nodes:[imageNode, outputNode, generatedNode, generator],
        CANVAS_MEDIA_OUTPUT_TYPES:['video'],
        outputUrlValue:item => item?.url || '',
        mediaKindForOutputItem:item => item?.kind || 'image',
        outputImageName:() => '',
        generatedImageRefs:() => [{
            url:'https://files.sudashuiapi.com/proxy/uploads/video-expired.mp4',
            originalLocalUrl:'/output/video.mp4',
            kind:'video',
        }],
        mediaKindForNode:node => node.mediaKind || 'image',
    };
    vm.createContext(generatorSandbox);
    vm.runInContext(
        sourceBetween(canvasSource, 'function generatorSources(gen){', 'function orderedSources'),
        generatorSandbox,
    );
    const sources = generatorSandbox.generatorSources(generator);
    const refs = sources.flatMap(source => source.refs || []);
    assert.equal(refs.find(ref => ref.nodeId === imageNode.id).originalLocalUrl, '/assets/image.png');
    assert.equal(refs.find(ref => ref.nodeId === outputNode.id).originalLocalUrl, '/output/image.png');
    assert.equal(refs.find(ref => ref.nodeId === generatedNode.id).originalLocalUrl, '/output/video.mp4');
}

async function testSmartCanvasCloudUploadBypassesTransientCache() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/expired-smart.png';
    const firstUrl = 'https://files.sudashuiapi.com/proxy/uploads/refreshed-smart-1.png';
    const secondUrl = 'https://files.sudashuiapi.com/proxy/uploads/refreshed-smart-2.png';
    const localUrl = '/assets/smart-reference.png';
    let refs = [{url:oldUrl, originalLocalUrl:localUrl, kind:'image'}];
    const requests = [];
    const uploadedUrls = [firstUrl, secondUrl];
    const toasts = [];
    const sandbox = {
        settings:{videoTempShLinks:[]},
        transientSmartCloudLinks:[{source:localUrl, url:oldUrl, service:'sudashui', kind:'image'}],
        window:{StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))}},
        mediaKindForItem:ref => ref?.kind || 'image',
        localDisplayUrlForMediaItem:ref => ref?.originalLocalUrl || ref?.sourceUrl || ref?.url || '',
        isCloudHostedMediaUrl:url => /^https?:\/\//i.test(String(url || '')),
        smartResponseErrorMessage:async () => '上传失败',
        fetch:async (_url, options) => {
            requests.push(JSON.parse(options.body));
            return jsonResponse({url:uploadedUrls.shift(), service:'sudashui'});
        },
        activeSettingsSubject:() => ({id:'smart-video'}),
        savePromptDraftForCurrent:() => {},
        dynamicParams:null,
        inputThumbsRow:{
            querySelectorAll:() => refs.map((ref, index) => ({dataset:{
                url:ref.url || '',
                sourceUrl:ref.originalLocalUrl || ref.sourceUrl || ref.url || '',
                nodeId:ref.nodeId || '',
                imageIndex:String(index),
            }})),
            querySelector:() => null,
        },
        persistActiveSmartSettings:() => {},
        scheduleSave:() => {},
        render:() => {},
        toast:message => toasts.push(message),
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(smartSource, 'function tempShUploadedUrlFor', 'function rhRequiredLabel'),
        sandbox,
    );

    assert.deepEqual(
        JSON.parse(JSON.stringify(await sandbox.uploadCurrentSmartVideosToCloud())),
        [firstUrl],
    );
    assert.deepEqual(requests[0], {url:localUrl, service:'auto'});
    assert.equal(sandbox.tempShUploadedUrlFor(localUrl), firstUrl);

    assert.deepEqual(
        JSON.parse(JSON.stringify(await sandbox.uploadCurrentSmartVideosToCloud())),
        [secondUrl],
    );
    assert.deepEqual(requests[1], {url:localUrl, service:'auto'});
    assert.equal(sandbox.tempShUploadedUrlFor(localUrl), secondUrl);

    refs = [{url:oldUrl, kind:'image'}];
    const requestCount = requests.length;
    assert.deepEqual(
        JSON.parse(JSON.stringify(await sandbox.uploadCurrentSmartVideosToCloud())),
        [],
    );
    assert.equal(requests.length, requestCount, '没有本地副本时不得下载并重传任意公网 URL');
    assert.equal(toasts.at(-1), '当前输入图片或视频已是云端链接');
}

async function testSmartCanvasFailedRefreshDropsExpiredCache() {
    const oldUrl = 'https://files.sudashuiapi.com/proxy/uploads/expired-smart-failed.png';
    const localUrl = '/assets/smart-reference-failed.png';
    const refs = [{url:oldUrl, originalLocalUrl:localUrl, kind:'image'}];
    const sandbox = {
        settings:{videoTempShLinks:[]},
        transientSmartCloudLinks:[{source:localUrl, url:oldUrl, service:'sudashui', kind:'image'}],
        window:{StudioVideoApi:{isPublicHttpUrl:url => /^https?:\/\//i.test(String(url || ''))}},
        mediaKindForItem:ref => ref?.kind || 'image',
        localDisplayUrlForMediaItem:ref => ref?.originalLocalUrl || ref?.sourceUrl || ref?.url || '',
        isCloudHostedMediaUrl:url => /^https?:\/\//i.test(String(url || '')),
        smartResponseErrorMessage:async () => '智能画布续传失败',
        fetch:async () => ({ok:false}),
        activeSettingsSubject:() => ({id:'smart-video-failed'}),
        savePromptDraftForCurrent:() => {},
        dynamicParams:null,
        inputThumbsRow:{
            querySelectorAll:() => refs.map((ref, index) => ({dataset:{
                url:ref.url,
                sourceUrl:ref.originalLocalUrl,
                nodeId:'',
                imageIndex:String(index),
            }})),
            querySelector:() => null,
        },
        persistActiveSmartSettings:() => {},
        scheduleSave:() => {},
        render:() => {},
        toast:() => {},
        tr:key => key,
        trf:key => key,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        sourceBetween(smartSource, 'function tempShUploadedUrlFor', 'function rhRequiredLabel'),
        sandbox,
    );

    await assert.rejects(
        sandbox.uploadCurrentSmartVideosToCloud(),
        /智能画布续传失败/,
    );
    assert.equal(
        sandbox.tempShUploadedUrlFor(localUrl),
        localUrl,
        '智能画布续传失败后不得继续返回旧内存链接',
    );
}

async function main() {
    await testCanvasCloudUploadRefreshesExpiredLink();
    await testCanvasFailedRefreshFallsBackToLocalSource();
    await testCanvasSourceUrlIsPersistedForLaterRefresh();
    testCanvasRefsKeepLocalSource();
    await testSmartCanvasCloudUploadBypassesTransientCache();
    await testSmartCanvasFailedRefreshDropsExpiredCache();
    console.log('cloud media reupload ok');
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
