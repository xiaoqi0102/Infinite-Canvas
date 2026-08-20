const assert = require('node:assert/strict');

require('../static/js/video-api-utils.js');

const api = globalThis.StudioVideoApi;

function issue(mode, model, counts) {
    const profile = api.videoProtocolProfile({video_request_mode:mode}, model, '');
    return api.videoProtocolReferenceIssue(profile, counts);
}

assert.deepEqual(
    issue(api.MODES.GEEKNOW, 'Kling-3.0', {image:2}),
    {code:'limit', kind:'image', count:1},
);
assert.deepEqual(
    issue(api.MODES.GEEKNOW, 'Kling-3.0', {video:1}),
    {code:'unsupported', kind:'video', count:0},
);
assert.deepEqual(
    issue(api.MODES.GEEKNOW, 'grok-imagine-video-1.5-preview', {image:2}),
    {code:'limit', kind:'image', count:1},
);

assert.deepEqual(
    issue(api.MODES.MEAI, 'sd-2', {image:10}),
    {code:'limit', kind:'image', count:9},
);
assert.deepEqual(
    issue(api.MODES.MEAI, 'sd-2-fast', {video:4}),
    {code:'limit', kind:'video', count:3},
);
const meaiProfile = api.videoProtocolProfile({base_url:'https://api.meai.cloud'}, 'sd-2', '');
assert.equal(meaiProfile.mode, api.MODES.MEAI);
assert.equal(meaiProfile.submitPath, '/v1/videos');
assert.equal(meaiProfile.taskPathPrefix, '/v1/videos/');
assert.deepEqual(meaiProfile.aspectRatios, ['1:1', '16:9', '9:16', '4:3', '3:4']);
assert.deepEqual(meaiProfile.resolutions, ['720p', '1080p']);
assert.equal(meaiProfile.supportsFrameRoles, true);
assert.equal(meaiProfile.supportsAdvancedOptions, false);

const aicostSeedance25 = api.videoProtocolProfile(
    {base_url:'https://www.aicost.me'},
    'seedance2.5-720p',
    '',
);
assert.equal(aicostSeedance25.mode, api.MODES.AICOST);
assert.equal(aicostSeedance25.submitPath, '/v1/videos');
assert.equal(aicostSeedance25.taskPathPrefix, '/v1/videos/');
assert.equal(aicostSeedance25.minDuration, 1);
assert.equal(aicostSeedance25.maxDuration, 30);
assert.equal(aicostSeedance25.maxImageReferences, 30);
assert.equal(aicostSeedance25.maxVideoReferences, 10);
assert.equal(aicostSeedance25.maxAudioReferences, 10);

const aicostH3 = api.videoProtocolProfile(
    {video_request_mode:api.MODES.AICOST},
    'minimax-h3',
    '',
);
assert.deepEqual(aicostH3.aspectRatios, ['21:9', '16:9', '4:3', '1:1', '3:4', '9:16']);
assert.deepEqual(aicostH3.resolutions, ['1440P']);
assert.equal(aicostH3.supportsFrameRoles, true);
assert.deepEqual(
    api.videoProtocolReferenceIssue(aicostH3, {video:1}),
    {code:'unsupported', kind:'video', count:0},
);

console.log('video api utils ok');
