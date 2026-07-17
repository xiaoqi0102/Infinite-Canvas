"""Sudashui ``/v1/video/generations`` 视频协议适配。

本模块负责 Sudashui 的素材校验与上传、请求构造、业务状态解析、
轮询恢复和结果落盘编排，不依赖 Infinite Canvas 的全局状态。
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from .common import canonical_video_api_root, humanize_video_task_failure, resolve_video_download_url


SUDASHUI_VIDEO_REQUEST_MODE = "sudashui-video-generations"
SUDASHUI_FILES_BASE_URL = "https://files.sudashuiapi.com"
SUDASHUI_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"}
SUDASHUI_UPLOAD_RULES = {
    "图片": ({"image/jpeg", "image/png", "image/webp"}, 30 * 1024 * 1024),
    "视频": ({"video/mp4", "video/quicktime"}, 50 * 1024 * 1024),
    "音频": ({"audio/mpeg", "audio/wav"}, 15 * 1024 * 1024),
}

_SUCCESS_STATUSES = {
    "SUCCESS", "SUCCEED", "SUCCEEDED", "COMPLETED", "COMPLETE",
    "DONE", "FINISHED", "FINISH", "OK", "READY",
}
_FAILURE_STATUSES = {
    "FAILURE", "FAILED", "FAIL", "ERROR", "ERRORED",
    "CANCELED", "CANCELLED", "TIMEOUT", "TIMEDOUT", "REJECTED", "EXPIRED",
}
_PENDING_STATUSES = {
    "NOT_START", "NOT_STARTED", "SUBMITTED", "QUEUED", "QUEUEING", "PENDING",
    "IN_PROGRESS", "PROCESSING", "RUNNING",
}
_NOT_STARTED_STATUSES = {
    "NOT_START", "NOT_STARTED", "SUBMITTED", "QUEUED", "QUEUEING", "PENDING",
}
_URL_KEYS = {
    "url", "video_url", "videoUrl", "mp4_url", "mp4Url",
    "output", "result", "content", "video",
    "output_url", "outputUrl", "result_url", "resultUrl",
    "content_url", "contentUrl", "local_url", "localUrl",
    "download_url", "downloadUrl", "file_url", "FileUrl",
    "src", "uri", "path", "preview_url", "previewUrl",
    "last_frame_url", "lastFrameUrl", "remixed_from_video_id",
}
_CONTAINER_KEYS = {
    "videos", "outputs", "data", "detail", "result", "results",
    "creations", "content", "metadata", "output", "video", "response",
}
_TERMINAL_ERROR_PATTERNS = (
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
)


class SudashuiProtocolError(Exception):
    """带 HTTP 状态语义的 Sudashui 协议错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
ResolveLocalPath = Callable[[str], Optional[str]]
ContentTypeForPath = Callable[[str], str]
SaveVideo = Callable[[str], Awaitable[str]]


def _provider_root(base_url: Any) -> str:
    return canonical_video_api_root(base_url)


def _duration(value: Any) -> int:
    try:
        numeric = float(value)
    except Exception as exc:
        raise SudashuiProtocolError(400, "Sudashui 视频时长必须是 4 到 15 秒的整数") from exc
    if isinstance(value, bool) or not numeric.is_integer():
        raise SudashuiProtocolError(400, "Sudashui 视频时长必须是 4 到 15 秒的整数")
    duration = int(numeric)
    if duration < 4 or duration > 15:
        raise SudashuiProtocolError(400, "Sudashui 视频时长仅支持 4 到 15 秒")
    return duration


def _aspect_ratio(value: Any) -> str:
    ratio = str(value or "").strip()
    if ratio == "keep_ratio":
        ratio = "adaptive"
    if ratio not in SUDASHUI_ASPECT_RATIOS:
        allowed = "、".join(("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"))
        raise SudashuiProtocolError(
            400,
            f"Sudashui 视频比例不支持：{ratio or '(empty)'}；仅支持 {allowed}",
        )
    return ratio


def _frame_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"first_frame", "first"}:
        return "first_frame"
    if role in {"last_frame", "last"}:
        return "last_frame"
    return role


def _source_is_files_url(value: Any) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "files.sudashuiapi.com"


def _source_is_public_url(value: Any) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except Exception:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return not (
        host in {"127.0.0.1", "localhost", "::1"}
        or re.match(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)", host)
    )


def _local_source_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except Exception:
        return text
    if parsed.scheme.lower() in {"http", "https"} and not _source_is_public_url(text):
        return urllib.parse.unquote(parsed.path or "")
    return text


def _validate_upload(kind: str, mime: str, size: int) -> None:
    allowed_types, max_bytes = SUDASHUI_UPLOAD_RULES[kind]
    normalized_mime = str(mime or "").split(";", 1)[0].strip().lower()
    if normalized_mime not in allowed_types:
        allowed = "、".join(sorted(allowed_types))
        raise SudashuiProtocolError(
            400,
            f"Sudashui 参考{kind}不支持 MIME 类型 {normalized_mime or '(empty)'}；仅支持 {allowed}",
        )
    if size >= max_bytes:
        raise SudashuiProtocolError(
            400,
            f"Sudashui 参考{kind}文件过大：必须小于 {max_bytes // (1024 * 1024)} MB",
        )


def _data_url_file(value: str, kind: str) -> Tuple[str, bytes, str]:
    match = re.match(r"^data:([^;,]+);base64,(.+)$", str(value or ""), re.IGNORECASE | re.DOTALL)
    if not match:
        raise SudashuiProtocolError(400, f"Sudashui 参考{kind}的 data URL 不合法，仅支持 base64 data URL")
    mime = match.group(1).strip().lower()
    try:
        encoded = re.sub(r"\s+", "", match.group(2))
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise SudashuiProtocolError(400, f"Sudashui 参考{kind}的 data URL 解码失败") from exc
    if not content:
        raise SudashuiProtocolError(400, f"Sudashui 参考{kind}的 data URL 内容为空")
    _validate_upload(kind, mime, len(content))
    extension = mimetypes.guess_extension(mime) or {"图片": ".png", "视频": ".mp4", "音频": ".mp3"}[kind]
    name = {"图片": "canvas_image", "视频": "canvas_video", "音频": "canvas_audio"}[kind]
    return f"{name}{extension}", content, mime


def _local_file(
    value: str,
    kind: str,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Tuple[str, bytes, str]:
    source = _local_source_value(value)
    path = resolve_local_path(source)
    if not path:
        raise SudashuiProtocolError(
            400,
            f"Sudashui 参考{kind}只支持公网 URL、合法 data URL 或画布内受控本地文件",
        )
    mime = content_type_for_path(path)
    try:
        size = os.path.getsize(path)
        _validate_upload(kind, mime, size)
        with open(path, "rb") as handle:
            content = handle.read()
    except SudashuiProtocolError:
        raise
    except Exception as exc:
        raise SudashuiProtocolError(400, f"读取 Sudashui 参考{kind}失败：{exc}") from exc
    return os.path.basename(path), content, mime


async def _upload_media(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    value: Any,
    kind: str,
    cache: Dict[str, Tuple[str, str]],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> str:
    source = str(value or "").strip()
    if not source:
        raise SudashuiProtocolError(400, f"Sudashui 参考{kind}地址不能为空")
    if source in cache:
        cached_kind, cached_url = cache[source]
        if cached_kind != kind:
            raise SudashuiProtocolError(400, f"同一个 Sudashui 素材不能同时作为参考{cached_kind}和参考{kind}")
        return cached_url
    if _source_is_public_url(source):
        cache[source] = (kind, source)
        return source
    file_tuple = (
        _data_url_file(source, kind)
        if source.startswith("data:")
        else _local_file(source, kind, resolve_local_path, content_type_for_path)
    )
    try:
        response = await client.post(
            SUDASHUI_FILES_BASE_URL,
            headers=dict(headers),
            files={"file": file_tuple},
            timeout=180,
        )
    except httpx.TransportError as exc:
        raise SudashuiProtocolError(502, f"Sudashui 文件上传失败：{exc}") from exc
    if response.status_code >= 400:
        raise SudashuiProtocolError(response.status_code, f"Sudashui 文件上传失败：{response.text[:300]}")
    raw = _json_response(response, "文件上传", 300)
    uploaded_url = str(raw.get("url") or "").strip()
    if not _source_is_files_url(uploaded_url):
        raise SudashuiProtocolError(502, f"Sudashui 文件上传成功但未返回合法文件 URL：{raw}")
    cache[source] = (kind, uploaded_url)
    return uploaded_url


def _image_refs(request: Mapping[str, Any]) -> List[Dict[str, Any]]:
    refs = []
    for item in request.get("images") or []:
        if isinstance(item, Mapping):
            ref = dict(item)
        else:
            ref = {"url": getattr(item, "url", ""), "role": getattr(item, "role", "")}
        if str(ref.get("url") or "").strip():
            refs.append(ref)
    return refs


def _nonempty_strings(values: Sequence[Any]) -> List[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _official_asset_indexes(request: Mapping[str, Any], model: str, image_count: int) -> List[int]:
    indexes = list(request.get("official_asset_indexes") or [])
    if not indexes:
        return []
    if not str(model or "").strip().lower().startswith("sdas-gf-"):
        raise SudashuiProtocolError(400, "official_asset_indexes 仅可用于 sdas-gf- 开头的 Sudashui 官方模型")
    if len(set(indexes)) != len(indexes):
        raise SudashuiProtocolError(400, "official_asset_indexes 不能包含重复编号")
    for index in indexes:
        if isinstance(index, bool) or not isinstance(index, int):
            raise SudashuiProtocolError(400, "official_asset_indexes 必须是整数编号")
        if index < 0 or index >= image_count:
            raise SudashuiProtocolError(
                400,
                f"official_asset_indexes 图片编号越界：{index}；当前共有 {image_count} 张图片",
            )
    return indexes


def _validate_official_sources(image_sources: List[str], official_indexes: List[int]) -> None:
    for index in official_indexes:
        source = image_sources[index]
        if _source_is_public_url(source) and not _source_is_files_url(source):
            raise SudashuiProtocolError(
                400,
                f"真人素材图片 {index + 1} 必须先导入画布并上传到 Sudashui 文件域，不能直接使用外部公网图片",
            )


def _validate_sources(
    model: str,
    image_refs: List[Dict[str, Any]],
    video_sources: List[str],
    audio_sources: List[str],
) -> None:
    if len(image_refs) > 9:
        raise SudashuiProtocolError(400, "Sudashui references 模式最多支持 9 张图片")
    if len(video_sources) > 3:
        raise SudashuiProtocolError(400, "Sudashui references 模式最多支持 3 个视频")
    if len(audio_sources) > 3:
        raise SudashuiProtocolError(400, "Sudashui references 模式最多支持 3 个音频")
    if len(image_refs) + len(video_sources) + len(audio_sources) > 12:
        raise SudashuiProtocolError(400, "Sudashui references 模式的图片、视频和音频总数不能超过 12 个")
    if str(model or "").strip().lower().startswith("sdas-gf-") and video_sources:
        raise SudashuiProtocolError(400, "Sudashui 官方模型不支持参考视频，请移除视频素材")


async def _frames_payload(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    headers: Mapping[str, str],
    sources: Mapping[str, Any],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Dict[str, Any]:
    image_refs = sources["image_refs"]
    roles = sources["roles"]
    if sources["video_sources"] or sources["audio_sources"]:
        raise SudashuiProtocolError(400, "Sudashui frames 模式不能同时使用参考视频或参考音频")
    if len(image_refs) != 2 or roles.count("first_frame") != 1 or roles.count("last_frame") != 1:
        raise SudashuiProtocolError(400, "Sudashui frames 模式必须恰好提供一张首帧和一张尾帧图片")
    first_index = roles.index("first_frame")
    last_index = roles.index("last_frame")
    ordered_refs = [image_refs[first_index], image_refs[last_index]]
    original_indexes = _official_asset_indexes(request, model, len(image_refs))
    index_mapping = {first_index: 0, last_index: 1}
    official_indexes = [index_mapping[index] for index in original_indexes]
    image_sources = [str(ref.get("url") or "").strip() for ref in ordered_refs]
    _validate_official_sources(image_sources, official_indexes)
    cache: Dict[str, Tuple[str, str]] = {}
    first_url = await _upload_media(
        client, headers, image_sources[0], "图片", cache, resolve_local_path, content_type_for_path
    )
    last_url = await _upload_media(
        client, headers, image_sources[1], "图片", cache, resolve_local_path, content_type_for_path
    )
    inner: Dict[str, Any] = {
        "aspectRatio": sources["aspect_ratio"],
        "mode": "frames",
        "firstFrameUrl": first_url,
        "lastFrameUrl": last_url,
    }
    if official_indexes:
        inner["officialAssetIndexes"] = official_indexes
    return inner


async def _references_payload(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    headers: Mapping[str, str],
    sources: Mapping[str, Any],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Dict[str, Any]:
    image_sources = [str(ref.get("url") or "").strip() for ref in sources["image_refs"]]
    official_indexes = _official_asset_indexes(request, model, len(image_sources))
    _validate_official_sources(image_sources, official_indexes)
    cache: Dict[str, Tuple[str, str]] = {}

    async def upload(value: str, kind: str) -> str:
        return await _upload_media(
            client, headers, value, kind, cache, resolve_local_path, content_type_for_path
        )

    image_urls = [await upload(source, "图片") for source in image_sources]
    video_urls = [await upload(source, "视频") for source in sources["video_sources"]]
    audio_urls = [await upload(source, "音频") for source in sources["audio_sources"]]
    inner: Dict[str, Any] = {"aspectRatio": sources["aspect_ratio"], "mode": "references"}
    for key, values in (
        ("imageUrls", image_urls),
        ("videoUrls", video_urls),
        ("audioUrls", audio_urls),
        ("officialAssetIndexes", official_indexes),
    ):
        if values:
            inner[key] = values
    return inner


async def _video_body(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    model: str,
    headers: Mapping[str, str],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Dict[str, Any]:
    image_refs = _image_refs(request)
    video_sources = _nonempty_strings(request.get("videos") or [])
    audio_sources = _nonempty_strings(request.get("audios") or [])
    _validate_sources(model, image_refs, video_sources, audio_sources)
    roles = [_frame_role(ref.get("role")) for ref in image_refs]
    sources = {
        "image_refs": image_refs,
        "video_sources": video_sources,
        "audio_sources": audio_sources,
        "roles": roles,
        "aspect_ratio": _aspect_ratio(request.get("aspect_ratio")),
    }
    if any(role in {"first_frame", "last_frame"} for role in roles):
        inner = await _frames_payload(
            client, request, model, headers, sources, resolve_local_path, content_type_for_path
        )
    else:
        inner = await _references_payload(
            client, request, model, headers, sources, resolve_local_path, content_type_for_path
        )
    return {
        "model": model,
        "prompt": str(request.get("prompt") or ""),
        "duration": _duration(request.get("duration")),
        "metadata": {"payload": json.dumps(inner, ensure_ascii=False, separators=(",", ":"))},
    }


def _collect_urls(value: Any, urls: List[str], key_hint: str = "") -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if (key_hint in _URL_KEYS or key_hint in _CONTAINER_KEYS) and text.startswith(
            ("http://", "https://", "/", "v1/", "v2/")
        ):
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
    urls: List[str] = []
    _collect_urls(payload, urls)
    urls = [resolve_video_download_url(value, base_url) for value in urls]
    deduped = []
    for url in urls:
        if url and url not in deduped:
            deduped.append(url)
    return deduped


def _task_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return payload.get("data") if isinstance(payload.get("data"), Mapping) else payload


def _task_id(payload: Mapping[str, Any], depth: int = 0) -> str:
    if depth > 6:
        return ""
    for key in ("task_id", "taskId", "submit_id", "video_id", "videoId", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("data", "detail", "result"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            value = _task_id(nested, depth + 1)
            if value:
                return value
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
            for item in nested:
                if isinstance(item, Mapping):
                    value = _task_id(item, depth + 1)
                    if value:
                        return value
    return ""


def sudashui_video_task_pending(raw: Any) -> bool:
    """判断持久化失败任务的最后状态是否其实仍在上游排队或生成。"""
    if not isinstance(raw, Mapping):
        return False
    task_data = _task_data(raw)
    status = str(task_data.get("status") or task_data.get("task_status") or "").strip().upper()
    if status in _SUCCESS_STATUSES or status in _FAILURE_STATUSES or _video_urls(raw):
        return False
    inner = task_data.get("data") if isinstance(task_data.get("data"), Mapping) else {}
    inner_state = str(inner.get("state") or inner.get("status") or "").strip().upper()
    return status in _PENDING_STATUSES or inner_state in _PENDING_STATUSES


def _task_started(raw: Mapping[str, Any]) -> bool:
    task_data = _task_data(raw)
    status = str(task_data.get("status") or task_data.get("task_status") or "").strip().upper()
    if status in _NOT_STARTED_STATUSES:
        return False
    try:
        if float(task_data.get("start_time") or 0) > 0:
            return True
    except Exception:
        pass
    progress_match = re.search(r"\d+(?:\.\d+)?", str(task_data.get("progress") or "").strip())
    if progress_match and float(progress_match.group(0)) > 0:
        return True
    inner = task_data.get("data") if isinstance(task_data.get("data"), Mapping) else {}
    inner_state = str(inner.get("state") or inner.get("status") or "").strip().upper()
    return status in {"IN_PROGRESS", "PROCESSING", "RUNNING"} or inner_state in {
        "IN_PROGRESS", "PROCESSING", "RUNNING"
    }


def _business_failure(value: Any, depth: int = 0) -> bool:
    if value is None or depth > 8:
        return False
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in {"{", "["}:
            try:
                return _business_failure(json.loads(text), depth + 1)
            except Exception:
                return False
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_business_failure(item, depth + 1) for item in value)
    if not isinstance(value, Mapping):
        return False
    status = str(value.get("status") or value.get("task_status") or value.get("state") or "").strip().upper()
    if status in _FAILURE_STATUSES or value.get("success") is False:
        return True
    code = str(value.get("code") or "").strip().lower()
    if code and code not in {"0", "200", "ok", "success"}:
        return True
    if str(value.get("fail_reason") or value.get("failReason") or "").strip():
        return True
    err_code = str(value.get("err_code") or value.get("errCode") or "").strip()
    if err_code and err_code.lower() not in {"0", "none", "null"}:
        return True
    error_value = value.get("error")
    if isinstance(error_value, str) and error_value.strip():
        return True
    if isinstance(error_value, Mapping) and error_value:
        return True
    return any(
        _business_failure(value.get(key), depth + 1)
        for key in ("error", "data", "detail", "result", "response")
        if key in value
    )


def _flatten_error_text(value: Any, parts: List[str], depth: int = 0) -> None:
    if value is None or depth > 8:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            parts.append(str(key))
            _flatten_error_text(item, parts, depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _flatten_error_text(item, parts, depth + 1)
    else:
        parts.append(str(value))


def _is_terminal_error(value: Any) -> bool:
    parts: List[str] = []
    _flatten_error_text(value, parts)
    text = "\n".join(part for part in parts if part).lower()
    return bool(text) and any(re.search(pattern, text, re.IGNORECASE) for pattern in _TERMINAL_ERROR_PATTERNS)


def _failure_reason(payload: Any) -> str:
    fail_reasons: List[str] = []
    messages: List[str] = []
    error_codes: List[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if value is None or depth > 8:
            return
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in {"{", "["}:
                try:
                    walk(json.loads(text), depth + 1)
                except Exception:
                    pass
            return
        if isinstance(value, Mapping):
            for key in ("fail_reason", "failReason"):
                text = str(value.get(key) or "").strip()
                if text and text not in fail_reasons:
                    fail_reasons.append(text)
            for key in ("message", "msg"):
                text = str(value.get(key) or "").strip()
                if text[:1] in {"{", "["}:
                    walk(text, depth + 1)
                elif text and text not in messages:
                    messages.append(text)
            for key in ("code", "error_code", "errorCode", "err_code", "errCode"):
                text = str(value.get(key) or "").strip()
                if text and text.lower() not in {"0", "none", "null", "success", "ok"} and text not in error_codes:
                    error_codes.append(text)
            error_value = value.get("error")
            if isinstance(error_value, str) and error_value.strip() and error_value.strip() not in messages:
                messages.append(error_value.strip())
            for key in ("error", "data", "detail", "result", "results", "response"):
                if key in value:
                    walk(value.get(key), depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item, depth + 1)

    walk(payload)
    reasons = fail_reasons or messages
    if reasons:
        return f"{reasons[0]}（错误码：{error_codes[0]}）" if error_codes else reasons[0]
    if error_codes:
        return f"错误码：{error_codes[0]}"
    return str(payload or "")


def _retry_after_seconds(response: httpx.Response, poll_timeout: float) -> Optional[float]:
    values: List[float] = []

    def add(value: Any) -> None:
        try:
            seconds = float(value)
        except Exception:
            return
        if seconds > 0:
            values.append(seconds)

    add(response.headers.get("Retry-After"))
    try:
        payload: Any = response.json()
    except Exception:
        payload = response.text

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in {"retry_after", "retryafter"}:
                    add(item)
                else:
                    walk(item, depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, str):
            for pattern in (
                r"retry[_\s-]*after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)",
                r"请等待\s*(\d+(?:\.\d+)?)\s*秒",
                r"(\d+(?:\.\d+)?)\s*秒后再试",
                r"(?:retry after|wait)\s*(\d+(?:\.\d+)?)\s*(?:s|sec|second|seconds)?",
            ):
                match = re.search(pattern, value, re.IGNORECASE)
                if match:
                    add(match.group(1))

    walk(payload)
    if not values:
        return None
    return min(max(values), max(5.0, float(poll_timeout)))


def _json_response(response: httpx.Response, action: str, limit: int = 500) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise SudashuiProtocolError(502, f"Sudashui {action}返回非 JSON 响应：{response.text[:limit]}") from exc
    if not isinstance(payload, dict):
        raise SudashuiProtocolError(502, f"Sudashui {action}返回非 JSON 对象：{payload}")
    return payload


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
        raise SudashuiProtocolError(502, f"Sudashui 视频生成成功但没有返回视频：{payload}")
    local_urls = []
    for url in urls:
        local_url = str(await save_video(url) or "").strip()
        if not local_url:
            raise SudashuiProtocolError(502, f"Sudashui 视频下载失败：{url}")
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
    status_url = f"{_provider_root(base_url)}/v1/video/generations/{quoted_id}"
    deadline: Optional[float] = None
    delay = max(0.0, float(poll_interval))
    last_payload: Dict[str, Any] = {}
    while deadline is None or time.monotonic() < deadline:
        if delay:
            sleep_for = delay if deadline is None else min(delay, max(0.0, deadline - time.monotonic()))
            await asyncio.sleep(sleep_for)
        try:
            response = await client.get(status_url, headers=dict(headers))
        except (httpx.TransportError, TimeoutError) as exc:
            _report(progress, {
                "status": "polling",
                "message": f"Sudashui 视频任务查询暂时失败，将自动重试：{str(exc).strip() or type(exc).__name__}",
                "next_poll_at": time.time() + delay,
            })
            continue
        retry_after = _retry_after_seconds(response, poll_timeout)
        if response.status_code >= 400:
            try:
                error_payload: Any = response.json()
            except Exception:
                error_payload = {"error": response.text}
            if _is_terminal_error(error_payload):
                raise SudashuiProtocolError(
                    response.status_code,
                    humanize_video_task_failure(_failure_reason(error_payload)),
                )
            if retry_after:
                delay = max(float(poll_interval), retry_after)
                last_payload = error_payload if isinstance(error_payload, dict) else {"error": error_payload}
                _report(progress, {
                    "status": "polling",
                    "retry_after": retry_after,
                    "next_poll_at": time.time() + delay,
                    "raw_last": last_payload,
                })
                continue
            raise SudashuiProtocolError(response.status_code, f"Sudashui 视频任务查询失败：{response.text[:500]}")
        raw = _json_response(response, "视频任务查询")
        last_payload = raw
        _report(progress, {"status": "polling", "raw_last": raw})
        if _business_failure(raw) or _is_terminal_error(raw):
            raise SudashuiProtocolError(502, humanize_video_task_failure(_failure_reason(raw)))
        task_data = _task_data(raw)
        status = str(task_data.get("status") or task_data.get("task_status") or "").strip().upper()
        if deadline is None and _task_started(raw):
            deadline = time.monotonic() + max(1.0, float(poll_timeout))
        urls = _video_urls(raw, base_url)
        if status in _SUCCESS_STATUSES or (status not in _FAILURE_STATUSES and urls):
            return await _save_result(raw, task_id, base_url, save_video)
        retry_after = _retry_after_seconds(response, poll_timeout)
        delay = max(float(poll_interval), retry_after or 0.0)
        if retry_after:
            _report(progress, {
                "status": "polling",
                "retry_after": retry_after,
                "next_poll_at": time.time() + delay,
                "raw_last": raw,
            })
    raise SudashuiProtocolError(504, f"Sudashui 视频生成任务超时：{last_payload or task_id}")


async def generate_sudashui_video(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = 25.0,
) -> Dict[str, Any]:
    """提交 Sudashui 视频任务并等待完成。"""
    root = _provider_root(base_url)
    if not root:
        raise SudashuiProtocolError(400, "Sudashui 未配置 Base URL")
    model = str(request.get("model") or "veo3-fast").strip() or "veo3-fast"
    body = await _video_body(
        client, request, model, headers, resolve_local_path, content_type_for_path
    )
    submit_url = f"{root}/v1/video/generations"
    try:
        response = await client.post(submit_url, headers=dict(headers), json=body)
    except httpx.TransportError as exc:
        raise SudashuiProtocolError(
            502,
            f"Sudashui 创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        raise SudashuiProtocolError(response.status_code, f"Sudashui 视频创建失败：{response.text[:500]}")
    raw = _json_response(response, "视频创建")
    if _business_failure(raw) or _is_terminal_error(raw):
        raise SudashuiProtocolError(502, humanize_video_task_failure(_failure_reason(raw)))
    task_id = _task_id(raw)
    if not task_id:
        raise SudashuiProtocolError(
            502,
            f"Sudashui 视频接口未返回任务 ID，已停止处理以避免重复扣费：{raw}",
        )
    _report(progress, {
        "status": "polling",
        "upstream_task_id": task_id,
        "task_id": task_id,
        "submit_url": submit_url,
        "raw_submit": raw,
    })
    if _video_urls(raw, root):
        return await _save_result(raw, task_id, root, save_video)
    return await _poll_video(
        client, task_id, root, headers, progress, save_video, poll_timeout, poll_interval
    )


async def resume_sudashui_video(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = 25.0,
) -> Dict[str, Any]:
    """只恢复查询已有 Sudashui 任务，绝不重新提交创建请求。"""
    root = _provider_root(base_url)
    if not root:
        raise SudashuiProtocolError(400, "Sudashui 未配置 Base URL")
    return await _poll_video(
        client, str(task_id), root, headers, progress, save_video, poll_timeout, poll_interval
    )
