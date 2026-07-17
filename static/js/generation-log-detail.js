(function(){
    const SENSITIVE_KEY_RE = /(?:authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|password|passwd|secret|cookie|credential)/i;
    const DATA_URL_RE = /^data:/i;
    const MAX_STRING_LENGTH = 2400;
    const MAX_REQUEST_STRING_LENGTH = 12000;
    const MAX_ARRAY_LENGTH = 60;
    let activeModal = null;
    let restoreFocus = null;

    function t(key, fallback){
        const value = window.StudioI18n?.t?.(key);
        return value && value !== key ? value : fallback;
    }
    function escapeHtml(value){
        return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    }
    function safeResourceUrl(value){
        const raw = String(value || '').trim();
        if(!raw) return '';
        if(DATA_URL_RE.test(raw)) return t('canvas.logEmbeddedDataOmitted', '[embedded data omitted]');
        if(raw.startsWith('blob:')) return 'blob:[local object URL]';
        const queryIndex = raw.search(/[?#]/);
        return queryIndex >= 0 ? raw.slice(0, queryIndex) : raw;
    }
    function sanitize(value, key='', depth=0, maxStringLength=MAX_STRING_LENGTH){
        if(SENSITIVE_KEY_RE.test(String(key || ''))){
            if(String(key || '').toLowerCase() === 'authorization' && value === 'Bearer YOUR_API_KEY') return value;
            return t('canvas.logSensitiveValueHidden', '[sensitive value hidden]');
        }
        if(value == null || typeof value === 'number' || typeof value === 'boolean') return value;
        if(typeof value === 'string'){
            const resourceLike = /(?:url|uri|src|path|image|video|audio|reference|output)/i.test(String(key || ''));
            const clean = resourceLike ? safeResourceUrl(value) : value;
            return clean.length > maxStringLength ? `${clean.slice(0, maxStringLength)}...` : clean;
        }
        if(depth >= 6) return t('canvas.logNestedValueOmitted', '[nested value omitted]');
        if(Array.isArray(value)) return value.slice(0, MAX_ARRAY_LENGTH).map(item => sanitize(item, key, depth + 1, maxStringLength));
        if(typeof value === 'object'){
            return Object.fromEntries(Object.entries(value).map(([childKey, childValue]) => [childKey, sanitize(childValue, childKey, depth + 1, maxStringLength)]));
        }
        return String(value);
    }
    function sanitizeRequestDetails(value){
        return sanitize(value, '', 0, MAX_REQUEST_STRING_LENGTH);
    }
    function compactObject(value){
        if(!value || typeof value !== 'object' || Array.isArray(value)) return value;
        return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== '' && item !== undefined && item !== null));
    }
    function mediaSummary(item, index){
        const source = typeof item === 'string' ? {url:item} : (item || {});
        return compactObject({
            index:index + 1,
            name:source.name || source.filename || '',
            kind:source.kind || source.type || source.mediaKind || '',
            width:source.width || '',
            height:source.height || '',
            url:safeResourceUrl(source.url || source.path || source.src || source.uri || '')
        });
    }
    function responseSummary({status='', identifiers={}, outputs=[], error=''}){
        return sanitize(compactObject({
            status,
            identifiers:compactObject(identifiers || {}),
            output_count:(outputs || []).length,
            outputs:(outputs || []).map(mediaSummary),
            error:error || ''
        }));
    }
    function formatDate(value){
        if(!value) return '-';
        const date = new Date(value);
        if(Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString(window.StudioI18n?.lang?.() === 'en' ? 'en-US' : 'zh-CN');
    }
    function formatDuration(ms){
        const total = Math.max(0, Math.round(Number(ms || 0) / 1000));
        const minutes = Math.floor(total / 60);
        const seconds = total % 60;
        return minutes ? `${minutes}m ${String(seconds).padStart(2, '0')}s` : `${seconds}s`;
    }
    function detailData(log){
        const references = (log?.refs || []).map(mediaSummary);
        const outputs = (log?.outputs || []).map(mediaSummary);
        const storedRequest = log?.requestDetails && Object.keys(log.requestDetails).length ? sanitizeRequestDetails(log.requestDetails) : null;
        const upstreamRequest = storedRequest?.url && storedRequest?.body ? compactObject({
            method:storedRequest.method || 'POST',
            url:storedRequest.url,
            headers:storedRequest.headers || {},
            body:storedRequest.body
        }) : null;
        const request = upstreamRequest || (storedRequest ? compactObject({
            method:storedRequest.method || 'POST',
            endpoint:storedRequest.endpoint || '',
            body:compactObject({
                prompt:log?.prompt || '',
                model_prompt:log?.modelPrompt && log.modelPrompt !== log.prompt ? log.modelPrompt : '',
                reference_count:references.length,
                references,
                ...(storedRequest.parameters || {})
            })
        }) : sanitize(log?.request || {}));
        const response = responseSummary({status:log?.status, identifiers:log?.request || {}, outputs:log?.outputs || [], error:log?.error || ''});
        return {
            id:log?.id || '',
            status:log?.status || '',
            created_at:log?.createdAt || '',
            platform:log?.platform || '',
            node_type:log?.nodeType || '',
            model:log?.model || '',
            duration_ms:Number(log?.runMs || 0),
            request,
            curl_request:upstreamRequest ? buildBashCurl(upstreamRequest) : '',
            prompt:log?.prompt || '',
            model_prompt:log?.modelPrompt || '',
            references,
            outputs,
            response,
            error:log?.error || ''
        };
    }
    function jsonBlock(value){
        if(!value || (typeof value === 'object' && !Object.keys(value).length)){
            return `<div class="generation-log-detail-empty">${escapeHtml(t('canvas.logNotRecorded', 'Not recorded'))}</div>`;
        }
        return `<pre class="generation-log-detail-code">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
    }
    function textBlock(value){
        return value
            ? `<pre class="generation-log-detail-text">${escapeHtml(value)}</pre>`
            : `<div class="generation-log-detail-empty">${escapeHtml(t('canvas.logNotRecorded', 'Not recorded'))}</div>`;
    }
    function bashSingleQuote(value){
        return `'${String(value ?? '').replace(/'/g, `'"'"'`)}'`;
    }
    function buildBashCurl(request){
        if(!request?.url || !request?.body) return '';
        const parts = [`curl ${bashSingleQuote(request.url)}`];
        Object.entries(request.headers || {}).forEach(([key, value]) => {
            parts.push(`-H ${bashSingleQuote(`${key}: ${value}`)}`);
        });
        parts.push(`--data-raw ${bashSingleQuote(JSON.stringify(request.body, null, 2))}`);
        return parts.join(' \\\n  ');
    }
    function mediaRows(items){
        if(!items?.length) return `<div class="generation-log-detail-empty">${escapeHtml(t('canvas.logNone', 'None'))}</div>`;
        return `<div class="generation-log-detail-media-list">${items.map(item => `
            <div class="generation-log-detail-media-row">
                <span class="generation-log-detail-media-index">${escapeHtml(item.index || '')}</span>
                <div><strong>${escapeHtml(item.name || item.kind || '-')}</strong><span>${escapeHtml(item.url || '-')}</span></div>
            </div>`).join('')}</div>`;
    }
    function section(title, content, wide=false, action=''){
        return `<section class="generation-log-detail-section${wide ? ' wide' : ''}"><div class="generation-log-detail-section-head"><h3>${escapeHtml(title)}</h3>${action}</div>${content}</section>`;
    }
    function ensureModal(){
        let modal = document.getElementById('generationLogDetailModal');
        if(modal) return modal;
        modal = document.createElement('div');
        modal.id = 'generationLogDetailModal';
        modal.className = 'generation-log-detail-modal';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'generationLogDetailTitle');
        modal.innerHTML = `
            <div class="generation-log-detail-panel">
                <header class="generation-log-detail-head">
                    <div><h2 id="generationLogDetailTitle"></h2><p id="generationLogDetailSubtitle"></p></div>
                    <div class="generation-log-detail-actions">
                        <button type="button" data-log-detail-copy><i data-lucide="copy"></i></button>
                        <button type="button" data-log-detail-close><i data-lucide="x"></i></button>
                    </div>
                </header>
                <div class="generation-log-detail-body"></div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', event => {
            if(event.target === modal || event.target.closest('[data-log-detail-close]')) close();
        });
        return modal;
    }
    function close(){
        if(!activeModal) return;
        activeModal.classList.remove('open');
        activeModal = null;
        restoreFocus?.focus?.();
        restoreFocus = null;
    }
    async function copyText(button, value){
        let copied = false;
        try {
            await navigator.clipboard.writeText(value);
            copied = true;
        } catch(_) {
            try {
                const textarea = document.createElement('textarea');
                textarea.value = value;
                textarea.style.position = 'fixed';
                textarea.style.opacity = '0';
                document.body.appendChild(textarea);
                textarea.select();
                copied = document.execCommand('copy');
                textarea.remove();
            } catch(_) {}
        }
        const original = button.getAttribute('title') || '';
        button.classList.toggle('copied', copied);
        button.setAttribute('title', copied ? t('canvas.copied', 'Copied') : t('canvas.copyFailed', 'Copy failed'));
        setTimeout(() => {
            button.classList.remove('copied');
            button.setAttribute('title', original);
        }, 1200);
    }
    function copyDetail(button, data){
        return copyText(button, JSON.stringify(data, null, 2));
    }
    function open(log){
        if(!log) return;
        const modal = ensureModal();
        const data = detailData(log);
        const request = data.request;
        const curlRequest = data.curl_request;
        const modelPrompt = data.model_prompt && data.model_prompt !== data.prompt ? data.model_prompt : '';
        const statusText = data.status === 'failed' ? t('canvas.failed', 'Failed') : t('canvas.success', 'Success');
        const summary = [
            [t('canvas.logStatus', 'Status'), statusText],
            [t('canvas.logCreatedAt', 'Created at'), formatDate(data.created_at)],
            [t('canvas.logPlatform', 'Platform'), data.platform || '-'],
            [t('canvas.logModel', 'Model'), data.model || '-'],
            [t('canvas.logNodeType', 'Node type'), data.node_type || '-'],
            [t('canvas.logDuration', 'Duration'), formatDuration(data.duration_ms)]
        ];
        modal.querySelector('#generationLogDetailTitle').textContent = t('canvas.logDetails', 'Log details');
        modal.querySelector('#generationLogDetailSubtitle').textContent = `${formatDate(data.created_at)} · ${data.platform || '-'}`;
        const copyButton = modal.querySelector('[data-log-detail-copy]');
        const closeButton = modal.querySelector('[data-log-detail-close]');
        copyButton.setAttribute('title', t('canvas.logCopyDetails', 'Copy details'));
        copyButton.setAttribute('aria-label', t('canvas.logCopyDetails', 'Copy details'));
        closeButton.setAttribute('title', t('common.close', 'Close'));
        closeButton.setAttribute('aria-label', t('common.close', 'Close'));
        modal.querySelector('.generation-log-detail-body').innerHTML = `
            <section class="generation-log-detail-summary">${summary.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('')}</section>
            <div class="generation-log-detail-grid">
                ${curlRequest ? section(
                    t('canvas.logCurlRequest', 'Redacted input request (cURL / Bash)'),
                    textBlock(curlRequest),
                    true,
                    `<button type="button" class="generation-log-detail-section-copy" data-log-detail-copy-curl title="${escapeHtml(t('canvas.logCopyCurl', 'Copy cURL'))}" aria-label="${escapeHtml(t('canvas.logCopyCurl', 'Copy cURL'))}"><i data-lucide="copy"></i></button>`
                ) : ''}
                ${section(t('canvas.logRequest', 'Request snapshot'), jsonBlock(request), true)}
                ${section(t('canvas.logResponse', 'Response summary'), jsonBlock(data.response), true)}
                ${section(t('canvas.logPrompt', 'Prompt'), textBlock(data.prompt), true)}
                ${modelPrompt ? section(t('canvas.logModelPrompt', 'Model prompt'), textBlock(modelPrompt), true) : ''}
                ${section(t('canvas.logReferences', 'References'), mediaRows(data.references))}
                ${section(t('canvas.logOutputs', 'Outputs'), mediaRows(data.outputs))}
                ${data.error ? section(t('canvas.logError', 'Error'), textBlock(data.error), true) : ''}
            </div>`;
        copyButton.onclick = () => copyDetail(copyButton, data);
        const curlCopyButton = modal.querySelector('[data-log-detail-copy-curl]');
        if(curlCopyButton) curlCopyButton.onclick = () => copyText(curlCopyButton, curlRequest);
        restoreFocus = document.activeElement;
        activeModal = modal;
        modal.classList.add('open');
        window.lucide?.createIcons?.();
        closeButton.focus();
    }
    document.addEventListener('keydown', event => {
        if(event.key !== 'Escape' || !activeModal) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        close();
    }, true);

    window.StudioGenerationLogDetail = {
        open,
        close,
        sanitize,
        sanitizeRequestDetails,
        safeResourceUrl,
        mediaSummary,
        responseSummary,
        buildBashCurl,
        detailData
    };
})();
