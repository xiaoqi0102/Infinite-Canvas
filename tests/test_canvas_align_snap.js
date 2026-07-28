const assert = require('node:assert/strict');

require('../static/js/canvas-align-snap.js');

const snap = globalThis.CanvasAlignSnap;

// 导出的默认常量
assert.equal(snap.DEFAULT_THRESHOLD, 6);
assert.equal(snap.DEFAULT_EXTEND, 8);

// 1. 左缘对左缘:阈值内吸附,dx 为修正量,垂直参考线贯穿双方并外延(省略 threshold/extend 走默认值)
assert.deepEqual(
    snap.resolve({
        moving:{left:104, top:0, width:50, height:40},
        candidates:[{left:100, top:100, width:200, height:50}],
    }),
    {dx:-4, dy:0, guides:{v:[{x:100, y1:-8, y2:158}], h:[]}},
);

// 2. 超出阈值:不吸附、无参考线
assert.deepEqual(
    snap.resolve({
        moving:{left:120, top:0, width:50, height:40},
        candidates:[{left:100, top:100, width:200, height:50}],
        threshold:6, extend:8,
    }),
    {dx:0, dy:0, guides:{v:[], h:[]}},
);

// 3. 两候选取 |diff| 最小者:右缘贴 candB 左缘(-3)胜过右缘贴 candA 左缘(+5)
assert.deepEqual(
    snap.resolve({
        moving:{left:0, top:0, width:100, height:100},
        candidates:[
            {left:105, top:300, width:100, height:100},
            {left:97, top:600, width:100, height:100},
        ],
        threshold:6, extend:8,
    }),
    {dx:-3, dy:0, guides:{v:[{x:97, y1:-8, y2:708}], h:[]}},
);

// 4. 中心线对中心线:移动中线 50 吸到候选中线 53
assert.deepEqual(
    snap.resolve({
        moving:{left:0, top:0, width:100, height:100},
        candidates:[{left:13, top:400, width:80, height:60}],
        threshold:6, extend:8,
    }),
    {dx:3, dy:0, guides:{v:[{x:53, y1:-8, y2:468}], h:[]}},
);

// 5. 双轴同时吸附:右缘贴左缘(dx=4)+ 底缘贴顶缘(dy=-4),十字参考线
assert.deepEqual(
    snap.resolve({
        moving:{left:0, top:0, width:100, height:100},
        candidates:[{left:104, top:96, width:50, height:50}],
        threshold:6, extend:8,
    }),
    {dx:4, dy:-4, guides:{
        v:[{x:104, y1:-12, y2:154}],
        h:[{y:96, x1:-4, x2:162}],
    }},
);

// 6. 参考线范围 = 全部共线候选与被拖包围盒的并集 ± extend(两候选左缘同在 x=0)
assert.deepEqual(
    snap.resolve({
        moving:{left:6, top:0, width:100, height:100},
        candidates:[
            {left:0, top:200, width:30, height:50},
            {left:0, top:-300, width:44, height:40},
        ],
        threshold:6, extend:8,
    }),
    {dx:-6, dy:0, guides:{v:[{x:0, y1:-308, y2:258}], h:[]}},
);

// 7. 空候选:原样返回,不吸附
assert.deepEqual(
    snap.resolve({moving:{left:0, top:0, width:10, height:10}, candidates:[]}),
    {dx:0, dy:0, guides:{v:[], h:[]}},
);

// 8. 含非有限数值的候选被过滤,不参与吸附
assert.deepEqual(
    snap.resolve({
        moving:{left:0, top:0, width:10, height:10},
        candidates:[
            {left:NaN, top:0, width:10, height:10},
            {left:0, top:0, width:NaN, height:10},
        ],
        threshold:6, extend:8,
    }),
    {dx:0, dy:0, guides:{v:[], h:[]}},
);

// 9. 本就精确对齐(d=0):修正量为 0 但仍返回参考线,保证拖动中持续显示
assert.deepEqual(
    snap.resolve({
        moving:{left:0, top:0, width:100, height:100},
        candidates:[{left:0, top:150, width:30, height:30}],
        threshold:6, extend:8,
    }),
    {dx:0, dy:0, guides:{v:[{x:0, y1:-8, y2:188}], h:[]}},
);

console.log('canvas align snap ok');
