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
    issue(api.MODES.TUDOU, 'grok-imagine-video-1.5', {image:0}),
    {code:'image_required', kind:'image', count:1},
);
assert.deepEqual(
    issue(api.MODES.TUDOU, 'grok-imagine-video-1.5', {image:2}),
    {code:'limit', kind:'image', count:1},
);
assert.deepEqual(
    issue(api.MODES.TUDOU, 'grok-imagine-video', {image:8}),
    {code:'limit', kind:'image', count:7},
);
assert.deepEqual(
    issue(api.MODES.TUDOU, 'sora2', {audio:1}),
    {code:'unsupported', kind:'audio', count:0},
);

console.log('video api utils ok');
