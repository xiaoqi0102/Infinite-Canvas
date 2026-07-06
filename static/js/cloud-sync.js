const CLOUD_SYNC_JIANGUOYUN_URL = 'https://dav.jianguoyun.com/dav/';

const cloudSyncProviderInput = document.getElementById('cloudSyncProviderInput');
const cloudSyncBaseInput = document.getElementById('cloudSyncBaseInput');
const cloudSyncUserInput = document.getElementById('cloudSyncUserInput');
const cloudSyncPasswordInput = document.getElementById('cloudSyncPasswordInput');
const cloudSyncPasswordHint = document.getElementById('cloudSyncPasswordHint');
const cloudSyncRemoteRootInput = document.getElementById('cloudSyncRemoteRootInput');
const cloudSyncProfileInput = document.getElementById('cloudSyncProfileInput');
const cloudSyncAutoInput = document.getElementById('cloudSyncAutoInput');
const cloudSyncStatus = document.getElementById('cloudSyncStatus');
const cloudSyncLastSync = document.getElementById('cloudSyncLastSync');
const cloudSyncRemoteFile = document.getElementById('cloudSyncRemoteFile');
const cloudSyncTestBtn = document.getElementById('cloudSyncTestBtn');
const cloudSyncSaveBtn = document.getElementById('cloudSyncSaveBtn');
const cloudSyncUploadBtn = document.getElementById('cloudSyncUploadBtn');
const cloudSyncDownloadBtn = document.getElementById('cloudSyncDownloadBtn');
const cloudSyncImportFile = document.getElementById('cloudSyncImportFile');
const cloudSyncImportBtn = document.getElementById('cloudSyncImportBtn');
const cloudSyncExportBtn = document.getElementById('cloudSyncExportBtn');

let cloudSyncConfig = null;
let cloudSyncBusy = false;

function refreshIcons(){
    if(window.lucide) lucide.createIcons();
}

function tr(key){
    return window.StudioI18n ? window.StudioI18n.t(key) : key;
}

function trf(key, vars = {}){
    let text = tr(key);
    Object.entries(vars).forEach(([name, value]) => {
        text = text.replaceAll(`{${name}}`, String(value ?? ''));
    });
    return text;
}

function broadcastStudioApiChange(type = 'providers-changed'){
    const message = { type, source:'cloud-sync', updated_at:Date.now() };
    try { new BroadcastChannel('studio-api').postMessage(message); } catch(e) {}
    try { window.parent?.postMessage(message, '*'); } catch(e) {}
    try { window.top?.postMessage(message, '*'); } catch(e) {}
}

function cloudSyncSetStatus(text = '', type = ''){
    if(!cloudSyncStatus) return;
    cloudSyncStatus.textContent = text || '';
    cloudSyncStatus.className = `cloud-sync-status${type ? ` is-${type}` : ''}`;
}

function cloudSyncSetBusy(busy){
    cloudSyncBusy = Boolean(busy);
    [cloudSyncTestBtn, cloudSyncSaveBtn, cloudSyncUploadBtn, cloudSyncDownloadBtn, cloudSyncImportBtn, cloudSyncExportBtn].forEach(btn => {
        if(btn) btn.disabled = cloudSyncBusy;
    });
}

function cloudSyncFormatTime(value){
    const ms = Number(value || 0);
    if(!ms) return '-';
    try { return new Date(ms).toLocaleString(); } catch(e) { return '-'; }
}

function cloudSyncPayload(){
    const provider = cloudSyncProviderInput?.value || 'jianguoyun';
    return {
        method:'webdav',
        provider,
        base_url:(cloudSyncBaseInput?.value || '').trim() || (provider === 'jianguoyun' ? CLOUD_SYNC_JIANGUOYUN_URL : ''),
        username:(cloudSyncUserInput?.value || '').trim(),
        password:cloudSyncPasswordInput?.value || undefined,
        remote_root:(cloudSyncRemoteRootInput?.value || 'infinite-canvas-sync').trim(),
        profile:(cloudSyncProfileInput?.value || 'default').trim(),
        auto_sync:Boolean(cloudSyncAutoInput?.checked)
    };
}

function renderCloudSyncMeta(){
    const config = cloudSyncConfig || {};
    if(cloudSyncLastSync) cloudSyncLastSync.textContent = cloudSyncFormatTime(config.last_sync_at || config.last_upload_at || config.last_download_at);
    if(cloudSyncRemoteFile) cloudSyncRemoteFile.textContent = config.remote_file || '-';
    if(cloudSyncPasswordHint){
        cloudSyncPasswordHint.textContent = config.has_password
            ? trf('api.cloudSyncPasswordSaved', {preview:config.password_preview || ''})
            : tr('api.cloudSyncPasswordEmpty');
    }
}

function applyCloudSyncConfig(config = {}){
    cloudSyncConfig = config || {};
    if(cloudSyncProviderInput) cloudSyncProviderInput.value = cloudSyncConfig.provider || 'jianguoyun';
    if(cloudSyncBaseInput) cloudSyncBaseInput.value = cloudSyncConfig.base_url || CLOUD_SYNC_JIANGUOYUN_URL;
    if(cloudSyncUserInput) cloudSyncUserInput.value = cloudSyncConfig.username || '';
    if(cloudSyncPasswordInput) cloudSyncPasswordInput.value = '';
    if(cloudSyncRemoteRootInput) cloudSyncRemoteRootInput.value = cloudSyncConfig.remote_root || 'infinite-canvas-sync';
    if(cloudSyncProfileInput) cloudSyncProfileInput.value = cloudSyncConfig.profile || 'default';
    if(cloudSyncAutoInput) cloudSyncAutoInput.checked = Boolean(cloudSyncConfig.auto_sync);
    renderCloudSyncMeta();
}

async function cloudSyncJson(url, options = {}){
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || data.message || `${res.status} ${res.statusText}`);
    return data;
}

async function loadCloudSyncConfig(){
    try {
        const data = await cloudSyncJson('/api/cloud-sync/config');
        applyCloudSyncConfig(data.config || {});
        cloudSyncSetStatus(tr('api.cloudSyncReady'));
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncLoadFailed'), 'error');
    } finally {
        refreshIcons();
    }
}

async function saveCloudSyncConfig(options = {}){
    const silent = Boolean(options.silent);
    cloudSyncSetBusy(true);
    if(!silent) cloudSyncSetStatus(tr('api.cloudSyncSaving'));
    try {
        const data = await cloudSyncJson('/api/cloud-sync/config', {
            method:'PUT',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(cloudSyncPayload())
        });
        applyCloudSyncConfig(data.config || {});
        if(!silent) cloudSyncSetStatus(tr('api.cloudSyncSaved'), 'ok');
        return true;
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncSaveFailed'), 'error');
        return false;
    } finally {
        cloudSyncSetBusy(false);
    }
}

async function testCloudSync(){
    cloudSyncSetBusy(true);
    cloudSyncSetStatus(tr('api.cloudSyncTesting'));
    try {
        const data = await cloudSyncJson('/api/cloud-sync/test', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(cloudSyncPayload())
        });
        if(data.remote_file && cloudSyncRemoteFile) cloudSyncRemoteFile.textContent = data.remote_file;
        cloudSyncSetStatus(tr('api.cloudSyncTestOk'), 'ok');
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncTestFailed'), 'error');
    } finally {
        cloudSyncSetBusy(false);
    }
}

async function uploadCloudSync(options = {}){
    const auto = Boolean(options.auto);
    if(cloudSyncBusy && !auto) return;
    if(!(await saveCloudSyncConfig({silent:true}))) return;
    cloudSyncSetBusy(!auto);
    cloudSyncSetStatus(auto ? tr('api.cloudSyncAutoUploading') : tr('api.cloudSyncUploading'));
    try {
        const data = await cloudSyncJson('/api/cloud-sync/upload', {method:'POST'});
        applyCloudSyncConfig(data.config || cloudSyncConfig || {});
        cloudSyncSetStatus(auto ? tr('api.cloudSyncAutoUploaded') : tr('api.cloudSyncUploadOk'), 'ok');
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncUploadFailed'), 'error');
    } finally {
        cloudSyncSetBusy(false);
    }
}

async function downloadCloudSync(){
    if(!confirm(tr('api.cloudSyncDownloadConfirm'))) return;
    if(!(await saveCloudSyncConfig({silent:true}))) return;
    cloudSyncSetBusy(true);
    cloudSyncSetStatus(tr('api.cloudSyncDownloading'));
    try {
        const data = await cloudSyncJson('/api/cloud-sync/download', {method:'POST'});
        applyCloudSyncConfig(data.config || cloudSyncConfig || {});
        broadcastStudioApiChange('providers-changed');
        const count = Number(data.provider_count || 0);
        const suffix = count ? ` (${count})` : '';
        cloudSyncSetStatus(`${tr('api.cloudSyncDownloadOk')}${suffix}`, 'ok');
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncDownloadFailed'), 'error');
    } finally {
        cloudSyncSetBusy(false);
    }
}

function chooseCloudSyncImport(){
    if(cloudSyncBusy || !cloudSyncImportFile) return;
    cloudSyncImportFile.value = '';
    cloudSyncImportFile.click();
}

function cloudSyncExportFilename(res){
    const fallback = 'infinite-canvas-api-settings.json';
    const header = res.headers.get('Content-Disposition') || '';
    const match = header.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/i);
    const rawName = match ? (match[1] || match[2] || '') : '';
    if(!rawName) return fallback;
    try { return decodeURIComponent(rawName); } catch(e) { return rawName; }
}

async function exportCloudSyncFile(){
    if(cloudSyncBusy) return;
    cloudSyncSetBusy(true);
    cloudSyncSetStatus(tr('api.cloudSyncManualExporting'));
    try {
        const res = await fetch('/api/cloud-sync/export');
        if(!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || data.message || `${res.status} ${res.statusText}`);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = cloudSyncExportFilename(res);
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        cloudSyncSetStatus(tr('api.cloudSyncManualExportOk'), 'ok');
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncManualExportFailed'), 'error');
    } finally {
        cloudSyncSetBusy(false);
    }
}

async function importCloudSyncFile(event){
    const file = event.target?.files?.[0];
    if(!file) return;
    if(!confirm(tr('api.cloudSyncManualImportConfirm'))) {
        event.target.value = '';
        return;
    }
    cloudSyncSetBusy(true);
    cloudSyncSetStatus(tr('api.cloudSyncManualImporting'));
    try {
        const text = await file.text();
        let payload = null;
        try {
            payload = JSON.parse(text);
        } catch(e) {
            throw new Error(tr('api.cloudSyncManualInvalid'));
        }
        const data = await cloudSyncJson('/api/cloud-sync/import', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(payload)
        });
        broadcastStudioApiChange('providers-changed');
        const count = Number(data.provider_count || 0);
        const suffix = count ? ` (${count})` : '';
        cloudSyncSetStatus(`${tr('api.cloudSyncManualImportOk')}${suffix}`, 'ok');
    } catch(err) {
        cloudSyncSetStatus(err.message || tr('api.cloudSyncManualImportFailed'), 'error');
    } finally {
        cloudSyncSetBusy(false);
        event.target.value = '';
    }
}

function updateCloudSyncProviderDefaults(){
    if(!cloudSyncProviderInput || !cloudSyncBaseInput) return;
    if(cloudSyncProviderInput.value === 'jianguoyun' && !cloudSyncBaseInput.value.trim()){
        cloudSyncBaseInput.value = CLOUD_SYNC_JIANGUOYUN_URL;
    }
}

function refreshLanguageView(){
    renderCloudSyncMeta();
    refreshIcons();
}

function initializeCloudSyncPage(){
    if(window.StudioTheme) window.StudioTheme.apply();
    if(window.StudioI18n) window.StudioI18n.apply();
    cloudSyncProviderInput?.addEventListener('change', updateCloudSyncProviderDefaults);
    cloudSyncImportFile?.addEventListener('change', importCloudSyncFile);
    loadCloudSyncConfig();
    refreshIcons();
}

window.addEventListener('message', event => {
    if(event.data?.type === 'studio-theme' && window.StudioTheme) window.StudioTheme.set(event.data.theme);
    if(event.data?.type === 'studio-lang' && window.StudioI18n) {
        window.StudioI18n.set(event.data.lang);
        refreshLanguageView();
    }
});

window.addEventListener('studio-lang-change', refreshLanguageView);
document.addEventListener('DOMContentLoaded', initializeCloudSyncPage, {once:true});

window.testCloudSync = testCloudSync;
window.saveCloudSyncConfig = saveCloudSyncConfig;
window.uploadCloudSync = uploadCloudSync;
window.downloadCloudSync = downloadCloudSync;
window.chooseCloudSyncImport = chooseCloudSyncImport;
window.exportCloudSyncFile = exportCloudSyncFile;
