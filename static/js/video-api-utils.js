(function(global){
    'use strict';

    const MODES = Object.freeze({
        VIDEOS: 'openai-videos-generations',
        VIDEO: 'openai-video-generations',
        SUDASHUI: 'sudashui-video-generations',
        MEGABYAI: 'megabyai-v1-videos',
        GEEKNOW: 'geeknow-v1-videos',
        TUDOU: 'tudou-video',
        AICOST: 'aicost-video',
    });
    const SUDASHUI_MODE = MODES.SUDASHUI;
    const SUDASHUI_DURATIONS = Object.freeze(Array.from({length:12}, (_, index) => index + 4));
    const SUDASHUI_ASPECT_RATIOS = Object.freeze(['16:9', '9:16', '1:1', '4:3', '3:4', '21:9', 'adaptive']);
    const SUDASHUI_ASPECT_RATIO_SET = new Set(SUDASHUI_ASPECT_RATIOS);
    const MEGABYAI_DURATIONS = Object.freeze(Array.from({length:12}, (_, index) => index + 4));
    const MEGABYAI_ASPECT_RATIOS = Object.freeze(['16:9', '9:16', '1:1']);
    const MEGABYAI_OFFICIAL_HOSTNAMES = Object.freeze(['newapi.megabyai.cc', 'cn.megabyai.cc']);
    const MEGABYAI_OFFICIAL_HOSTNAME_SET = new Set(MEGABYAI_OFFICIAL_HOSTNAMES);
    const AICOST_OFFICIAL_HOSTNAME_SET = new Set(['aicost.xyz', 'www.aicost.xyz']);
    const GEEKNOW_OFFICIAL_HOSTNAMES = Object.freeze(['geeknow.ai', 'api.geeknow.ai']);
    const GEEKNOW_OFFICIAL_HOSTNAME_SET = new Set(GEEKNOW_OFFICIAL_HOSTNAMES);
    const TUDOU_OFFICIAL_HOSTNAMES = Object.freeze(['api.ai-tudou.net']);
    const TUDOU_OFFICIAL_HOSTNAME_SET = new Set(TUDOU_OFFICIAL_HOSTNAMES);
    const RESOLUTION_TOKEN_RE = /(?:^|[^a-z0-9])(480p|720p|1080p|2160p|1k|2k|4k|8k)(?=$|[^a-z0-9])/gi;

    function normalizeVideoRequestMode(value){
        const mode = String(value || '').trim().toLowerCase();
        if(['openai-video', 'single-video', 'video-generations'].includes(mode)) return MODES.VIDEO;
        if(['openai-videos', 'videos-generations'].includes(mode)) return MODES.VIDEOS;
        if(['sudashui', 'sudashui-video'].includes(mode)) return MODES.SUDASHUI;
        if(['megabyai', 'megabyai-videos'].includes(mode)) return MODES.MEGABYAI;
        if(['geeknow', 'geeknow-video', 'geeknow-videos'].includes(mode)) return MODES.GEEKNOW;
        if(['tudou', 'tudou-video', 'tudou-videos'].includes(mode)) return MODES.TUDOU;
        if(['aicost', 'aicost-video', 'aicost-videos'].includes(mode)) return MODES.AICOST;
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

    function isAICostBaseUrl(value){
        return AICOST_OFFICIAL_HOSTNAME_SET.has(videoProviderHostname(value));
    }

    function isGeekNowBaseUrl(value){
        return GEEKNOW_OFFICIAL_HOSTNAME_SET.has(videoProviderHostname(value));
    }

    function isTudouBaseUrl(value){
        return TUDOU_OFFICIAL_HOSTNAME_SET.has(videoProviderHostname(value));
    }

    function providerVideoRequestMode(providerOrMode){
        if(providerOrMode && typeof providerOrMode === 'object'){
            if(isAICostBaseUrl(providerOrMode.base_url || providerOrMode.baseUrl)) return MODES.AICOST;
            if(isMegabyAiBaseUrl(providerOrMode.base_url || providerOrMode.baseUrl)) return MODES.MEGABYAI;
            if(isGeekNowBaseUrl(providerOrMode.base_url || providerOrMode.baseUrl)) return MODES.GEEKNOW;
            if(isTudouBaseUrl(providerOrMode.base_url || providerOrMode.baseUrl)) return MODES.TUDOU;
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

    function isGeekNowVideoMode(providerOrMode){
        return providerVideoRequestMode(providerOrMode) === MODES.GEEKNOW;
    }

    function isTudouVideoMode(providerOrMode){
        return providerVideoRequestMode(providerOrMode) === MODES.TUDOU;
    }

    function isAICostVideoMode(providerOrMode){
        return providerVideoRequestMode(providerOrMode) === MODES.AICOST;
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

    function geekNowModelProfile(model){
        const value = String(model || '').trim().toLowerCase();
        const commonMediaLimits = {
            maxVideoReferences:0,
            maxAudioReferences:0,
            supportsVideoReferences:false,
            supportsAudioReferences:false,
            supportsFrameRoles:false,
        };
        if(value === 'kling-3.0' || value === 'kling-3.0-omni'){
            return {
                ...commonMediaLimits,
                maxImageReferences:1,
            };
        }
        if(value.startsWith('grok-video-')){
            const durations = value.endsWith('-pro') ? [10] : value.endsWith('-max') ? [15] : [10, 15];
            return {
                ...commonMediaLimits,
                durations,
                minDuration:durations[0],
                maxDuration:durations[durations.length - 1],
                defaultDuration:durations[0],
                aspectRatios:['2:3', '3:2', '1:1'],
                defaultAspectRatio:'1:1',
                resolutions:['480p', '540p', '720p', '1080p'],
                defaultResolution:'1080p',
                maxImageReferences:value.startsWith('grok-video-1.5') ? 1 : null,
            };
        }
        if(value === 'grok-imagine-video' || value === 'grok-imagine-video-1.5-preview'){
            return {
                ...commonMediaLimits,
                durations:null,
                minDuration:1,
                maxDuration:60,
                defaultDuration:5,
                aspectRatios:['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '2:1', '1:2', '19.5:9', '9:19.5', '20:9', '9:20'],
                defaultAspectRatio:'16:9',
                resolutions:['480p', '720p'],
                defaultResolution:'720p',
                maxImageReferences:value === 'grok-imagine-video-1.5-preview' ? 1 : null,
            };
        }
        return {};
    }

    function tudouModelProfile(model){
        const value = String(model || '').trim().toLowerCase();
        if(value === 'grok-imagine-video' || value === 'grok-imagine-video-1.5'){
            return {
                durations:[6, 10, 12, 16, 20],
                minDuration:6,
                maxDuration:20,
                defaultDuration:6,
                aspectRatios:['9:16', '16:9', '1:1'],
                defaultAspectRatio:'9:16',
                resolutions:['480p', '720p'],
                defaultResolution:'720p',
                submitPath:'/v1/videos',
                taskPathPrefix:'/v1/videos/',
                requiresImageReference:value === 'grok-imagine-video-1.5',
                minImageReferences:value === 'grok-imagine-video-1.5' ? 1 : 0,
                maxImageReferences:value === 'grok-imagine-video-1.5' ? 1 : 7,
                maxVideoReferences:0,
                maxAudioReferences:0,
                supportsVideoReferences:false,
                supportsAudioReferences:false,
                supportsFrameRoles:false,
            };
        }
        if(value === 'sora2'){
            return {
                durations:[4, 8, 12],
                minDuration:4,
                maxDuration:12,
                defaultDuration:8,
                aspectRatios:['16:9', '9:16'],
                defaultAspectRatio:'16:9',
                resolutions:[''],
                defaultResolution:'',
                submitPath:'/v1/videos/generations',
                taskPathPrefix:'/v1/tasks/',
                maxImageReferences:1,
                maxVideoReferences:0,
                maxAudioReferences:0,
                supportsVideoReferences:false,
                supportsAudioReferences:false,
                supportsFrameRoles:false,
            };
        }
        return {};
    }

    function aicostModelProfile(model){
        const value = String(model || '').trim().toLowerCase();
        if(value.includes('grok')){
            const fixedResolution = value.includes('1080p') ? '1080p' : value.includes('480p') ? '480p' : '';
            return {
                durations:[6, 10, 15], minDuration:6, maxDuration:15, defaultDuration:10,
                aspectRatios:['16:9', '9:16', '1:1', '2:3', '3:2'], defaultAspectRatio:'16:9',
                resolutions:fixedResolution ? [fixedResolution] : ['480p', '720p', '1080p'],
                defaultResolution:fixedResolution || '720p',
                requiresImageReference:true, minImageReferences:1,
                maxImageReferences:1, maxVideoReferences:0, maxAudioReferences:0,
                supportsVideoReferences:false, supportsAudioReferences:false, supportsFrameRoles:false,
            };
        }
        return {
            minDuration:4, maxDuration:15, defaultDuration:6,
            aspectRatios:['16:9', '9:16', '1:1'], defaultAspectRatio:'16:9',
            resolutions:['480p', '720p'], defaultResolution:'720p',
            maxImageReferences:9, maxVideoReferences:3, maxAudioReferences:3,
            supportsVideoReferences:true, supportsAudioReferences:true, supportsFrameRoles:false,
        };
    }

    function normalizeVideoProtocolValues(profile, values){
        const result = {...(values || {})};
        const durations = Array.isArray(profile?.durations) ? profile.durations.map(Number) : null;
        let duration = Math.round(Number(result.duration) || Number(profile?.defaultDuration) || 5);
        if(durations?.length && !durations.includes(duration)) duration = Number(profile.defaultDuration || durations[0]);
        else {
            if(profile?.minDuration != null && Number.isFinite(Number(profile.minDuration))) duration = Math.max(Number(profile.minDuration), duration);
            if(profile?.maxDuration != null && Number.isFinite(Number(profile.maxDuration))) duration = Math.min(Number(profile.maxDuration), duration);
        }
        result.duration = duration;
        const aspectRatios = Array.isArray(profile?.aspectRatios) ? profile.aspectRatios : null;
        if(aspectRatios?.length && !aspectRatios.includes(String(result.aspectRatio || ''))){
            result.aspectRatio = profile.defaultAspectRatio || aspectRatios[0];
        }
        const resolutions = Array.isArray(profile?.resolutions) ? profile.resolutions : null;
        if(resolutions?.length && !resolutions.includes(String(result.resolution || ''))){
            result.resolution = profile.defaultResolution || resolutions[0];
        }
        return result;
    }

    function videoProtocolReferenceIssue(profile, counts={}){
        const image = Math.max(0, Number(counts.image) || 0);
        const video = Math.max(0, Number(counts.video) || 0);
        const audio = Math.max(0, Number(counts.audio) || 0);
        const minimum = Math.max(1, Number(profile?.minImageReferences) || 1);
        if(profile?.requiresImageReference && image < minimum) return {code:'image_required', kind:'image', count:minimum};
        for(const [kind, count] of Object.entries({image, video, audio})){
            const supportKey = kind === 'video' ? 'supportsVideoReferences' : kind === 'audio' ? 'supportsAudioReferences' : '';
            if(supportKey && profile?.[supportKey] === false && count > 0) return {code:'unsupported', kind, count:0};
            const limitKey = `max${kind[0].toUpperCase()}${kind.slice(1)}References`;
            const limit = profile?.[limitKey];
            if(limit != null && Number.isFinite(Number(limit)) && count > Number(limit)) return {code:'limit', kind, count:Number(limit)};
        }
        return null;
    }

    function videoProtocolProfile(provider, model, storedValue){
        const mode = providerVideoRequestMode(provider);
        const sudashui = mode === MODES.SUDASHUI;
        const megabyai = mode === MODES.MEGABYAI;
        const geeknow = mode === MODES.GEEKNOW;
        const tudou = mode === MODES.TUDOU;
        const aicost = mode === MODES.AICOST;
        const geeknowProfile = geeknow ? geekNowModelProfile(model) : {};
        const tudouProfile = tudou ? tudouModelProfile(model) : {};
        const aicostProfile = aicost ? aicostModelProfile(model) : {};
        const pluginProfile = geeknow ? geeknowProfile : tudou ? tudouProfile : aicost ? aicostProfile : {};
        const resolution = effectiveVideoResolution(mode, model, storedValue) || pluginProfile.defaultResolution || '';
        return Object.freeze({
            mode,
            isSudashui: sudashui,
            isMegabyAi: megabyai,
            isGeekNow: geeknow,
            isTudou: tudou,
            isAICost: aicost,
            submitPath: pluginProfile.submitPath || ((megabyai || geeknow) ? '/v1/videos' : mode === MODES.VIDEOS ? '/v1/videos/generations' : '/v1/video/generations'),
            taskPathPrefix: pluginProfile.taskPathPrefix || ((megabyai || geeknow) ? '/v1/videos/' : mode === MODES.VIDEOS ? '/v1/videos/generations/' : '/v1/video/generations/'),
            durations: sudashui ? SUDASHUI_DURATIONS : megabyai ? MEGABYAI_DURATIONS : (pluginProfile.durations || null),
            minDuration: (sudashui || megabyai) ? 4 : (pluginProfile.minDuration ?? null),
            maxDuration: (sudashui || megabyai) ? 15 : (pluginProfile.maxDuration ?? null),
            defaultDuration: (sudashui || megabyai) ? 5 : (pluginProfile.defaultDuration ?? null),
            aspectRatios: sudashui ? SUDASHUI_ASPECT_RATIOS : megabyai ? MEGABYAI_ASPECT_RATIOS : (pluginProfile.aspectRatios || null),
            defaultAspectRatio: pluginProfile.defaultAspectRatio || null,
            resolutions: megabyai ? ['', '480p', '720p'] : (pluginProfile.resolutions || null),
            defaultResolution: pluginProfile.defaultResolution || null,
            requiresImageReference:Boolean(pluginProfile.requiresImageReference),
            minImageReferences:pluginProfile.minImageReferences ?? 0,
            maxImageReferences:pluginProfile.maxImageReferences ?? null,
            maxVideoReferences:pluginProfile.maxVideoReferences ?? null,
            maxAudioReferences:pluginProfile.maxAudioReferences ?? null,
            resolution,
            resolutionLabel: resolution ? resolution.toUpperCase() : '',
            resolutionReadOnly: sudashui,
            officialAssetsEnabled: sudashui && isSudashuiOfficialModel(model),
            supportsVideoReferences: pluginProfile.supportsVideoReferences ?? (!tudou && !(sudashui && isSudashuiOfficialModel(model))),
            supportsAudioReferences: pluginProfile.supportsAudioReferences ?? !tudou,
            supportsAdvancedOptions: !megabyai,
            supportsFrameRoles: pluginProfile.supportsFrameRoles ?? (!megabyai && !tudou),
            supportsTrustedAssets: !aicost && !geeknow && !tudou,
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
        GEEKNOW_OFFICIAL_HOSTNAMES,
        TUDOU_OFFICIAL_HOSTNAMES,
        normalizeVideoRequestMode,
        videoProviderHostname,
        isMegabyAiBaseUrl,
        isAICostBaseUrl,
        isGeekNowBaseUrl,
        isTudouBaseUrl,
        providerVideoRequestMode,
        isSudashuiVideoMode,
        isMegabyAiVideoMode,
        isGeekNowVideoMode,
        isTudouVideoMode,
        isAICostVideoMode,
        isSudashuiOfficialModel,
        normalizeSudashuiAspectRatio,
        isAllowedSudashuiDuration,
        parseOfficialAssetIndexes,
        inferModelResolution,
        effectiveVideoResolution,
        geekNowModelProfile,
        tudouModelProfile,
        aicostModelProfile,
        normalizeVideoProtocolValues,
        videoProtocolReferenceIssue,
        videoProtocolProfile,
    });
})(typeof window !== 'undefined' ? window : globalThis);
