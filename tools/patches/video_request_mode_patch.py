import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime


VIDEO_SELECT_HTML = '''                                <div class="field-frame video-request-mode-wrap">
                                    <select id="videoRequestModeInput" title="视频接口">
                                        <option value="openai-videos-generations">视频：videos</option>
                                        <option value="openai-video-generations">视频：video</option>
                                    </select>
                                </div>
'''

VIDEO_HELPERS_PY = r'''def video_submit_url_candidates(provider, base_url):
    if is_agnes_provider(provider):
        return [f"{base_url}/v1/videos"]
    video_request_mode = effective_video_request_mode(provider)
    if video_request_mode == "openai-video-generations":
        return [f"{base_url}/v1/video/generations"]
    if is_apimart_provider(provider):
        return [f"{base_url}/videos/generations" if base_url.endswith("/v1") else f"{base_url}/v1/videos/generations"]
    if is_volcengine_provider(provider):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.path and parsed.path.rstrip("/"):
            return [base_url]
        return [f"{base_url}/api/v3/contents/generations/tasks"]
    if is_yuli_provider(provider):
        return [f"{base_url}/v1/video/create"]
    return [f"{base_url}/v1/videos/generations", f"{base_url}/v2/videos/generations"]

def video_task_url_candidates(provider, base_url, task_id, submit_url=""):
    quoted_id = urllib.parse.quote(str(task_id), safe="")
    if is_agnes_provider(provider):
        return [
            f"{base_url}/agnesapi?{urllib.parse.urlencode({'video_id': task_id})}",
            f"{base_url}/v1/videos/{quoted_id}",
        ]
    video_request_mode = effective_video_request_mode(provider)
    if video_request_mode == "openai-video-generations":
        return [f"{base_url}/v1/video/generations/{quoted_id}"]
    if is_apimart_provider(provider):
        task_path = f"{base_url}/tasks/{quoted_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{quoted_id}"
        return [f"{task_path}?language=zh"]
    if is_volcengine_provider(provider):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.path and parsed.path.rstrip("/"):
            return [f"{base_url}/{quoted_id}"]
        return [f"{base_url}/api/v3/contents/generations/tasks/{quoted_id}"]
    if is_yuli_provider(provider):
        return [f"{base_url}/v1/videos/{quoted_id}", f"{base_url}/v1/video/query?{urllib.parse.urlencode({'id': task_id})}"]
    v1_task = f"{base_url}/v1/videos/generations/{quoted_id}"
    v1_generic_task = f"{base_url}/v1/tasks/{quoted_id}"
    v2_task = f"{base_url}/v2/videos/generations/{quoted_id}"
    if "/v2/videos/generations" in str(submit_url or ""):
        return [v2_task, v1_task, v1_generic_task]
    return [v1_task, v1_generic_task, v2_task]

def effective_video_request_mode(provider) -> str:
    if (
        is_agnes_provider(provider)
        or is_volcengine_provider(provider)
        or is_yuli_provider(provider)
        or is_runninghub_provider(provider)
        or is_jimeng_provider(provider)
    ):
        return "openai-videos-generations"
    return normalize_video_request_mode((provider or {}).get("video_request_mode"))

def is_openai_video_generations_mode(provider) -> bool:
    return effective_video_request_mode(provider) == "openai-video-generations"

def openai_video_generations_duration(duration) -> str:
    try:
        value = int(duration)
    except Exception:
        value = 5
    if value <= 0:
        return "auto"
    return str(max(1, min(60, value)))

def openai_video_generations_aspect_ratio(aspect_ratio: str) -> str:
    value = str(aspect_ratio or "").strip()
    return value if value in {"16:9", "9:16", "1:1", "21:9", "auto"} else "auto"

def openai_video_generations_resolution(resolution: str) -> str:
    value = str(resolution or "").strip().lower()
    return value if value in {"480p", "720p"} else "720p"

async def openai_video_generations_public_url(value, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"} or re.match(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)", host):
            text = urllib.parse.unquote(parsed.path or "")
        else:
            return text
    if text.startswith("asset://"):
        return text
    try:
        uploaded = await upload_local_video_to_cloud(text)
        url = str((uploaded or {}).get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            return url
    except HTTPException as exc:
        public_url = local_asset_public_url(text)
        if public_url:
            return public_url
        raise HTTPException(status_code=400, detail=f"{label} cannot be converted to a public URL: {exc.detail}") from exc
    raise HTTPException(status_code=400, detail=f"{label} is not a public URL: {text[:160]}")

async def openai_video_generations_reference_urls(values, label: str, limit: int) -> List[str]:
    urls = []
    for value in list(values or [])[:limit]:
        url = await openai_video_generations_public_url(value, label)
        if url:
            urls.append(url)
    return urls
'''

EXTRACT_TASK_ID_PY = r'''def extract_task_id(data, allow_plain_id=False):
    if not isinstance(data, dict):
        return None
    if data.get("task_id"):
        return str(data["task_id"])
    if data.get("taskId"):
        return str(data["taskId"])
    if data.get("submit_id"):
        return str(data["submit_id"])
    if data.get("video_id"):
        return str(data["video_id"])
    if data.get("videoId"):
        return str(data["videoId"])
    raw_id = str(data.get("id") or "").strip()
    if raw_id and (allow_plain_id or raw_id.startswith(("task", "video", "vidgen", "upstream_task"))):
        return raw_id
    nested = data.get("data")
    if isinstance(nested, list) and nested:
        first = nested[0]
        if isinstance(first, dict):
            return extract_task_id(first, allow_plain_id=True)
    if isinstance(nested, dict):
        return extract_task_id(nested, allow_plain_id=True)
    return None
'''

SINGLE_VIDEO_BODY_PY = r'''                else:
                    if is_single_video_generations:
                        image_urls = await openai_video_generations_reference_urls(
                            [ref.url for ref in payload.images if ref.url], "reference image", 9
                        )
                        video_urls = await openai_video_generations_reference_urls(payload.videos, "reference video", 3)
                        audio_urls = await openai_video_generations_reference_urls(payload.audios, "reference audio", 3)
                        body = {
                            "prompt": payload.prompt,
                            "model": selected_model(payload.model, "veo3-fast"),
                            "type": "image-to-video",
                            "duration": openai_video_generations_duration(payload.duration),
                            "aspect_ratio": openai_video_generations_aspect_ratio(payload.aspect_ratio),
                            "resolution": openai_video_generations_resolution(payload.resolution),
                            "generate_audio": bool(payload.generate_audio),
                        }
                        if image_urls:
                            body["image_urls"] = image_urls
                        if video_urls:
                            body["video_urls"] = video_urls
                        if audio_urls:
                            body["audio_urls"] = audio_urls
                    else:
                        image_payload = []
                        for ref in payload.images[:4]:
                            if ref.url:
                                image_payload.append(reference_to_data_url(ref.dict(), max_size=1536))
                        body = {
                            "prompt": payload.prompt,
                            "model": selected_model(payload.model, "veo3-fast"),
                            "duration": payload.duration,
                            "watermark": payload.watermark,
                        }
                        if payload.aspect_ratio:
                            body["aspect_ratio"] = payload.aspect_ratio
                            body["ratio"] = payload.aspect_ratio
                        if payload.size:
                            body["size"] = payload.size
                        if payload.resolution:
                            body["resolution"] = payload.resolution
                        if image_payload:
                            body["images"] = image_payload
                        if payload.videos:
                            body["videos"] = [v for v in payload.videos if v]
                        if payload.enhance_prompt:
                            body["enhance_prompt"] = True
                        if payload.enable_upsample:
                            body["enable_upsample"] = True
                        if payload.seed is not None:
                            body["seed"] = payload.seed
                        if payload.camerafixed:
                            body["camerafixed"] = True
                        if payload.return_last_frame:
                            body["return_last_frame"] = True
                        if payload.generate_audio:
                            body["generate_audio"] = True
'''


class PatchError(RuntimeError):
    pass


def read(path):
    return path.read_text(encoding="utf-8")


def replace_once(text, old, new, label, required=False):
    if new in text:
        return text
    if old not in text:
        if required:
            raise PatchError(f"anchor not found: {label}")
        return text
    return text.replace(old, new, 1)


def regex_replace(text, pattern, repl, label, required=False, flags=re.S):
    new_text, count = re.subn(pattern, repl, text, count=1, flags=flags)
    if count == 0 and required:
        raise PatchError(f"anchor not found: {label}")
    return new_text


def patch_main(text):
    if "SUPPORTED_VIDEO_REQUEST_MODES" not in text:
        text = regex_replace(
            text,
            r"(SUPPORTED_IMAGE_REQUEST_MODES = \{[^\n]+\}\n)",
            r'\1SUPPORTED_VIDEO_REQUEST_MODES = {"openai-videos-generations", "openai-video-generations"}\n',
            "SUPPORTED_VIDEO_REQUEST_MODES",
            required=True,
            flags=0,
        )

    text = re.sub(
        r'("image_request_mode": "openai",\n)(?![ \t]+"video_request_mode")([ \t]+)',
        r'\1\2"video_request_mode": "openai-videos-generations",\n\2',
        text,
    )

    if "def normalize_video_request_mode" not in text:
        text = regex_replace(
            text,
            r"(def normalize_image_request_mode\(value\):\n(?:    .+\n)+?    return .+?\n)",
            r'''\1
def normalize_video_request_mode(value):
    mode = str(value or "").strip().lower()
    if mode in {"openai-video", "single-video", "video-generations"}:
        return "openai-video-generations"
    if mode in {"openai-videos", "videos-generations"}:
        return "openai-videos-generations"
    return mode if mode in SUPPORTED_VIDEO_REQUEST_MODES else "openai-videos-generations"
''',
            "normalize_video_request_mode",
            required=True,
        )

    text = replace_once(
        text,
        '    image_request_mode = detect_image_request_mode(base_url, item.get("image_models") or []) or normalize_image_request_mode(item.get("image_request_mode"))\n',
        '    image_request_mode = detect_image_request_mode(base_url, item.get("image_models") or []) or normalize_image_request_mode(item.get("image_request_mode"))\n    video_request_mode = normalize_video_request_mode(item.get("video_request_mode"))\n',
        "normalize_provider video_request_mode",
    )
    text = replace_once(
        text,
        '        "image_request_mode": image_request_mode,\n',
        '        "image_request_mode": image_request_mode,\n        "video_request_mode": video_request_mode,\n',
        "normalize_provider return video_request_mode",
    )
    text = replace_once(
        text,
        '    image_request_mode: str = "openai"\n',
        '    image_request_mode: str = "openai"\n    video_request_mode: str = "openai-videos-generations"\n',
        "ApiProviderPayload video_request_mode",
    )

    if "def extract_task_id(data, allow_plain_id=False):" not in text:
        text = regex_replace(
            text,
            r"def extract_task_id\(data\):\n.*?\ndef extract_task_id_from_text\(text\):",
            EXTRACT_TASK_ID_PY + "\ndef extract_task_id_from_text(text):",
            "extract_task_id",
            required=True,
        )

    text = text.replace(
        'def local_media_path_for_cloud_upload(ref_url: str, allowed_prefixes=("image/", "video/")) -> str:',
        'def local_media_path_for_cloud_upload(ref_url: str, allowed_prefixes=("image/", "video/", "audio/")) -> str:',
    )

    if "def effective_video_request_mode(provider)" not in text:
        text = regex_replace(
            text,
            r"def video_submit_url_candidates\(provider, base_url\):\n.*?\nVIDEO_TASK_SUCCESS_STATUSES = \{",
            VIDEO_HELPERS_PY + "\nVIDEO_TASK_SUCCESS_STATUSES = {",
            "video URL helpers",
            required=True,
        )

    if "is_single_video_generations = is_openai_video_generations_mode(provider)" not in text:
        text = replace_once(
            text,
            "    is_apimart = is_apimart_provider(provider)\n",
            "    is_single_video_generations = is_openai_video_generations_mode(provider)\n    is_apimart = is_apimart_provider(provider) and not is_single_video_generations\n",
            "canvas_video mode flags",
            required=True,
        )

    if "if is_single_video_generations:\n                        image_urls = await openai_video_generations_reference_urls" not in text:
        text = regex_replace(
            text,
            r"                else:\n                    image_payload = \[\]\n.*?                        if payload\.generate_audio:\n                            body\[\"generate_audio\"\] = True\n",
            SINGLE_VIDEO_BODY_PY,
            "canvas_video single video body",
            required=True,
        )

    return text


def patch_html(text):
    replacements = {
        'title="Video API"': 'title="视频接口"',
        "Video: /v1/videos/generations": "视频：videos",
        "Video: /v1/video/generations": "视频：video",
        "视频：/v1/videos/generations": "视频：videos",
        "视频：/v1/video/generations": "视频：video",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if "videoRequestModeInput" in text:
        return text
    pattern = r'([ \t]*<div class="field-frame image-request-mode-wrap">\n.*?<select id="imageRequestModeInput".*?</select>\n[ \t]*</div>\n)'
    return regex_replace(text, pattern, r"\1" + VIDEO_SELECT_HTML, "videoRequestModeInput HTML", required=True)


def patch_css(text):
    hidden_contexts = [
        "body.show-ms",
        "body.show-runninghub",
        "body.show-volcengine-standalone",
        "body.show-jimeng",
        "body.show-codex",
        "body.show-gemini-cli",
    ]
    for selector in hidden_contexts:
        text = replace_once(
            text,
            f"{selector} .image-request-mode-wrap,\n{selector} .image-edit-route-wrap",
            f"{selector} .image-request-mode-wrap,\n{selector} .video-request-mode-wrap,\n{selector} .image-edit-route-wrap",
            f"{selector} video hidden rule",
        )

    text = replace_once(
        text,
        ".protocol-selector-wrap,\n.image-request-mode-wrap,\n.image-edit-route-wrap",
        ".protocol-selector-wrap,\n.image-request-mode-wrap,\n.video-request-mode-wrap,\n.image-edit-route-wrap",
        "video wrapper base style",
    )
    text = replace_once(
        text,
        "body.studio-theme-dark .image-request-mode-wrap,\nhtml.studio-theme-dark body .image-request-mode-wrap,\nbody.studio-theme-dark .image-edit-route-wrap,",
        "body.studio-theme-dark .image-request-mode-wrap,\nhtml.studio-theme-dark body .image-request-mode-wrap,\nbody.studio-theme-dark .video-request-mode-wrap,\nhtml.studio-theme-dark body .video-request-mode-wrap,\nbody.studio-theme-dark .image-edit-route-wrap,",
        "video wrapper dark style",
    )
    text = replace_once(
        text,
        ".protocol-selector-wrap select,\n.image-request-mode-wrap select,\n.image-edit-route-wrap select",
        ".protocol-selector-wrap select,\n.image-request-mode-wrap select,\n.video-request-mode-wrap select,\n.image-edit-route-wrap select",
        "video select base style",
    )
    text = replace_once(
        text,
        ".protocol-selector-wrap select:disabled,\n.image-request-mode-wrap select:disabled,\n.image-edit-route-wrap select:disabled",
        ".protocol-selector-wrap select:disabled,\n.image-request-mode-wrap select:disabled,\n.video-request-mode-wrap select:disabled,\n.image-edit-route-wrap select:disabled",
        "video select disabled style",
    )
    text = replace_once(
        text,
        ".image-request-mode-wrap select { min-width:150px; }\n.image-edit-route-wrap select { min-width:128px; }",
        ".image-request-mode-wrap select { min-width:150px; }\n.video-request-mode-wrap select { min-width:118px; }\n.image-edit-route-wrap select { min-width:128px; }",
        "video select width",
    )
    text = replace_once(
        text,
        ".protocol-selector-wrap select:hover,\n.image-request-mode-wrap select:hover,\n.image-edit-route-wrap select:hover",
        ".protocol-selector-wrap select:hover,\n.image-request-mode-wrap select:hover,\n.video-request-mode-wrap select:hover,\n.image-edit-route-wrap select:hover",
        "video select hover style",
    )
    text = replace_once(
        text,
        "body.studio-theme-dark .image-request-mode-wrap select:hover,\nhtml.studio-theme-dark body .image-request-mode-wrap select:hover,\nbody.studio-theme-dark .image-edit-route-wrap select:hover,",
        "body.studio-theme-dark .image-request-mode-wrap select:hover,\nhtml.studio-theme-dark body .image-request-mode-wrap select:hover,\nbody.studio-theme-dark .video-request-mode-wrap select:hover,\nhtml.studio-theme-dark body .video-request-mode-wrap select:hover,\nbody.studio-theme-dark .image-edit-route-wrap select:hover,",
        "video select dark hover style",
    )
    return text


def patch_js(text):
    text = replace_once(
        text,
        "const imageRequestModeInput = document.getElementById('imageRequestModeInput');\n",
        "const imageRequestModeInput = document.getElementById('imageRequestModeInput');\nconst videoRequestModeInput = document.getElementById('videoRequestModeInput');\n",
        "videoRequestModeInput const",
    )

    if "function normalizeVideoRequestMode" not in text:
        text = regex_replace(
            text,
            r"(function normalizeImageRequestMode\(value\)\{\n(?:    .+\n)+?\}\n)",
            r'''\1function normalizeVideoRequestMode(value){
    const mode = String(value || '').trim().toLowerCase();
    if(['openai-video', 'single-video', 'video-generations'].includes(mode)) return 'openai-video-generations';
    if(['openai-videos', 'videos-generations'].includes(mode)) return 'openai-videos-generations';
    return ['openai-videos-generations', 'openai-video-generations'].includes(mode) ? mode : 'openai-videos-generations';
}
''',
            "normalizeVideoRequestMode",
            required=True,
        )

    if "function videoRequestModeLabel" not in text:
        text = regex_replace(
            text,
            r"(function imageRequestModeLabel\(mode\)\{\n(?:    .+\n)+?\}\n)",
            r'''\1function videoRequestModeLabel(mode){
    const normalized = normalizeVideoRequestMode(mode);
    return normalized === 'openai-video-generations' ? '/v1/video/generations' : '/v1/videos/generations';
}
''',
            "videoRequestModeLabel",
            required=True,
        )

    text = replace_once(
        text,
        "    item.image_request_mode = normalizeImageRequestMode(api.image_request_mode);\n",
        "    item.image_request_mode = normalizeImageRequestMode(api.image_request_mode);\n    item.video_request_mode = normalizeVideoRequestMode(api.video_request_mode);\n",
        "locked recommended video mode",
    )
    text = replace_once(
        text,
        "        item.image_request_mode = normalizeImageRequestMode(api.image_request_mode || item.image_request_mode);\n",
        "        item.image_request_mode = normalizeImageRequestMode(api.image_request_mode || item.image_request_mode);\n        item.video_request_mode = normalizeVideoRequestMode(api.video_request_mode || item.video_request_mode);\n",
        "recommended existing video mode",
    )
    text = replace_once(
        text,
        "        image_request_mode:normalizeImageRequestMode(api.image_request_mode),\n",
        "        image_request_mode:normalizeImageRequestMode(api.image_request_mode),\n        video_request_mode:normalizeVideoRequestMode(api.video_request_mode),\n",
        "recommended new video mode",
    )

    text = replace_once(
        text,
        "                image_request_mode:item.image_request_mode || 'openai',\n",
        "                image_request_mode:item.image_request_mode || 'openai',\n                video_request_mode:item.video_request_mode || 'openai-videos-generations',\n",
        "saveProviders video_request_mode",
    )

    text = text.replace(
        "image_request_mode:'openai', image_edit_route",
        "image_request_mode:'openai', video_request_mode:'openai-videos-generations', image_edit_route",
    )
    text = text.replace(
        "image_request_mode:'openai',\n            image_edit_route",
        "image_request_mode:'openai',\n            video_request_mode:'openai-videos-generations',\n            image_edit_route",
    )

    if "if(videoRequestModeInput) videoRequestModeInput.addEventListener('change'" not in text:
        text = regex_replace(
            text,
            r"(    if\(imageRequestModeInput\) imageRequestModeInput\.addEventListener\('change', \(\) => \{\n.*?        item\.image_request_mode = normalizeImageRequestMode\(imageRequestModeInput\.value\);\n    \}\);\n)",
            r'''\1    if(videoRequestModeInput) videoRequestModeInput.addEventListener('change', () => {
        const item = provider();
        if(!item) return;
        if(applyLockedRecommendedProtocol(item)){
            if(protocolInput) protocolInput.value = item.protocol;
            if(imageRequestModeInput) imageRequestModeInput.value = item.image_request_mode;
            videoRequestModeInput.value = item.video_request_mode;
            return;
        }
        item.video_request_mode = normalizeVideoRequestMode(videoRequestModeInput.value);
    });
''',
            "videoRequestModeInput change listener",
        )

    if "if(videoRequestModeInput){" not in text:
        text = regex_replace(
            text,
            r"(    if\(imageRequestModeInput\)\{\n        imageRequestModeInput\.value = normalizeImageRequestMode\(item\.image_request_mode\);\n        imageRequestModeInput\.disabled = .*?\n        imageRequestModeInput\.title = .*?\n    \}\n)",
            r'''\1    if(videoRequestModeInput){
        videoRequestModeInput.value = normalizeVideoRequestMode(item.video_request_mode);
        videoRequestModeInput.disabled = Boolean(lockedApi) || item.id === 'modelscope' || item.id === 'runninghub' || item.id === 'volcengine' || CLI_PROTOCOLS.has(String(protocolInput?.value || item.protocol || '').toLowerCase());
        videoRequestModeInput.title = lockedApi ? 'Fixed video protocol for recommended providers' : '';
    }
''',
            "renderEditor video mode",
        )

    return text


def patch_config(text):
    try:
        data = json.loads(text)
    except Exception as exc:
        raise PatchError(f"data/api_providers.json is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        return text
    target = None
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("id") == "custom-api-4" or item.get("base_url") == "https://aigc.cglol.com/v1" or "miniAPI-1.8" in str(item.get("name") or ""):
            target = item
            break
    if target is None:
        return text
    if not target.get("name") or target.get("name") == "API":
        target["name"] = "Jimeng miniAPI-1.8"
    target["protocol"] = "openai"
    target["image_request_mode"] = "openai"
    target["video_request_mode"] = "openai-video-generations"
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_if_changed(path, text, root, dry_run, backup_dir, changed):
    old = read(path)
    if old == text:
        return
    changed.append(str(path.relative_to(root)))
    if dry_run:
        return
    backup = backup_dir / path.relative_to(root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8", newline="")


def validate(root):
    checks = {
        "main.py": [
            "SUPPORTED_VIDEO_REQUEST_MODES",
            "def effective_video_request_mode(provider)",
            "openai_video_generations_reference_urls",
            "is_single_video_generations = is_openai_video_generations_mode(provider)",
        ],
        "static/api-settings.html": ["videoRequestModeInput"],
        "static/css/api-settings.css": [
            ".video-request-mode-wrap select",
            ".video-request-mode-wrap select { min-width:118px; }",
        ],
        "static/js/api-settings.js": [
            "const videoRequestModeInput",
            "function normalizeVideoRequestMode",
            "video_request_mode:item.video_request_mode || 'openai-videos-generations'",
        ],
    }
    missing = []
    for rel, needles in checks.items():
        text = read(root / rel)
        for needle in needles:
            if needle not in text:
                missing.append(f"{rel}: {needle}")
    if missing:
        raise PatchError("patch incomplete:\n" + "\n".join(missing))


def run_check(cmd, root):
    return subprocess.run(cmd, cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-checks", action="store_true")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    backup_dir = root / "patch_backups" / ("video_request_mode_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    changed = []
    targets = [
        ("main.py", patch_main),
        ("static/api-settings.html", patch_html),
        ("static/css/api-settings.css", patch_css),
        ("static/js/api-settings.js", patch_js),
    ]
    if (root / "data/api_providers.json").exists():
        targets.append(("data/api_providers.json", patch_config))

    for rel, func in targets:
        path = root / rel
        if not path.exists():
            raise PatchError(f"missing file: {rel}")
        write_if_changed(path, func(read(path)), root, args.dry_run, backup_dir, changed)

    if args.dry_run:
        print("DRY-RUN: would change " + (", ".join(changed) if changed else "nothing"))
        return

    validate(root)
    print("video request mode patch applied" + (f"; backup: {backup_dir}" if changed else "; already applied"))

    if not args.skip_checks:
        py = root / "venv" / "Scripts" / "python.exe"
        compile_code = "import pathlib; compile(pathlib.Path('main.py').read_text(encoding='utf-8'), 'main.py', 'exec')"
        py_cmd = [str(py), "-c", compile_code] if py.exists() else [sys.executable, "-c", compile_code]
        checks = [
            ("Python syntax check", py_cmd),
            ("Frontend JS syntax check", ["node", "--check", "static/js/api-settings.js"]),
        ]
        for label, cmd in checks:
            try:
                result = run_check(cmd, root)
            except FileNotFoundError:
                print(f"{label}: skipped, command not found: {cmd[0]}")
                continue
            if result.returncode != 0:
                print(result.stdout)
                raise PatchError(f"{label} failed")
            print(f"{label}: passed")


if __name__ == "__main__":
    try:
        main()
    except PatchError as exc:
        print(f"patch failed: {exc}", file=sys.stderr)
        sys.exit(1)
