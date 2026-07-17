"""GeekNow 可灵与 Grok 视频协议适配。

本模块只负责 GeekNow 上游协议，不依赖 Infinite Canvas 的全局状态。
宿主通过回调提供本地路径解析、可灵参考图公网化、任务进度记录和视频落盘。
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
    submit_video_http_request,
)


GEEKNOW_VIDEO_REQUEST_MODE = "geeknow-v1-videos"
GEEKNOW_OFFICIAL_HOSTNAMES = {"geeknow.ai", "api.geeknow.ai"}

KLING_MODELS = {"Kling-3.0", "Kling-3.0-Omni"}
GROK_STANDARD_MODELS = {
    "grok-video-1.5",
    "grok-video-1.5-pro",
    "grok-video-1.5-max",
    "grok-video-3",
    "grok-video-3-pro",
    "grok-video-3-max",
}
GROK_IMAGINE_MODELS = {
    "grok-imagine-video",
    "grok-imagine-video-1.5-preview",
}
SUPPORTED_MODELS = KLING_MODELS | GROK_STANDARD_MODELS | GROK_IMAGINE_MODELS

_CANONICAL_MODELS = {name.lower(): name for name in SUPPORTED_MODELS}
_GROK_DURATION_MODEL_MAP = {
    "grok-video-1.5": {10: "grok-video-1.5-pro", 15: "grok-video-1.5-max"},
    "grok-video-3": {10: "grok-video-3-pro", 15: "grok-video-3-max"},
}
_GROK_FIXED_DURATIONS = {
    "grok-video-1.5-pro": 10,
    "grok-video-1.5-max": 15,
    "grok-video-3-pro": 10,
    "grok-video-3-max": 15,
}
_GROK_ONE_IMAGE_MODELS = {
    "grok-video-1.5",
    "grok-video-1.5-pro",
    "grok-video-1.5-max",
    "grok-imagine-video-1.5-preview",
}
_GROK_STANDARD_RATIOS = {"2:3", "3:2", "1:1"}
_GROK_IMAGINE_RESOLUTIONS = {"480P", "720P"}
_GROK_STANDARD_RESOLUTIONS = {"480P", "540P", "720P", "1080P"}
_RATIO_RE = re.compile(r"^\d+(?:\.\d+)?:\d+(?:\.\d+)?$")
_MAX_REFERENCE_BYTES = 30 * 1024 * 1024
_MAX_POLL_ERRORS = 5

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
    "result_url", "resultUrl", "content_url", "contentUrl", "local_url", "localUrl",
    "download_url", "downloadUrl", "FileUrl", "file_url", "src", "uri", "path",
    "output", "result", "content", "video",
}
_CONTAINER_KEYS = {
    "videos", "outputs", "data", "detail", "result", "results", "creations", "content",
    "metadata", "output", "AigcVideoTask", "aigc_video_task", "FileInfos", "file_infos",
}


class GeekNowProtocolError(Exception):
    """带 HTTP 状态语义的 GeekNow 协议错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


@dataclass
class _Submission:
    request_format: str
    body: Dict[str, Any]
    files: List[Tuple[str, Tuple[str, bytes, str]]]


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
ResolveLocalPath = Callable[[str], Optional[str]]
ContentTypeForPath = Callable[[str], str]
PublicReferenceUrl = Callable[[str], Awaitable[str]]
SaveVideo = Callable[[str], Awaitable[str]]


def is_geeknow_official_provider(provider: Optional[Mapping[str, Any]]) -> bool:
    base_url = str((provider or {}).get("base_url") or "").strip()
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "").lower() in GEEKNOW_OFFICIAL_HOSTNAMES
    except Exception:
        return False


def _provider_root(base_url: Any) -> str:
    return canonical_video_api_root(base_url)


def canonical_model(value: Any) -> str:
    model = str(value or "").strip()
    canonical = _CANONICAL_MODELS.get(model.lower())
    if not canonical:
        supported = "、".join(sorted(SUPPORTED_MODELS))
        raise GeekNowProtocolError(400, f"GeekNow 当前只适配可灵和 Grok 模型：{supported}")
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
        raise GeekNowProtocolError(400, f"{model} 的 GeekNow 适配暂不支持参考视频")
    if request.get("audios"):
        raise GeekNowProtocolError(400, f"{model} 的 GeekNow 适配暂不支持参考音频")


def _positive_duration(value: Any, default: int = 5) -> int:
    try:
        duration = int(value)
    except Exception:
        duration = default
    return max(1, duration)


def _numeric_ratio(value: Any, default: str = "16:9") -> str:
    ratio = str(value or default).strip()
    if not _RATIO_RE.fullmatch(ratio):
        raise GeekNowProtocolError(400, f"GeekNow 视频比例必须是数字比例，例如 16:9；当前为：{ratio or '(empty)'}")
    return ratio


def _standard_grok_model_and_duration(model: str, value: Any) -> Tuple[str, int]:
    duration = _positive_duration(value, 15)
    if model in _GROK_FIXED_DURATIONS:
        fixed = _GROK_FIXED_DURATIONS[model]
        if duration != fixed:
            raise GeekNowProtocolError(400, f"{model} 固定为 {fixed} 秒，请把视频时长改为 {fixed} 秒")
        return model, fixed
    if duration not in {10, 15}:
        raise GeekNowProtocolError(400, "GeekNow Grok 普通视频只支持 10 秒或 15 秒")
    return _GROK_DURATION_MODEL_MAP.get(model, {}).get(duration, model), duration


def _standard_grok_resolution(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in _GROK_STANDARD_RESOLUTIONS else "1080P"


def _imagine_resolution(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in _GROK_IMAGINE_RESOLUTIONS else "720P"


def _mime_for_path(path: str, resolver: ContentTypeForPath) -> str:
    mime = str(resolver(path) or "").split(";", 1)[0].strip().lower()
    if not mime:
        mime = mimetypes.guess_type(path)[0] or "image/png"
    return mime


def _checked_image_file(filename: str, content: bytes, mime: str) -> Tuple[str, bytes, str]:
    mime = str(mime or "image/png").split(";", 1)[0].strip().lower()
    if not mime.startswith("image/"):
        raise GeekNowProtocolError(400, f"Grok 参考文件不是图片：{mime or '(unknown)'}")
    if not content:
        raise GeekNowProtocolError(400, "Grok 参考图片为空")
    if len(content) > _MAX_REFERENCE_BYTES:
        raise GeekNowProtocolError(400, "Grok 单张参考图片不能超过 30MB")
    return filename or "input_reference.png", content, mime


async def _reference_file(
    client: httpx.AsyncClient,
    value: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Tuple[str, bytes, str]:
    text = str(value or "").strip()
    if not text:
        raise GeekNowProtocolError(400, "Grok 参考图片地址为空")
    if text.startswith("asset://"):
        raise GeekNowProtocolError(400, "GeekNow Grok 不支持 asset:// 认证素材")
    if text.startswith("data:"):
        match = re.match(r"^data:([^;,]+);base64,(.+)$", text, re.IGNORECASE | re.DOTALL)
        if not match:
            raise GeekNowProtocolError(400, "Grok 参考图片 data URL 格式无效")
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise GeekNowProtocolError(400, "Grok 参考图片 base64 解码失败") from exc
        mime = match.group(1).lower()
        ext = (mime.split("/", 1)[-1] or "png").split("+", 1)[0]
        return _checked_image_file(f"input_reference.{ext}", content, mime)

    path = resolve_local_path(text)
    if path:
        try:
            size = os.path.getsize(path)
            if size > _MAX_REFERENCE_BYTES:
                raise GeekNowProtocolError(400, "Grok 单张参考图片不能超过 30MB")
            with open(path, "rb") as handle:
                content = handle.read()
        except GeekNowProtocolError:
            raise
        except Exception as exc:
            raise GeekNowProtocolError(400, f"读取 Grok 本地参考图片失败：{exc}") from exc
        return _checked_image_file(os.path.basename(path), content, _mime_for_path(path, content_type_for_path))

    parsed = urllib.parse.urlsplit(text)
    try:
        response = await public_http_get(text)
        response.raise_for_status()
    except UnsafePublicUrlError as exc:
        raise GeekNowProtocolError(400, f"Grok 参考图片地址不安全：{exc}") from exc
    except httpx.HTTPError as exc:
        raise GeekNowProtocolError(400, f"下载 Grok 参考图片失败：{exc}") from exc
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_REFERENCE_BYTES:
        raise GeekNowProtocolError(400, "Grok 单张参考图片不能超过 30MB")
    mime = (response.headers.get("content-type") or "image/png").split(";", 1)[0]
    filename = os.path.basename(parsed.path) or "input_reference"
    return _checked_image_file(filename, response.content, mime)


def _file_data_url(file_tuple: Tuple[str, bytes, str]) -> str:
    _, content, mime = file_tuple
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def _build_kling_submission(
    request: Mapping[str, Any],
    model: str,
    public_reference_url: PublicReferenceUrl,
) -> _Submission:
    _reject_unsupported_media(request, model)
    images = _request_images(request)
    if len(images) > 1:
        raise GeekNowProtocolError(400, f"{model} 当前只支持一张参考图片")
    duration = _positive_duration(request.get("duration"), 5)
    ratio = _numeric_ratio(request.get("aspect_ratio"), "16:9")
    body: Dict[str, Any] = {
        "model": model,
        "prompt": str(request.get("prompt") or ""),
        "seconds": str(duration),
        "metadata": {
            "output_config": {
                "aspect_ratio": ratio,
                "audio_generation": "Enabled" if request.get("generate_audio") else "Disabled",
                "duration": duration,
            }
        },
    }
    if images:
        try:
            image_url = str(await public_reference_url(images[0]["url"]) or "").strip()
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            raise GeekNowProtocolError(400, f"可灵参考图无法转换为公网 URL：{detail}") from exc
        parsed = urllib.parse.urlsplit(image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GeekNowProtocolError(400, "可灵参考图必须是公网 HTTP/HTTPS URL")
        body["image"] = image_url
    return _Submission("json", body, [])


async def _build_standard_grok_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> _Submission:
    _reject_unsupported_media(request, model)
    images = _request_images(request)
    if model in _GROK_ONE_IMAGE_MODELS and len(images) > 1:
        raise GeekNowProtocolError(400, "Grok 1.5 系列最多支持一张参考图片")
    actual_model, duration = _standard_grok_model_and_duration(model, request.get("duration"))
    ratio = str(request.get("aspect_ratio") or "1:1").strip()
    if ratio not in _GROK_STANDARD_RATIOS:
        raise GeekNowProtocolError(400, "GeekNow Grok 视频比例仅支持 2:3、3:2、1:1")
    body = {
        "model": actual_model,
        "prompt": str(request.get("prompt") or ""),
        "seconds": str(duration),
        "aspect_ratio": ratio,
        "size": _standard_grok_resolution(request.get("resolution") or request.get("size")),
    }
    files = []
    for image in images:
        files.append((
            "input_reference",
            await _reference_file(client, image["url"], resolve_local_path, content_type_for_path),
        ))
    return _Submission("multipart", body, files)


async def _build_imagine_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> _Submission:
    _reject_unsupported_media(request, model)
    prompt = str(request.get("prompt") or "")
    if len(prompt) > 4096:
        raise GeekNowProtocolError(400, f"Grok Imagine 提示词最多 4096 字符，当前为 {len(prompt)} 字符")
    images = _request_images(request)
    if model in _GROK_ONE_IMAGE_MODELS and len(images) > 1:
        raise GeekNowProtocolError(400, "Grok Imagine 1.5 Preview 最多支持一张参考图片")
    ratio = _numeric_ratio(request.get("aspect_ratio"), "16:9")
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "seconds": str(_positive_duration(request.get("duration"), 5)),
        "aspect_ratio": ratio,
        "resolution": _imagine_resolution(request.get("resolution") or request.get("size")),
    }
    data_urls = []
    for image in images:
        ref_file = await _reference_file(client, image["url"], resolve_local_path, content_type_for_path)
        data_urls.append(_file_data_url(ref_file))
    if len(data_urls) == 1:
        body["image"] = data_urls[0]
    elif data_urls:
        body["images"] = data_urls
    return _Submission("json", body, [])


async def _build_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    public_reference_url: PublicReferenceUrl,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> _Submission:
    model = canonical_model(request.get("model"))
    if model in KLING_MODELS:
        return await _build_kling_submission(request, model, public_reference_url)
    if model in GROK_STANDARD_MODELS:
        return await _build_standard_grok_submission(
            client, request, model, resolve_local_path, content_type_for_path
        )
    return await _build_imagine_submission(
        client, request, model, resolve_local_path, content_type_for_path
    )


def _json_response(response: httpx.Response, action: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise GeekNowProtocolError(
            502,
            f"GeekNow {action}返回非 JSON 响应（HTTP {response.status_code}）：{response.text[:500]}",
        ) from exc
    if not isinstance(payload, dict):
        raise GeekNowProtocolError(502, f"GeekNow {action}返回非 JSON 对象：{payload}")
    return payload


def _status(payload: Mapping[str, Any]) -> str:
    nodes: List[Mapping[str, Any]] = [payload]
    for key in ("data", "detail", "AigcVideoTask", "aigc_video_task"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            nodes.append(value)
    detail = payload.get("detail")
    if isinstance(detail, Mapping):
        for key in ("AigcVideoTask", "aigc_video_task"):
            value = detail.get(key)
            if isinstance(value, Mapping):
                nodes.append(value)
    for node in nodes:
        for key in ("status", "Status", "task_status", "state"):
            value = str(node.get(key) or "").strip().upper()
            if value:
                if value == "WAITING":
                    return "QUEUED"
                if value in {"PROCESSING", "IN_PROGRESS", "RUNNING", "GENERATING"}:
                    return "PROCESSING"
                if value in {"FINISH", "FINISHED"}:
                    return "FINISHED"
                if value == "ABORTED":
                    return "ABORTED"
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


def _video_urls(payload: Mapping[str, Any], base_url: str) -> List[str]:
    found: List[str] = []
    _collect_urls(payload, found)
    normalized = []
    for value in found:
        url = resolve_video_download_url(value, base_url)
        if url not in normalized:
            normalized.append(url)
    return [value for value in normalized if value]


def _failure_reason(payload: Mapping[str, Any]) -> str:
    values: List[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if value is None or depth > 8:
            return
        if isinstance(value, Mapping):
            for key in (
                "failure_reason", "fail_reason", "message", "Message", "msg", "reason",
                "error", "ErrCode", "ErrCodeExt", "code", "err_code",
            ):
                item = value.get(key)
                if isinstance(item, str) and item.strip() and item.strip() not in values:
                    values.append(item.strip())
                elif isinstance(item, Mapping):
                    walk(item, depth + 1)
            for key in ("data", "detail", "AigcVideoTask", "aigc_video_task"):
                walk(value.get(key), depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item, depth + 1)

    walk(payload)
    return "；".join(values[:4]) or "任务失败，但上游未提供原因"


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
    base_url: str,
    save_video: SaveVideo,
) -> Dict[str, Any]:
    urls = _video_urls(payload, base_url)
    if not urls:
        quoted_id = urllib.parse.quote(str(task_id), safe="")
        urls = [f"{_provider_root(base_url)}/v1/videos/{quoted_id}/content"]
    local_urls = []
    for url in urls:
        local_url = str(await save_video(url) or "").strip()
        if not local_url:
            raise GeekNowProtocolError(502, f"GeekNow 视频下载失败：{url}")
        if urllib.parse.urlsplit(url).path.endswith("/content") and not local_url.startswith(("/output/", "/assets/")):
            raise GeekNowProtocolError(502, "GeekNow /content 视频下载失败，未能保存到本地输出目录")
        local_urls.append(local_url)
    return {"videos": local_urls, "task_id": task_id, "raw": dict(payload)}


async def _poll_video(
    client: httpx.AsyncClient,
    task_id: str,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float,
) -> Dict[str, Any]:
    quoted_id = urllib.parse.quote(str(task_id), safe="")
    status_url = f"{_provider_root(base_url)}/v1/videos/{quoted_id}"
    deadline = time.monotonic() + max(1.0, float(poll_timeout))
    delay = max(0.0, float(poll_interval))
    errors = 0
    last_payload: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        if delay:
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        try:
            response = await client.get(status_url, headers=dict(headers))
        except httpx.TransportError as exc:
            errors += 1
            if errors >= _MAX_POLL_ERRORS:
                raise GeekNowProtocolError(502, f"GeekNow 视频状态连续查询失败：{exc}") from exc
            _report(progress, {
                "status": "polling",
                "message": f"GeekNow 状态查询暂时失败，将自动重试：{exc}",
                "next_poll_at": time.time() + delay,
            })
            continue
        retry_after = _retry_after(response)
        if response.status_code == 429 or response.status_code >= 500:
            errors += 1
            if response.status_code >= 500 and errors >= _MAX_POLL_ERRORS:
                raise GeekNowProtocolError(
                    response.status_code,
                    f"GeekNow 视频状态连续查询失败：{response.text[:500]}",
                )
            delay = max(float(poll_interval), retry_after or 0.0)
            _report(progress, {
                "status": "polling",
                "retry_after": retry_after,
                "next_poll_at": time.time() + delay,
            })
            continue
        if response.status_code >= 400:
            body = response.text[:500]
            raise GeekNowProtocolError(response.status_code, f"GeekNow 视频状态查询失败：{body}")
        raw = _json_response(response, "状态查询")
        last_payload = raw
        errors = 0
        delay = max(0.0, float(poll_interval))
        state = _status(raw)
        _report(progress, {
            "status": "polling",
            "raw_last": raw,
            "next_poll_at": time.time() + delay,
        })
        if state in _FAILURE_STATUSES:
            raise GeekNowProtocolError(502, f"GeekNow 视频任务失败：{_failure_reason(raw)}")
        urls = _video_urls(raw, base_url)
        if state in _SUCCESS_STATUSES or (not state and urls):
            return await _save_result(raw, task_id, base_url, save_video)
    raise GeekNowProtocolError(504, f"GeekNow 视频任务超时：{last_payload or task_id}")


async def generate_geeknow_video(
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
    poll_interval: float = 10.0,
) -> Dict[str, Any]:
    """提交 GeekNow 可灵/Grok 视频任务并等待完成。"""
    root = _provider_root(base_url)
    if not root:
        raise GeekNowProtocolError(400, "GeekNow 未配置 Base URL")
    submission = await _build_submission(
        client, request, public_reference_url, resolve_local_path, content_type_for_path
    )
    submit_url = f"{root}/v1/videos"
    try:
        if submission.request_format == "json":
            response = await submit_video_http_request(
                client, progress=progress, url=submit_url, headers=dict(headers),
                json_body=submission.body,
                context={"protocol": "geeknow", "model": request.get("model")},
            )
        else:
            multipart = [(key, (None, str(value))) for key, value in submission.body.items()]
            multipart.extend(submission.files)
            response = await submit_video_http_request(
                client, progress=progress, url=submit_url, headers=dict(headers),
                files=multipart,
                context={"protocol": "geeknow", "model": request.get("model")},
            )
    except httpx.TransportError as exc:
        raise GeekNowProtocolError(
            502,
            f"GeekNow 创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        raise GeekNowProtocolError(response.status_code, f"GeekNow 视频创建失败：{response.text[:500]}")
    raw = _json_response(response, "创建任务")
    state = _status(raw)
    if state in _FAILURE_STATUSES:
        raise GeekNowProtocolError(502, f"GeekNow 视频任务失败：{_failure_reason(raw)}")
    task_id = str(raw.get("id") or raw.get("task_id") or "").strip()
    urls = _video_urls(raw, root)
    if urls:
        return await _save_result(raw, task_id, root, save_video)
    if not task_id:
        raise GeekNowProtocolError(502, f"GeekNow 创建响应没有 id 或 task_id，已停止处理以避免重复扣费：{raw}")
    _report(progress, {
        "status": "polling",
        "upstream_task_id": task_id,
        "task_id": task_id,
        "submit_url": submit_url,
        "raw_submit": raw,
    })
    return await _poll_video(
        client, task_id, root, headers, progress, save_video, poll_timeout, poll_interval
    )


async def resume_geeknow_video(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = 10.0,
) -> Dict[str, Any]:
    """只恢复查询已有 GeekNow 任务，绝不重新提交创建请求。"""
    root = _provider_root(base_url)
    if not root:
        raise GeekNowProtocolError(400, "GeekNow 未配置 Base URL")
    return await _poll_video(
        client, str(task_id), root, headers, progress, save_video, poll_timeout, poll_interval
    )
