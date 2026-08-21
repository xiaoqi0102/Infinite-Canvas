"""七牛 Modelink Fal Queue 异步图片协议适配。

插件只接收宿主准备好的公网参考图 URL，负责模型参数映射、异步任务提交、
状态轮询和结果归一化。素材上传、本地路径解析与图片落盘均由宿主处理。
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx


QINIU_IMAGE_REQUEST_MODE = "qiniu-image"
QINIU_IMAGE_OFFICIAL_HOSTNAMES = {"api.qnaigc.com", "api.modelink.ai"}

_MODEL_ROUTES = {
    "gemini-3-pro-image-preview": "/queue/fal-ai/gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview": "/queue/fal-ai/gemini-3.1-flash-image-preview",
    "gpt-image-2": "/queue/openai/gpt-image-2",
}
_GEMINI_MODELS = {
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
}
_GEMINI_ASPECT_RATIOS = (
    "21:9", "16:9", "3:2", "4:3", "5:4", "1:1",
    "4:5", "3:4", "2:3", "9:16",
)
_FLASH_EXTRA_ASPECT_RATIOS = ("4:1", "1:4", "8:1", "1:8")
_PENDING_STATUSES = {"IN_QUEUE", "IN_PROGRESS", "QUEUED", "PENDING", "PROCESSING", "RUNNING"}
_SUCCESS_STATUSES = {"COMPLETED", "COMPLETE", "SUCCESS", "SUCCEEDED", "DONE", "FINISHED"}
_FAILURE_STATUSES = {"FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}
_REFERENCE_LIMIT = 10

ImageProgress = Optional[Callable[[Mapping[str, Any]], None]]
ImagePayload = Dict[str, Any]


class QiniuImageProtocolError(Exception):
    """带 HTTP 状态语义的七牛图片协议错误。"""

    def __init__(self, status_code: int, detail: str, upstream_task_id: str = ""):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)
        self.upstream_task_id = str(upstream_task_id or "").strip()


def is_qiniu_image_official_provider(provider: Mapping[str, Any] | None) -> bool:
    """仅对文档列出的七牛 Modelink API 主机名启用自动识别。"""
    try:
        host = urllib.parse.urlsplit(str((provider or {}).get("base_url") or "").strip()).hostname
    except Exception:
        return False
    return str(host or "").lower() in QINIU_IMAGE_OFFICIAL_HOSTNAMES


def _api_root(base_url: Any) -> str:
    value = str(base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise QiniuImageProtocolError(400, "七牛生图未配置合法的 HTTP/HTTPS Base URL")
    while urllib.parse.urlsplit(value).path.rstrip("/").endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


def _timeout_seconds(value: Any, default: float, label: str) -> float:
    if isinstance(value, httpx.Timeout):
        value = value.read if value.read is not None else default
    try:
        timeout = float(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise QiniuImageProtocolError(400, f"七牛生图{label}超时配置无效") from exc
    if timeout <= 0:
        raise QiniuImageProtocolError(400, f"七牛生图{label}超时必须大于 0 秒")
    return timeout


def _client(timeout: Any) -> httpx.AsyncClient:
    limits = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(
        connect=20.0,
        read=_timeout_seconds(timeout, 300.0, "请求"),
        write=120.0,
        pool=20.0,
    )
    return httpx.AsyncClient(timeout=limits, follow_redirects=True)


def _model(request: Mapping[str, Any] | str) -> str:
    raw = request.get("model") if isinstance(request, Mapping) else request
    value = str(raw or "").strip()
    if value not in _MODEL_ROUTES:
        supported = "、".join(sorted(_MODEL_ROUTES))
        raise QiniuImageProtocolError(
            400,
            f"七牛生图模型不受支持：{value or '(empty)'}；可用模型：{supported}",
        )
    return value


def _prompt(request: Mapping[str, Any]) -> str:
    value = str(request.get("prompt") or "").strip()
    if not value:
        raise QiniuImageProtocolError(400, "七牛生图提示词不能为空")
    return value


def _size_pair(value: Any) -> Tuple[int, int]:
    text = str(value or "").strip().lower().replace("*", "x")
    match = re.fullmatch(r"([1-9]\d{1,4})x([1-9]\d{1,4})", text)
    if not match:
        raise QiniuImageProtocolError(400, f"七牛生图尺寸无效：{text or '(empty)'}")
    return int(match.group(1)), int(match.group(2))


def _closest_aspect_ratio(width: int, height: int, model: str) -> str:
    requested = width / height
    allowed = list(_GEMINI_ASPECT_RATIOS)
    if model == "gemini-3.1-flash-image-preview":
        allowed.extend(_FLASH_EXTRA_ASPECT_RATIOS)

    def distance(value: str) -> float:
        left, right = (int(part) for part in value.split(":", 1))
        return abs((left / right) - requested)

    return min(allowed, key=distance)


def _gemini_resolution(request: Mapping[str, Any], width: int, height: int, model: str) -> str:
    value = str(request.get("resolution") or request.get("image_size") or "").strip().upper()
    allowed = {"1K", "2K", "4K"}
    if model == "gemini-3.1-flash-image-preview":
        allowed.add("0.5K")
    if value:
        if value not in allowed:
            raise QiniuImageProtocolError(400, f"七牛 Gemini 分辨率不受支持：{value}")
        return value
    pixels = width * height
    longest = max(width, height)
    if pixels >= 7_000_000 or longest >= 3400:
        return "4K"
    if pixels >= 3_000_000 or longest >= 1800:
        return "2K"
    return "1K"


def _gpt_image_size(width: int, height: int) -> Any:
    # Fal presets are fixed pixel sizes, not scalable aspect-ratio aliases.
    presets = {
        (1024, 1024): "square",
        (768, 1024): "portrait_4_3",
        (864, 1536): "portrait_16_9",
        (1024, 768): "landscape_4_3",
        (1536, 864): "landscape_16_9",
    }
    preset = presets.get((width, height))
    if preset:
        return preset
    # The shared canvas still uses 4096x4096 for its generic 4K square
    # option.  GPT Image 2's custom-size contract caps the square at
    # 2880x2880 (8,294,400 pixels), so normalize that legacy value to the
    # largest supported square instead of rejecting the request.
    if (width, height) == (4096, 4096):
        return {"width": 2880, "height": 2880}
    pixels = width * height
    if max(width, height) > 3840 or max(width / height, height / width) > 3:
        raise QiniuImageProtocolError(400, "七牛 GPT Image 2 尺寸最大边不能超过 3840，且长宽比不能超过 3:1")
    if pixels < 655360 or pixels > 8294400:
        raise QiniuImageProtocolError(400, "七牛 GPT Image 2 自定义尺寸总像素需在 655360 到 8294400 之间")
    return {"width": width, "height": height}


def _reference_urls(request: Mapping[str, Any]) -> Tuple[List[str], str]:
    values = request.get("reference_images") or request.get("images") or []
    image_urls: List[str] = []
    mask_url = ""
    for item in values:
        if isinstance(item, Mapping):
            value = str(item.get("url") or "").strip()
            role = str(item.get("role") or "").strip().lower()
            name = str(item.get("name") or "").strip().lower()
            is_mask = role == "mask" or name.endswith("_mask.png")
        else:
            value = str(item or "").strip()
            is_mask = False
        if not value:
            continue
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise QiniuImageProtocolError(400, "七牛参考图必须是可公网访问的 HTTP/HTTPS URL")
        if is_mask and not mask_url:
            mask_url = value
        else:
            image_urls.append(value)
    if len(image_urls) > _REFERENCE_LIMIT:
        raise QiniuImageProtocolError(400, f"七牛参考图最多 {_REFERENCE_LIMIT} 张")
    return image_urls, mask_url


def _authorization_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    result = {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in {"authorization", "content-type", "x-goog-api-key"}
    }
    raw = ""
    for key, value in headers.items():
        if str(key).lower() == "authorization":
            raw = str(value or "").strip()
            break
        if str(key).lower() == "x-goog-api-key" and not raw:
            raw = str(value or "").strip()
    token = re.sub(r"^(?:bearer|key)\s+", "", raw, flags=re.IGNORECASE).strip()
    if not token:
        raise QiniuImageProtocolError(400, "七牛生图缺少 API Key")
    result["Authorization"] = f"Key {token}"
    result["Content-Type"] = "application/json"
    result.setdefault("Accept", "application/json")
    return result


def _json_response(response: httpx.Response, action: str, task_id: str = "") -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise QiniuImageProtocolError(
            502,
            f"七牛生图{action}返回非 JSON 响应：{response.text[:500]}",
            upstream_task_id=task_id,
        ) from exc
    if not isinstance(payload, dict):
        raise QiniuImageProtocolError(502, f"七牛生图{action}返回非 JSON 对象", task_id)
    return payload


def _failure_reason(payload: Any) -> str:
    if isinstance(payload, Mapping):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, Mapping):
            for key in ("msg", "message", "detail", "type"):
                value = detail.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("message", "reason", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, Mapping):
                nested = _failure_reason(value)
                if nested:
                    return nested
    return "上游未提供失败原因"


def _status(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("status") or payload.get("state") or "").strip().upper()


def _request_id(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("request_id") or payload.get("task_id") or "").strip()


def _extract_images(payload: Any, depth: int = 0) -> List[ImagePayload]:
    if depth > 8:
        return []
    found: List[ImagePayload] = []
    if isinstance(payload, Mapping):
        url = payload.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            found.append({"type": "url", "value": url})
        for key in ("images", "result", "data", "output"):
            found.extend(_extract_images(payload.get(key), depth + 1))
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            found.extend(_extract_images(item, depth + 1))
    unique: List[ImagePayload] = []
    seen = set()
    for item in found:
        value = item.get("value")
        if value and value not in seen:
            seen.add(value)
            unique.append(item)
    return unique


def _request_body(request: Mapping[str, Any], model: str) -> Tuple[Dict[str, Any], bool]:
    prompt = _prompt(request)
    width, height = _size_pair(request.get("size"))
    image_urls, mask_url = _reference_urls(request)
    output_format = str(request.get("output_format") or "png").strip().lower()
    if output_format not in {"jpeg", "png", "webp"}:
        raise QiniuImageProtocolError(400, "七牛 output_format 仅支持 jpeg、png 或 webp")
    body: Dict[str, Any] = {"prompt": prompt, "output_format": output_format}
    if model in _GEMINI_MODELS:
        if mask_url:
            raise QiniuImageProtocolError(400, "七牛 Gemini 图片编辑不支持 mask，请移除遮罩图")
        requested_ratio = str(request.get("aspect_ratio") or "").strip()
        allowed = set(_GEMINI_ASPECT_RATIOS)
        if model == "gemini-3.1-flash-image-preview":
            allowed.update(_FLASH_EXTRA_ASPECT_RATIOS)
        aspect_ratio = requested_ratio or _closest_aspect_ratio(width, height, model)
        if aspect_ratio not in allowed | {"auto"}:
            raise QiniuImageProtocolError(400, f"七牛 Gemini 画幅比例不受支持：{aspect_ratio}")
        body.update({
            "aspect_ratio": aspect_ratio,
            "resolution": _gemini_resolution(request, width, height, model),
        })
        for key in ("system_prompt", "safety_tolerance"):
            value = str(request.get(key) or "").strip()
            if value:
                body[key] = value
        if model == "gemini-3.1-flash-image-preview":
            thinking = str(request.get("thinking_level") or "").strip().lower()
            if thinking:
                if thinking not in {"minimal", "high"}:
                    raise QiniuImageProtocolError(400, "七牛 Gemini thinking_level 仅支持 minimal 或 high")
                body["thinking_level"] = thinking
    else:
        quality = str(request.get("quality") or "high").strip().lower()
        if quality not in {"auto", "low", "medium", "high"}:
            quality = "high"
        body.update({
            "image_size": _gpt_image_size(width, height),
            "quality": quality,
            "num_images": 1,
        })
    if image_urls:
        body["image_urls"] = image_urls
        if model == "gpt-image-2" and mask_url:
            body["mask_url"] = mask_url
    elif mask_url:
        raise QiniuImageProtocolError(400, "七牛图片编辑至少需要一张非遮罩参考图")
    return body, bool(image_urls)


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    action: str,
    task_id: str,
) -> Dict[str, Any]:
    response = await client.get(url, headers=dict(headers))
    if response.status_code >= 400:
        payload = _json_response(response, action, task_id)
        raise QiniuImageProtocolError(
            response.status_code,
            f"七牛生图{action}失败：{_failure_reason(payload)}",
            task_id,
        )
    return _json_response(response, action, task_id)


async def _query_task_with_client(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    model: str,
    api_root: str,
    headers: Mapping[str, str],
) -> Dict[str, Any]:
    route = _MODEL_ROUTES[model]
    quoted = urllib.parse.quote(task_id, safe="")
    status_url = f"{api_root}{route}/requests/{quoted}/status"
    status_payload = await _get_json(
        client,
        status_url,
        headers=headers,
        action="任务状态查询",
        task_id=task_id,
    )
    status = _status(status_payload)
    if status in _FAILURE_STATUSES:
        raise QiniuImageProtocolError(
            502,
            f"七牛生图任务失败：{_failure_reason(status_payload)}",
            task_id,
        )
    if status in _SUCCESS_STATUSES:
        images = _extract_images(status_payload)
        if images:
            return status_payload
        result_url = f"{api_root}{route}/requests/{quoted}"
        result = await _get_json(
            client,
            result_url,
            headers=headers,
            action="任务结果查询",
            task_id=task_id,
        )
        if not _extract_images(result):
            raise QiniuImageProtocolError(502, "七牛生图任务已完成，但没有返回图片", task_id)
        return result
    return status_payload


async def _poll_task(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    model: str,
    api_root: str,
    headers: Mapping[str, str],
    timeout: float,
    interval: float,
    progress: ImageProgress,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = await _query_task_with_client(
                client,
                task_id,
                model=model,
                api_root=api_root,
                headers=headers,
            )
        except httpx.TransportError:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            continue
        if callable(progress):
            progress({
                "status": "polling",
                "upstream_task_id": task_id,
                "model": model,
                "raw_last": payload,
            })
        if _extract_images(payload):
            return payload
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raise QiniuImageProtocolError(504, f"七牛生图任务超时：request_id={task_id}", task_id)


async def generate_qiniu_image(
    request: Mapping[str, Any],
    *,
    base_url: str,
    headers: Mapping[str, str],
    request_timeout: Any = 300.0,
    poll_timeout: float = 3600.0,
    poll_interval: float = 2.0,
    progress: ImageProgress = None,
) -> Tuple[ImagePayload, Dict[str, Any]]:
    """提交一次七牛异步生图任务，等待完成并返回首张图片。"""
    model = _model(request)
    api_root = _api_root(base_url)
    body, is_edit = _request_body(request, model)
    route = _MODEL_ROUTES[model] + ("/edit" if is_edit else "")
    auth_headers = _authorization_headers(headers)
    timeout = _timeout_seconds(poll_timeout, 3600.0, "轮询")
    interval = max(0.5, _timeout_seconds(poll_interval, 2.0, "轮询间隔"))
    async with _client(request_timeout) as client:
        try:
            response = await client.post(
                f"{api_root}{route}",
                headers=auth_headers,
                json=body,
            )
        except httpx.TransportError as exc:
            raise QiniuImageProtocolError(
                502,
                f"七牛生图创建请求未收到响应，不能自动重试以避免重复扣费：{exc}",
            ) from exc
        raw = _json_response(response, "任务提交")
        if response.status_code >= 400:
            suffix = "，已停止自动重试以避免重复扣费" if response.status_code >= 500 else ""
            raise QiniuImageProtocolError(
                response.status_code,
                f"七牛生图任务提交失败{suffix}：{_failure_reason(raw)}",
            )
        task_id = _request_id(raw)
        status = _status(raw)
        if status in _FAILURE_STATUSES:
            raise QiniuImageProtocolError(502, f"七牛生图任务提交失败：{_failure_reason(raw)}", task_id)
        if not task_id:
            raise QiniuImageProtocolError(502, "七牛生图提交响应未返回 request_id，已停止处理以避免重复扣费")
        if callable(progress):
            progress({
                "status": "polling",
                "upstream_task_id": task_id,
                "model": model,
                "raw_submit": raw,
            })
        final = await _poll_task(
            client,
            task_id,
            model=model,
            api_root=api_root,
            headers=auth_headers,
            timeout=timeout,
            interval=interval,
            progress=progress,
        )
        images = _extract_images(final)
        if not images:
            raise QiniuImageProtocolError(502, "七牛生图任务完成但没有返回图片", task_id)
        return images[0], final


async def query_qiniu_image_task(
    task_id: str,
    *,
    model: str,
    base_url: str,
    headers: Mapping[str, str],
    request_timeout: Any = 300.0,
) -> Dict[str, Any]:
    """查询一次已有七牛图片任务，绝不重新提交创建请求。"""
    value = str(task_id or "").strip()
    if not value:
        raise QiniuImageProtocolError(400, "七牛生图任务 ID 不能为空")
    normalized_model = _model(model)
    try:
        async with _client(request_timeout) as client:
            return await _query_task_with_client(
                client,
                value,
                model=normalized_model,
                api_root=_api_root(base_url),
                headers=_authorization_headers(headers),
            )
    except httpx.TransportError as exc:
        raise QiniuImageProtocolError(502, f"查询七牛生图任务失败：{exc}", value) from exc
