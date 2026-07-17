"""Tudou Grok Imagine 与 Sora2 视频协议适配。

本模块只负责 Tudou 上游协议，不依赖 Infinite Canvas 的全局状态。
宿主通过回调提供本地路径解析、参考图公网化、任务进度记录和视频落盘。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from .common import (
    UnsafePublicUrlError,
    canonical_video_api_root,
    public_http_get,
    resolve_video_download_url,
)


TUDOU_VIDEO_REQUEST_MODE = "tudou-video"
TUDOU_OFFICIAL_HOSTNAMES = {"api.ai-tudou.net"}

GROK_MODEL = "grok-imagine-video"
GROK_IMAGE_TO_VIDEO_MODEL = "grok-imagine-video-1.5"
GROK_MODELS = {GROK_MODEL, GROK_IMAGE_TO_VIDEO_MODEL}
SORA_MODEL = "sora2"
SUPPORTED_MODELS = GROK_MODELS | {SORA_MODEL}

_CANONICAL_MODELS = {model.lower(): model for model in SUPPORTED_MODELS}
_GROK_DURATIONS = {6, 10, 12, 16, 20}
_SORA_DURATIONS = {4, 8, 12}
_GROK_SIZES = {
    "720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024",
}
_GROK_DEFAULT_SIZE_BY_RATIO = {
    "9:16": "720x1280",
    "16:9": "1280x720",
    "1:1": "1024x1024",
}
_GROK_RESOLUTIONS = {"480p", "720p"}
_GROK_PRESETS = {"fun", "normal", "spicy", "custom"}
_SORA_RATIOS = {"16:9", "9:16"}
_MAX_GROK_IMAGES = 7
_MAX_SORA_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_LOCAL_REFERENCE_BYTES = 30 * 1024 * 1024
_MAX_POLL_ERRORS = 5
_REFERENCE_DOWNLOAD_ATTEMPTS = 3

_SUCCESS_STATUSES = {
    "SUCCESS", "SUCCEED", "SUCCEEDED", "COMPLETED", "COMPLETE",
    "DONE", "FINISHED", "FINISH", "OK", "READY",
}
_FAILURE_STATUSES = {
    "FAILURE", "FAILED", "FAIL", "ERROR", "ERRORED", "ABORTED", "DELETED",
    "CANCELED", "CANCELLED", "TIMEOUT", "TIMEDOUT", "REJECTED", "EXPIRED",
}
_URL_KEYS = {
    "url", "video_url", "videoUrl", "mp4_url", "mp4Url", "output_url", "outputUrl",
    "result_url", "resultUrl", "content_url", "contentUrl", "download_url", "downloadUrl",
    "src", "uri", "path", "output", "result", "content", "video",
}
_CONTAINER_KEYS = {
    "videos", "outputs", "data", "detail", "result", "results", "content", "metadata",
}
_PRIVATE_HOST_RE = re.compile(
    r"^(?:localhost|0\.0\.0\.0|127\.|10\.|192\.168\.|169\.254\.|172\.(?:1[6-9]|2\d|3[01])\.)",
    re.IGNORECASE,
)


class TudouProtocolError(Exception):
    """带 HTTP 状态语义的 Tudou 协议错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


@dataclass
class _Submission:
    model: str
    submit_path: str
    request_format: str
    body: Dict[str, Any]
    files: List[Tuple[str, Tuple[str, bytes, str]]]


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
ResolveLocalPath = Callable[[str], Optional[str]]
ContentTypeForPath = Callable[[str], str]
PublicReferenceUrl = Callable[[str], Awaitable[str]]
SaveVideo = Callable[[str], Awaitable[str]]


def is_tudou_official_provider(provider: Optional[Mapping[str, Any]]) -> bool:
    base_url = str((provider or {}).get("base_url") or "").strip()
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "").lower() in TUDOU_OFFICIAL_HOSTNAMES
    except Exception:
        return False


def _api_root(base_url: Any) -> str:
    return canonical_video_api_root(base_url)


def _api_url(base_url: Any, path: str) -> str:
    root = _api_root(base_url)
    suffix = str(path or "").strip().lstrip("/")
    return f"{root}/{suffix}" if suffix else root


def canonical_model(value: Any) -> str:
    model = str(value or "").strip()
    canonical = _CANONICAL_MODELS.get(model.lower())
    if not canonical:
        supported = "、".join(sorted(SUPPORTED_MODELS))
        raise TudouProtocolError(400, f"Tudou 当前只适配以下视频模型：{supported}")
    return canonical


def _request_images(request: Mapping[str, Any]) -> List[Dict[str, Any]]:
    images = []
    for item in request.get("images") or []:
        if isinstance(item, Mapping):
            url = str(item.get("url") or "").strip()
            if url:
                images.append(dict(item))
    return images


def _reject_unsupported_media(request: Mapping[str, Any], model: str) -> None:
    if request.get("videos"):
        raise TudouProtocolError(400, f"{model} 的 Tudou 适配不支持参考视频")
    if request.get("audios"):
        raise TudouProtocolError(400, f"{model} 的 Tudou 适配不支持参考音频")


def _allowed_duration(value: Any, allowed: set[int], model: str, default: int) -> int:
    try:
        duration = int(value)
    except Exception as exc:
        raise TudouProtocolError(400, f"{model} 视频时长必须是整数") from exc
    if duration not in allowed:
        values = " / ".join(str(item) for item in sorted(allowed))
        raise TudouProtocolError(400, f"{model} 视频时长仅支持 {values} 秒")
    return duration or default


def _mime_for_path(path: str, resolver: ContentTypeForPath) -> str:
    mime = str(resolver(path) or "").split(";", 1)[0].strip().lower()
    return mime or mimetypes.guess_type(path)[0] or "image/png"


def _checked_image_file(filename: str, content: bytes, mime: str) -> Tuple[str, bytes, str]:
    mime = str(mime or "image/png").split(";", 1)[0].strip().lower()
    if not mime.startswith("image/"):
        raise TudouProtocolError(400, f"Tudou Grok 参考文件不是图片：{mime or '(unknown)'}")
    if not content:
        raise TudouProtocolError(400, "Tudou Grok 参考图片为空")
    if len(content) > _MAX_LOCAL_REFERENCE_BYTES:
        raise TudouProtocolError(400, "Tudou Grok 单张本地参考图片不能超过 30MB")
    return filename or "input_reference.png", content, mime


async def _reference_file(
    client: httpx.AsyncClient,
    value: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    fallback_values: Sequence[str] = (),
) -> Tuple[str, bytes, str]:
    text = str(value or "").strip()
    if not text:
        raise TudouProtocolError(400, "Tudou Grok 参考图片地址为空")
    if text.startswith("asset://"):
        raise TudouProtocolError(400, "Tudou Grok 不支持 asset:// 认证素材")
    if text.startswith("data:"):
        match = re.match(r"^data:([^;,]+);base64,(.+)$", text, re.IGNORECASE | re.DOTALL)
        if not match:
            raise TudouProtocolError(400, "Tudou Grok 参考图片 data URL 格式无效")
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise TudouProtocolError(400, "Tudou Grok 参考图片 base64 解码失败") from exc
        mime = match.group(1).lower()
        ext = (mime.split("/", 1)[-1] or "png").split("+", 1)[0]
        return _checked_image_file(f"input_reference.{ext}", content, mime)

    path = None
    for candidate in [*fallback_values, text]:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        try:
            path = resolve_local_path(candidate)
        except Exception as exc:
            raise TudouProtocolError(400, f"解析 Tudou Grok 本地参考图片失败：{exc}") from exc
        if path:
            break
    if path:
        try:
            size = os.path.getsize(path)
            if size > _MAX_LOCAL_REFERENCE_BYTES:
                raise TudouProtocolError(400, "Tudou Grok 单张本地参考图片不能超过 30MB")
            with open(path, "rb") as handle:
                content = handle.read()
        except TudouProtocolError:
            raise
        except Exception as exc:
            raise TudouProtocolError(400, f"读取 Tudou Grok 本地参考图片失败：{exc}") from exc
        return _checked_image_file(os.path.basename(path), content, _mime_for_path(path, content_type_for_path))

    parsed = urllib.parse.urlsplit(text)
    response = None
    last_error: Optional[httpx.HTTPError] = None
    for attempt in range(_REFERENCE_DOWNLOAD_ATTEMPTS):
        try:
            response = await public_http_get(text, timeout=120.0)
            response.raise_for_status()
            break
        except UnsafePublicUrlError as exc:
            raise TudouProtocolError(400, f"Tudou Grok 参考图片地址不安全：{exc}") from exc
        except httpx.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
            retryable = isinstance(exc, httpx.TransportError) or status == 429 or status >= 500
            if retryable and attempt + 1 < _REFERENCE_DOWNLOAD_ATTEMPTS:
                await asyncio.sleep(1.0 + attempt)
                continue
            error_status = 502 if retryable else 400
            raise TudouProtocolError(
                error_status,
                f"下载 Tudou Grok 参考图片失败（已尝试 {attempt + 1} 次）：{exc}",
            ) from exc
    if response is None:
        raise TudouProtocolError(502, f"下载 Tudou Grok 参考图片失败：{last_error or text}")
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_LOCAL_REFERENCE_BYTES:
        raise TudouProtocolError(400, "Tudou Grok 单张远程参考图片不能超过 30MB")
    mime = (response.headers.get("content-type") or "image/png").split(";", 1)[0]
    filename = os.path.basename(parsed.path) or "input_reference"
    return _checked_image_file(filename, response.content, mime)


def _check_sora_local_image_size(value: str, resolve_local_path: ResolveLocalPath) -> None:
    text = str(value or "").strip()
    if text.startswith("data:"):
        match = re.match(r"^data:[^;,]+;base64,(.+)$", text, re.IGNORECASE | re.DOTALL)
        if match:
            estimated = len(match.group(1).strip()) * 3 // 4
            if estimated > _MAX_SORA_IMAGE_BYTES:
                raise TudouProtocolError(400, "Tudou Sora2 单张参考图片不能超过 10MB")
        return
    try:
        path = resolve_local_path(text)
        if path and os.path.getsize(path) > _MAX_SORA_IMAGE_BYTES:
            raise TudouProtocolError(400, "Tudou Sora2 单张参考图片不能超过 10MB")
    except TudouProtocolError:
        raise
    except OSError as exc:
        raise TudouProtocolError(400, f"读取 Tudou Sora2 本地参考图片失败：{exc}") from exc


async def _build_grok_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> _Submission:
    _reject_unsupported_media(request, model)
    images = _request_images(request)
    if model == GROK_IMAGE_TO_VIDEO_MODEL and len(images) != 1:
        raise TudouProtocolError(400, f"Tudou {model} 是纯图生视频模型，必须且只能提供一张参考图片")
    if len(images) > _MAX_GROK_IMAGES:
        raise TudouProtocolError(400, f"Tudou Grok 最多支持 {_MAX_GROK_IMAGES} 张参考图片")
    duration = _allowed_duration(request.get("duration"), _GROK_DURATIONS, model, 6)
    ratio = str(request.get("aspect_ratio") or "9:16").strip()
    size = str(request.get("size") or "").strip().lower()
    if not size:
        size = _GROK_DEFAULT_SIZE_BY_RATIO.get(ratio, "")
    if size not in _GROK_SIZES:
        allowed = "、".join(sorted(_GROK_SIZES))
        raise TudouProtocolError(400, f"Tudou Grok 不支持当前尺寸或比例；可用尺寸：{allowed}")
    resolution = str(request.get("resolution") or "720p").strip().lower()
    if resolution not in _GROK_RESOLUTIONS:
        raise TudouProtocolError(400, "Tudou Grok 清晰度仅支持 480p 或 720p")
    body: Dict[str, Any] = {
        "model": model,
        "prompt": str(request.get("prompt") or ""),
        "seconds": str(duration),
        "size": size,
        "resolution_name": resolution,
    }
    preset = str(request.get("preset") or "").strip().lower()
    if preset:
        if preset not in _GROK_PRESETS:
            raise TudouProtocolError(400, "Tudou Grok 风格仅支持 fun / normal / spicy / custom")
        body["preset"] = preset
    files = []
    for image in images:
        local_fallbacks = (
            image.get("originalLocalUrl"),
            image.get("sourceUrl"),
            image.get("original_local_url"),
            image.get("source_url"),
        )
        files.append((
            "input_reference[]",
            await _reference_file(
                client,
                image["url"],
                resolve_local_path,
                content_type_for_path,
                local_fallbacks,
            ),
        ))
    return _Submission(model, "/v1/videos", "multipart", body, files)


async def _build_sora_submission(
    request: Mapping[str, Any],
    resolve_local_path: ResolveLocalPath,
    public_reference_url: PublicReferenceUrl,
) -> _Submission:
    _reject_unsupported_media(request, SORA_MODEL)
    images = _request_images(request)
    if len(images) > 1:
        raise TudouProtocolError(400, "Tudou Sora2 最多支持一张首帧参考图片")
    duration = _allowed_duration(request.get("duration"), _SORA_DURATIONS, SORA_MODEL, 8)
    ratio = str(request.get("aspect_ratio") or "16:9").strip()
    if ratio not in _SORA_RATIOS:
        raise TudouProtocolError(400, "Tudou Sora2 视频比例仅支持 16:9 或 9:16")
    body: Dict[str, Any] = {
        "model": SORA_MODEL,
        "prompt": str(request.get("prompt") or ""),
        "duration": duration,
        "aspect_ratio": ratio,
        "generate_audio": bool(request.get("generate_audio")),
    }
    negative_prompt = str(request.get("negative_prompt") or "").strip()
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
    if images:
        source = images[0]["url"]
        _check_sora_local_image_size(source, resolve_local_path)
        try:
            public_url = str(await public_reference_url(source) or "").strip()
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            raise TudouProtocolError(400, f"Tudou Sora2 参考图无法转换为公网 URL：{detail}") from exc
        parsed = urllib.parse.urlsplit(public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TudouProtocolError(400, "Tudou Sora2 参考图必须是公网 HTTP/HTTPS URL")
        if _PRIVATE_HOST_RE.match(parsed.hostname or "") or (parsed.hostname or "") == "::1":
            raise TudouProtocolError(400, "Tudou Sora2 参考图不能使用本机或内网地址")
        body["images"] = [public_url]
    return _Submission(SORA_MODEL, "/v1/videos/generations", "json", body, [])


async def _build_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    public_reference_url: PublicReferenceUrl,
) -> _Submission:
    model = canonical_model(request.get("model"))
    if model in GROK_MODELS:
        return await _build_grok_submission(client, request, model, resolve_local_path, content_type_for_path)
    return await _build_sora_submission(request, resolve_local_path, public_reference_url)


def _json_response(response: httpx.Response, action: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise TudouProtocolError(
            502,
            f"Tudou {action}返回非 JSON 响应（HTTP {response.status_code}）：{response.text[:500]}",
        ) from exc
    if not isinstance(payload, dict):
        raise TudouProtocolError(502, f"Tudou {action}返回非 JSON 对象：{payload}")
    return payload


def _task_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _task_id(payload: Mapping[str, Any]) -> str:
    for node in (payload, _task_data(payload)):
        for key in ("id", "task_id", "taskId", "video_id", "videoId"):
            value = str(node.get(key) or "").strip()
            if value:
                return value
    return ""


def _status(payload: Mapping[str, Any]) -> str:
    for node in (_task_data(payload), payload):
        for key in ("status", "task_status", "state"):
            value = str(node.get(key) or "").strip().upper()
            if value:
                if value in {"SUBMITTED", "QUEUED", "WAITING"}:
                    return "QUEUED"
                if value in {"PROCESSING", "IN_PROGRESS", "RUNNING", "GENERATING"}:
                    return "PROCESSING"
                return value
    return ""


def _collect_urls(value: Any, urls: List[str], key_hint: str = "") -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if key_hint in _URL_KEYS and text.startswith(("http://", "https://", "/", "v1/", "v2/")):
            urls.append(text)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_urls(item, urls, key_hint)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _URL_KEYS:
                _collect_urls(item, urls, key)
            elif key in _CONTAINER_KEYS:
                _collect_urls(item, urls, key)


def _video_urls(payload: Mapping[str, Any], base_url: str = "") -> List[str]:
    found: List[str] = []
    _collect_urls(payload, found)
    normalized = [resolve_video_download_url(value, base_url) for value in found]
    return list(dict.fromkeys(value for value in normalized if value))


def _failure_reason(payload: Mapping[str, Any]) -> str:
    values: List[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if value is None or depth > 8:
            return
        if isinstance(value, Mapping):
            for key in ("message", "msg", "reason", "failure_reason", "fail_reason", "error", "code"):
                item = value.get(key)
                if isinstance(item, str) and item.strip() and item.strip() not in values:
                    values.append(item.strip())
                elif isinstance(item, Mapping):
                    walk(item, depth + 1)
            for key in ("data", "detail", "result"):
                walk(value.get(key), depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item, depth + 1)

    walk(payload)
    return "；".join(values[:4]) or "任务失败，但上游未提供原因"


def _has_business_error(payload: Mapping[str, Any]) -> bool:
    for node in (payload, _task_data(payload)):
        error = node.get("error")
        if isinstance(error, Mapping) and error:
            return True
        if isinstance(error, str) and error.strip():
            return True
        code = str(node.get("code") or "").strip().lower()
        if code and code not in {"0", "200", "ok", "success"}:
            return True
    return False


def _business_error_code(payload: Mapping[str, Any]) -> str:
    for node in (payload, _task_data(payload)):
        error = node.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or "").strip().lower()
            if code:
                return code
    return ""


def _retry_after(response: httpx.Response) -> Optional[float]:
    value = str(response.headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except Exception:
        return None


def _report(progress: ProgressCallback, patch: Dict[str, Any]) -> None:
    if progress:
        progress(patch)


async def _save_result(
    payload: Mapping[str, Any],
    task_id: str,
    model: str,
    base_url: str,
    save_video: SaveVideo,
) -> Dict[str, Any]:
    urls = _video_urls(payload, base_url)
    if model in GROK_MODELS and not urls:
        quoted_id = urllib.parse.quote(str(task_id), safe="")
        urls = [_api_url(base_url, f"v1/videos/{quoted_id}/content")]
    if not urls:
        raise TudouProtocolError(502, f"Tudou {model} 任务已完成，但响应中没有视频地址")
    local_urls = []
    for url in urls:
        local_url = str(await save_video(url) or "").strip()
        if not local_url:
            raise TudouProtocolError(502, f"Tudou 视频下载失败：{url}")
        if urllib.parse.urlsplit(url).path.endswith("/content") and not local_url.startswith(("/output/", "/assets/")):
            raise TudouProtocolError(502, "Tudou Grok /content 视频下载失败，未能保存到本地输出目录")
        local_urls.append(local_url)
    return {"videos": local_urls, "task_id": task_id, "raw": dict(payload)}


def _poll_path(model: str, task_id: str) -> str:
    quoted_id = urllib.parse.quote(str(task_id), safe="")
    return f"/v1/tasks/{quoted_id}" if model == SORA_MODEL else f"/v1/videos/{quoted_id}"


async def _poll_video(
    client: httpx.AsyncClient,
    task_id: str,
    model: str,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
) -> Dict[str, Any]:
    status_url = _api_url(base_url, _poll_path(model, task_id))
    poll_interval = 8.0 if model == SORA_MODEL else 5.0
    deadline = time.monotonic() + max(1.0, float(poll_timeout))
    delay = 15.0
    errors = 0
    last_payload: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        try:
            response = await client.get(status_url, headers=dict(headers))
        except httpx.TransportError as exc:
            errors += 1
            if errors >= _MAX_POLL_ERRORS:
                raise TudouProtocolError(502, f"Tudou 视频状态连续查询失败：{exc}") from exc
            delay = poll_interval
            _report(progress, {
                "status": "polling",
                "message": f"Tudou 状态查询暂时失败，将自动重试：{exc}",
                "next_poll_at": time.time() + delay,
            })
            continue
        retry_after = _retry_after(response)
        if response.status_code == 429 or response.status_code >= 500:
            errors += 1
            if errors >= _MAX_POLL_ERRORS:
                raise TudouProtocolError(
                    response.status_code,
                    f"Tudou 视频状态连续查询失败：{response.text[:500]}",
                )
            delay = max(poll_interval, retry_after or 0.0)
            _report(progress, {
                "status": "polling",
                "retry_after": retry_after,
                "next_poll_at": time.time() + delay,
            })
            continue
        if response.status_code >= 400:
            raise TudouProtocolError(response.status_code, f"Tudou 视频状态查询失败：{response.text[:500]}")
        raw = _json_response(response, "状态查询")
        if model in GROK_MODELS and _business_error_code(raw) == "not_found":
            generic_url = _api_url(base_url, f"v1/tasks/{urllib.parse.quote(str(task_id), safe='')}")
            try:
                generic_response = await client.get(generic_url, headers=dict(headers))
                if generic_response.status_code < 400:
                    generic_raw = _json_response(generic_response, "通用任务状态查询")
                    if _status(generic_raw) or _has_business_error(generic_raw):
                        raw = generic_raw
            except httpx.TransportError:
                pass
        last_payload = raw
        errors = 0
        delay = poll_interval
        state = _status(raw)
        _report(progress, {
            "status": "polling",
            "raw_last": raw,
            "next_poll_at": time.time() + delay,
        })
        if state in _FAILURE_STATUSES or _has_business_error(raw):
            raise TudouProtocolError(502, f"Tudou 视频任务失败：{_failure_reason(raw)}")
        urls = _video_urls(raw, base_url)
        if state in _SUCCESS_STATUSES or (not state and urls):
            return await _save_result(raw, task_id, model, base_url, save_video)
    raise TudouProtocolError(504, f"Tudou 视频任务超时：{last_payload or task_id}")


async def generate_tudou_video(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    public_reference_url: PublicReferenceUrl,
    save_video: SaveVideo,
    poll_timeout: float,
) -> Dict[str, Any]:
    """提交 Tudou Grok/Sora2 视频任务并等待完成。"""
    root = _api_root(base_url)
    if not root:
        raise TudouProtocolError(400, "Tudou 未配置 Base URL")
    submission = await _build_submission(
        client, request, resolve_local_path, content_type_for_path, public_reference_url
    )
    submit_url = _api_url(root, submission.submit_path)
    try:
        if submission.request_format == "json":
            response = await client.post(submit_url, headers=dict(headers), json=submission.body)
        else:
            multipart = [(key, (None, str(value))) for key, value in submission.body.items()]
            multipart.extend(submission.files)
            response = await client.post(submit_url, headers=dict(headers), files=multipart)
    except httpx.TransportError as exc:
        raise TudouProtocolError(
            502,
            f"Tudou 创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        raise TudouProtocolError(response.status_code, f"Tudou 视频创建失败：{response.text[:500]}")
    raw = _json_response(response, "创建任务")
    state = _status(raw)
    if state in _FAILURE_STATUSES or _has_business_error(raw):
        raise TudouProtocolError(502, f"Tudou 视频任务失败：{_failure_reason(raw)}")
    task_id = _task_id(raw)
    urls = _video_urls(raw, root)
    if urls:
        return await _save_result(raw, task_id, submission.model, root, save_video)
    if not task_id:
        raise TudouProtocolError(502, f"Tudou 创建响应没有任务 ID，已停止处理以避免重复扣费：{raw}")
    _report(progress, {
        "status": "polling",
        "upstream_task_id": task_id,
        "task_id": task_id,
        "submit_url": submit_url,
        "raw_submit": raw,
    })
    return await _poll_video(
        client, task_id, submission.model, root, headers, progress, save_video, poll_timeout
    )


async def resume_tudou_video(
    client: httpx.AsyncClient,
    task_id: str,
    model: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
) -> Dict[str, Any]:
    """只恢复查询已有 Tudou 任务，绝不重新提交创建请求。"""
    root = _api_root(base_url)
    if not root:
        raise TudouProtocolError(400, "Tudou 未配置 Base URL")
    canonical = canonical_model(model)
    return await _poll_video(
        client, str(task_id), canonical, root, headers, progress, save_video, poll_timeout
    )
