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
                                        <option value="sudashui-video-generations">Sudashui：Seedance</option>
                                    </select>
                                </div>
'''

VIDEO_HELPERS_PY = r'''def video_submit_url_candidates(provider, base_url):
    if is_agnes_provider(provider):
        return [f"{base_url}/v1/videos"]
    video_request_mode = effective_video_request_mode(provider)
    if video_request_mode in {"openai-video-generations", "sudashui-video-generations"}:
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
    if video_request_mode in {"openai-video-generations", "sudashui-video-generations"}:
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

def is_sudashui_video_generations_mode(provider) -> bool:
    return effective_video_request_mode(provider) == "sudashui-video-generations"

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

VIDEO_RETRY_AFTER_PY = r'''def video_task_failure_reason(payload):
    if isinstance(payload, dict):
        task_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
        return (
            task_data.get("fail_reason")
            or task_data.get("message")
            or error.get("message")
            or payload.get("error")
            or payload.get("message")
            or str(payload)
        )
    return str(payload or "")

def is_video_terminal_error(source):
    parts = []

    def add(value, depth=0):
        if value is None or depth > 8:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                parts.append(str(key))
                add(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item, depth + 1)
            return
        parts.append(str(value))

    response = getattr(source, "response", None)
    if response is not None:
        try:
            add(response.json())
        except Exception:
            add(getattr(response, "text", "") or str(source))
    elif hasattr(source, "json"):
        try:
            add(source.json())
        except Exception:
            add(getattr(source, "text", "") or str(source))
    elif isinstance(source, BaseException):
        add(str(source))
    else:
        add(source)
    text = "\n".join(part for part in parts if part).lower()
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in (
        r"insufficient[_\s-]*quota",
        r"insufficient\s+credits?",
        r"credits[_\s-]*remaining",
        r"not\s+enough\s+credits?",
        r"quota\s+exceeded",
        r"payment\s+required",
        r"billing[_\s-]*(?:error|failed|failure|disabled|issue|problem)",
        r"billing\s+account\s+(?:disabled|inactive|suspended)",
        r"余额不足",
        r"额度不足",
    ))

def video_retry_after_seconds(source):
    values = []

    def add_number(value):
        try:
            seconds = float(value)
        except Exception:
            return
        if seconds > 0:
            values.append(seconds)

    def scan_text(text):
        text = str(text or "").strip()
        if not text:
            return
        if text[0:1] in {"{", "["}:
            try:
                walk(json.loads(text))
            except Exception:
                pass
        for pattern in (
            r"retry[_\s-]*after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)",
            r"请等待\s*(\d+(?:\.\d+)?)\s*秒",
            r"(\d+(?:\.\d+)?)\s*秒后再试",
            r"(?:retry after|wait)\s*(\d+(?:\.\d+)?)\s*(?:s|sec|second|seconds)?",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                add_number(match.group(1))

    def walk(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip().lower().replace("-", "_")
                if normalized in {"retry_after", "retryafter"}:
                    add_number(item)
                else:
                    walk(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)
            return
        if isinstance(value, str):
            scan_text(value)

    response = getattr(source, "response", None)
    if response is not None:
        add_number(response.headers.get("Retry-After"))
        try:
            walk(response.json())
        except Exception:
            scan_text(getattr(response, "text", "") or str(source))
    elif hasattr(source, "headers") and hasattr(source, "json"):
        add_number(source.headers.get("Retry-After"))
        try:
            walk(source.json())
        except Exception:
            scan_text(getattr(source, "text", "") or str(source))
    elif isinstance(source, BaseException):
        scan_text(str(source))
    else:
        walk(source)
    if not values:
        return None
    return min(max(values), max(5.0, VIDEO_POLL_TIMEOUT))
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

CANVAS_VIDEO_TASK_HELPERS_PY = r'''CANVAS_VIDEO_TERMINAL_STATUSES = {"succeeded", "failed"}
CANVAS_VIDEO_RESUMABLE_STATUSES = {"queued", "submitting", "polling", "running", "jimeng_pending"}

def canvas_video_task_snapshot_unlocked():
    return {
        task_id: task
        for task_id, task in CANVAS_TASKS.items()
        if isinstance(task, dict) and task.get("type") == "online-video"
    }

def write_canvas_video_tasks_snapshot(snapshot):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = f"{CANVAS_VIDEO_TASKS_FILE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CANVAS_VIDEO_TASKS_FILE)

def persist_canvas_video_tasks():
    with CANVAS_TASK_LOCK:
        snapshot = canvas_video_task_snapshot_unlocked()
    write_canvas_video_tasks_snapshot(snapshot)

def load_persisted_canvas_video_tasks():
    if not os.path.exists(CANVAS_VIDEO_TASKS_FILE):
        return {}
    try:
        with open(CANVAS_VIDEO_TASKS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        print(f"读取视频任务状态失败: {exc}")
        return {}
    if isinstance(raw, list):
        items = {str(item.get("id") or ""): item for item in raw if isinstance(item, dict)}
    elif isinstance(raw, dict):
        items = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    else:
        return {}
    return {task_id: task for task_id, task in items.items() if task_id}

def load_canvas_video_tasks_into_memory():
    restored = load_persisted_canvas_video_tasks()
    if not restored:
        return {}
    with CANVAS_TASK_LOCK:
        for task_id, task in restored.items():
            current = CANVAS_TASKS.get(task_id)
            if isinstance(current, dict) and current.get("status") not in CANVAS_VIDEO_TERMINAL_STATUSES:
                continue
            task.setdefault("id", task_id)
            task.setdefault("type", "online-video")
            CANVAS_TASKS[task_id] = task
    return restored

def normalize_canvas_video_task_patch(task_id: str, patch: Dict[str, Any]):
    data = dict(patch or {})
    upstream_task_id = str(data.get("task_id") or "").strip()
    if upstream_task_id and upstream_task_id != task_id and not upstream_task_id.startswith("canvas_video_"):
        data.setdefault("upstream_task_id", upstream_task_id)
        data.pop("task_id", None)
    return data

def update_canvas_video_task(task_id: str, patch: Dict[str, Any], persist=True):
    now = time.time()
    patch_data = normalize_canvas_video_task_patch(task_id, patch)
    with CANVAS_TASK_LOCK:
        task = CANVAS_TASKS.get(task_id)
        if not isinstance(task, dict):
            return {}
        task.update(patch_data)
        task["updated_at"] = now
        snapshot = canvas_video_task_snapshot_unlocked()
        result = dict(task)
    if persist:
        write_canvas_video_tasks_snapshot(snapshot)
    return result

def video_task_request_meta(payload: "CanvasVideoRequest"):
    return {
        "provider_id": payload.provider_id,
        "model": payload.model,
        "prompt": str(payload.prompt or "")[:500],
        "duration": payload.duration,
        "aspect_ratio": payload.aspect_ratio,
        "resolution": payload.resolution,
        "generate_audio": bool(payload.generate_audio),
        "multimodal": bool(payload.multimodal),
    }

def report_canvas_video_progress(progress, patch: Dict[str, Any]):
    if not callable(progress):
        return
    try:
        progress(patch or {})
    except Exception as exc:
        print(f"更新视频任务进度失败: {exc}")

def canvas_video_result_urls(result):
    if not isinstance(result, dict):
        return []
    return [url for url in result.get("videos") or [] if url]

def canvas_video_upstream_task_id(task: Dict[str, Any]):
    for key in ("upstream_task_id", "submit_id", "video_id"):
        value = str((task or {}).get(key) or "").strip()
        if value:
            return value
    legacy_task_id = str((task or {}).get("task_id") or "").strip()
    if legacy_task_id and not legacy_task_id.startswith("canvas_video_"):
        return legacy_task_id
    return ""

async def resume_canvas_video_task_result(task_id: str):
    with CANVAS_TASK_LOCK:
        task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        raise HTTPException(status_code=404, detail="视频任务不存在")
    provider_id = str(task.get("provider_id") or "").strip()
    try:
        provider = get_api_provider_exact(provider_id)
    except HTTPException as exc:
        raise HTTPException(status_code=409, detail=f"视频任务所用 API 平台已不存在或已禁用：{provider_id or '(empty)'}") from exc
    model = str(task.get("model") or "").strip()
    upstream_task_id = canvas_video_upstream_task_id(task)
    if not upstream_task_id:
        raise HTTPException(status_code=409, detail="服务重启前尚未拿到上游视频任务 ID，已停止自动恢复以避免重复扣费。")

    progress = lambda patch: update_canvas_video_task(task_id, patch)
    submit_url = str(task.get("submit_url") or "").strip()
    if is_jimeng_provider(provider):
        last_raw = None
        deadline = time.monotonic() + VIDEO_POLL_TIMEOUT
        while time.monotonic() < deadline:
            queried = await jimeng_query_result(upstream_task_id, "video")
            last_raw = queried
            report_canvas_video_progress(progress, {"status": "polling", "raw_last": queried})
            try:
                urls = await jimeng_store_outputs(queried, "video", allow_query=False)
                return {"videos": urls, "task_id": upstream_task_id, "raw": queried}
            except JimengPendingError:
                await asyncio.sleep(min(VIDEO_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
        raise HTTPException(status_code=504, detail=f"即梦视频任务恢复超时：{last_raw or upstream_task_id}")

    timeout = httpx.Timeout(connect=20.0, read=VIDEO_POLL_TIMEOUT, write=120.0, pool=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if is_runninghub_provider(provider):
            result = await wait_for_runninghub_openapi_task(client, provider, upstream_task_id, "video", progress)
            urls = video_output_urls(result)
            if not urls:
                outputs = runninghub_extract_outputs(result.get("data") if isinstance(result, dict) else result)
                urls = [url for url in outputs if str(url).startswith(("http://", "https://", "/output/", "/assets/"))]
            if not urls:
                raise HTTPException(status_code=502, detail=f"RunningHub 视频生成成功但没有返回视频：{result}")
            local_urls = [await save_remote_video_to_output(url, prefix="rh_video_") for url in urls]
            return {"videos": local_urls, "task_id": upstream_task_id, "raw": result}
        if is_agnes_provider(provider, model):
            result = await wait_for_agnes_video_task(client, provider, upstream_task_id, model or "agnes-video-v2.0", progress)
        else:
            result = await wait_for_video_task(client, provider, upstream_task_id, submit_url, progress)
        urls = video_output_urls(result)
        if not urls:
            raise HTTPException(status_code=502, detail=f"视频生成成功但没有返回视频：{result}")
        local_urls = [await save_remote_video_to_output(url) for url in urls]
        return {"videos": local_urls, "task_id": upstream_task_id, "raw": result}

async def run_canvas_video_task(task_id: str, payload: Optional["CanvasVideoRequest"] = None, resume=False):
    update_canvas_video_task(task_id, {"status": "polling" if resume else "submitting", "error": ""})
    try:
        if resume:
            result = await resume_canvas_video_task_result(task_id)
        else:
            if payload is None:
                raise HTTPException(status_code=400, detail="缺少视频任务请求")
            progress = lambda patch: update_canvas_video_task(task_id, patch)
            result = await build_canvas_video_result(payload, progress)
    except JimengPendingError as exc:
        info = jimeng_pending_payload(exc)
        update_canvas_video_task(task_id, {
            "status": "polling",
            "jimeng_pending": True,
            "upstream_task_id": exc.submit_id,
            "submit_id": exc.submit_id,
            "kind": exc.kind,
            "queue_info": exc.queue_info,
            "message": info["message"],
            "error": "",
            "raw_last": exc.raw,
        })
        try:
            result = await resume_canvas_video_task_result(task_id)
        except Exception as resume_exc:
            detail = getattr(resume_exc, "detail", None) or str(resume_exc)
            status_code = getattr(resume_exc, "status_code", 500)
            update_canvas_video_task(task_id, {"status": "failed", "error": str(detail), "status_code": status_code, "retry_after": None, "next_poll_at": None, "message": "", "jimeng_pending": False})
            return
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        status_code = getattr(exc, "status_code", 500)
        update_canvas_video_task(task_id, {"status": "failed", "error": str(detail), "status_code": status_code, "retry_after": None, "next_poll_at": None, "message": "", "jimeng_pending": False})
        return
    update_canvas_video_task(task_id, {"status": "succeeded", "result": result, "videos": canvas_video_result_urls(result), "error": "", "retry_after": None, "next_poll_at": None, "message": "", "jimeng_pending": False, "status_code": None, "raw_last": result.get("raw") if isinstance(result, dict) else result})

async def resume_canvas_video_tasks_on_startup():
    restored = load_canvas_video_tasks_into_memory()
    if not restored:
        return
    for task_id, task in restored.items():
        status = str(task.get("status") or "").strip().lower()
        if status in CANVAS_VIDEO_TERMINAL_STATUSES or status not in CANVAS_VIDEO_RESUMABLE_STATUSES:
            continue
        if canvas_video_upstream_task_id(task):
            update_canvas_video_task(task_id, {"status": "polling", "message": "服务重启后已恢复视频任务查询"})
            asyncio.create_task(run_canvas_video_task(task_id, resume=True))
        else:
            update_canvas_video_task(task_id, {"status": "failed", "error": "服务重启前尚未拿到上游视频任务 ID，已停止自动恢复以避免重复扣费。", "retry_after": None, "next_poll_at": None, "message": "", "jimeng_pending": False})
'''

CANVAS_VIDEO_TASK_ENDPOINTS_PY = r'''@app.post("/api/canvas-video")
async def canvas_video(payload: CanvasVideoRequest):
    return await build_canvas_video_result(payload)

@app.post("/api/canvas-video-tasks")
async def create_canvas_video_task(payload: CanvasVideoRequest):
    task_id = f"canvas_video_{uuid.uuid4().hex}"
    task = {
        "id": task_id,
        "type": "online-video",
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "result": None,
        "videos": [],
        "error": "",
        "provider_id": payload.provider_id,
        "model": payload.model,
        "request": video_task_request_meta(payload),
        "upstream_task_id": "",
        "submit_url": "",
        "retry_after": None,
        "next_poll_at": None,
    }
    with CANVAS_TASK_LOCK:
        CANVAS_TASKS[task_id] = task
    persist_canvas_video_tasks()
    asyncio.create_task(run_canvas_video_task(task_id, payload))
    return {"task_id": task_id, "status": "queued"}

@app.get("/api/canvas-video-tasks/{task_id}")
async def get_canvas_video_task(task_id: str):
    with CANVAS_TASK_LOCK:
        task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        load_canvas_video_tasks_into_memory()
        with CANVAS_TASK_LOCK:
            task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        raise HTTPException(status_code=404, detail="视频任务不存在，可能服务已重启或任务已过期")
    return task
'''

CREATE_CANVAS_VIDEO_TASK_JS = r'''async function createCanvasVideoTask(payload, options={}){
    const res = await cascadeFetch('/api/canvas-video-tasks', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
    }, options);
    if(!res.ok) throw new Error(await responseErrorMessage(res, tr('canvas.videoFailed')));
    return res.json();
}
'''

SMART_CREATE_CANVAS_VIDEO_TASK_JS = r'''async function createSmartCanvasVideoTask(payload){
    const res = await fetch('/api/canvas-video-tasks', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
    });
    if(!res.ok) throw new Error(await smartResponseErrorMessage(res, tr('smart.errRunFailed')));
    return res.json();
}
'''

SMART_POLL_CANVAS_VIDEO_TASK_JS = r'''async function pollSmartCanvasVideoTask(taskId){
    if(!taskId) throw new Error(tr('smart.errRunFailed'));
    if(activeSmartTaskPolls.has(taskId)) return activeSmartTaskPolls.get(taskId);
    const promise = (async () => {
        for(let i = 0; i < 1440; i++){
            await sleep(5000);
            const task = await fetch(`/api/canvas-video-tasks/${encodeURIComponent(taskId)}`).then(async r => {
                if(!r.ok) throw new Error(await smartResponseErrorMessage(r, tr('smart.errRunFailed')));
                return r.json();
            });
            if(task.status === 'succeeded') return task.result || task;
            if(task.status === 'failed'){
                const recoverTaskId = task.upstream_task_id || task.submit_id || '';
                if(recoverTaskId && !isSmartTerminalTaskError(task.error)) throw new ImageTaskRecoverSignal({taskId, recoverTaskId, providerId:task.provider_id, kind:'video', message:task.error || tr('smart.errRunFailed')});
                throw new Error(task.error || tr('smart.errRunFailed'));
            }
        }
        throw new Error(tr('smart.errRunTimeout'));
    })();
    activeSmartTaskPolls.set(taskId, promise);
    try {
        return await promise;
    } finally {
        activeSmartTaskPolls.delete(taskId);
    }
}
'''

CANVAS_VIDEO_TASK_HELPERS_JS = r'''function canvasVideoOutputItems(result){
    return resultMediaUrls(result).map(item => {
        const url = outputUrlValue(item);
        return item && typeof item === 'object' ? {...item, url, kind:item.kind || 'video'} : {url, kind:'video'};
    }).filter(item => item.url);
}
async function pollCanvasVideoTask(taskId, options={}){
    if(!taskId) return 'failed';
    if(activeCanvasTaskPolls.has(taskId)) return 'running';
    activeCanvasTaskPolls.add(taskId);
    try {
        while(true){
            const found = findPendingTask(taskId);
            if(!found) return 'missing';
            const cascadeTargetId = String(options?.cascadeTargetId || found?.pending?.cascadeTargetId || '');
            if(cascadeTargetId) ensureCascadeActive(cascadeTargetId);
            const res = await cascadeFetch(`/api/canvas-video-tasks/${encodeURIComponent(taskId)}`, {}, {cascadeTargetId});
            if(!res.ok){
                if(res.status === 404) throw new Error(cascadeBackendRestartMessage());
                throw new Error(await responseErrorMessage(res, tr('canvas.videoFailed')));
            }
            const data = await res.json();
            found.pending.canvasTaskStatus = data.status || 'polling';
            found.pending.recoverTaskId = data.upstream_task_id || data.task_id || data.submit_id || found.pending.recoverTaskId || '';
            found.pending.retryAfter = data.retry_after || null;
            found.pending.nextPollAt = data.next_poll_at || null;
            if(data.status === 'succeeded'){
                completeCanvasVideoTask(taskId, data.result || data);
                return 'succeeded';
            }
            if(data.status === 'failed'){
                failCanvasVideoTask(taskId, data.error || tr('canvas.videoFailed'), data);
                return 'failed';
            }
            refreshNodes([found.out.id]);
            await sleep(5000);
        }
    } catch(err) {
        const message = normalizeCanvasTaskError(err, tr('canvas.videoFailed'));
        if(isCascadeAbortError(err)) return 'aborted';
        failCanvasVideoTask(taskId, message);
        return 'failed';
    } finally {
        activeCanvasTaskPolls.delete(taskId);
    }
}
async function waitCanvasVideoTaskResult(taskId, options={}){
    if(!taskId) throw new Error(tr('canvas.videoFailed'));
    while(true){
        const cascadeTargetId = cascadeTargetIdFromOptions(options);
        if(cascadeTargetId) ensureCascadeActive(cascadeTargetId);
        const res = await cascadeFetch(`/api/canvas-video-tasks/${encodeURIComponent(taskId)}`, {}, {cascadeTargetId});
        if(!res.ok){
            if(res.status === 404) throw new Error(cascadeBackendRestartMessage());
            throw new Error(await responseErrorMessage(res, tr('canvas.videoFailed')));
        }
        const data = await res.json();
        if(data.status === 'succeeded') return data.result || data;
        if(data.status === 'failed') throw new Error(data.error || tr('canvas.videoFailed'));
        await sleep(5000);
    }
}
function completeCanvasVideoTask(taskId, result){
    const found = findPendingTask(taskId);
    if(!found) return;
    const {out, pending} = found;
    const meta = {
        runMs: nowMs() - Number(pending.startedAt || nowMs()),
        run: pending.run || {},
        kind: 'video',
    };
    meta.run.request = requestMetaFromResult(result);
    const outputUrls = canvasVideoOutputItems(result);
    if(!outputUrls.length){
        failCanvasVideoTask(taskId, tr('canvas.videoFailed'));
        return;
    }
    out._pending = (out._pending || []).filter(p => p.id !== pending.id);
    appendOutputImages(out, outputUrls, meta.run?.refs?.[0], [meta]);
    const gen = nodes.find(n => n.id === meta.run?.node?.id);
    if(gen){
        mergeGeneratedOutputs(gen, outputUrls, Boolean(pending.appendGenerated));
        gen.runStatus = 'done';
        gen.runError = '';
        gen.running = false;
    }
    addGenerationLog({run:meta.run, outputs:outputUrls, runMs:meta.runMs || 0});
    refreshRunNodes(gen, out);
    scheduleSave();
}
function failCanvasVideoTask(taskId, message, taskData={}){
    const found = findPendingTask(taskId);
    if(!found) return;
    const {out, pending} = found;
    const run = pending.run || {};
    const runMs = nowMs() - Number(pending.startedAt || nowMs());
    const recoverTaskId = taskData?.upstream_task_id || taskData?.task_id || taskData?.submit_id || pending.canvasTaskId || extractUpstreamTaskId(message);
    const gen = nodes.find(n => n.id === run?.node?.id);
    if(recoverTaskId){
        pending.failed = true;
        pending.querying = false;
        pending.error = message || tr('canvas.videoFailed');
        pending.recoverTaskId = recoverTaskId;
        pending.providerId = taskData?.provider_id || pending.providerId || providerIdForPending(pending);
        pending.canvasTaskStatus = 'failed';
        if(gen){
            gen.runStatus = 'failed';
            gen.runError = pending.error;
            if(pending?.cascadeTargetId) gen._cascadeFailed = true;
            gen.running = false;
        }
        addGenerationLog({run, outputs:[], runMs, error:pending.error});
        refreshRunNodes(gen, out);
        scheduleSave();
        return;
    }
    out._pending = (out._pending || []).filter(p => p.id !== pending.id);
    if(gen){
        gen.runStatus = 'failed';
        gen.runError = message || tr('canvas.videoFailed');
        if(pending?.cascadeTargetId) gen._cascadeFailed = true;
        gen.running = false;
    }
    addGenerationLog({run, outputs:[], runMs, error:message || tr('canvas.videoFailed')});
    refreshRunNodes(gen, out);
    scheduleSave();
}
'''

RUN_VIDEO_NODE_TASK_JS = r'''async function runVideoNode(nodeId, opts={}){
    const node = nodes.find(n => n.id === nodeId);
    if(!node || (node.running && !opts.cascade)) return;
    const cascadeTargetId = cascadeTargetIdFromOptions(opts);
    const sources = orderedSources(node, generatorSources(node));
    const prompt = sources.map(s => s.prompt).filter(Boolean).join('\n\n');
    const allRefs = sources.flatMap(s => s.refs || []);
    const mediaRefs = applyUploadedUrlToRefs((allRefs || []).filter(ref => ['image','video','audio'].includes(mediaKindForRef(ref))), node);
    const refs = imageRefsOnly(mediaRefs);
    const videoRefs = videoRefsOnly(mediaRefs);
    const audioRefs = audioRefsOnly(mediaRefs);
    if(node.useFrameRoles && refs[0]) refs[0] = {...refs[0], role:'first_frame'};
    if(node.useFrameRoles && refs[1]) refs[1] = {...refs[1], role:'last_frame'};
    if(!prompt){ alert(tr('canvas.videoNeedsPrompt')); return; }
    let out = outputForNode(node, 460);
    const pendingId = uid('p');
    const run = runSnapshot(node, prompt, refs);
    const manualVideoUrl = manualVideoUrlForNode(node);
    const payload = {
        prompt,
        provider_id:resolveVideoProviderId(node.apiProvider || 'comfly'),
        model:node.model || 'veo3-fast',
        duration:Number(node.duration || 5),
        aspect_ratio:node.aspectRatio || '16:9',
        resolution:node.resolution || '',
        images:refs,
        videos:manualVideoUrl ? [manualVideoUrl] : videoRefs.map(ref => tempShUploadedUrlForNode(node, ref.url)),
        audios:audioRefs.map(ref => ref.url).filter(Boolean),
        enhance_prompt:Boolean(node.enhancePrompt),
        enable_upsample:Boolean(node.enableUpsample),
        watermark:Boolean(node.watermark),
        camerafixed:Boolean(node.cameraFixed),
        generate_audio:Boolean(node.generateAudio),
        multimodal:Boolean(node.multimodal)
    };
    const startedAt = nowMs();
    let taskInfo = null;
    if(!opts.cascade){
        node.running = true;
        refreshRunNodes(node, out);
        setTimeout(() => { node.running = false; refreshRunNodes(node, out); }, 2000);
    }
    else refreshRunNodes(node, out);
    try {
        taskInfo = await createCanvasVideoTask(payload, {cascadeTargetId});
        if(!out){
            const result = await waitCanvasVideoTaskResult(taskInfo.task_id, {cascadeTargetId});
            const outputUrls = canvasVideoOutputItems(result);
            if(!outputUrls.length) throw new Error(tr('canvas.videoFailed'));
            run.request = requestMetaFromResult(result);
            mergeGeneratedOutputs(node, outputUrls, Boolean(opts.cascade));
            addGenerationLog({run, outputs:outputUrls, runMs:nowMs() - startedAt});
            node.runStatus = 'done';
            node.runError = '';
            node.running = false;
            refreshRunNodes(node, out);
            scheduleSave();
            return;
        }
        out._pending = [
            ...(out._pending || []),
            makePendingForRun(pendingId, run, node, {refs, cascadeTargetId}, {
                canvasTaskId:taskInfo.task_id,
                canvasTaskType:'online-video',
                providerId:payload.provider_id,
                model:payload.model,
                appendGenerated:Boolean(opts.cascade)
            })
        ];
        refreshRunNodes(node, out);
        scheduleSave();
        await saveCanvas();
        const status = await pollCanvasVideoTask(taskInfo.task_id, {cascadeTargetId});
        if(status === 'aborted') throw cascadeAbortError(cascadeStopMessage());
        if(status === 'failed') throw new Error(node.runError || tr('canvas.videoFailed'));
    } catch(err) {
        const pending = pendingById(out, pendingId);
        if(pending && !(pending.failed && pending.recoverTaskId)){
            const meta = collectRunMeta(out, pendingId);
            addGenerationLog({run, outputs:[], runMs:meta.runMs || 0, error:err.message || String(err)});
            if(out) out._pending = (out._pending || []).filter(p => p.id !== pendingId);
        } else if(!pending && !taskInfo) {
            addGenerationLog({run, outputs:[], runMs:nowMs() - startedAt, error:err.message || String(err)});
        }
        if(isCascadeAbortError(err)){
            if(opts.cascade) throw err;
            return;
        }
        node.runStatus = 'failed'; node.runError = err.message || String(err);
        if(pending?.failed && pending.recoverTaskId){
            refreshRunNodes(node, out);
            scheduleSave();
            return;
        }
        refreshRunNodes(node, out);
        scheduleSave();
        if(opts.cascade) throw err;
        showErrorModal(err.message || tr('canvas.videoFailed'), tr('canvas.apiFailed'));
    } finally {
        node.running = false;
        refreshRunNodes(node, out);
    }
}
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
    had_video_protocol_support = "SUPPORTED_VIDEO_REQUEST_MODES" in text
    text = text.replace(
        'VIDEO_POLL_TIMEOUT = float(os.getenv("VIDEO_POLL_TIMEOUT", "1800"))',
        'VIDEO_POLL_TIMEOUT = float(os.getenv("VIDEO_POLL_TIMEOUT", "7200"))',
    )
    text = text.replace(
        'async def wait_for_runninghub_openapi_task(client, provider, task_id, output_kind="", on_progress=None):\n'
        '    query_url = runninghub_openapi_url(provider, "query")\n'
        '    deadline = time.monotonic() + 1800\n',
        'async def wait_for_runninghub_openapi_task(client, provider, task_id, output_kind="", on_progress=None):\n'
        '    query_url = runninghub_openapi_url(provider, "query")\n'
        '    timeout = VIDEO_POLL_TIMEOUT if output_kind == "video" else 1800\n'
        '    deadline = time.monotonic() + timeout\n',
    )
    if "SUPPORTED_VIDEO_REQUEST_MODES" not in text:
        text = regex_replace(
            text,
            r"(SUPPORTED_IMAGE_REQUEST_MODES = \{[^\n]+\}\n)",
            r'\1SUPPORTED_VIDEO_REQUEST_MODES = {"openai-videos-generations", "openai-video-generations", "sudashui-video-generations"}\n',
            "SUPPORTED_VIDEO_REQUEST_MODES",
            required=True,
            flags=0,
        )

    text = text.replace(
        'SUPPORTED_VIDEO_REQUEST_MODES = {"openai-videos-generations", "openai-video-generations"}',
        'SUPPORTED_VIDEO_REQUEST_MODES = {"openai-videos-generations", "openai-video-generations", "sudashui-video-generations"}',
    )

    if "VIDEO_POLL_INTERVAL = 25.0" not in text:
        text = regex_replace(
            text,
            r'(VIDEO_POLL_TIMEOUT = float\(os\.getenv\("VIDEO_POLL_TIMEOUT", "7200"\)\)\n)',
            r'\1VIDEO_POLL_INTERVAL = 25.0\n',
            "VIDEO_POLL_INTERVAL",
            required=True,
            flags=0,
        )

    if not had_video_protocol_support:
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
    if mode in {"sudashui", "sudashui-video"}:
        return "sudashui-video-generations"
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

    if "def video_retry_after_seconds(source):" not in text:
        match = re.search(
            r"def humanize_video_task_failure\(reason\) -> str:\n.*?    return f\"视频生成任务失败：\{text\}\"\n\n",
            text,
            flags=re.S,
        )
        if not match:
            raise PatchError("anchor not found: video retry_after helper")
        text = text[:match.end()] + VIDEO_RETRY_AFTER_PY + "\n\n" + text[match.end():]
    elif 'elif hasattr(source, "headers") and hasattr(source, "json"):' not in text:
        text = replace_once(
            text,
            '    elif isinstance(source, BaseException):\n        scan_text(str(source))\n',
            '    elif hasattr(source, "headers") and hasattr(source, "json"):\n        add_number(source.headers.get("Retry-After"))\n        try:\n            walk(source.json())\n        except Exception:\n            scan_text(getattr(source, "text", "") or str(source))\n    elif isinstance(source, BaseException):\n        scan_text(str(source))\n',
            "video_retry_after raw response support",
        )
    if "def is_video_terminal_error(source):" not in text:
        helper_prefix = VIDEO_RETRY_AFTER_PY.split("def video_retry_after_seconds(source):", 1)[0].rstrip()
        text = replace_once(
            text,
            "def video_retry_after_seconds(source):\n",
            helper_prefix + "\n\ndef video_retry_after_seconds(source):\n",
            "video terminal error helper",
            required=True,
        )

    for legacy_delay in (
        "    delay = max(2.0, IMAGE_POLL_INTERVAL)\n",
        "    delay = max(5.0, IMAGE_POLL_INTERVAL)\n",
    ):
        if legacy_delay in text:
            text = text.replace(legacy_delay, "    delay = VIDEO_POLL_INTERVAL\n", 1)
    text = replace_once(
        text,
        "        raw = None\n        last_error = None\n        for task_url in task_urls:\n",
        "        raw = None\n        last_error = None\n        retry_after_delay = None\n        for task_url in task_urls:\n",
        "video retry_after loop state",
    )
    text = replace_once(
        text,
        "            except Exception as exc:\n                last_error = exc\n                continue\n        if raw is None:\n            if last_error:\n                raise last_error\n            raise HTTPException(status_code=502, detail=f\"视频任务查询失败：{task_id}\")\n",
        "            except Exception as exc:\n                last_error = exc\n                retry_after_delay = video_retry_after_seconds(exc) or retry_after_delay\n                if retry_after_delay:\n                    break\n                continue\n        if raw is None:\n            if retry_after_delay:\n                delay = min(max(VIDEO_POLL_INTERVAL, retry_after_delay), max(0.0, deadline - time.monotonic()))\n                if delay <= 0:\n                    break\n                continue\n            if last_error:\n                raise last_error\n            raise HTTPException(status_code=502, detail=f\"视频任务查询失败：{task_id}\")\n",
        "video retry_after exception handling",
    )
    text = replace_once(
        text,
        "        if status not in VIDEO_TASK_FAILURE_STATUSES and video_output_urls(raw):\n            return raw\n        if status in VIDEO_TASK_FAILURE_STATUSES:\n",
        "        if status not in VIDEO_TASK_FAILURE_STATUSES and video_output_urls(raw):\n            return raw\n        retry_after_delay = video_retry_after_seconds(raw)\n        if retry_after_delay:\n            delay = min(max(VIDEO_POLL_INTERVAL, retry_after_delay), max(0.0, deadline - time.monotonic()))\n            if delay <= 0:\n                break\n            continue\n        if status in VIDEO_TASK_FAILURE_STATUSES:\n",
        "video retry_after payload handling",
    )
    if "response.status_code >= 400 and is_video_terminal_error(response)" not in text:
        text = text.replace(
            "                retry_after_delay = video_retry_after_seconds(response) or retry_after_delay\n                if response.status_code >= 400 and retry_after_delay:\n",
            "                retry_after_delay = video_retry_after_seconds(response) or retry_after_delay\n                if response.status_code >= 400 and is_video_terminal_error(response):\n                    try:\n                        payload = response.json()\n                    except Exception:\n                        payload = {\"error\": response.text}\n                    raise HTTPException(status_code=response.status_code, detail=humanize_video_task_failure(video_task_failure_reason(payload)))\n                if response.status_code >= 400 and retry_after_delay:\n",
        )
    if "if is_video_terminal_error(exc):\n                    raise exc" not in text:
        text = text.replace(
            "            except Exception as exc:\n                last_error = exc\n                retry_after_delay = video_retry_after_seconds(exc) or retry_after_delay\n",
            "            except Exception as exc:\n                if is_video_terminal_error(exc):\n                    raise exc\n                last_error = exc\n                retry_after_delay = video_retry_after_seconds(exc) or retry_after_delay\n",
        )
    if "if is_video_terminal_error(raw):" not in text:
        text = text.replace(
            "        task_data = raw.get(\"data\") if isinstance(raw.get(\"data\"), dict) else raw\n        status = ",
            "        task_data = raw.get(\"data\") if isinstance(raw.get(\"data\"), dict) else raw\n        if is_video_terminal_error(raw):\n            raise HTTPException(status_code=502, detail=humanize_video_task_failure(video_task_failure_reason(raw)))\n        status = ",
        )

    text = text.replace(
        "delay = min(retry_after_delay, max(0.0, deadline - time.monotonic()))",
        "delay = min(max(VIDEO_POLL_INTERVAL, retry_after_delay), max(0.0, deadline - time.monotonic()))",
    )
    text = text.replace(
        "await asyncio.sleep(min(60.0, max(0.0, deadline - time.monotonic())))",
        "await asyncio.sleep(min(VIDEO_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))",
    )
    text = text.replace("delay = min(delay * 1.6, 12)", "delay = VIDEO_POLL_INTERVAL")
    text = text.replace("delay = min(delay * 1.35, 12)", "delay = VIDEO_POLL_INTERVAL")
    text = regex_replace(
        text,
        r'(async def wait_for_agnes_video_task\(.*?deadline = time\.monotonic\(\) \+ VIDEO_POLL_TIMEOUT\n)    delay = 5\.0\n',
        r'\1    delay = VIDEO_POLL_INTERVAL\n',
        "Agnes video poll interval",
    )
    text = regex_replace(
        text,
        r'(async def wait_for_runninghub_openapi_task\(.*?while time\.monotonic\(\) < deadline:\n)        await asyncio\.sleep\(3\)\n',
        r'\1        interval = VIDEO_POLL_INTERVAL if output_kind == "video" else 3.0\n        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))\n',
        "RunningHub video poll interval",
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

    if "CANVAS_VIDEO_TASKS_FILE" not in text:
        text = replace_once(
            text,
            'RUNNINGHUB_WORKFLOW_STORE_FILE = os.path.join(DATA_DIR, "runninghub_workflows.json")\n',
            'RUNNINGHUB_WORKFLOW_STORE_FILE = os.path.join(DATA_DIR, "runninghub_workflows.json")\nCANVAS_VIDEO_TASKS_FILE = os.path.join(DATA_DIR, "canvas_video_tasks.json")\n',
            "canvas video tasks file",
            required=True,
        )

    if "resume_canvas_video_tasks_on_startup()" not in text:
        text = replace_once(
            text,
            '    except Exception as exc:\n        print(f"纠正图片扩展名失败: {exc}")\n\n@app.websocket("/ws/stats")\n',
            '    except Exception as exc:\n        print(f"纠正图片扩展名失败: {exc}")\n    try:\n        await resume_canvas_video_tasks_on_startup()\n    except Exception as exc:\n        print(f"恢复视频任务失败: {exc}")\n\n@app.websocket("/ws/stats")\n',
            "resume canvas video tasks startup",
            required=True,
        )

    if "CANVAS_VIDEO_TERMINAL_STATUSES" not in text:
        text = replace_once(
            text,
            "CANVAS_TASKS: Dict[str, Dict[str, Any]] = {}\nCANVAS_TASK_LOCK = Lock()\n",
            "CANVAS_TASKS: Dict[str, Dict[str, Any]] = {}\nCANVAS_TASK_LOCK = Lock()\n" + CANVAS_VIDEO_TASK_HELPERS_PY + "\n\n",
            "canvas video task helpers",
            required=True,
        )

    text = text.replace(
        "async def generate_jimeng_video(payload: CanvasVideoRequest, provider):",
        "async def generate_jimeng_video(payload: CanvasVideoRequest, provider, progress=None):",
    )
    text = text.replace(
        "        raw = await run_jimeng_cli(args, timeout=jimeng_poll_seconds() + 180)\n        urls = await jimeng_store_outputs(raw, \"video\")\n        return {\"videos\": urls, \"task_id\": jimeng_submit_id(raw) or None, \"raw\": raw}\n",
        "        raw = await run_jimeng_cli(args, timeout=jimeng_poll_seconds() + 180)\n        submit_id = jimeng_submit_id(raw)\n        if submit_id:\n            report_canvas_video_progress(progress, {\"status\": \"polling\", \"upstream_task_id\": submit_id, \"task_id\": submit_id, \"submit_id\": submit_id, \"raw_submit\": raw})\n        urls = await jimeng_store_outputs(raw, \"video\")\n        return {\"videos\": urls, \"task_id\": submit_id or None, \"raw\": raw}\n",
    )

    text = text.replace(
        "async def wait_for_video_task(client, provider, task_id, submit_url=\"\"):",
        "async def wait_for_video_task(client, provider, task_id, submit_url=\"\", on_progress=None):",
    )
    if "def sudashui_video_task_started(raw)" not in text:
        text = replace_once(
            text,
            'async def wait_for_video_task(client, provider, task_id, submit_url="", on_progress=None):\n',
            '''def sudashui_video_task_pending(raw) -> bool:
    if not isinstance(raw, dict):
        return False
    task_data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    status = str(task_data.get("status") or task_data.get("task_status") or "").strip().upper()
    if status in VIDEO_TASK_SUCCESS_STATUSES or status in VIDEO_TASK_FAILURE_STATUSES:
        return False
    if video_output_urls(raw):
        return False
    inner = task_data.get("data") if isinstance(task_data.get("data"), dict) else {}
    inner_state = str(inner.get("state") or inner.get("status") or "").strip().upper()
    return status in {
        "NOT_START", "NOT_STARTED", "SUBMITTED", "QUEUED", "QUEUEING", "PENDING",
        "IN_PROGRESS", "PROCESSING", "RUNNING",
    } or inner_state in {"QUEUEING", "QUEUED", "PENDING", "IN_PROGRESS", "PROCESSING", "RUNNING"}

def sudashui_video_task_started(raw) -> bool:
    if not isinstance(raw, dict):
        return False
    task_data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    status = str(task_data.get("status") or task_data.get("task_status") or "").strip().upper()
    if status in {"NOT_START", "NOT_STARTED", "SUBMITTED", "QUEUED", "QUEUEING", "PENDING"}:
        return False
    try:
        if float(task_data.get("start_time") or 0) > 0:
            return True
    except Exception:
        pass
    progress_text = str(task_data.get("progress") or "").strip()
    progress_match = re.search(r"\\d+(?:\\.\\d+)?", progress_text)
    if progress_match and float(progress_match.group(0)) > 0:
        return True
    inner = task_data.get("data") if isinstance(task_data.get("data"), dict) else {}
    inner_state = str(inner.get("state") or inner.get("status") or "").strip().upper()
    return status in {"IN_PROGRESS", "PROCESSING", "RUNNING"} or inner_state in {"IN_PROGRESS", "PROCESSING", "RUNNING"}

async def wait_for_video_task(client, provider, task_id, submit_url="", on_progress=None):
''',
            "Sudashui queue start detector",
            required=True,
        )
    if "recover_failed_sudashui" not in text:
        text = replace_once(
            text,
            '''        if status in CANVAS_VIDEO_TERMINAL_STATUSES:
            continue
        if status not in CANVAS_VIDEO_RESUMABLE_STATUSES:
            continue
        if canvas_video_upstream_task_id(task):
            update_canvas_video_task(task_id, {"status": "polling", "message": "服务重启后已恢复视频任务查询"})
''',
            '''        if status in CANVAS_VIDEO_TERMINAL_STATUSES:
            recover_failed_sudashui = False
            if status == "failed" and canvas_video_upstream_task_id(task):
                try:
                    provider = get_api_provider_exact(str(task.get("provider_id") or "").strip())
                    recover_failed_sudashui = (
                        is_sudashui_video_generations_mode(provider)
                        and sudashui_video_task_pending(task.get("raw_last"))
                    )
                except Exception:
                    recover_failed_sudashui = False
            if not recover_failed_sudashui:
                continue
        if status not in CANVAS_VIDEO_RESUMABLE_STATUSES:
            if status != "failed":
                continue
        if canvas_video_upstream_task_id(task):
            update_canvas_video_task(task_id, {
                "status": "polling",
                "error": "",
                "message": "服务重启后已恢复视频任务查询",
            })
''',
            "recover pending Sudashui task after false terminal failure",
            required=True,
        )
        text = replace_once(
            text,
            "    task_urls = video_task_url_candidates(provider, base_url, task_id, submit_url)\n    deadline = time.monotonic() + VIDEO_POLL_TIMEOUT\n",
            "    task_urls = video_task_url_candidates(provider, base_url, task_id, submit_url)\n    wait_for_sudashui_start = is_sudashui_video_generations_mode(provider)\n    deadline = None if wait_for_sudashui_start else time.monotonic() + VIDEO_POLL_TIMEOUT\n",
            "Sudashui queue deadline",
            required=True,
        )
        text = replace_once(
            text,
            "    while time.monotonic() < deadline:\n",
            "    while deadline is None or time.monotonic() < deadline:\n",
            "Sudashui queue polling loop",
            required=True,
        )
        for interval_name in ("poll_interval", "VIDEO_POLL_INTERVAL"):
            old_delay = f"                delay = min(max({interval_name}, retry_after_delay), max(0.0, deadline - time.monotonic()))\n"
            new_delay = (
                f"                delay = max({interval_name}, retry_after_delay)\n"
                "                if deadline is not None:\n"
                "                    delay = min(delay, max(0.0, deadline - time.monotonic()))\n"
            )
            text = text.replace(old_delay, new_delay, 2)
        text = replace_once(
            text,
            '        status = str(task_data.get("status") or task_data.get("task_status") or raw.get("status") or raw.get("task_status") or "").upper()\n',
            '        status = str(task_data.get("status") or task_data.get("task_status") or raw.get("status") or raw.get("task_status") or "").upper()\n'
            '        if wait_for_sudashui_start and deadline is None and sudashui_video_task_started(raw):\n'
            '            deadline = time.monotonic() + VIDEO_POLL_TIMEOUT\n',
            "Sudashui generation deadline start",
            required=True,
        )
    text = text.replace(
        "async def wait_for_agnes_video_task(client, provider, video_id, model):",
        "async def wait_for_agnes_video_task(client, provider, video_id, model, on_progress=None):",
    )
    text = text.replace(
        "async def generate_agnes_video(client, payload, provider, base_url, requested_model):",
        "async def generate_agnes_video(client, payload, provider, base_url, requested_model, progress=None):",
    )
    text = text.replace(
        "async def generate_runninghub_video(payload, provider):",
        "async def generate_runninghub_video(payload, provider, progress=None):",
    )
    text = text.replace(
        "async def generate_yuli_openai_video(client, payload, provider, base_url, requested_model):",
        "async def generate_yuli_openai_video(client, payload, provider, base_url, requested_model, progress=None):",
    )
    text = text.replace(
        "result = await wait_for_video_task(client, provider, task_id, submit_url)",
        "result = await wait_for_video_task(client, provider, task_id, submit_url, progress)",
    )
    text = text.replace(
        "result = await wait_for_agnes_video_task(client, provider, video_id, model)",
        "result = await wait_for_agnes_video_task(client, provider, video_id, model, progress)",
    )
    text = text.replace(
        "return await generate_runninghub_video(payload, provider)",
        "return await generate_runninghub_video(payload, provider, progress)",
    )
    text = text.replace(
        "return await generate_jimeng_video(payload, provider)",
        "return await generate_jimeng_video(payload, provider, progress)",
    )
    text = text.replace(
        "return await generate_agnes_video(agnes_client, payload, provider, base_url, requested_model)",
        "return await generate_agnes_video(agnes_client, payload, provider, base_url, requested_model, progress)",
    )
    text = text.replace(
        "return await generate_yuli_openai_video(yuli_client, payload, provider, base_url, requested_model)",
        "return await generate_yuli_openai_video(yuli_client, payload, provider, base_url, requested_model, progress)",
    )
    text = text.replace(
        "        task_id = runninghub_extract_task_id(raw)\n        result = raw\n",
        "        task_id = runninghub_extract_task_id(raw)\n        if task_id:\n            report_canvas_video_progress(progress, {\"status\": \"polling\", \"upstream_task_id\": task_id, \"task_id\": task_id, \"submit_url\": endpoint, \"raw_submit\": raw})\n        result = raw\n",
    )
    text = text.replace(
        "    task_id = str(raw.get(\"task_id\") or raw.get(\"id\") or \"\").strip()\n    result = raw\n",
        "    task_id = str(raw.get(\"task_id\") or raw.get(\"id\") or \"\").strip()\n    if video_id or task_id:\n        report_canvas_video_progress(progress, {\"status\": \"polling\", \"upstream_task_id\": video_id or task_id, \"task_id\": video_id or task_id, \"submit_url\": submit_url, \"raw_submit\": raw})\n    result = raw\n",
    )
    text = text.replace(
        "    task_id = raw.get(\"id\") or extract_task_id(raw) or raw.get(\"task_id\")\n    result = raw\n",
        "    task_id = raw.get(\"id\") or extract_task_id(raw) or raw.get(\"task_id\")\n    if task_id:\n        report_canvas_video_progress(progress, {\"status\": \"polling\", \"upstream_task_id\": task_id, \"task_id\": task_id, \"submit_url\": submit_url, \"raw_submit\": raw})\n    result = raw\n",
    )
    text = text.replace(
        "            task_id = extract_task_id(raw) or raw.get(\"task_id\") or raw.get(\"id\")\n            result = raw\n",
        "            task_id = extract_task_id(raw) or raw.get(\"task_id\") or raw.get(\"id\")\n            if task_id:\n                report_canvas_video_progress(progress, {\"status\": \"polling\", \"upstream_task_id\": task_id, \"task_id\": task_id, \"submit_url\": submit_url, \"raw_submit\": raw})\n            result = raw\n",
    )

    if "async def build_canvas_video_result(payload: CanvasVideoRequest, progress=None):" not in text:
        text = replace_once(
            text,
            '@app.post("/api/canvas-video")\nasync def canvas_video(payload: CanvasVideoRequest):\n',
            "async def build_canvas_video_result(payload: CanvasVideoRequest, progress=None):\n",
            "extract canvas video builder",
            required=True,
        )
    if '@app.post("/api/canvas-video-tasks")' not in text:
        text = replace_once(
            text,
            "\n# --- Canvas LLM ---\n",
            "\n" + CANVAS_VIDEO_TASK_ENDPOINTS_PY + "\n# --- Canvas LLM ---\n",
            "canvas video task endpoints",
            required=True,
        )

    return text


def patch_html(text):
    text = ensure_video_api_utils_script(text, "api-settings.js")
    replacements = {
        'title="Video API"': 'title="视频接口"',
        "Video: /v1/videos/generations": "视频：videos",
        "Video: /v1/video/generations": "视频：video",
        "视频：/v1/videos/generations": "视频：videos",
        "视频：/v1/video/generations": "视频：video",
        "Video: Sudashui": "Sudashui: Seedance",
        "视频：Sudashui": "Sudashui：Seedance",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if "videoRequestModeInput" in text:
        if 'value="sudashui-video-generations"' not in text:
            text = text.replace(
                '                                        <option value="openai-video-generations">视频：video</option>\n',
                '                                        <option value="openai-video-generations">视频：video</option>\n                                        <option value="sudashui-video-generations">Sudashui：Seedance</option>\n',
                1,
            )
        return text
    pattern = r'([ \t]*<div class="field-frame image-request-mode-wrap">\n.*?<select id="imageRequestModeInput".*?</select>\n[ \t]*</div>\n)'
    return regex_replace(text, pattern, r"\1" + VIDEO_SELECT_HTML, "videoRequestModeInput HTML", required=True)


def ensure_video_api_utils_script(text, target_script):
    helper_path = "/static/js/video-api-utils.js"
    target_path = f"/static/js/{target_script}"
    if helper_path in text:
        helper_pos = text.find(helper_path)
        target_pos = text.find(target_path)
        if target_pos >= 0 and helper_pos > target_pos:
            raise PatchError(f"video-api-utils.js 必须在 {target_script} 之前加载")
        return text
    pattern = rf'([ \t]*<script src="{re.escape(target_path)}\?v=([^"\s]+)"></script>)'
    match = re.search(pattern, text)
    if not match:
        raise PatchError(f"anchor not found: {target_script} script tag")
    indent = re.match(r"[ \t]*", match.group(1)).group(0)
    helper_tag = f'{indent}<script src="{helper_path}?v={match.group(2)}"></script>\n'
    return text[:match.start(1)] + helper_tag + text[match.start(1):]


def patch_canvas_html(text):
    return ensure_video_api_utils_script(text, "canvas.js")


def patch_smart_canvas_html(text):
    return ensure_video_api_utils_script(text, "smart-canvas.js")


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
    if(['sudashui', 'sudashui-video'].includes(mode)) return 'sudashui-video-generations';
    return ['openai-videos-generations', 'openai-video-generations', 'sudashui-video-generations'].includes(mode) ? mode : 'openai-videos-generations';
}
''',
            "normalizeVideoRequestMode",
            required=True,
        )
    text = text.replace(
        '        r"billing",\n        r"余额不足",',
        '        r"billing[_\\s-]*(?:error|failed|failure|disabled|issue|problem)",\n'
        '        r"billing\\s+account\\s+(?:disabled|inactive|suspended)",\n'
        '        r"余额不足",',
        1,
    )

    text = text.replace(
        '    if mode in {"openai-videos", "videos-generations"}:\n        return "openai-videos-generations"\n    return mode if mode in SUPPORTED_VIDEO_REQUEST_MODES else "openai-videos-generations"',
        '    if mode in {"openai-videos", "videos-generations"}:\n        return "openai-videos-generations"\n    if mode in {"sudashui", "sudashui-video"}:\n        return "sudashui-video-generations"\n    return mode if mode in SUPPORTED_VIDEO_REQUEST_MODES else "openai-videos-generations"',
    )

    text = text.replace(
        "    return ['openai-videos-generations', 'openai-video-generations'].includes(mode) ? mode : 'openai-videos-generations';",
        "    if(['sudashui', 'sudashui-video'].includes(mode)) return 'sudashui-video-generations';\n    return ['openai-videos-generations', 'openai-video-generations', 'sudashui-video-generations'].includes(mode) ? mode : 'openai-videos-generations';",
    )
    text = text.replace(
        "if(normalized === 'sudashui-video-generations') return 'Sudashui /v1/video/generations';",
        "if(normalized === 'sudashui-video-generations') return 'Sudashui: Seedance';",
    )

    if "function videoRequestModeLabel" not in text:
        text = regex_replace(
            text,
            r"(function imageRequestModeLabel\(mode\)\{\n(?:    .+\n)+?\}\n)",
            r'''\1function videoRequestModeLabel(mode){
    const normalized = normalizeVideoRequestMode(mode);
    if(normalized === 'sudashui-video-generations') return 'Sudashui: Seedance';
    return normalized === 'openai-video-generations' ? '/v1/video/generations' : '/v1/videos/generations';
}
''',
            "videoRequestModeLabel",
            required=True,
        )

    if "normalized === 'sudashui-video-generations'" not in text:
        text = text.replace(
            "    const normalized = normalizeVideoRequestMode(mode);\n    return normalized === 'openai-video-generations' ? '/v1/video/generations' : '/v1/videos/generations';",
            "    const normalized = normalizeVideoRequestMode(mode);\n    if(normalized === 'sudashui-video-generations') return 'Sudashui: Seedance';\n    return normalized === 'openai-video-generations' ? '/v1/video/generations' : '/v1/videos/generations';",
            1,
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


def patch_canvas_js(text):
    if "async function createCanvasVideoTask" not in text:
        text = regex_replace(
            text,
            r"(async function createCanvasImageTask\(payload, options=\{\}\)\{\n.*?    return res\.json\(\);\n\}\n)",
            r"\1" + CREATE_CANVAS_VIDEO_TASK_JS,
            "createCanvasVideoTask",
            required=True,
        )

    if "function canvasVideoOutputItems" not in text:
        text = regex_replace(
            text,
            r"(async function waitCanvasImageTaskResult\(taskId, options=\{\}\)\{\n.*?        await sleep\(1800\);\n    \}\n\}\n)",
            r"\1" + CANVAS_VIDEO_TASK_HELPERS_JS,
            "canvas video task frontend helpers",
            required=True,
        )

    if "canvasTaskType:'online-video'" not in text:
        text = regex_replace(
            text,
            r"async function runVideoNode\(nodeId, opts=\{\}\)\{\n.*?\n\}\nasync function uploadCanvasUrlToComfy",
            RUN_VIDEO_NODE_TASK_JS + "\nasync function uploadCanvasUrlToComfy",
            "runVideoNode task mode",
            required=True,
        )

    if "pollCanvasVideoTask(p.canvasTaskId" not in text:
        text = replace_once(
            text,
            "            if(p.canvasTaskType === 'online-image' && p.canvasTaskId && !p.failed) pollCanvasImageTask(p.canvasTaskId, {cascadeTargetId:p.cascadeTargetId || ''});\n",
            "            if(p.canvasTaskType === 'online-image' && p.canvasTaskId && !p.failed) pollCanvasImageTask(p.canvasTaskId, {cascadeTargetId:p.cascadeTargetId || ''});\n            if(p.canvasTaskType === 'online-video' && p.canvasTaskId && !p.failed) pollCanvasVideoTask(p.canvasTaskId, {cascadeTargetId:p.cascadeTargetId || ''});\n",
            "resume canvas video tasks",
            required=True,
        )

    return text


def patch_smart_canvas_js(text):
    text = text.replace(
        "payment\\s+required|billing|余额不足|额度不足",
        "payment\\s+required|billing[_\\s-]*(?:error|failed|failure|disabled|issue|problem)|billing\\s+account\\s+(?:disabled|inactive|suspended)|余额不足|额度不足",
    )
    if "async function createSmartCanvasVideoTask" not in text:
        text = regex_replace(
            text,
            r"(async function createSmartComfyTask\(payload\)\{\n.*?    return res\.json\(\);\n\}\n)",
            r"\1" + SMART_CREATE_CANVAS_VIDEO_TASK_JS,
            "smart canvas video task submit",
            required=True,
        )

    text = replace_once(
        text,
        "    if(activeSettings.engine === 'comfy') return generateComfyUrlsWithSettings(activeSettings, prompt, refs);\n    if(isApiLikeEngine(activeSettings.engine) && activeSettings.apiKind === 'video'){\n        return {urls:await runApiVideoGeneration(prompt, refs, activeSettings), kind:'video'};\n    }\n",
        "    if(activeSettings.engine === 'comfy') return generateComfyUrlsWithSettings(activeSettings, prompt, refs);\n    if(isApiLikeEngine(activeSettings.engine) && activeSettings.apiKind === 'video'){\n        const taskResult = await runApiVideoGeneration(prompt, refs, activeSettings);\n        const taskIds = Array.isArray(taskResult?.taskIds) ? taskResult.taskIds : [];\n        if(taskIds.length){\n            const settled = await Promise.all(taskIds.map(taskId => pollSmartCanvasVideoTask(taskId)));\n            const urls = settled.flatMap(result => resultMediaUrls(result?.videos?.length ? result.videos : (result?.result || result))).filter(Boolean);\n            return {urls, kind:'video'};\n        }\n        const urls = resultMediaUrls(taskResult);\n        return {urls, kind:'video'};\n    }\n",
        "smart generateUrls video task polling",
    )

    if "return runApiVideoGeneration(prompt, refs, runSettings);" not in text:
        text = replace_once(
            text,
            "async function runApiGeneration(prompt, refs, runSettings=settings){\n    if(!runSettings.provider_id || !runSettings.model) throw new Error(tr('smart.errNoApiModel'));\n",
            "async function runApiGeneration(prompt, refs, runSettings=settings){\n    if(isApiLikeEngine(runSettings.engine) && runSettings.apiKind === 'video'){\n        return runApiVideoGeneration(prompt, refs, runSettings);\n    }\n    if(!runSettings.provider_id || !runSettings.model) throw new Error(tr('smart.errNoApiModel'));\n",
            "smart runApiGeneration video route",
            required=True,
        )

    if "const task = await createSmartCanvasVideoTask(payload);" not in text:
        text = replace_once(
            text,
            "        const result = await fetch('/api/canvas-video', {\n            method:'POST',\n            headers:{'Content-Type':'application/json'},\n            body:JSON.stringify(payload)\n        }).then(async r => { if(!r.ok) throw new Error(await smartResponseErrorMessage(r, tr('smart.errRunFailed'))); return r.json(); });\n        if(result && result.jimeng_pending) throw new JimengPendingSignal({submitId:result.submit_id, kind:result.kind || 'video', queueInfo:result.queue_info, message:result.message});\n        return resultMediaUrls(result);\n",
            "        const task = await createSmartCanvasVideoTask(payload);\n        const taskId = task?.task_id || task?.id || '';\n        if(!taskId) throw new Error(tr('smart.errRunFailed'));\n        return {taskIds:[taskId], count:1, providerId:payload.provider_id, model:payload.model, kind:'video'};\n",
            "smart video long request to task submit",
            required=True,
        )

    if "const outVideos = await runApiVideoGeneration" in text:
        text = replace_once(
            text,
            "        if(isApiLikeEngine(settings.engine) && settings.apiKind === 'video'){\n            const outVideos = await runApiVideoGeneration(prompt, refs);\n            if(!outVideos.length) throw new Error(tr('smart.errNoOutVideos'));\n            finalizePendingNode(pendingNode, outVideos, pendingMeta, 'video');\n            if(sourceVisualState) restoreSourceVisualState(node, sourceVisualState);\n            addSmartGenerationLog({run:runLog, outputs:outVideos, runMs:nowMs() - runLogStart});\n            clearPromptInput({preserveDraft:true});\n            settings = previousSettings;\n            scheduleSave();\n            return;\n        }\n",
            "",
            "smart runGeneration remove long video branch",
            required=True,
        )

    text = replace_once(
        text,
        "        ? (settings.comfyMode === 'text' || settings.comfyMode === 'enhance' || settings.comfyMode === 'edit' || settings.comfyMode === 'custom' ? 1 : 1)\n        : Math.max(1, Math.min(8, Number(settings.count || 1)));\n",
        "        ? (settings.comfyMode === 'text' || settings.comfyMode === 'enhance' || settings.comfyMode === 'edit' || settings.comfyMode === 'custom' ? 1 : 1)\n        : isApiLikeEngine(settings.engine) && settings.apiKind === 'video'\n        ? 1\n        : Math.max(1, Math.min(8, Number(settings.count || 1)));\n",
        "smart video expected count",
    )
    text = replace_once(
        text,
        "            pendingNode.pendingTasks = taskIds.map(taskId => ({taskId, kind:'image', providerId:outImages.providerId, model:outImages.model}));\n",
        "            const taskKind = outImages.kind || 'image';\n            pendingNode.pendingTasks = taskIds.map(taskId => ({taskId, kind:taskKind, providerId:outImages.providerId, model:outImages.model}));\n",
        "smart pending task kind",
    )
    text = replace_once(
        text,
        "            if(!(pendingNode.images || []).length) throw new Error(tr('smart.errNoOutImages'));\n",
        "            if(!(pendingNode.images || []).length) throw new Error(taskKind === 'video' ? tr('smart.errNoOutVideos') : tr('smart.errNoOutImages'));\n",
        "smart pending media empty error",
    )

    if "function isSmartTerminalTaskError" not in text:
        text = replace_once(
            text,
            "function providerIdForSmartTask(node, task){\n    return task?.providerId || node?.runSettings?.provider_id || settings.provider_id || 'comfly';\n}\n",
            "function providerIdForSmartTask(node, task){\n    return task?.providerId || node?.runSettings?.provider_id || settings.provider_id || 'comfly';\n}\nfunction isSmartTerminalTaskError(message){\n    const text = String(message || '').toLowerCase();\n    return /(insufficient[_\\s-]*quota|insufficient\\s+credits?|credits[_\\s-]*remaining|not\\s+enough\\s+credits?|quota\\s+exceeded|payment\\s+required|billing[_\\s-]*(?:error|failed|failure|disabled|issue|problem)|billing\\s+account\\s+(?:disabled|inactive|suspended)|余额不足|额度不足)/i.test(text);\n}\n",
            "smart terminal task error helper",
            required=True,
        )

    text = replace_once(
        text,
        "    const recoverTaskId = task.recoverTaskId || extractUpstreamTaskId(task.error || '');\n",
        "    const recoverTaskId = task.kind === 'video'\n        ? (task.recoverTaskId || task.taskId || localTaskId || extractUpstreamTaskId(task.error || ''))\n        : (task.recoverTaskId || extractUpstreamTaskId(task.error || ''));\n",
        "smart recover task id by kind",
    )
    if "if(task.kind === 'video'){" not in text:
        text = replace_once(
            text,
            "    try {\n        const data = await fetchImageTaskQuery(providerIdForSmartTask(node, task), recoverTaskId);\n",
            "    try {\n        if(task.kind === 'video'){\n            const res = await fetch(`/api/canvas-video-tasks/${encodeURIComponent(task.taskId || localTaskId)}`);\n            if(!res.ok) throw new Error(await smartResponseErrorMessage(res, tr('smart.errRunFailed')));\n            const data = await res.json();\n            if(data.status === 'succeeded'){\n                task.failed = false;\n                task.querying = false;\n                finalizeSmartPendingTask(node, task.taskId, resultMediaUrls(data.videos?.length ? data.videos : (data.result || data)), 'video');\n                render();\n                scheduleSave();\n                return;\n            }\n            if(data.status === 'failed'){\n                task.error = data.error || tr('smart.errRunFailed');\n                toast(task.error.slice(0, 160));\n            } else {\n                task.failed = false;\n                task.error = data.message || '视频任务仍在生成中';\n                toast(task.error);\n                pollSmartCanvasVideoTask(task.taskId || localTaskId).then(result => {\n                    finalizeSmartPendingTask(node, task.taskId || localTaskId, resultMediaUrls(result?.videos?.length ? result.videos : (result?.result || result)), 'video');\n                    render();\n                    scheduleSave();\n                }).catch(err => toast((err.message || tr('smart.errRunFailed')).slice(0, 160)));\n            }\n            return;\n        }\n        const data = await fetchImageTaskQuery(providerIdForSmartTask(node, task), recoverTaskId);\n",
            "smart video manual task query",
            required=True,
        )

    text = replace_once(
        text,
        "                if(recoverTaskId) throw new ImageTaskRecoverSignal({taskId, recoverTaskId, providerId:task.provider_id, kind:'image', message:task.error || tr('smart.errRunFailed')});\n",
        "                if(recoverTaskId && !isSmartTerminalTaskError(task.error)) throw new ImageTaskRecoverSignal({taskId, recoverTaskId, providerId:task.provider_id, kind:task.kind || 'image', message:task.error || tr('smart.errRunFailed')});\n",
        "smart terminal image task failure",
    )
    if "async function pollSmartCanvasVideoTask" not in text:
        text = replace_once(
            text,
            "function finalizeSmartPendingTask(node, taskId, images, kind='image'){\n",
            SMART_POLL_CANVAS_VIDEO_TASK_JS + "function finalizeSmartPendingTask(node, taskId, images, kind='image'){\n",
            "smart video task poller",
            required=True,
        )

    text = replace_once(
        text,
        "            const result = await pollSmartCanvasTask(task.taskId);\n            finalizeSmartPendingTask(node, task.taskId, resultMediaUrls(result?.image_items?.length ? result.image_items : (result?.images?.length ? result.images : result)), task.kind || 'image');\n",
        "            const taskKind = task.kind || 'image';\n            const result = taskKind === 'video'\n                ? await pollSmartCanvasVideoTask(task.taskId)\n                : await pollSmartCanvasTask(task.taskId);\n            const media = taskKind === 'video'\n                ? (result?.videos?.length ? result.videos : (result?.result || result))\n                : (result?.image_items?.length ? result.image_items : (result?.images?.length ? result.images : result));\n            finalizeSmartPendingTask(node, task.taskId, resultMediaUrls(media), taskKind);\n",
        "smart resume media task polling",
    )
    text = replace_once(
        text,
        "                task.providerId = e.providerId || task.providerId || providerIdForSmartTask(node, task);\n                task.error = e.message || tr('smart.errRunFailed');\n",
        "                task.providerId = e.providerId || task.providerId || providerIdForSmartTask(node, task);\n                task.kind = e.kind || task.kind || 'image';\n                task.error = e.message || tr('smart.errRunFailed');\n",
        "smart recover task kind",
    )
    text = replace_once(
        text,
        "        if(!e?.smartGenerationLogged) addSmartGenerationLog({run:runLog, outputs:[], runMs:nowMs() - runLogStart, error:e.message || String(e)});\n        toast((e.message || tr('smart.errRunFailed')).slice(0, 160));\n",
        "        if(!e?.smartGenerationLogged) addSmartGenerationLog({run:runLog, outputs:[], runMs:nowMs() - runLogStart, error:e.message || String(e)});\n        scheduleSave();\n        toast((e.message || tr('smart.errRunFailed')).slice(0, 160));\n",
        "smart failure save",
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


def validate(root, overrides=None):
    overrides = overrides or {}
    checks = {
        "main.py": [
            "SUPPORTED_VIDEO_REQUEST_MODES",
            '"sudashui-video-generations"',
            "def effective_video_request_mode(provider)",
            "def is_sudashui_video_generations_mode(provider)",
            "def sudashui_video_task_pending(raw)",
            "def sudashui_video_task_started(raw)",
            "recover_failed_sudashui",
            "wait_for_sudashui_start and sudashui_business_failure(raw)",
            "Sudashui 视频任务查询暂时失败，将自动重试",
            "def sudashui_video_body(",
            "async def generate_sudashui_video(",
            "official_asset_indexes: List[StrictInt]",
            '"metadata": {"payload": json.dumps(',
            'SUDASHUI_FILES_BASE_URL = "https://files.sudashuiapi.com"',
            "openai_video_generations_reference_urls",
            "is_single_video_generations = is_openai_video_generations_mode(provider)",
            "def video_retry_after_seconds(source):",
            "def is_video_terminal_error(source):",
            "VIDEO_POLL_INTERVAL = 25.0",
            "delay = VIDEO_POLL_INTERVAL",
            "CANVAS_VIDEO_TASKS_FILE",
            "def update_canvas_video_task",
            '@app.post("/api/canvas-video-tasks")',
        ],
        "static/api-settings.html": [
            "videoRequestModeInput",
            'value="sudashui-video-generations"',
            "/static/js/video-api-utils.js",
        ],
        "static/canvas.html": [
            "/static/js/video-api-utils.js",
            "/static/js/canvas.js",
        ],
        "static/smart-canvas.html": [
            "/static/js/video-api-utils.js",
            "/static/js/smart-canvas.js",
        ],
        "static/css/api-settings.css": [
            ".video-request-mode-wrap select",
            ".video-request-mode-wrap select { min-width:118px; }",
        ],
        "static/js/api-settings.js": [
            "const videoRequestModeInput",
            "function normalizeVideoRequestMode",
            "sudashui-video-generations",
            "videoRequestModeSudashuiLabel",
            "video_request_mode:item.video_request_mode || 'openai-videos-generations'",
        ],
        "static/js/video-api-utils.js": [
            "global.StudioVideoApi",
            "SUDASHUI_ASPECT_RATIOS",
            "function parseOfficialAssetIndexes",
            "function inferModelResolution",
            "function effectiveVideoResolution",
        ],
        "static/js/canvas.js": [
            "async function createCanvasVideoTask",
            "async function pollCanvasVideoTask",
            "canvasTaskType:'online-video'",
            "pollCanvasVideoTask(p.canvasTaskId",
            "validateCanvasSudashuiVideoRequest",
            "official_asset_indexes",
            "resolutionReadOnly",
        ],
        "static/js/smart-canvas.js": [
            "async function createSmartCanvasVideoTask",
            "async function pollSmartCanvasVideoTask",
            "pollSmartCanvasVideoTask(task.taskId)",
            "kind:taskKind",
            "isSmartTerminalTaskError",
            "validateSmartSudashuiVideoRequest",
            "official_asset_indexes",
            "resolutionReadOnly",
        ],
    }
    missing = []
    for rel, needles in checks.items():
        path = root / rel
        if rel not in overrides and not path.exists():
            missing.append(f"{rel}: file missing")
            continue
        text = overrides.get(rel, read(path) if path.exists() else "")
        for needle in needles:
            if needle not in text:
                missing.append(f"{rel}: {needle}")
    for rel, target_script in (
        ("static/api-settings.html", "api-settings.js"),
        ("static/canvas.html", "canvas.js"),
        ("static/smart-canvas.html", "smart-canvas.js"),
    ):
        path = root / rel
        text = overrides.get(rel, read(path) if path.exists() else "")
        if text.find("/static/js/video-api-utils.js") > text.find(f"/static/js/{target_script}"):
            missing.append(f"{rel}: video-api-utils.js load order")
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
        ("static/canvas.html", patch_canvas_html),
        ("static/smart-canvas.html", patch_smart_canvas_html),
        ("static/css/api-settings.css", patch_css),
        ("static/js/api-settings.js", patch_js),
        ("static/js/canvas.js", patch_canvas_js),
        ("static/js/smart-canvas.js", patch_smart_canvas_js),
    ]
    planned = {}
    for rel, func in targets:
        path = root / rel
        if not path.exists():
            raise PatchError(f"missing file: {rel}")
        patched = func(read(path))
        planned[rel] = patched
        write_if_changed(path, patched, root, args.dry_run, backup_dir, changed)

    if args.dry_run:
        validate(root, planned)
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
            ("Smart canvas JS syntax check", ["node", "--check", "static/js/smart-canvas.js"]),
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
