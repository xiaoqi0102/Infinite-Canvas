(function(global){
    'use strict';

    const MODES = Object.freeze({
        VIDEOS: 'openai-videos-generations',
        VIDEO: 'openai-video-generations',
        SUDASHUI: 'sudashui-video-generations',
        MEGABYAI: 'megabyai-v1-videos',
    });
    const SUDASHUI_MODE = MODES.SUDASHUI;
    const SUDASHUI_DURATIONS = Object.freeze(Array.from({length:12}, (_, index) => index + 4));
    const SUDASHUI_ASPECT_RATIOS = Object.freeze(['16:9', '9:16', '1:1', '4:3', '3:4', '21:9', 'adaptive']);
    const SUDASHUI_ASPECT_RATIO_SET = new Set(SUDASHUI_ASPECT_RATIOS);
    const MEGABYAI_DURATIONS = Object.freeze(Array.from({length:12}, (_, index) => index + 4));
    const MEGABYAI_ASPECT_RATIOS = Object.freeze(['16:9', '9:16', '1:1']);
    const MEGABYAI_OFFICIAL_HOSTNAMES = Object.freeze(['newapi.megabyai.cc', 'cn.megabyai.cc']);
    const MEGABYAI_OFFICIAL_HOSTNAME_SET = new Set(MEGABYAI_OFFICIAL_HOSTNAMES);
    const RESOLUTION_TOKEN_RE = /(?:^|[^a-z0-9])(480p|720p|1080p|2160p|1k|2k|4k|8k)(?=$|[^a-z0-9])/gi;

    function normalizeVideoRequestMode(value){
        const mode = String(value || '').trim().toLowerCase();
        if(['openai-video', 'single-video', 'video-generations'].includes(mode)) return MODES.VIDEO;
        if(['openai-videos', 'videos-generations'].includes(mode)) return MODES.VIDEOS;
        if(['sudashui', 'sudashui-video'].includes(mode)) return MODES.SUDASHUI;
        if(['megabyai', 'megabyai-videos'].includes(mode)) return MODES.MEGABYAI;
        return Object.values(MODES).includes(mode) ? mode : MODES.VIDEOS;
    }

    function videoProviderHostname(value){
        const text = String(value || '').trim();
        if(!text) return '';
        try {
            const hostname = new URL(text).hostname.toLowerCase();
            if(hostname) return hostname;
        } catch (_) {}
        try {
            return new URL(`https://${text.replace(/^\/+/, '')}`).hostname.toLowerCase();
        } catch (_) { return ''; }
    }

    function isMegabyAiBaseUrl(value){
        return MEGABYAI_OFFICIAL_HOSTNAME_SET.has(videoProviderHostname(value));
    }

    function providerVideoRequestMode(providerOrMode){
        if(providerOrMode && typeof providerOrMode === 'object'){
            if(isMegabyAiBaseUrl(providerOrMode.base_url || providerOrMode.baseUrl)) return MODES.MEGABYAI;
            return normalizeVideoRequestMode(
                providerOrMode.video_request_mode
                || providerOrMode.videoRequestMode
                || providerOrMode.request_mode
                || providerOrMode.mode
            );
        }
        return normalizeVideoRequestMode(providerOrMode);
    }

    function isSudashuiVideoMode(providerOrMode){
        return providerVideoRequestMode(providerOrMode) === MODES.SUDASHUI;
    }

    function isMegabyAiVideoMode(providerOrMode){
        return providerVideoRequestMode(providerOrMode) === MODES.MEGABYAI;
    }

    function isSudashuiOfficialModel(model){
        return String(model || '').trim().toLowerCase().startsWith('sdas-gf-');
    }

    function normalizeSudashuiAspectRatio(value){
        const ratio = String(value || '').trim().toLowerCase();
        const normalized = ratio === 'keep_ratio' ? 'adaptive' : ratio;
        return SUDASHUI_ASPECT_RATIO_SET.has(normalized) ? normalized : '';
    }

    function isAllowedSudashuiDuration(value){
        const duration = Number(value);
        return Number.isInteger(duration) && duration >= 4 && duration <= 15;
    }

    function officialAssetIndexError(code, message){
        const error = new Error(message);
        error.code = code;
        return error;
    }

    // 界面输入使用一基编号；返回提交本地任务所需的严格零基索引。
    function parseOfficialAssetIndexes(value, imageCount){
        if(value == null || (typeof value === 'string' && !value.trim())) return [];
        const parts = Array.isArray(value) ? value : String(value).trim().split(/[,，\s]+/).filter(Boolean);
        const count = imageCount == null || imageCount === '' ? null : Number(imageCount);
        if(count != null && (!Number.isInteger(count) || count < 0)){
            throw officialAssetIndexError('invalid_image_count', '图片数量无效');
        }
        const seen = new Set();
        return parts.map(part => {
            const raw = typeof part === 'number' ? String(part) : String(part || '').trim();
            if(!/^\d+$/.test(raw)){
                throw officialAssetIndexError('invalid_official_asset_index', '真人素材编号必须是正整数');
            }
            const oneBased = Number(raw);
            if(!Number.isSafeInteger(oneBased) || oneBased < 1){
                throw officialAssetIndexError('invalid_official_asset_index', '真人素材编号必须从 1 开始');
            }
            const zeroBased = oneBased - 1;
            if(count != null && zeroBased >= count){
                throw officialAssetIndexError('official_asset_index_out_of_range', `真人素材编号 ${oneBased} 超出图片数量`);
            }
            if(seen.has(zeroBased)){
                throw officialAssetIndexError('duplicate_official_asset_index', `真人素材编号 ${oneBased} 重复`);
            }
            seen.add(zeroBased);
            return zeroBased;
        });
    }

    function inferModelResolution(model){
        const text = String(model || '');
        RESOLUTION_TOKEN_RE.lastIndex = 0;
        let match;
        let resolution = '';
        while((match = RESOLUTION_TOKEN_RE.exec(text)) !== null){
            resolution = String(match[1] || '').toLowerCase();
        }
        RESOLUTION_TOKEN_RE.lastIndex = 0;
        return resolution;
    }

    function effectiveVideoResolution(provider, model, storedValue){
        if(isSudashuiVideoMode(provider)) return inferModelResolution(model);
        return String(storedValue || '').trim();
    }

    function videoProtocolProfile(provider, model, storedValue){
        const mode = providerVideoRequestMode(provider);
        const sudashui = mode === MODES.SUDASHUI;
        const megabyai = mode === MODES.MEGABYAI;
        const resolution = effectiveVideoResolution(mode, model, storedValue);
        return Object.freeze({
            mode,
            isSudashui: sudashui,
            isMegabyAi: megabyai,
            submitPath: megabyai ? '/v1/videos' : mode === MODES.VIDEOS ? '/v1/videos/generations' : '/v1/video/generations',
            taskPathPrefix: megabyai ? '/v1/videos/' : mode === MODES.VIDEOS ? '/v1/videos/generations/' : '/v1/video/generations/',
            durations: sudashui ? SUDASHUI_DURATIONS : megabyai ? MEGABYAI_DURATIONS : null,
            minDuration: (sudashui || megabyai) ? 4 : null,
            maxDuration: (sudashui || megabyai) ? 15 : null,
            aspectRatios: sudashui ? SUDASHUI_ASPECT_RATIOS : megabyai ? MEGABYAI_ASPECT_RATIOS : null,
            resolutions: megabyai ? ['', '480p', '720p'] : null,
            resolution,
            resolutionLabel: resolution ? resolution.toUpperCase() : '',
            resolutionReadOnly: sudashui,
            officialAssetsEnabled: sudashui && isSudashuiOfficialModel(model),
            supportsVideoReferences: !(sudashui && isSudashuiOfficialModel(model)),
            supportsAdvancedOptions: !megabyai,
            supportsFrameRoles: !megabyai,
        });
    }

    global.StudioVideoApi = Object.freeze({
        MODES,
        SUDASHUI_MODE,
        SUDASHUI_DURATIONS,
        SUDASHUI_ASPECT_RATIOS,
        MEGABYAI_DURATIONS,
        MEGABYAI_ASPECT_RATIOS,
        MEGABYAI_OFFICIAL_HOSTNAMES,
        normalizeVideoRequestMode,
        videoProviderHostname,
        isMegabyAiBaseUrl,
        providerVideoRequestMode,
        isSudashuiVideoMode,
        isMegabyAiVideoMode,
        isSudashuiOfficialModel,
        normalizeSudashuiAspectRatio,
        isAllowedSudashuiDuration,
        parseOfficialAssetIndexes,
        inferModelResolution,
        effectiveVideoResolution,
        videoProtocolProfile,
    });
})(typeof window !== 'undefined' ? window : globalThis);
