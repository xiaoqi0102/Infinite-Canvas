"""视频协议插件共享的 URL 与错误文案工具。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import os
import re
import socket
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx


_VIDEO_API_VERSION_RE = re.compile(r"/v[12]$", re.IGNORECASE)
_SENSITIVE_FIELD_RE = re.compile(
    r"authorization|proxy-authorization|api[-_]?key|(?:^|[-_])token(?:$|[-_])|access[-_]?token|refresh[-_]?token|password|passwd|secret|cookie|credential",
    re.IGNORECASE,
)
_BASE64_FIELD_RE = re.compile(r"base64|b64|file[_-]?data|binary", re.IGNORECASE)
_BASE64_TEXT_RE = re.compile(r"^[A-Za-z0-9+/=_\-\s]+$")
_HTTP_PREVIEW_MAX_TEXT = 12000
_HTTP_PREVIEW_MAX_ITEMS = 60
_FAKE_IP_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="video-public-dns",
)


class UnsafePublicUrlError(ValueError):
    """远程素材 URL 不是可安全直连的公网 HTTP(S) 地址。"""


def video_http_preview_url(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return text[:_HTTP_PREVIEW_MAX_TEXT]
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    if text.startswith("data:"):
        media_type = text[5:].split(";", 1)[0] or "application/octet-stream"
        return f"[data:{media_type} 内嵌数据已省略，原长度 {len(text)} 字符]"
    if text.startswith("blob:"):
        return "[blob 本地对象地址已省略]"
    return text[:_HTTP_PREVIEW_MAX_TEXT]


def video_http_preview_value(
    value: Any,
    key: str = "",
    *,
    depth: int = 0,
    secret_values: Sequence[str] = (),
):
    if _SENSITIVE_FIELD_RE.search(str(key or "")):
        if str(key or "").lower() == "authorization":
            return "Bearer YOUR_API_KEY"
        return "[敏感信息已隐藏]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"[二进制内容已省略，共 {len(value)} 字节]"
    if isinstance(value, str):
        text = value
        for secret in secret_values:
            if secret:
                text = text.replace(secret, "[敏感信息已隐藏]")
        likely_inline_data = (
            str(key or "").strip().lower() == "data"
            and len(text) >= 256
            and _BASE64_TEXT_RE.fullmatch(text) is not None
        )
        if _BASE64_FIELD_RE.search(str(key or "")) or likely_inline_data:
            return f"[内嵌数据已省略，原长度 {len(text)} 字符]"
        if text.startswith(("http://", "https://", "data:", "blob:")):
            return video_http_preview_url(text)
        return text[:_HTTP_PREVIEW_MAX_TEXT] + ("..." if len(text) > _HTTP_PREVIEW_MAX_TEXT else "")
    if depth >= 8:
        return "[过深内容已省略]"
    if isinstance(value, Mapping):
        return {
            str(child_key): video_http_preview_value(
                child_value,
                str(child_key),
                depth=depth + 1,
                secret_values=secret_values,
            )
            for child_key, child_value in list(value.items())[:_HTTP_PREVIEW_MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            video_http_preview_value(item, key, depth=depth + 1, secret_values=secret_values)
            for item in list(value)[:_HTTP_PREVIEW_MAX_ITEMS]
        ]
    return str(value)[:_HTTP_PREVIEW_MAX_TEXT]


def video_http_preview_headers(headers: Mapping[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    preview: Dict[str, str] = {}
    secrets: List[str] = []
    for key, raw_value in (headers or {}).items():
        name = str(key)
        value = str(raw_value or "")
        if _SENSITIVE_FIELD_RE.search(name):
            if value:
                secrets.append(value)
                if value.lower().startswith("bearer "):
                    secrets.append(value[7:])
            preview[name] = "Bearer YOUR_API_KEY" if name.lower() == "authorization" else "[敏感信息已隐藏]"
        else:
            preview[name] = value[:1000]
    return preview, secrets


def video_http_preview_files(files: Any) -> List[Dict[str, Any]]:
    if isinstance(files, Mapping):
        entries = list(files.items())
    else:
        entries = list(files or [])
    result = []
    for field, raw in entries[:_HTTP_PREVIEW_MAX_ITEMS]:
        item: Dict[str, Any] = {"field": str(field)}
        if not isinstance(raw, (list, tuple)):
            item["value"] = video_http_preview_value(raw, str(field))
            result.append(item)
            continue
        filename = raw[0] if len(raw) > 0 else None
        content = raw[1] if len(raw) > 1 else None
        content_type = raw[2] if len(raw) > 2 else ""
        if filename is None:
            item["value"] = video_http_preview_value(content, str(field))
        else:
            item.update({
                "filename": os.path.basename(str(filename).replace("\\", "/")),
                "content_type": str(content_type or "application/octet-stream"),
            })
            if isinstance(content, bytes):
                item["size"] = len(content)
            elif hasattr(content, "getbuffer"):
                try:
                    item["size"] = int(content.getbuffer().nbytes)
                except Exception:
                    item["size"] = None
            elif hasattr(content, "fileno"):
                try:
                    item["size"] = int(os.fstat(content.fileno()).st_size)
                except Exception:
                    item["size"] = None
            else:
                item["size"] = None
        result.append(item)
    return result


def _video_http_response_preview(response: Any, secret_values: Sequence[str]) -> Dict[str, Any]:
    headers = getattr(response, "headers", {}) or {}
    selected_headers = {
        str(key): str(value)[:1000]
        for key, value in headers.items()
        if str(key).lower() in {"content-type", "retry-after", "x-request-id", "request-id", "trace-id"}
    }
    try:
        body = response.json()
    except Exception:
        body = getattr(response, "text", "") or ""
    return {
        "received": True,
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "headers": selected_headers,
        "body": video_http_preview_value(body, secret_values=secret_values),
    }


async def submit_http_request_with_logging(
    client: Any,
    *,
    progress,
    url: str,
    headers: Mapping[str, Any],
    json_body: Any = None,
    form: Any = None,
    files: Any = None,
    attempts: Optional[List[Dict[str, Any]]] = None,
    context: Optional[Mapping[str, Any]] = None,
    **kwargs,
):
    """提交创建请求，并持久化不含密钥和二进制内容的真实 HTTP 交换。"""
    request_headers, secret_values = video_http_preview_headers(headers)
    request: Dict[str, Any] = {
        "method": "POST",
        "url": video_http_preview_url(url),
        "headers": request_headers,
        "format": "multipart" if files is not None else "form" if form is not None else "json",
    }
    if json_body is not None:
        request["body"] = video_http_preview_value(json_body, secret_values=secret_values)
    if form is not None:
        request["form"] = video_http_preview_value(dict(form) if not isinstance(form, Mapping) else form, secret_values=secret_values)
    if files is not None:
        request["files"] = video_http_preview_files(files)
    exchange = {"request": request, "response": {"received": False}}
    exchange_list = attempts if attempts is not None else []
    exchange_list.append(exchange)

    def report():
        if not callable(progress):
            return
        payload = {
            "transport": "backend_http",
            "context": video_http_preview_value(dict(context or {})),
            "attempts": exchange_list[-8:],
        }
        if exchange_list:
            payload.update(exchange_list[-1]["request"])
        progress({"request_details": payload})

    report()
    try:
        call_kwargs = dict(kwargs)
        if json_body is not None:
            call_kwargs["json"] = json_body
        if form is not None:
            call_kwargs["data"] = form
        if files is not None:
            call_kwargs["files"] = files
        response = await client.post(url, headers=dict(headers), **call_kwargs)
    except Exception as exc:
        exchange["response"] = {
            "received": False,
            "error_type": type(exc).__name__,
            "error": video_http_preview_value(str(exc), secret_values=secret_values),
        }
        report()
        raise
    exchange["response"] = _video_http_response_preview(response, secret_values)
    report()
    return response


# 保留视频插件既有导入名，图片与视频共用同一套脱敏 HTTP 日志契约。
submit_video_http_request = submit_http_request_with_logging


def _is_global_ip_address(value: Any, *, allow_https_fake_ip: bool = False) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return False
    if address.is_global:
        return True
    return (
        allow_https_fake_ip
        and isinstance(address, ipaddress.IPv4Address)
        and address in _FAKE_IP_PROXY_NETWORK
    )


async def _resolve_public_http_target(
    value: Any,
) -> Tuple[urllib.parse.SplitResult, str, List[str]]:
    text = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        raise UnsafePublicUrlError("远程素材 URL 格式无效")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafePublicUrlError("远程素材必须使用不含账号信息的 HTTP/HTTPS URL")
    host = parsed.hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafePublicUrlError("远程素材不能使用本机地址")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafePublicUrlError("远程素材 hostname 无法解析") from exc
    try:
        address = ipaddress.ip_address(ascii_host.split("%", 1)[0])
    except ValueError:
        resolved_from_dns = True
        try:
            loop = asyncio.get_running_loop()
            records = await asyncio.wait_for(
                loop.run_in_executor(
                    _DNS_EXECUTOR,
                    socket.getaddrinfo,
                    ascii_host,
                    port,
                    0,
                    socket.SOCK_STREAM,
                ),
                timeout=5.0,
            )
        except (OSError, UnicodeError, asyncio.TimeoutError):
            raise UnsafePublicUrlError("远程素材 hostname 无法解析为公网地址")
        addresses = sorted({record[4][0] for record in records if record and record[4]})
    else:
        resolved_from_dns = False
        addresses = [str(address)]
    allow_https_fake_ip = resolved_from_dns and parsed.scheme.lower() == "https"
    if not addresses or not all(
        _is_global_ip_address(item, allow_https_fake_ip=allow_https_fake_ip)
        for item in addresses
    ):
        raise UnsafePublicUrlError("远程素材 hostname 包含非公网解析结果")
    return parsed, ascii_host, addresses


async def is_public_http_url(value: Any) -> bool:
    """校验服务端可下载的公网 HTTP(S) URL，包括 DNS 解析结果。"""
    try:
        await _resolve_public_http_target(value)
        return True
    except UnsafePublicUrlError:
        return False


async def public_http_get(value: Any, *, timeout: float = 120.0) -> httpx.Response:
    """把已验证的 DNS 结果固定到实际连接，并保留原始 Host 与 TLS SNI。"""
    parsed, ascii_host, addresses = await _resolve_public_http_target(value)
    explicit_port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    host_name = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    host_header = f"{host_name}:{explicit_port}" if explicit_port is not None else host_name
    last_error: httpx.TransportError | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=20.0, read=timeout, write=30.0, pool=20.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for address in addresses:
            target_host = f"[{address}]" if ":" in address else address
            port_suffix = (
                f":{explicit_port}"
                if explicit_port is not None and explicit_port != default_port
                else ""
            )
            target_url = urllib.parse.urlunsplit(
                (
                    parsed.scheme,
                    f"{target_host}{port_suffix}",
                    parsed.path or "/",
                    parsed.query,
                    "",
                )
            )
            try:
                response = await client.request(
                    "GET",
                    target_url,
                    headers={"Host": host_header},
                    follow_redirects=False,
                    extensions={"sni_hostname": ascii_host},
                )
            except httpx.TransportError as exc:
                last_error = exc
                continue
            if response.is_redirect:
                raise UnsafePublicUrlError("远程素材下载不允许重定向")
            return response
    if last_error:
        raise last_error
    raise httpx.ConnectError("远程素材没有可连接的公网地址")


def canonical_video_api_root(base_url: Any) -> str:
    """返回不含尾斜杠和重复 v1/v2 尾段的视频 API 根地址。"""
    text = str(base_url or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    while _VIDEO_API_VERSION_RE.search(path):
        path = path[:-3].rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_video_download_url(value: Any, base_url: Any = "") -> str:
    """把上游相对下载路径补成同源绝对 URL，并保留本地资源路径。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("/output/", "/assets/")):
        return text
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return text
    root = canonical_video_api_root(base_url)
    if not root:
        return text
    root_parsed = urllib.parse.urlsplit(root)
    if root_parsed.scheme.lower() not in {"http", "https"} or not root_parsed.netloc:
        return ""
    if text.startswith("//"):
        return f"{root_parsed.scheme}:{text}"
    if parsed.scheme or parsed.netloc or text.startswith(("./", "../")):
        return ""
    if text.startswith("/"):
        return f"{root}{text}"
    if text.startswith(("v1/", "v2/")):
        return f"{root}/{text}"
    return ""


def humanize_video_task_failure(reason: Any) -> str:
    """把常见视频内容安全错误转换为现有用户提示。"""
    text = str(reason or "").strip()
    upper = text.upper()
    if "PROMINENT_PEOPLE_FILTER" in upper or "PROMINENT_PEOPLE" in upper:
        return (
            "视频生成被上游内容安全策略拦截：检测到提示词或参考图里包含知名人物 / 真人面孔"
            f"（错误码：{text}）。\n\n"
            "这不是代码错误，而是 veo（Google）的内容审核规则——它会拒绝生成涉及真实/知名人物的视频。\n\n"
            "建议这样处理：\n"
            "  1. 去掉提示词里的人名、明星、公众人物等指向具体真人的描述；\n"
            "  2. 换用非真人参考图，例如插画、AI 头像、卡通形象、商品图、场景图；\n"
            "  3. 如果用了真人照片做参考图，先做模糊/遮挡/转成明显的二次元插画风，或干脆只用文字提示词测试。"
        )
    if "SAFETY" in upper or "CONTENT_FILTER" in upper or "POLICY" in upper:
        return (
            "视频生成被上游内容安全策略拦截"
            f"（错误码：{text}）。\n\n"
            "这是 veo 的内容审核规则，提示词或参考图触发了安全过滤。\n"
            "请调整提示词/参考图后重试，避免涉及真人、暴力、敏感或受限内容。"
        )
    return f"视频生成任务失败：{text}"
