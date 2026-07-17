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

console.log('generation log detail tests passed');
