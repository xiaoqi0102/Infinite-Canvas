(function(){
    const SENSITIVE_KEY_RE = /(?:authorization|api[-_]?key|(?:^|[-_])token(?:$|[-_])|access[-_]?token|refresh[-_]?token|password|passwd|secret|cookie|credential)/i;
    const BASE64_KEY_RE = /(?:base64|b64|file[_-]?data|binary)/i;
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
            if(BASE64_KEY_RE.test(String(key || ''))) return t('canvas.logEmbeddedDataOmitted', '[embedded data omitted]');
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
    function attemptStatusText(attempt){
        const response = attempt?.response || {};
        const statusCode = Number(response.status_code || 0);
        if(statusCode) return `HTTP ${statusCode}`;
        if(response.received === false){
            const details = [
                t('canvas.logNoResponse', 'No response'),
                response.error_type || '',
                response.error || ''
            ].filter(Boolean);
            return details.join(' / ');
        }
        if(response.received === true) return t('canvas.logResponseReceived', 'Response received');
        return t('canvas.logWaitingResponse', 'Waiting for response');
    }
    function localRequestSnapshot(log, storedRequest, references){
        const localDetails = log?.localRequestDetails && Object.keys(log.localRequestDetails).length
            ? sanitizeRequestDetails(log.localRequestDetails)
            : (storedRequest?.endpoint ? storedRequest : null);
        if(!localDetails) return null;
        return compactObject({
            method:localDetails.method || 'POST',
            endpoint:localDetails.endpoint || '',
            body:compactObject({
                prompt:log?.prompt || '',
                model_prompt:log?.modelPrompt && log.modelPrompt !== log.prompt ? log.modelPrompt : '',
                reference_count:references.length,
                references,
                ...(localDetails.parameters || {})
            })
        });
    }
    function detailData(log){
        const references = (log?.refs || []).map(mediaSummary);
        const outputs = (log?.outputs || []).map(mediaSummary);
        const storedRequest = log?.requestDetails && Object.keys(log.requestDetails).length ? sanitizeRequestDetails(log.requestDetails) : null;
        const requestAttempts = storedRequest?.transport === 'backend_http' && Array.isArray(storedRequest.attempts)
            ? storedRequest.attempts.map(attempt => {
                const request = sanitizeRequestDetails(attempt?.request || {});
                const response = sanitizeRequestDetails(attempt?.response || {});
                return {request, response, curl_request:buildBashCurl(request)};
            })
            : [];
        const latestAttempt = requestAttempts[requestAttempts.length - 1] || null;
        const upstreamRequest = storedRequest?.url && (storedRequest?.body || storedRequest?.form || storedRequest?.files) ? compactObject({
            method:storedRequest.method || 'POST',
            url:storedRequest.url,
            headers:storedRequest.headers || {},
            format:storedRequest.format || 'json',
            body:storedRequest.body,
            form:storedRequest.form,
            files:storedRequest.files
        }) : null;
        const localRequest = localRequestSnapshot(log, storedRequest, references);
        const request = latestAttempt?.request || upstreamRequest || localRequest || sanitize(log?.request || {});
        const resultSummary = responseSummary({status:log?.status, identifiers:log?.request || {}, outputs:log?.outputs || [], error:log?.error || ''});
        const response = latestAttempt?.response || resultSummary;
        return {
            id:log?.id || '',
            status:log?.status || '',
            created_at:log?.createdAt || '',
            platform:log?.platform || '',
            node_type:log?.nodeType || '',
            model:log?.model || '',
            duration_ms:Number(log?.runMs || 0),
            local_request:localRequest,
            request_attempts:requestAttempts,
            request,
            curl_request:latestAttempt?.curl_request || (upstreamRequest ? buildBashCurl(upstreamRequest) : ''),
            prompt:log?.prompt || '',
            model_prompt:log?.modelPrompt || '',
            references,
            outputs,
            response,
            result_summary:resultSummary,
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
    function curlFieldValue(value){
        if(value == null) return '';
        return typeof value === 'string' ? value : JSON.stringify(value);
    }
    function buildBashCurl(request){
        if(!request?.url) return '';
        const parts = [`curl ${bashSingleQuote(request.url)}`];
        const method = String(request.method || 'POST').toUpperCase();
        if(method !== 'GET') parts.push(`-X ${bashSingleQuote(method)}`);
        Object.entries(request.headers || {}).forEach(([key, value]) => {
            parts.push(`-H ${bashSingleQuote(`${key}: ${value}`)}`);
        });
        const format = String(request.format || (request.files ? 'multipart' : request.form ? 'form' : 'json')).toLowerCase();
        if(format === 'multipart'){
            Object.entries(request.form || {}).forEach(([key, value]) => {
                parts.push(`-F ${bashSingleQuote(`${key}=${curlFieldValue(value)}`)}`);
            });
            (request.files || []).forEach(file => {
                const field = String(file?.field || 'file');
                if(Object.prototype.hasOwnProperty.call(file || {}, 'value')){
                    parts.push(`-F ${bashSingleQuote(`${field}=${curlFieldValue(file.value)}`)}`);
                    return;
                }
                const filename = String(file?.filename || 'attachment.bin');
                const contentType = file?.content_type ? `;type=${file.content_type}` : '';
                parts.push(`-F ${bashSingleQuote(`${field}=@${filename}${contentType}`)}`);
            });
        } else if(format === 'form'){
            Object.entries(request.form || request.body || {}).forEach(([key, value]) => {
                parts.push(`--data-urlencode ${bashSingleQuote(`${key}=${curlFieldValue(value)}`)}`);
            });
        } else if(Object.prototype.hasOwnProperty.call(request, 'body')){
            parts.push(`--data-raw ${bashSingleQuote(JSON.stringify(request.body, null, 2))}`);
        }
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
    function logStatusText(status){
        const normalized = String(status || '').toLowerCase();
        const key = {
            queued:'canvas.logStatusQueued',
            submitting:'canvas.logStatusSubmitting',
            polling:'canvas.logStatusPolling',
            running:'canvas.logStatusRunning',
            success:'canvas.logStatusSucceeded',
            succeeded:'canvas.logStatusSucceeded',
            failed:'canvas.logStatusFailed',
        }[normalized];
        if(key) return t(key, normalized || '-');
        return status || '-';
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
        const statusText = logStatusText(data.status);
        const attemptSections = data.request_attempts.map((attempt, index) => {
            const number = index + 1;
            const label = `${t('canvas.logUpstreamAttempt', 'Upstream attempt')} ${number}`;
            const status = attemptStatusText(attempt);
            const requestSection = section(
                `${label} / ${status} / ${t('canvas.logHttpRequest', 'HTTP request')}`,
                jsonBlock(attempt.request),
                true
            );
            const curlSection = attempt.curl_request ? section(
                `${label} / ${t('canvas.logCurlRequest', 'Redacted input request (cURL / Bash)')}`,
                textBlock(attempt.curl_request),
                true,
                `<button type="button" class="generation-log-detail-section-copy" data-log-detail-copy-attempt-curl="${index}" title="${escapeHtml(t('canvas.logCopyAttemptCurl', 'Copy this cURL'))}" aria-label="${escapeHtml(t('canvas.logCopyAttemptCurl', 'Copy this cURL'))}"><i data-lucide="copy"></i></button>`
            ) : '';
            const responseSection = section(
                `${label} / ${t('canvas.logHttpResponse', 'HTTP response')}`,
                jsonBlock(attempt.response),
                true
            );
            return `${requestSection}${curlSection}${responseSection}`;
        }).join('');
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
                ${data.local_request ? section(t('canvas.logLocalRequest', 'Local task request'), jsonBlock(data.local_request), true) : ''}
                ${data.request_attempts.length ? attemptSections : (curlRequest ? section(
                    t('canvas.logCurlRequest', 'Redacted input request (cURL / Bash)'),
                    textBlock(curlRequest),
                    true,
                    `<button type="button" class="generation-log-detail-section-copy" data-log-detail-copy-curl title="${escapeHtml(t('canvas.logCopyCurl', 'Copy cURL'))}" aria-label="${escapeHtml(t('canvas.logCopyCurl', 'Copy cURL'))}"><i data-lucide="copy"></i></button>`
                ) : '')}
                ${!data.request_attempts.length && !data.local_request ? section(t('canvas.logRequest', 'Request snapshot'), jsonBlock(request), true) : ''}
                ${section(t('canvas.logResultSummary', 'Log result summary'), jsonBlock(data.result_summary), true)}
                ${section(t('canvas.logPrompt', 'Prompt'), textBlock(data.prompt), true)}
                ${modelPrompt ? section(t('canvas.logModelPrompt', 'Model prompt'), textBlock(modelPrompt), true) : ''}
                ${section(t('canvas.logReferences', 'References'), mediaRows(data.references))}
                ${section(t('canvas.logOutputs', 'Outputs'), mediaRows(data.outputs))}
                ${data.error ? section(t('canvas.logError', 'Error'), textBlock(data.error), true) : ''}
            </div>`;
        copyButton.onclick = () => copyDetail(copyButton, data);
        const curlCopyButton = modal.querySelector('[data-log-detail-copy-curl]');
        if(curlCopyButton) curlCopyButton.onclick = () => copyText(curlCopyButton, curlRequest);
        modal.querySelectorAll('[data-log-detail-copy-attempt-curl]').forEach(button => {
            const attempt = data.request_attempts[Number(button.dataset.logDetailCopyAttemptCurl || 0)];
            button.onclick = () => copyText(button, attempt?.curl_request || '');
        });
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
        attemptStatusText,
        buildBashCurl,
        detailData
    };
})();
