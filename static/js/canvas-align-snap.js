// 画布节点拖动"智能对齐吸附"共享计算模块(纯计算,零 DOM 依赖)。
// 供 static/js/canvas.js(普通画布)与 static/js/smart-canvas.js(智能画布)共用,
// 避免两条平行画布实现的吸附逻辑各自漂移;node 下可直接 require 后经
// globalThis.CanvasAlignSnap 做单元测试(见 tests/test_canvas_align_snap.js)。
(function(global){
    'use strict';

    // 默认吸附阈值(世界单位;调用方通常传 屏幕像素/viewport.scale)
    const DEFAULT_THRESHOLD = 6;
    // 吸附后判定"精确共线"的容差:吸收浮点误差,并收集恰好也在同一线上的其他候选
    const ALIGN_EPSILON = 0.5;
    // 参考线两端相对涉及矩形范围的外延长度(世界单位)
    const DEFAULT_EXTEND = 8;

    // 矩形在某轴上的三个对齐锚点:X 轴 → 左/中/右,Y 轴 → 顶/中/底
    function anchorsX(rect){ return [rect.left, rect.left + rect.width / 2, rect.left + rect.width]; }
    function anchorsY(rect){ return [rect.top, rect.top + rect.height / 2, rect.top + rect.height]; }

    function isFiniteRect(rect){
        return Boolean(rect)
            && Number.isFinite(rect.left) && Number.isFinite(rect.top)
            && Number.isFinite(rect.width) && Number.isFinite(rect.height);
    }

    // 单轴最优吸附:遍历 候选 × 移动锚点 × 候选锚点 全部组合,
    // 取阈值内 |差| 严格最小者;同差保留先遍历到的(候选数组顺序,结果稳定)。
    function bestAxisDelta(movingAnchors, candidates, anchorsOf, threshold){
        let best = null;
        candidates.forEach(rect => {
            const candAnchors = anchorsOf(rect);
            movingAnchors.forEach(a => {
                candAnchors.forEach(b => {
                    const d = b - a;
                    if(Math.abs(d) <= threshold && (best === null || Math.abs(d) < Math.abs(best))) best = d;
                });
            });
        });
        return best;
    }

    // 按吸附后的锚点位置收集参考线:每个精确共线的位置一条,
    // 线的范围为"被拖包围盒 + 全部命中候选"在垂直方向上的并集 ± extend。
    // movingAnchors/anchorsOf 为吸附轴锚点,spanOf/movingSpan 为另一轴上的 [起,止] 区间。
    function collectGuides(movingAnchors, candidates, anchorsOf, spanOf, movingSpan, extend){
        const guides = [];
        movingAnchors.forEach(pos => {
            // 宽/高为 0 时移动锚点重合,同一位置只画一条线
            if(guides.some(g => Math.abs(g.pos - pos) < ALIGN_EPSILON)) return;
            let lo = Infinity, hi = -Infinity;
            candidates.forEach(rect => {
                if(anchorsOf(rect).some(v => Math.abs(v - pos) < ALIGN_EPSILON)){
                    const span = spanOf(rect);
                    lo = Math.min(lo, span[0]);
                    hi = Math.max(hi, span[1]);
                }
            });
            if(!Number.isFinite(lo)) return;   // 该锚点没有精确共线的候选
            guides.push({
                pos,
                lo:Math.min(lo, movingSpan[0]) - extend,
                hi:Math.max(hi, movingSpan[1]) + extend,
            });
        });
        return guides;
    }

    /**
     * 计算拖动包围盒相对候选矩形的吸附修正与参考线(输入输出均为世界坐标)。
     * @param {Object} input
     * @param {{left:number,top:number,width:number,height:number}} input.moving
     *        被拖集合的整体包围盒,位置已包含当前原始拖动位移
     * @param {Array<{left:number,top:number,width:number,height:number}>} input.candidates
     *        吸附候选矩形(需已排除被拖节点自身)
     * @param {number} [input.threshold=DEFAULT_THRESHOLD] 吸附阈值
     * @param {number} [input.extend=DEFAULT_EXTEND] 参考线两端外延
     * @returns {{dx:number, dy:number, guides:{v:Array<{x,y1,y2}>, h:Array<{y,x1,x2}>}}}
     *          dx/dy 为需叠加到原始位移上的修正量;无吸附时为 0 且 guides 两数组为空
     */
    function resolve(input){
        const empty = {dx:0, dy:0, guides:{v:[], h:[]}};
        const moving = input && input.moving;
        if(!isFiniteRect(moving)) return empty;
        const candidates = ((input && input.candidates) || []).filter(isFiniteRect);
        if(!candidates.length) return empty;
        const threshold = Number.isFinite(input.threshold) ? input.threshold : DEFAULT_THRESHOLD;
        const extend = Number.isFinite(input.extend) ? input.extend : DEFAULT_EXTEND;

        // 两轴独立取最优吸附;d=0(本就对齐)也算命中,以便持续显示参考线
        const bestX = bestAxisDelta(anchorsX(moving), candidates, anchorsX, threshold);
        const bestY = bestAxisDelta(anchorsY(moving), candidates, anchorsY, threshold);
        if(bestX === null && bestY === null) return empty;

        const dx = bestX === null ? 0 : bestX;
        const dy = bestY === null ? 0 : bestY;
        // 参考线按吸附后的最终位置收集,可能同时命中多条(如左缘贴 A 左、同时右缘贴 B 右)
        const snapped = {left:moving.left + dx, top:moving.top + dy, width:moving.width, height:moving.height};
        const guides = {v:[], h:[]};
        if(bestX !== null){
            guides.v = collectGuides(
                anchorsX(snapped), candidates, anchorsX,
                rect => [rect.top, rect.top + rect.height],
                [snapped.top, snapped.top + snapped.height],
                extend
            ).map(g => ({x:g.pos, y1:g.lo, y2:g.hi}));
        }
        if(bestY !== null){
            guides.h = collectGuides(
                anchorsY(snapped), candidates, anchorsY,
                rect => [rect.left, rect.left + rect.width],
                [snapped.left, snapped.left + snapped.width],
                extend
            ).map(g => ({y:g.pos, x1:g.lo, x2:g.hi}));
        }
        return {dx, dy, guides};
    }

    global.CanvasAlignSnap = Object.freeze({
        DEFAULT_THRESHOLD,
        ALIGN_EPSILON,
        DEFAULT_EXTEND,
        resolve,
    });
})(typeof window !== 'undefined' ? window : globalThis);
