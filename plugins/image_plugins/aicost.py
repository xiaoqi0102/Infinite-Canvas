"""aicost.xyz 图片模型协议适配。

本模块封装 GPT Image 与 Gemini 图片接口。宿主负责提供本地素材路径解析，
插件负责请求构造、直连上游、响应归一化与异步任务轮询。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx


AICOST_IMAGE_REQUEST_MODE = "aicost-image"
AICOST_IMAGE_OFFICIAL_HOSTNAMES = {"aicost.xyz", "www.aicost.xyz"}

_GPT_IMAGE_MODELS = {"gpt-image-2"}
_GEMINI_IMAGE_MODELS = {
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
}
_IMAGE_SIZE_OPTIONS = {"1K", "2K", "4K"}
_ASPECT_RATIO_OPTIONS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
_SIZE_CONFIG = {
    "1024x1024": ("1K", "1:1"),
    "1536x864": ("1K", "16:9"),
    "864x1536": ("1K", "9:16"),
    "1360x1024": ("1K", "4:3"),
    "1024x1360": ("1K", "3:4"),
    "1536x1024": ("1K", "3:2"),
    "1024x1536": ("1K", "2:3"),
    "2048x2048": ("2K", "1:1"),
    "3072x1728": ("2K", "16:9"),
    "1728x3072": ("2K", "9:16"),
    "2720x2048": ("2K", "4:3"),
    "2048x2720": ("2K", "3:4"),
    "3072x2048": ("2K", "3:2"),
    "2048x3072": ("2K", "2:3"),
    "2880x2880": ("4K", "1:1"),
    "3840x2160": ("4K", "16:9"),
    "2160x3840": ("4K", "9:16"),
    "3328x2496": ("4K", "4:3"),
    "2496x3328": ("4K", "3:4"),
    "3520x2336": ("4K", "3:2"),
    "2336x3520": ("4K", "2:3"),
}
_PENDING_STATUSES = {"QUEUED", "PENDING", "PROCESSING", "RUNNING", "IN_PROGRESS"}
_SUCCESS_STATUSES = {"SUCCESS", "SUCCEEDED", "COMPLETED", "COMPLETE", "DONE", "FINISHED", "READY"}
_FAILURE_STATUSES = {"FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "REJECTED", "CONTENT_FILTER", "EXPIRED"}
_REFERENCE_LIMIT = 20
_MAX_REFERENCE_BYTES = 30 * 1024 * 1024
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
_IMAGE_DIRECT_KEYS = {"b64_json", "image_base64", "base64", "url", "image_url", "image"}
_IMAGE_CONTAINER_KEYS = {
    "images", "output", "result", "data", "choices", "candidates",
    "message", "delta", "content", "parts", "text",
}

ResolveLocalPath = Callable[[str], Optional[str]]
ContentTypeForPath = Callable[[str], str]
ImagePayload = Dict[str, Any]


class AICostImageProtocolError(Exception):
    """带 HTTP 状态语义的 aicost 图片协议错误。"""

    def __init__(self, status_code: int, detail: str, upstream_task_id: str = ""):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)
        self.upstream_task_id = str(upstream_task_id or "").strip()


def is_aicost_image_official_provider(provider: Mapping[str, Any] | None) -> bool:
    """仅对 aicost 官方主机名启用自动识别。"""
    try:
        host = urllib.parse.urlsplit(str((provider or {}).get("base_url") or "").strip()).hostname
    except Exception:
        return False
    return str(host or "").lower() in AICOST_IMAGE_OFFICIAL_HOSTNAMES


def _api_roots(base_url: Any) -> Tuple[str, str]:
    value = str(base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AICostImageProtocolError(400, "aicost 未配置合法的 HTTP/HTTPS Base URL")
    while urllib.parse.urlsplit(value).path.rstrip("/").endswith("/v1"):
        value = value[:-3].rstrip("/")
    return f"{value}/v1", value


def _timeout_seconds(value: Any, default: float, label: str) -> float:
    if isinstance(value, httpx.Timeout):
        value = value.read if value.read is not None else default
    try:
        timeout = float(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise AICostImageProtocolError(400, f"aicost {label}超时配置无效") from exc
    if timeout <= 0:
        raise AICostImageProtocolError(400, f"aicost {label}超时必须大于 0 秒")
    return timeout


def _client(timeout: Any) -> httpx.AsyncClient:
    limits = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(
        connect=20.0,
        read=_timeout_seconds(timeout, 3600.0, "图片请求"),
        write=120.0,
        pool=20.0,
    )
    return httpx.AsyncClient(timeout=limits, follow_redirects=True, trust_env=False)


def _model(request: Mapping[str, Any]) -> str:
    value = str(request.get("model") or "").strip()
    if value not in _GPT_IMAGE_MODELS | _GEMINI_IMAGE_MODELS:
        supported = "、".join(sorted(_GPT_IMAGE_MODELS | _GEMINI_IMAGE_MODELS))
        raise AICostImageProtocolError(400, f"aicost 图片模型不受支持：{value or '(empty)'}；可用模型：{supported}")
    return value


def _prompt(request: Mapping[str, Any]) -> str:
    value = str(request.get("prompt") or "").strip()
    if not value:
        raise AICostImageProtocolError(400, "aicost 图片提示词不能为空")
    return value


def _size(request: Mapping[str, Any]) -> str:
    value = str(request.get("size") or "").strip().lower()
    if not re.fullmatch(r"[1-9]\d{1,4}x[1-9]\d{1,4}", value):
        raise AICostImageProtocolError(400, f"aicost 图片尺寸无效：{value or '(empty)'}")
    return value


def _gemini_size_config(request: Mapping[str, Any], size: str) -> Tuple[str, str]:
    image_size = str(request.get("image_size") or "").strip().upper()
    aspect_ratio = str(request.get("aspect_ratio") or "").strip()
    if image_size or aspect_ratio:
        if image_size not in _IMAGE_SIZE_OPTIONS or aspect_ratio not in _ASPECT_RATIO_OPTIONS:
            raise AICostImageProtocolError(400, "aicost Gemini 图片规格仅支持 1K/2K/4K 与文档列出的比例")
        return image_size, aspect_ratio
    config = _SIZE_CONFIG.get(size)
    if not config:
        raise AICostImageProtocolError(400, f"aicost Gemini 不支持尺寸 {size}，请使用接口文档中的标准尺寸")
    return config


def _references(request: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    values = request.get("reference_images") or request.get("images") or []
    result = [item for item in values if isinstance(item, Mapping) and str(item.get("url") or "").strip()]
    if len(result) > _REFERENCE_LIMIT:
        raise AICostImageProtocolError(400, f"aicost 参考图最多 {_REFERENCE_LIMIT} 张")
    return result


def _mime_type(path: str, resolver: ContentTypeForPath) -> str:
    value = str(resolver(path) or "").split(";", 1)[0].strip().lower()
    return value or mimetypes.guess_type(path)[0] or "application/octet-stream"


def _decode_data_url(value: str) -> Tuple[bytes, str]:
    match = re.match(r"^data:([^;,]+);base64,(.+)$", value, re.IGNORECASE | re.DOTALL)
    if not match:
        raise AICostImageProtocolError(400, "aicost 参考图 data URL 格式无效")
    try:
        content = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except Exception as exc:
        raise AICostImageProtocolError(400, "aicost 参考图 base64 解码失败") from exc
    return content, match.group(1).lower()


def _reference_file(
    reference: Mapping[str, Any],
    index: int,
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Tuple[str, bytes, str]:
    source = str(reference.get("url") or "").strip()
    if source.startswith("data:"):
        content, mime = _decode_data_url(source)
        extension = mimetypes.guess_extension(mime) or ".png"
        filename = f"reference_{index}{extension}"
    else:
        path = resolve_local_path(source)
        if not path or not os.path.isfile(path):
            raise AICostImageProtocolError(400, f"aicost 参考图无法解析为本地文件：{source[:160]}")
        try:
            with open(path, "rb") as handle:
                content = handle.read(_MAX_REFERENCE_BYTES + 1)
        except OSError as exc:
            raise AICostImageProtocolError(400, f"读取 aicost 参考图失败：{exc}") from exc
        mime = _mime_type(path, content_type_for_path)
        filename = os.path.basename(path) or f"reference_{index}.png"
    if not content:
        raise AICostImageProtocolError(400, "aicost 参考图内容为空")
    if len(content) > _MAX_REFERENCE_BYTES:
        raise AICostImageProtocolError(400, "aicost 单张参考图不能超过 30MB")
    if not mime.startswith("image/"):
        raise AICostImageProtocolError(400, f"aicost 参考图类型无效：{mime}")
    return filename, content, mime


def _json_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    result = {str(key): str(value) for key, value in headers.items() if str(key).lower() != "content-type"}
    result["Content-Type"] = "application/json"
    return result


def _multipart_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    return {str(key): str(value) for key, value in headers.items() if str(key).lower() != "content-type"}


def _json_response(response: httpx.Response, action: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise AICostImageProtocolError(502, f"aicost {action}返回非 JSON 响应：{response.text[:500]}") from exc
    if not isinstance(payload, dict):
        raise AICostImageProtocolError(502, f"aicost {action}返回非 JSON 对象")
    return payload


def _create_error(response: httpx.Response, action: str) -> AICostImageProtocolError:
    text = (response.text or response.reason_phrase or "").strip()[:500]
    if response.status_code >= 500:
        return AICostImageProtocolError(
            response.status_code,
            f"aicost {action}返回 HTTP {response.status_code}，已停止自动重试以避免重复扣费：{text}",
        )
    return AICostImageProtocolError(response.status_code, f"aicost {action}失败：{text}")


def _image_field_rejected(response: httpx.Response) -> bool:
    if response.status_code not in {400, 415, 422}:
        return False
    text = (response.text or "").lower()
    image_markers = ("image[]", '"image"', "'image'", "image field", "image字段")
    reject_markers = (
        "unknown", "unsupported", "invalid", "unexpected", "unrecognized",
        "field", "multipart", "不支持", "无效",
    )
    image_hint = any(marker in text for marker in image_markers)
    reject_hint = any(marker in text for marker in reject_markers)
    return image_hint and reject_hint


def _nested_task_id(values: Sequence[Any], depth: int) -> str:
    for item in values:
        result = _task_id(item, depth + 1)
        if result:
            return result
    return ""


def _mapping_task_id(value: Mapping[str, Any], depth: int) -> str:
    for key in ("task_id", "taskId", "generation_id"):
        item = value.get(key)
        if isinstance(item, (str, int)) and str(item).strip():
            return str(item).strip()
    generic_id = value.get("id")
    if generic_id and any(key in value for key in ("status", "state", "task_status")):
        return str(generic_id).strip()
    return _nested_task_id(list(value.values()), depth)


def _task_id(value: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, Mapping):
        return _mapping_task_id(value, depth)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _nested_task_id(value, depth)
    return ""


def _status(value: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, Mapping):
        for key in ("status", "task_status", "state"):
            item = str(value.get(key) or "").strip().upper()
            if item:
                return item
        for key in ("data", "detail", "result", "output"):
            result = _status(value.get(key), depth + 1)
            if result:
                return result
    return ""


def _failure_reason(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("fail_reason", "message", "reason", "error", "detail"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, Mapping):
                nested = _failure_reason(item)
                if nested:
                    return nested
        for key in ("data", "result"):
            nested = _failure_reason(value.get(key))
            if nested:
                return nested
    return "上游未提供失败原因"


def _base64_mime(value: str) -> str:
    sample = re.sub(r"\s+", "", str(value or ""))[:128]
    if not sample:
        return ""
    try:
        head = base64.b64decode(sample + "=" * (-len(sample) % 4), validate=False)
    except Exception:
        return ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return ""


def _add_image(
    found: List[ImagePayload],
    seen: set,
    kind: str,
    value: Any,
    mime: str = "image/png",
) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if text.startswith("data:image/"):
        header, separator, encoded = text.partition(",")
        if not separator:
            return
        kind, text = "b64", encoded.strip()
        mime = header.split(";", 1)[0].replace("data:", "", 1) or mime
    key = (kind, text)
    if key not in seen:
        seen.add(key)
        item = {"type": kind, "value": text}
        if kind == "b64":
            item["mime_type"] = _base64_mime(text) or mime or "image/png"
        found.append(item)


def _walk_image_string(value: str, key_hint: str, found: List[ImagePayload], seen: set) -> None:
    if key_hint in {"b64_json", "image_base64", "base64"}:
        _add_image(found, seen, "b64", value)
    elif key_hint in {"url", "image_url"}:
        _add_image(found, seen, "url", value)
    elif key_hint in {"image", "output", "result"}:
        kind = "url" if value.startswith(("http://", "https://", "data:image/")) else "b64"
        _add_image(found, seen, kind, value)
    elif key_hint in {"content", "text"}:
        for url in _HTTP_URL_RE.findall(value):
            _add_image(found, seen, "url", url.rstrip(".,;:)]}"))


def _walk_image_mapping(
    value: Mapping[str, Any],
    found: List[ImagePayload],
    seen: set,
    depth: int,
) -> None:
    inline = value.get("inlineData") or value.get("inline_data")
    if isinstance(inline, Mapping) and inline.get("data"):
        mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
        _add_image(found, seen, "b64", inline.get("data"), mime)
    for key, item in value.items():
        if key in _IMAGE_DIRECT_KEYS or key in _IMAGE_CONTAINER_KEYS:
            _walk_images(item, key, found, seen, depth + 1)


def _walk_images(
    value: Any,
    key_hint: str,
    found: List[ImagePayload],
    seen: set,
    depth: int,
) -> None:
    if value is None or depth > 10:
        return
    if isinstance(value, str):
        _walk_image_string(value, key_hint, found, seen)
    elif isinstance(value, Mapping):
        _walk_image_mapping(value, found, seen, depth)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _walk_images(item, key_hint, found, seen, depth + 1)


def _extract_images(payload: Any) -> List[ImagePayload]:
    found: List[ImagePayload] = []
    seen = set()
    _walk_images(payload, "", found, seen, 0)
    return found


async def _query_task_with_client(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    v1_root: str,
    headers: Mapping[str, str],
) -> Dict[str, Any]:
    quoted = urllib.parse.quote(str(task_id), safe="")
    response = await client.get(f"{v1_root}/images/generations/{quoted}", headers=dict(headers))
    if response.status_code >= 400:
        raise AICostImageProtocolError(
            response.status_code,
            f"aicost 图片任务查询失败：{response.text[:500]}",
            upstream_task_id=task_id,
        )
    try:
        return _json_response(response, "图片任务查询")
    except AICostImageProtocolError as exc:
        exc.upstream_task_id = str(task_id)
        raise


async def _poll_task(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    v1_root: str,
    headers: Mapping[str, str],
    timeout: float,
    interval: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_payload: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last_payload = await _query_task_with_client(
                client,
                task_id,
                v1_root=v1_root,
                headers=headers,
            )
        except httpx.TransportError:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            continue
        status = _status(last_payload)
        if status in _FAILURE_STATUSES:
            raise AICostImageProtocolError(
                502,
                f"aicost 图片任务失败：{_failure_reason(last_payload)}",
                upstream_task_id=task_id,
            )
        if _extract_images(last_payload):
            return last_payload
        if status in _SUCCESS_STATUSES:
            raise AICostImageProtocolError(
                502,
                "aicost 图片任务已完成，但没有返回图片",
                upstream_task_id=task_id,
            )
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raise AICostImageProtocolError(
        504,
        f"aicost 图片任务超时：task_id={task_id}",
        upstream_task_id=task_id,
    )


async def _post_gpt(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    *,
    v1_root: str,
    headers: Mapping[str, str],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Dict[str, Any]:
    model, prompt, size = _model(request), _prompt(request), _size(request)
    output_format = str(request.get("output_format") or "jpeg").strip().lower()
    if output_format not in {"jpeg", "png"}:
        raise AICostImageProtocolError(400, "aicost output_format 仅支持 jpeg 或 png")
    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": "auto",
        "output_format": output_format,
        "moderation": "auto",
    }
    references = _references(request)
    try:
        if not references:
            response = await client.post(
                f"{v1_root}/images/generations",
                headers=_json_headers(headers),
                json=body,
            )
        else:
            prepared = [
                _reference_file(item, index, resolve_local_path, content_type_for_path)
                for index, item in enumerate(references, 1)
            ]
            form = {key: str(value) for key, value in body.items()}
            response = await client.post(
                f"{v1_root}/images/edits",
                headers=_multipart_headers(headers),
                data=form,
                files=[("image[]", item) for item in prepared],
            )
            if _image_field_rejected(response):
                response = await client.post(
                    f"{v1_root}/images/edits",
                    headers=_multipart_headers(headers),
                    data=form,
                    files=[("image", item) for item in prepared],
                )
    except httpx.TransportError as exc:
        raise AICostImageProtocolError(
            502,
            f"aicost 图片创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        raise _create_error(response, "图片创建")
    return _json_response(response, "图片创建")


async def _post_gemini(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    *,
    api_root: str,
    headers: Mapping[str, str],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
) -> Dict[str, Any]:
    model, prompt, size = _model(request), _prompt(request), _size(request)
    image_size, aspect_ratio = _gemini_size_config(request, size)
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    for index, reference in enumerate(_references(request), 1):
        _, content, mime = _reference_file(
            reference,
            index,
            resolve_local_path,
            content_type_for_path,
        )
        parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(content).decode("ascii")}})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"imageSize": image_size, "aspectRatio": aspect_ratio},
        },
    }
    quoted_model = urllib.parse.quote(model, safe="-._~")
    try:
        response = await client.post(
            f"{api_root}/v1beta/models/{quoted_model}:generateContent",
            headers=_json_headers(headers),
            json=body,
        )
    except httpx.TransportError as exc:
        raise AICostImageProtocolError(
            502,
            f"aicost Gemini 图片创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        raise _create_error(response, "Gemini 图片创建")
    return _json_response(response, "Gemini 图片创建")


async def generate_aicost_image(
    request: Mapping[str, Any],
    *,
    base_url: str,
    headers: Mapping[str, str],
    resolve_local_path: ResolveLocalPath,
    content_type_for_path: ContentTypeForPath,
    request_timeout: Any = 3600.0,
    poll_timeout: float = 3600.0,
    poll_interval: float = 2.0,
) -> Tuple[ImagePayload, Dict[str, Any]]:
    """提交一次 aicost 图片请求，必要时轮询，并返回首张图片与最终原始响应。"""
    timeout = _timeout_seconds(poll_timeout, 3600.0, "图片轮询")
    interval = max(0.5, _timeout_seconds(poll_interval, 2.0, "图片轮询间隔"))
    v1_root, api_root = _api_roots(base_url)
    model = _model(request)
    async with _client(request_timeout) as client:
        if model in _GEMINI_IMAGE_MODELS:
            raw = await _post_gemini(
                client,
                request,
                api_root=api_root,
                headers=headers,
                resolve_local_path=resolve_local_path,
                content_type_for_path=content_type_for_path,
            )
        else:
            raw = await _post_gpt(
                client,
                request,
                v1_root=v1_root,
                headers=headers,
                resolve_local_path=resolve_local_path,
                content_type_for_path=content_type_for_path,
            )
        images = _extract_images(raw)
        status = _status(raw)
        task_id = _task_id(raw)
        if status in _FAILURE_STATUSES:
            raise AICostImageProtocolError(
                502,
                f"aicost 图片生成失败：{_failure_reason(raw)}",
                upstream_task_id=task_id,
            )
        if images:
            return images[0], raw
        if not task_id:
            raise AICostImageProtocolError(
                502,
                "aicost 图片接口未返回图片或任务 ID，已停止处理以避免重复扣费",
            )
        raw = await _poll_task(
            client,
            task_id,
            v1_root=v1_root,
            headers=headers,
            timeout=timeout,
            interval=interval,
        )
        images = _extract_images(raw)
        if not images:
            raise AICostImageProtocolError(
                502,
                "aicost 图片任务完成但没有返回图片",
                upstream_task_id=task_id,
            )
        return images[0], raw


async def query_aicost_image_task(
    task_id: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    request_timeout: Any = 300.0,
) -> Dict[str, Any]:
    """查询一次已有 aicost 图片任务，绝不重新提交创建请求。"""
    value = str(task_id or "").strip()
    if not value:
        raise AICostImageProtocolError(400, "aicost 图片任务 ID 不能为空")
    v1_root, _ = _api_roots(base_url)
    try:
        async with _client(request_timeout) as client:
            raw = await _query_task_with_client(client, value, v1_root=v1_root, headers=headers)
    except httpx.TransportError as exc:
        raise AICostImageProtocolError(
            502,
            f"查询 aicost 图片任务失败：{exc}",
            upstream_task_id=value,
        ) from exc
    status = _status(raw)
    if status in _FAILURE_STATUSES:
        raise AICostImageProtocolError(
            502,
            f"aicost 图片任务失败：{_failure_reason(raw)}",
            upstream_task_id=value,
        )
    if status in _SUCCESS_STATUSES and not _extract_images(raw):
        raise AICostImageProtocolError(
            502,
            "aicost 图片任务已完成，但没有返回图片",
            upstream_task_id=value,
        )
    return raw
