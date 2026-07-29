"""MeAI ``/v1/videos`` 视频协议适配。

本模块负责 MeAI 的请求校验、任务提交、轮询恢复和结果解析。
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

from .common import (
    canonical_video_api_root,
    humanize_video_task_failure,
    resolve_video_download_url,
    submit_video_http_request,
)


MEAI_VIDEO_REQUEST_MODE = "meai-v1-videos"
MEAI_OFFICIAL_HOSTNAMES = {"api.meai.cloud"}
MEAI_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}
MEAI_RESOLUTIONS = {"720p", "1080p"}
MEAI_MAX_IMAGES = 9
MEAI_MAX_VIDEOS = 3
MEAI_MAX_AUDIOS = 3
MEAI_POLL_INTERVAL = 20.0

_SUCCESS_STATUSES = {"SUCCEEDED", "SUCCESS", "COMPLETED", "COMPLETE", "DONE", "FINISHED"}
_PENDING_STATUSES = {"PENDING", "RUNNING", "QUEUED", "IN_PROGRESS", "PROCESSING", "SUBMITTED"}
_TRANSIENT_QUERY_HTTP_STATUSES = {408, 425, 429}


class MeAIProtocolError(Exception):
    """带 HTTP 状态语义的 MeAI 协议错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
PublicReferenceUrl = Callable[[Any], Awaitable[str]]
SaveVideo = Callable[[str], Awaitable[str]]


def _provider_root(base_url: Any) -> str:
    return canonical_video_api_root(base_url)


def is_meai_official_provider(provider: Optional[Mapping[str, Any]]) -> bool:
    base_url = str((provider or {}).get("base_url") or "").strip()
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "").lower() in MEAI_OFFICIAL_HOSTNAMES
    except Exception:
        return False


def _duration(value: Any) -> int:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+", text):
        raise MeAIProtocolError(400, "MeAI 视频时长必须是正整数秒")
    duration = int(text)
    if duration < 1:
        raise MeAIProtocolError(400, "MeAI 视频时长必须大于 0 秒")
    return duration


def _ratio(value: Any) -> str:
    ratio = str(value or "16:9").strip()
    if ratio not in MEAI_ASPECT_RATIOS:
        raise MeAIProtocolError(
            400,
            f"MeAI 视频比例不支持：{ratio or '(empty)'}；仅支持 1:1、16:9、9:16、4:3、3:4",
        )
    return ratio


def _resolution(value: Any) -> str:
    resolution = str(value or "720p").strip().lower()
    if resolution not in MEAI_RESOLUTIONS:
        raise MeAIProtocolError(
            400,
            f"MeAI 视频分辨率不支持：{resolution or '(empty)'}；仅支持 720p、1080p",
        )
    return resolution


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


async def _public_reference_url(
    value: Any,
    label: str,
    public_reference_url: PublicReferenceUrl,
) -> str:
    text = _reference_text(value)
    if not text:
        return ""
    if text.startswith("asset://"):
        raise MeAIProtocolError(400, f"MeAI {label}只支持公网 HTTP/HTTPS URL，不支持 asset:// 认证素材")
    try:
        url = str(await public_reference_url(value) or "").strip()
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        status_code = int(getattr(exc, "status_code", 400) or 400)
        raise MeAIProtocolError(status_code, f"MeAI {label}无法转换为公网 URL：{detail}") from exc
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MeAIProtocolError(400, f"MeAI {label}不是有效的公网 HTTP/HTTPS URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "::1", "0.0.0.0"} or re.match(
        r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)", host
    ):
        raise MeAIProtocolError(400, f"MeAI {label}不能使用本机或内网地址")
    return url


async def _media_items(
    request: Mapping[str, Any],
    public_reference_url: PublicReferenceUrl,
) -> List[Dict[str, str]]:
    images = _request_images(request)
    videos = [item for item in (request.get("videos") or []) if _reference_text(item)]
    audios = [item for item in (request.get("audios") or []) if _reference_text(item)]
    if len(images) > MEAI_MAX_IMAGES:
        raise MeAIProtocolError(400, f"MeAI 参考图片最多支持 {MEAI_MAX_IMAGES} 张，当前为 {len(images)} 张")
    if len(videos) > MEAI_MAX_VIDEOS:
        raise MeAIProtocolError(400, f"MeAI 参考视频最多支持 {MEAI_MAX_VIDEOS} 个，当前为 {len(videos)} 个")
    if len(audios) > MEAI_MAX_AUDIOS:
        raise MeAIProtocolError(400, f"MeAI 参考音频最多支持 {MEAI_MAX_AUDIOS} 个，当前为 {len(audios)} 个")

    media: List[Dict[str, str]] = []
    frame_roles = []
    for item in images:
        role = str(item.get("role") or "").strip().lower()
        media_type = role if role in {"first_frame", "last_frame"} else "reference_image"
        if media_type in {"first_frame", "last_frame"}:
            if media_type in frame_roles:
                raise MeAIProtocolError(400, f"MeAI {media_type} 只能提供一张图片")
            frame_roles.append(media_type)
        media.append({
            "type": media_type,
            "url": await _public_reference_url(item, "参考图片", public_reference_url),
        })
    for item in videos:
        media.append({
            "type": "reference_video",
            "url": await _public_reference_url(item, "参考视频", public_reference_url),
        })
    for item in audios:
        media.append({
            "type": "reference_voice",
            "url": await _public_reference_url(item, "参考音频", public_reference_url),
        })
    return media


def _task_id(payload: Mapping[str, Any]) -> str:
    for key in ("task_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _status(payload: Mapping[str, Any]) -> str:
    return str(payload.get("status") or "").strip().upper()


def _failure_reason(payload: Mapping[str, Any]) -> str:
    status = str(payload.get("status") or "").strip()
    if status.upper().startswith("FAILED"):
        detail = status.split(":", 1)[1].strip() if ":" in status else status
        if detail and detail.upper() != "FAILED":
            return detail
    for key in ("error", "message", "msg", "detail", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested = _failure_reason(value)
            if nested:
                return nested
    return status or str(payload)


def _video_url(payload: Mapping[str, Any], base_url: str = "") -> str:
    value = payload.get("object")
    if isinstance(value, str):
        return resolve_video_download_url(value.strip(), base_url)
    return ""


def _retry_after_seconds(response: httpx.Response, poll_timeout: float) -> Optional[float]:
    value = str(response.headers.get("retry-after") or "").strip()
    try:
        retry_after = float(value)
    except Exception:
        return None
    if retry_after <= 0:
        return None
    return min(retry_after, max(MEAI_POLL_INTERVAL, float(poll_timeout)))


def _json_response(response: httpx.Response, action: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise MeAIProtocolError(502, f"MeAI {action}返回非 JSON 响应：{response.text[:500]}") from exc
    if not isinstance(payload, dict):
        raise MeAIProtocolError(502, f"MeAI {action}返回非 JSON 对象：{payload}")
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
    url = _video_url(payload, base_url)
    if not url:
        raise MeAIProtocolError(502, f"MeAI 视频生成成功但没有返回 object 视频地址：{payload}")
    local_url = str(await save_video(url) or "").strip()
    if not local_url:
        raise MeAIProtocolError(502, f"MeAI 视频下载失败：{url}")
    return {"videos": [local_url], "task_id": task_id, "raw": dict(payload)}


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
    delay = max(MEAI_POLL_INTERVAL, float(poll_interval))
    last_payload: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        try:
            response = await client.get(status_url, headers=dict(headers))
        except (httpx.TransportError, TimeoutError) as exc:
            _report(progress, {
                "status": "polling",
                "message": f"MeAI 视频任务查询暂时失败，将自动重试：{str(exc).strip() or type(exc).__name__}",
                "next_poll_at": time.time() + delay,
            })
            continue
        retry_after = _retry_after_seconds(response, poll_timeout)
        if response.status_code >= 400:
            try:
                error_payload: Any = response.json()
            except Exception:
                error_payload = {"error": response.text}
            error_status = _status(error_payload) if isinstance(error_payload, Mapping) else ""
            if error_status.startswith("FAILED"):
                raise MeAIProtocolError(
                    response.status_code,
                    humanize_video_task_failure(_failure_reason(error_payload)),
                )
            if response.status_code in _TRANSIENT_QUERY_HTTP_STATUSES or response.status_code >= 500:
                delay = max(MEAI_POLL_INTERVAL, float(poll_interval), retry_after or 0.0)
                last_payload = error_payload if isinstance(error_payload, dict) else {"error": error_payload}
                _report(progress, {
                    "status": "polling",
                    "message": f"MeAI 视频任务查询暂时失败（HTTP {response.status_code}），将自动重试",
                    "retry_after": retry_after,
                    "next_poll_at": time.time() + delay,
                    "raw_last": last_payload,
                })
                continue
            raise MeAIProtocolError(response.status_code, f"MeAI 视频任务查询失败：{_failure_reason(error_payload)}")
        raw = _json_response(response, "视频任务查询")
        last_payload = raw
        state = _status(raw)
        _report(progress, {"status": "polling", "raw_last": raw})
        if state.startswith("FAILED"):
            raise MeAIProtocolError(502, humanize_video_task_failure(_failure_reason(raw)))
        if state in _SUCCESS_STATUSES:
            return await _save_result(raw, task_id, base_url, save_video)
        if state not in _PENDING_STATUSES:
            raise MeAIProtocolError(502, f"MeAI 视频任务返回未知状态：{raw}")
        delay = max(MEAI_POLL_INTERVAL, float(poll_interval), retry_after or 0.0)
    raise MeAIProtocolError(504, f"MeAI 视频生成任务超时：{last_payload or task_id}")


async def generate_meai_video(
    client: httpx.AsyncClient,
    request: Mapping[str, Any],
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    public_reference_url: PublicReferenceUrl,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = MEAI_POLL_INTERVAL,
) -> Dict[str, Any]:
    """提交 MeAI 视频任务并等待完成。"""
    root = _provider_root(base_url)
    if not root:
        raise MeAIProtocolError(400, "MeAI 未配置 Base URL")
    media = await _media_items(request, public_reference_url)
    parameters: Dict[str, Any] = {
        "resolution": _resolution(request.get("resolution")),
        "ratio": _ratio(request.get("aspect_ratio")),
        "duration": _duration(request.get("duration")),
    }
    body: Dict[str, Any] = {
        "model": str(request.get("model") or "sd-2").strip() or "sd-2",
        "input": {"prompt": str(request.get("prompt") or "")},
        "parameters": parameters,
    }
    if media:
        body["input"]["media"] = media
    submit_url = f"{root}/v1/videos"
    try:
        response = await submit_video_http_request(
            client,
            progress=progress,
            url=submit_url,
            headers=dict(headers),
            json_body=body,
            context={"protocol": "meai", "model": body.get("model")},
        )
    except httpx.TransportError as exc:
        raise MeAIProtocolError(
            502,
            f"MeAI 创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
        ) from exc
    if response.status_code >= 400:
        try:
            failure = _failure_reason(response.json())
        except Exception:
            failure = response.text[:500]
        raise MeAIProtocolError(response.status_code, f"MeAI 视频创建失败：{failure}")
    raw = _json_response(response, "视频创建")
    state = _status(raw)
    if state.startswith("FAILED"):
        raise MeAIProtocolError(502, humanize_video_task_failure(_failure_reason(raw)))
    task_id = _task_id(raw)
    if not task_id:
        raise MeAIProtocolError(502, f"MeAI 视频接口未返回任务 ID，已停止处理以避免重复扣费：{raw}")
    _report(progress, {
        "status": "polling",
        "upstream_task_id": task_id,
        "task_id": task_id,
        "submit_url": submit_url,
        "raw_submit": raw,
    })
    if state in _SUCCESS_STATUSES:
        return await _save_result(raw, task_id, root, save_video)
    if not state:
        raise MeAIProtocolError(502, f"MeAI 视频创建未返回任务状态：{raw}")
    if state not in _PENDING_STATUSES:
        raise MeAIProtocolError(502, f"MeAI 视频创建返回未知状态：{raw}")
    return await _poll_video(
        client,
        task_id,
        root,
        headers,
        progress,
        save_video,
        poll_timeout,
        poll_interval,
    )


async def resume_meai_video(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    base_url: str,
    headers: Mapping[str, str],
    progress: ProgressCallback,
    save_video: SaveVideo,
    poll_timeout: float,
    poll_interval: float = MEAI_POLL_INTERVAL,
) -> Dict[str, Any]:
    """只恢复查询已有 MeAI 任务，绝不重新提交创建请求。"""
    root = _provider_root(base_url)
    if not root:
        raise MeAIProtocolError(400, "MeAI 未配置 Base URL")
    return await _poll_video(
        client,
        str(task_id),
        root,
        headers,
        progress,
        save_video,
        poll_timeout,
        poll_interval,
    )
