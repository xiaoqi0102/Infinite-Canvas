"""MegabyAI ``/v1/videos`` 视频协议适配。

本模块负责 MegabyAI 的请求校验、任务提交、轮询恢复和结果解析。
宿主只通过回调提供参考素材公网化、任务进度记录和视频落盘能力。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import httpx

from .common import canonical_video_api_root, humanize_video_task_failure, resolve_video_download_url


MEGABYAI_VIDEO_REQUEST_MODE = "megabyai-v1-videos"
MEGABYAI_OFFICIAL_HOSTNAMES = {"newapi.megabyai.cc", "cn.megabyai.cc"}
MEGABYAI_ASPECT_RATIOS = {"16:9", "9:16", "1:1"}
MEGABYAI_RESOLUTIONS = {"480p", "720p"}

_SUCCESS_STATUSES = {
    "SUCCESS", "SUCCEED", "SUCCEEDED", "COMPLETED", "COMPLETE",
    "DONE", "FINISHED", "FINISH", "OK", "READY",
}
_FAILURE_STATUSES = {
    "FAILURE", "FAILED", "FAIL", "ERROR", "ERRORED",
    "CANCELED", "CANCELLED", "TIMEOUT", "TIMEDOUT", "REJECTED", "EXPIRED",
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
    "creations", "content", "metadata", "output", "video",
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


class MegabyAIProtocolError(Exception):
    """带 HTTP 状态语义的 MegabyAI 协议错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
PublicReferenceUrl = Callable[[Any], Awaitable[str]]
SaveVideo = Callable[[str], Awaitable[str]]


def _provider_root(base_url: Any) -> str:
    return canonical_video_api_root(base_url)


def is_megabyai_official_provider(provider: Optional[Mapping[str, Any]]) -> bool:
    base_url = str((provider or {}).get("base_url") or "").strip()
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "").lower() in MEGABYAI_OFFICIAL_HOSTNAMES
    except Exception:
        return False


def _duration(value: Any) -> int:
    try:
        duration = int(value)
    except Exception as exc:
        raise MegabyAIProtocolError(400, "MegabyAI 视频时长必须是 4 到 15 秒的整数") from exc
    if duration < 4 or duration > 15:
        raise MegabyAIProtocolError(400, "MegabyAI 视频时长仅支持 4 到 15 秒")
    return duration


def _ratio(value: Any) -> str:
    ratio = str(value or "16:9").strip()
    if ratio not in MEGABYAI_ASPECT_RATIOS:
        raise MegabyAIProtocolError(
            400,
            f"MegabyAI 视频比例不支持：{ratio or '(empty)'}；仅支持 16:9、9:16、1:1",
        )
    return ratio


def _resolution(value: Any) -> str:
    resolution = str(value or "720p").strip().lower()
    if resolution not in MEGABYAI_RESOLUTIONS:
        raise MegabyAIProtocolError(
            400,
            f"MegabyAI 视频分辨率不支持：{resolution or '(empty)'}；仅支持 480p、720p",
        )
    return resolution


async def _public_reference_url(
    value: Any,
    label: str,
    public_reference_url: PublicReferenceUrl,
) -> str:
    text = _reference_text(value)
    if not text:
        return ""
    if text.startswith("asset://"):
        raise MegabyAIProtocolError(
            400,
            f"MegabyAI {label}只支持公网 HTTP/HTTPS URL，不支持 asset:// 认证素材",
        )
    try:
        url = str(await public_reference_url(value) or "").strip()
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        status_code = int(getattr(exc, "status_code", 400) or 400)
        raise MegabyAIProtocolError(status_code, f"MegabyAI {label}无法转换为公网 URL：{detail}") from exc
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MegabyAIProtocolError(400, f"MegabyAI {label}不是有效的公网 HTTP/HTTPS URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "::1", "0.0.0.0"} or re.match(
        r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)", host
    ):
        raise MegabyAIProtocolError(400, f"MegabyAI {label}不能使用本机或内网地址")
    return url


async def _reference_urls(
    values: Sequence[Any],
    label: str,
    limit: int,
    public_reference_url: PublicReferenceUrl,
) -> List[str]:
    cleaned = [value for value in (values or []) if _reference_text(value)]
    if len(cleaned) > limit:
        raise MegabyAIProtocolError(400, f"MegabyAI {label}最多支持 {limit} 个，当前为 {len(cleaned)} 个")
    return [
        await _public_reference_url(value, label, public_reference_url)
        for value in cleaned
    ]


def _reference_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("url")
    else:
        value = getattr(value, "url", value)
    return str(value or "").strip()


def _request_images(request: Mapping[str, Any]) -> List[Dict[str, Any]]:
    images = []
    for item in request.get("images") or []:
        if isinstance(item, Mapping):
            value = str(item.get("url") or "").strip()
            if value:
                images.append(dict(item))
        else:
            value = str(getattr(item, "url", "") or "").strip()
            if value:
                images.append({"url": value})
    return images


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


def _status(payload: Mapping[str, Any]) -> str:
    nodes = [payload]
    for key in ("data", "detail", "result"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            nodes.append(value)
    for node in nodes:
        for key in ("status", "task_status", "state"):
            value = str(node.get(key) or "").strip().upper()
            if value:
                return value
    return ""


def _flatten_error_text(value: Any, parts: List[str], depth: int = 0) -> None:
    if value is None or depth > 8:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            parts.append(str(key))
            _flatten_error_text(item, parts, depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _flatten_error_text(item, parts, depth + 1)
        return
    parts.append(str(value))


def _is_terminal_error(value: Any) -> bool:
    parts: List[str] = []
    _flatten_error_text(value, parts)
    text = "\n".join(part for part in parts if part).lower()
    return bool(text) and any(re.search(pattern, text, re.IGNORECASE) for pattern in _TERMINAL_ERROR_PATTERNS)


def _failure_reason(payload: Mapping[str, Any]) -> str:
    values: List[str] = []

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
            for key in (
                "fail_reason", "failure_reason", "message", "msg", "reason",
                "code", "error_code", "err_code",
            ):
                item = value.get(key)
                if isinstance(item, str) and item.strip() and item.strip() not in values:
                    values.append(item.strip())
                elif isinstance(item, Mapping):
                    walk(item, depth + 1)
            for key in ("error", "data", "detail", "result", "response"):
                if key in value:
                    walk(value.get(key), depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item, depth + 1)

    walk(payload)
    return "；".join(values[:4]) or str(payload)


def _retry_after_seconds(response: httpx.Response, poll_timeout: float) -> Optional[float]:
    values: List[float] = []
    header = str(response.headers.get("retry-after") or "").strip()
    try:
        if header:
            values.append(float(header))
    except Exception:
        pass
    try:
        payload = response.json()
    except Exception:
        payload = response.text

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in {"retry_after", "retryafter"}:
                    try:
                        values.append(float(item))
                    except Exception:
                        pass
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
            ):
                match = re.search(pattern, value, re.IGNORECASE)
                if match:
                    values.append(float(match.group(1)))

    walk(payload)
    positive = [value for value in values if value > 0]
    if not positive:
        return None
    return min(max(positive), max(5.0, float(poll_timeout)))


def _json_response(response: httpx.Response, action: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise MegabyAIProtocolError(
            502,
            f"MegabyAI {action}返回非 JSON 响应：{response.text[:500]}",
        ) from exc
    if not isinstance(payload, dict):
        raise MegabyAIProtocolError(502, f"MegabyAI {action}返回非 JSON 对象：{payload}")
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
        raise MegabyAIProtocolError(502, f"MegabyAI 视频生成成功但没有返回视频：{payload}")
    local_urls = []
    for url in urls:
        local_url = str(await save_video(url) or "").strip()
        if not local_url:
            raise MegabyAIProtocolError(502, f"MegabyAI 视频下载失败：{url}")
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
    last_payload: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        if delay:
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        try:
            response = await client.get(status_url, headers=dict(headers))
        except httpx.TransportError as exc:
            raise MegabyAIProtocolError(502, f"MegabyAI 视频任务查询失败：{exc}") from exc
        retry_after = _retry_after_seconds(response, poll_timeout)
        if response.status_code >= 400:
            try:
                error_payload: Any = response.json()
            except Exception:
                error_payload = {"error": response.text}
            if _is_terminal_error(error_payload):
                raise MegabyAIProtocolError(
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
            raise MegabyAIProtocolError(response.status_code, f"MegabyAI 视频任务查询失败：{response.text[:500]}")
        raw = _json_response(response, "视频任务查询")
        last_payload = raw
        state = _status(raw)
        _report(progress, {"status": "polling", "raw_last": raw})
        if state in _FAILURE_STATUSES or _is_terminal_error(raw):
            raise MegabyAIProtocolError(502, humanize_video_task_failure(_failure_reason(raw)))
        urls = _video_urls(raw, base_url)
        if state in _SUCCESS_STATUSES or (state not in _FAILURE_STATUSES and urls):
            return await _save_result(raw, task_id, base_url, save_video)
        delay = max(float(poll_interval), retry_after or 0.0)
    raise MegabyAIProtocolError(504, f"MegabyAI 视频生成任务超时：{last_payload or task_id}")


async def generate_megabyai_video(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    public_reference_url: PublicReferenceUrl,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = 8.0,
) -> Dict[str, Any]:
    """提交 MegabyAI 视频任务并等待完成。"""
    root = _provider_root(base_url)
    if not root:
        raise MegabyAIProtocolError(400, "MegabyAI 未配置 Base URL")
    image_urls = await _reference_urls(
        _request_images(request), "参考图片", 9, public_reference_url
    )
    video_urls = await _reference_urls(
        request.get("videos") or [], "参考视频", 3, public_reference_url
    )
    audio_urls = await _reference_urls(
        request.get("audios") or [], "参考音频", 3, public_reference_url
    )
    body: Dict[str, Any] = {
        "model": str(request.get("model") or "videos-mini").strip() or "videos-mini",
        "prompt": str(request.get("prompt") or ""),
        "duration": _duration(request.get("duration")),
        "ratio": _ratio(request.get("aspect_ratio")),
        "resolution": _resolution(request.get("resolution")),
    }
    if image_urls:
        body["referenceImages"] = image_urls
    if video_urls:
        body["referenceVideos"] = video_urls
    if audio_urls:
        body["referenceAudios"] = audio_urls
    submit_url = f"{root}/v1/videos"
    try:
        response = await client.post(submit_url, headers=dict(headers), json=body)
    except httpx.TransportError as exc:
        raise MegabyAIProtocolError(
            502,
            f"MegabyAI 创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        raise MegabyAIProtocolError(response.status_code, f"MegabyAI 视频创建失败：{response.text[:500]}")
    raw = _json_response(response, "视频创建")
    state = _status(raw)
    if state in _FAILURE_STATUSES or _is_terminal_error(raw):
        raise MegabyAIProtocolError(502, humanize_video_task_failure(_failure_reason(raw)))
    task_id = _task_id(raw)
    if not task_id:
        raise MegabyAIProtocolError(
            502,
            f"MegabyAI 视频接口未返回任务 ID，已停止处理以避免重复扣费：{raw}",
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


async def resume_megabyai_video(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = 8.0,
) -> Dict[str, Any]:
    """只恢复查询已有 MegabyAI 任务，绝不重新提交创建请求。"""
    root = _provider_root(base_url)
    if not root:
        raise MegabyAIProtocolError(400, "MegabyAI 未配置 Base URL")
    return await _poll_video(
        client, str(task_id), root, headers, progress, save_video, poll_timeout, poll_interval
    )
