const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const window = {
    StudioI18n: {t:key => key},
};
const document = {
    addEventListener() {},
};

vm.runInNewContext(
    fs.readFileSync('static/js/generation-log-detail.js', 'utf8'),
    {window, document, navigator:{}, setTimeout},
    {filename:'generation-log-detail.js'}
);

const detail = window.StudioGenerationLogDetail;
const sanitized = detail.sanitizeRequestDetails({
    method:'POST',
    url:'https://newapi.megabyai.cc/v1/videos',
    headers:{
        Authorization:'Bearer YOUR_API_KEY',
        'X-Api-Key':'real-secret',
        'Content-Type':'application/json',
    },
    body:{
        model:'videos-mini',
        prompt:"导演说：'开始'\n下一镜",
        duration:5,
        ratio:'16:9',
        resolution:'720p',
        referenceImages:['https://media.example.com/person.jpg?token=signed-secret'],
        referenceVideos:['https://media.example.com/action.mp4#preview'],
        referenceAudios:['https://media.example.com/voice.mp3'],
    },
});

assert.strictEqual(sanitized.headers.Authorization, 'Bearer YOUR_API_KEY');
assert.notStrictEqual(sanitized.headers['X-Api-Key'], 'real-secret');
assert.strictEqual(sanitized.body.referenceImages[0], 'https://media.example.com/person.jpg');
assert.strictEqual(sanitized.body.referenceVideos[0], 'https://media.example.com/action.mp4');

const curl = detail.buildBashCurl(sanitized);
assert.match(curl, /^curl 'https:\/\/newapi\.megabyai\.cc\/v1\/videos'/);
assert.match(curl, /Authorization: Bearer YOUR_API_KEY/);
assert.match(curl, /referenceImages/);
assert.match(curl, /referenceVideos/);
assert.match(curl, /referenceAudios/);
assert.match(curl, /导演说/);
assert.doesNotMatch(curl, /real-secret|signed-secret/);
assert.match(curl, /'"'"'/);

const multipartRequest = detail.sanitizeRequestDetails({
    method:'POST',
    url:'https://api.example.com/v1/videos?token=multipart-url-secret',
    headers:{
        Authorization:'Bearer YOUR_API_KEY',
        'X-Api-Key':'multipart-api-secret',
        Accept:'application/json',
    },
    format:'multipart',
    form:{
        model:'grok-video',
        prompt:"导演说：'开始'",
        token:'multipart-form-secret',
    },
    files:[
        {field:'mode', value:'image-to-video'},
        {
            field:'input_reference',
            filename:'reference.png',
            content_type:'image/png',
            size:12345,
        },
    ],
});
const multipartCurl = detail.buildBashCurl(multipartRequest);
assert.match(multipartCurl, /^curl 'https:\/\/api\.example\.com\/v1\/videos'/);
assert.match(multipartCurl, /-F/);
assert.match(multipartCurl, /model=grok-video/);
assert.match(multipartCurl, /mode=image-to-video/);
assert.match(multipartCurl, /input_reference/);
assert.match(multipartCurl, /reference\.png/);
assert.match(multipartCurl, /image\/png/);
assert.doesNotMatch(multipartCurl, /--data-raw/);
assert.doesNotMatch(
    multipartCurl,
    /multipart-url-secret|multipart-api-secret|multipart-form-secret/
);

const attemptsLog = {
    id:'log-attempts',
    status:'failed',
    createdAt:Date.now(),
    platform:'Generic API',
    model:'videos-mini',
    prompt:'多次请求',
    requestDetails:{
        transport:'backend_http',
        context:{provider_id:'generic'},
        attempts:[
            {
                request:{
                    method:'POST',
                    url:'https://api.example.com/v1/videos?token=first-url-secret',
                    headers:{
                        Authorization:'Bearer YOUR_API_KEY',
                        'X-Api-Key':'first-api-secret',
                        'Content-Type':'application/json',
                    },
                    format:'json',
                    body:{model:'videos-mini', token:'first-body-secret'},
                },
                response:{
                    received:true,
                    status_code:404,
                    headers:{'Content-Type':'application/json'},
                    body:{
                        error:'not found',
                        token:'first-response-secret',
                    },
                },
            },
            {
                request:{
                    method:'POST',
                    url:'https://api.example.com/v2/videos?token=second-url-secret',
                    headers:{
                        Authorization:'Bearer YOUR_API_KEY',
                        Cookie:'second-cookie-secret',
                    },
                    format:'multipart',
                    form:{model:'videos-mini', token:'second-body-secret'},
                    files:[
                        {
                            field:'input_reference',
                            filename:'second.png',
                            content_type:'image/png',
                            size:42,
                        },
                    ],
                },
                response:{
                    received:false,
                    error_type:'ConnectError',
                    error:'connection failed',
                },
            },
        ],
    },
    request:{task_id:'local-task'},
    outputs:[],
    refs:[],
    error:'最终失败',
};
const attemptsData = detail.detailData(attemptsLog);
assert.strictEqual(attemptsData.request_attempts.length, 2);
assert.strictEqual(attemptsData.request.url, 'https://api.example.com/v2/videos');
assert.strictEqual(attemptsData.request.format, 'multipart');
assert.strictEqual(attemptsData.response.received, false);
assert.strictEqual(attemptsData.response.error_type, 'ConnectError');
assert.match(attemptsData.curl_request, /api\.example\.com\/v2\/videos/);
assert.match(attemptsData.curl_request, /-F/);
assert.doesNotMatch(attemptsData.curl_request, /api\.example\.com\/v1\/videos/);

const serializedAttempts = JSON.stringify(attemptsData);
assert.match(serializedAttempts, /api\.example\.com\\\/v1\\\/videos|api\.example\.com\/v1\/videos/);
assert.match(serializedAttempts, /api\.example\.com\\\/v2\\\/videos|api\.example\.com\/v2\/videos/);
assert.doesNotMatch(
    serializedAttempts,
    /first-url-secret|first-api-secret|first-body-secret|first-response-secret|second-url-secret|second-cookie-secret|second-body-secret/
);

assert.strictEqual(typeof detail.attemptStatusText, 'function');
assert.match(
    detail.attemptStatusText(attemptsData.request_attempts[0]),
    /404/
);
assert.match(
    detail.attemptStatusText(attemptsData.request_attempts[1]),
    /未收到|无响应|No response|ConnectError|连接/
);
assert.match(
    detail.attemptStatusText({
        response:{received:true, status_code:201, body:{id:'task-real-response'}},
    }),
    /201/
);

const successfulAttemptsData = detail.detailData({
    ...attemptsLog,
    status:'success',
    error:'',
    requestDetails:{
        ...attemptsLog.requestDetails,
        attempts:[
            attemptsLog.requestDetails.attempts[0],
            {
                ...attemptsLog.requestDetails.attempts[1],
                response:{
                    received:true,
                    status_code:201,
                    headers:{'X-Request-ID':'request-2'},
                    body:{
                        id:'real-upstream-task',
                        video_url:'https://cdn.example.com/output.mp4?token=output-secret',
                        token:'response-token-secret',
                    },
                },
            },
        ],
    },
});
assert.strictEqual(successfulAttemptsData.response.status_code, 201);
assert.strictEqual(successfulAttemptsData.response.body.id, 'real-upstream-task');
assert.strictEqual(
    successfulAttemptsData.response.body.video_url,
    'https://cdn.example.com/output.mp4'
);
assert.doesNotMatch(
    JSON.stringify(successfulAttemptsData),
    /output-secret|response-token-secret/
);

console.log('generation log detail tests passed');
