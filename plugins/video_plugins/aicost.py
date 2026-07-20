"""aicost.xyz 视频模型协议适配。

本模块封装 aicost 的 Seedance、Grok 1.5 Preview 与通用视频协议。
宿主只负责提供素材路径解析、进度持久化和视频落盘回调。
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import mimetypes
import os
import re
import time
import urllib.parse
import uuid
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


AICOST_VIDEO_REQUEST_MODE = "aicost-video"
AICOST_OFFICIAL_HOSTNAMES = {"aicost.xyz", "www.aicost.xyz"}

_GROK_SIZE_BY_RATIO = {
    "16:9": "1280x720",
    "9:16": "720x1280",
    "1:1": "1024x1024",
    "2:3": "1024x1792",
    "3:2": "1792x1024",
}
_SUCCESS_STATUSES = {"SUCCESS", "SUCCEED", "SUCCEEDED", "COMPLETED", "COMPLETE", "DONE", "FINISHED", "OK", "READY"}
_FAILURE_STATUSES = {"FAILURE", "FAILED", "FAIL", "ERROR", "ERRORED", "ABORTED", "DELETED", "CANCELED", "CANCELLED", "TIMEOUT", "REJECTED", "EXPIRED"}
_URL_KEYS = {"url", "video_url", "videoUrl", "result_url", "resultUrl", "output_url", "outputUrl", "content_url", "contentUrl", "download_url", "downloadUrl"}
_CONTAINER_KEYS = {"data", "detail", "result", "results", "output", "outputs", "videos", "choices", "message", "delta", "content", "events"}
_MAX_REFERENCE_BYTES = 30 * 1024 * 1024
_MAX_POLL_ERRORS = 5
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


class AICostProtocolError(Exception):
    """带 HTTP 状态语义的 aicost 协议错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


@dataclass
class _Submission:
    endpoint: str
    body: Dict[str, Any]
    request_id: str = ""


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
ResolveLocalPath = Callable[[str], Optional[str]]
ContentTypeForPath = Callable[[str], str]
PublicReferenceUrl = Callable[[str], Awaitable[str]]
SaveVideo = Callable[[str], Awaitable[str]]


def _provider_root(base_url: Any) -> str:
    return canonical_video_api_root(base_url)


def is_aicost_official_provider(provider: Mapping[str, Any] | None) -> bool:
    try:
        host = (urllib.parse.urlsplit(str((provider or {}).get("base_url") or "").strip()).hostname or "").lower()
    except Exception:
        return False
    return host in AICOST_OFFICIAL_HOSTNAMES


def canonical_model(value: Any) -> str:
    model = str(value or "").strip()
    if not model:
        raise AICostProtocolError(400, "aicost 视频模型不能为空")
    return model


def _model_family(model: Any) -> str:
    value = str(model or "").strip().lower()
    if "grok" in value:
        return "grok"
    if "seedance" in value:
        return "seedance"
    return "generic"


def _request_images(request: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for item in request.get("images") or []:
        if isinstance(item, Mapping) and str(item.get("url") or "").strip():
            result.append(dict(item))
    return result


def _string_list(values: Any) -> List[str]:
    return [str(value).strip() for value in (values or []) if str(value or "").strip()]


def _duration(value: Any, allowed: Sequence[int], model: str) -> int:
    try:
        result = int(value)
    except Exception as exc:
        raise AICostProtocolError(400, f"{model} 视频时长无效") from exc
    if result not in set(allowed):
        values = "、".join(str(item) for item in allowed)
        raise AICostProtocolError(400, f"{model} 视频时长仅支持 {values} 秒")
    return result


def _aspect_ratio(value: Any, allowed: Sequence[str], model: str, default: str = "16:9") -> str:
    ratio = str(value or default).strip() or default
    if ratio not in set(allowed):
        raise AICostProtocolError(400, f"{model} 视频比例仅支持：{'、'.join(allowed)}")
    return ratio


def _mime_for_path(path: str, resolver: ContentTypeForPath) -> str:
    return str(resolver(path) or "").split(";", 1)[0].strip().lower() or mimetypes.guess_type(path)[0] or "application/octet-stream"


def _checked_data_url(content: bytes, mime: str, kind: str) -> str:
    if not content:
        raise AICostProtocolError(400, f"aicost {kind}素材为空")
    if len(content) > _MAX_REFERENCE_BYTES:
        raise AICostProtocolError(400, f"aicost 单个{kind}素材不能超过 30MB")
    expected = {"图片": "image/", "音频": "audio/", "视频": "video/"}[kind]
    if not str(mime or "").lower().startswith(expected):
        raise AICostProtocolError(400, f"aicost {kind}素材类型无效：{mime or '(unknown)'}")
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


async def _media_data_url(
    client: httpx.AsyncClient,
    value: str,
    kind: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> str:
    text = str(value or "").strip()
    if text.startswith("data:"):
        match = re.match(r"^data:([^;,]+);base64,(.+)$", text, re.IGNORECASE | re.DOTALL)
        if not match:
            raise AICostProtocolError(400, f"aicost {kind} data URL 格式无效")
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise AICostProtocolError(400, f"aicost {kind} base64 解码失败") from exc
        return _checked_data_url(content, match.group(1).lower(), kind)

    path = resolve_local_path(text)
    if path:
        try:
            if os.path.getsize(path) > _MAX_REFERENCE_BYTES:
                raise AICostProtocolError(400, f"aicost 单个{kind}素材不能超过 30MB")
            with open(path, "rb") as handle:
                content = handle.read()
        except AICostProtocolError:
            raise
        except Exception as exc:
            raise AICostProtocolError(400, f"读取 aicost 本地{kind}素材失败：{exc}") from exc
        return _checked_data_url(content, _mime_for_path(path, content_type_for_path), kind)

    parsed = urllib.parse.urlsplit(text)
    try:
        response = await public_http_get(text)
        response.raise_for_status()
    except UnsafePublicUrlError as exc:
        raise AICostProtocolError(400, f"aicost {kind}素材地址不安全：{exc}") from exc
    except httpx.HTTPError as exc:
        raise AICostProtocolError(400, f"下载 aicost {kind}素材失败：{exc}") from exc
    content_length = str(response.headers.get("content-length") or "")
    if content_length.isdigit() and int(content_length) > _MAX_REFERENCE_BYTES:
        raise AICostProtocolError(400, f"aicost 单个{kind}素材不能超过 30MB")
    mime = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
    if mime in {"", "application/octet-stream"}:
        mime = mimetypes.guess_type(parsed.path)[0] or mime
    return _checked_data_url(response.content, mime, kind)


def _is_public_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost", "0.0.0.0"} or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified)


async def _chat_content(
    client: httpx.AsyncClient,
    images: Sequence[Mapping[str, Any]],
    prompt: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    content: List[Dict[str, Any]] = []
    data_urls: List[str] = []
    for image in images:
        data_url = await _media_data_url(client, str(image.get("url") or ""), "图片", resolve_local_path, content_type_for_path)
        data_urls.append(data_url)
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    content.append({"type": "text", "text": prompt})
    return content, data_urls


async def _build_seedance_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    public_reference_url: PublicReferenceUrl,
) -> _Submission:
    images = _request_images(request)
    videos = _string_list(request.get("videos"))
    audios = _string_list(request.get("audios"))
    duration = _duration(request.get("duration"), tuple(range(4, 16)), model)
    resolution = str(request.get("resolution") or "720p").strip().lower()
    max_images = 9
    max_audios = 3
    max_videos = 3
    if len(images) > max_images or len(audios) > max_audios or len(videos) > max_videos:
        raise AICostProtocolError(400, f"{model} 素材数量超限：最多 {max_images} 张图片、{max_audios} 个音频、{max_videos} 个视频")
    body: Dict[str, Any] = {
        "model": model,
        "prompt": str(request.get("prompt") or ""),
        "duration": duration,
        "aspect_ratio": str(request.get("aspect_ratio") or "16:9").strip() or "16:9",
        "resolution": resolution,
    }
    image_urls: List[str] = []
    image_data: List[str] = []
    for image in images:
        source = str(image.get("url") or "").strip()
        if _is_public_url(source):
            image_urls.append(source)
        else:
            image_data.append(await _media_data_url(client, source, "图片", resolve_local_path, content_type_for_path))
    audio_urls: List[str] = []
    audio_data: List[str] = []
    for source in audios:
        if _is_public_url(source):
            audio_urls.append(source)
        else:
            audio_data.append(await _media_data_url(client, source, "音频", resolve_local_path, content_type_for_path))
    video_urls: List[str] = []
    for source in videos:
        value = source if _is_public_url(source) else str(await public_reference_url(source) or "").strip()
        if not _is_public_url(value):
            raise AICostProtocolError(400, "Seedance 参考视频必须能转换为公网 HTTP/HTTPS URL")
        video_urls.append(value)
    for key, values in (
        ("image_urls", image_urls),
        ("images_base64", image_data),
        ("audio_urls", audio_urls),
        ("audios_base64", audio_data),
        ("video_urls", video_urls),
    ):
        if values:
            body[key] = values
    return _Submission("/v1/videos", body)


async def _build_grok_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> _Submission:
    images = _request_images(request)
    if request.get("videos") or request.get("audios"):
        raise AICostProtocolError(400, f"{model} 不支持参考视频或音频")
    if len(images) != 1:
        raise AICostProtocolError(400, f"{model} 仅支持并且必须提供一张首帧图片")
    duration = _duration(request.get("duration"), (6, 10, 15), model)
    ratio = _aspect_ratio(request.get("aspect_ratio"), tuple(_GROK_SIZE_BY_RATIO), model)
    resolution = str(request.get("resolution") or "720p").strip().lower() or "720p"
    _, data_urls = await _chat_content(client, images, str(request.get("prompt") or ""), resolve_local_path, content_type_for_path)
    request_id = uuid.uuid4().hex
    return _Submission(
        "/v1/videos",
        {
            "model": model,
            "prompt": str(request.get("prompt") or ""),
            "input_reference": data_urls[0],
            "seconds": str(duration),
            "size": _GROK_SIZE_BY_RATIO[ratio],
            "resolution": resolution,
            "resolution_name": resolution,
        },
        request_id=request_id,
    )


async def _build_submission(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    public_reference_url: PublicReferenceUrl,
) -> Tuple[str, _Submission]:
    model = canonical_model(request.get("model"))
    family = _model_family(model)
    if family == "grok":
        return model, await _build_grok_submission(client, request, model, resolve_local_path, content_type_for_path)
    return model, await _build_seedance_submission(client, request, model, resolve_local_path, content_type_for_path, public_reference_url)


def _json_response(response: httpx.Response, action: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise AICostProtocolError(response.status_code if response.status_code >= 400 else 502, f"aicost {action}返回非 JSON 响应：{response.text[:500]}") from exc
    if not isinstance(payload, dict):
        raise AICostProtocolError(502, f"aicost {action}返回非 JSON 对象：{payload}")
    return payload


def _identifier_value(value: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, (str, int)) and str(item).strip():
            return str(item).strip()
    return ""


def _nested_values(value: Any) -> Sequence[Any]:
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _generic_task_id(value: Mapping[str, Any], is_root: bool) -> str:
    identifier = _identifier_value(value, ("id",))
    has_task_context = (
        any(key in value for key in ("status", "task_status", "state", "progress", "result_url", "video_url"))
        or str(value.get("object") or "").strip().lower() in {"video", "video.task", "task"}
    )
    return identifier if identifier and (is_root or has_task_context) else ""


def _strong_task_id(value: Any, depth: int, root_depth: int) -> str:
    if depth > 8:
        return ""
    if isinstance(value, Mapping):
        identifier = _identifier_value(value, ("task_id", "taskId", "video_id", "videoId"))
        if identifier:
            return identifier
        identifier = _generic_task_id(value, depth == root_depth)
        if identifier:
            return identifier
    for item in _nested_values(value):
        identifier = _strong_task_id(item, depth + 1, root_depth)
        if identifier:
            return identifier
    return ""


def _request_task_id(value: Any, depth: int) -> str:
    if depth > 8:
        return ""
    if isinstance(value, Mapping):
        identifier = _identifier_value(value, ("request_id", "requestId"))
        if identifier:
            return identifier
        children = (value.get(key) for key in ("data", "detail", "result", "output", "response"))
    else:
        children = _nested_values(value)
    for item in children:
        identifier = _request_task_id(item, depth + 1)
        if identifier:
            return identifier
    return ""


def _task_id(value: Any, depth: int = 0) -> str:
    return _strong_task_id(value, depth, depth) or _request_task_id(value, depth)


def _status(value: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, Mapping):
        for key in ("status", "task_status", "state"):
            item = str(value.get(key) or "").strip().upper()
            if item:
                return item
        for key in ("data", "detail", "result", "output", "events"):
            result = _status(value.get(key), depth + 1)
            if result:
                return result
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            result = _status(item, depth + 1)
            if result:
                return result
    return ""


def _collect_urls(value: Any, urls: List[str], key_hint: str = "", depth: int = 0) -> None:
    if value is None or depth > 10:
        return
    if isinstance(value, str):
        text = value.strip()
        if key_hint in _URL_KEYS and text.startswith(("/", "v1/", "v2/")):
            urls.append(text)
        candidates = _HTTP_URL_RE.findall(value)
        for candidate in candidates:
            candidate = candidate.rstrip(".,;:)]}")
            path = urllib.parse.urlsplit(candidate).path.lower()
            if key_hint in _URL_KEYS or key_hint in {"content", "text"} or path.endswith((".mp4", ".webm", ".mov", "/content")):
                urls.append(candidate)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _URL_KEYS or key in _CONTAINER_KEYS:
                _collect_urls(item, urls, key, depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_urls(item, urls, key_hint, depth + 1)


def _video_urls(payload: Mapping[str, Any], base_url: str = "") -> List[str]:
    found: List[str] = []
    _collect_urls(payload, found)
    normalized = [resolve_video_download_url(value, base_url) for value in found]
    return list(dict.fromkeys(value for value in normalized if value))


def _failure_reason(payload: Any) -> str:
    values: List[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if value is None or depth > 8:
            return
        if isinstance(value, Mapping):
            for key in ("fail_reason", "failure_reason", "message", "msg", "reason", "error", "detail", "code"):
                item = value.get(key)
                if isinstance(item, str) and item.strip() and item.strip() not in values:
                    values.append(item.strip())
                elif isinstance(item, (Mapping, list)):
                    walk(item, depth + 1)
            for key in ("data", "result", "response"):
                walk(value.get(key), depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item, depth + 1)

    walk(payload)
    return "；".join(values[:4]) or "任务失败，但上游未提供原因"


def _retry_after(response: httpx.Response) -> Optional[float]:
    try:
        return max(0.0, float(response.headers.get("retry-after") or ""))
    except Exception:
        return None


def _report(progress: ProgressCallback, patch: Dict[str, Any]) -> None:
    if progress:
        progress(patch)


def _poll_paths(model: str, task_id: str) -> List[str]:
    quoted = urllib.parse.quote(str(task_id), safe="")
    return [f"/v1/videos/{quoted}"]


async def _save_result(
    payload: Mapping[str, Any],
    task_id: str,
    model: str,
    base_url: str,
    save_video: SaveVideo,
) -> Dict[str, Any]:
    root = _provider_root(base_url)
    urls = _video_urls(payload, root)
    if not urls and task_id:
        quoted = urllib.parse.quote(str(task_id), safe="")
        urls = [f"{root}/v1/videos/{quoted}/content"]
    if not urls:
        raise AICostProtocolError(502, f"aicost 视频生成成功但没有返回视频：{payload}")
    local_urls = []
    for url in urls:
        local_url = str(await save_video(url) or "").strip()
        if not local_url:
            raise AICostProtocolError(502, f"aicost 视频下载失败：{url}")
        if urllib.parse.urlsplit(url).path.endswith("/content") and not local_url.startswith(("/output/", "/assets/")):
            raise AICostProtocolError(502, "aicost /content 视频下载失败，未能保存到本地输出目录")
        local_urls.append(local_url)
    return {"videos": local_urls, "task_id": task_id, "raw": dict(payload)}


async def _poll_video(
    client: httpx.AsyncClient,
    task_id: str,
    model: str,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float,
    request_id: str = "",
) -> Dict[str, Any]:
    base_url = _provider_root(base_url)
    deadline = time.monotonic() + max(1.0, float(poll_timeout))
    delay = max(0.0, float(poll_interval))
    errors = 0
    paths = _poll_paths(model, task_id)
    selected_path = ""
    last_payload: Dict[str, Any] = {}
    poll_headers = dict(headers)
    if request_id:
        poll_headers["X-Request-ID"] = request_id
    while time.monotonic() < deadline:
        if delay:
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        response: Optional[httpx.Response] = None
        for path in ([selected_path] if selected_path else paths):
            try:
                candidate = await client.get(f"{base_url.rstrip('/')}{path}", headers=poll_headers)
            except httpx.TransportError as exc:
                errors += 1
                if errors >= _MAX_POLL_ERRORS:
                    raise AICostProtocolError(502, f"aicost 视频状态连续查询失败：{exc}") from exc
                response = None
                break
            if candidate.status_code == 404 and not selected_path and path != paths[-1]:
                continue
            response = candidate
            selected_path = path
            break
        if response is None:
            _report(progress, {"status": "polling", "message": "aicost 状态查询暂时失败，将自动重试", "next_poll_at": time.time() + delay})
            continue
        if response.status_code == 429 or response.status_code >= 500:
            errors += 1
            if errors >= _MAX_POLL_ERRORS:
                raise AICostProtocolError(response.status_code, f"aicost 视频状态连续查询失败：{response.text[:500]}")
            delay = max(float(poll_interval), _retry_after(response) or 0.0)
            continue
        if response.status_code >= 400:
            raise AICostProtocolError(response.status_code, f"aicost 视频任务查询失败：{response.text[:500]}")
        errors = 0
        raw = _json_response(response, "视频任务查询")
        last_payload = raw
        state = _status(raw)
        urls = _video_urls(raw, base_url)
        _report(progress, {"status": "polling", "raw_last": raw, "next_poll_at": time.time() + delay})
        if state in _FAILURE_STATUSES:
            raise AICostProtocolError(502, f"aicost 视频生成失败：{_failure_reason(raw)}")
        if state in _SUCCESS_STATUSES or (urls and state not in _FAILURE_STATUSES):
            return await _save_result(raw, task_id, model, base_url, save_video)
        delay = max(float(poll_interval), _retry_after(response) or 0.0)
    raise AICostProtocolError(504, f"aicost 视频生成任务超时：{last_payload or task_id}")


async def generate_aicost_video(
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
    """提交 aicost 视频任务并等待完成。"""
    root = _provider_root(base_url)
    if not root:
        raise AICostProtocolError(400, "aicost 未配置 Base URL")
    model, submission = await _build_submission(client, request, resolve_local_path, content_type_for_path, public_reference_url)
    submit_url = f"{root}{submission.endpoint}"
    request_headers = dict(headers)
    if submission.request_id:
        request_headers["X-Request-ID"] = submission.request_id
    try:
        response = await submit_video_http_request(
            client, progress=progress, url=submit_url, headers=request_headers,
            json_body=submission.body,
            context={"protocol": "aicost", "model": model},
        )
    except httpx.TransportError as exc:
        raise AICostProtocolError(502, f"aicost 创建请求未收到响应，不能自动重试以避免重复扣费：{exc}") from exc
    if response.status_code >= 400:
        raise AICostProtocolError(response.status_code, f"aicost 视频创建失败：{response.text[:500]}")
    raw = _json_response(response, "视频创建")
    state = _status(raw)
    if state in _FAILURE_STATUSES:
        raise AICostProtocolError(502, f"aicost 视频生成失败：{_failure_reason(raw)}")
    task_id = _task_id(raw)
    urls = _video_urls(raw, root)
    if urls:
        return await _save_result(raw, task_id, model, root, save_video)
    if not task_id:
        raise AICostProtocolError(502, f"aicost 视频接口未返回任务 ID 或视频地址，已停止处理以避免重复扣费：{raw}")
    _report(progress, {"status": "polling", "upstream_task_id": task_id, "task_id": task_id, "submit_url": submit_url, "raw_submit": raw})
    return await _poll_video(client, task_id, model, root, headers, progress, save_video, poll_timeout, poll_interval, submission.request_id)


async def resume_aicost_video(
    client: httpx.AsyncClient,
    task_id: str,
    model: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = 10.0,
) -> Dict[str, Any]:
    """只恢复查询已有 aicost 任务，绝不重新提交创建请求。"""
    root = _provider_root(base_url)
    if not root:
        raise AICostProtocolError(400, "aicost 未配置 Base URL")
    canonical = canonical_model(model)
    return await _poll_video(client, str(task_id), canonical, root, headers, progress, save_video, poll_timeout, poll_interval)
