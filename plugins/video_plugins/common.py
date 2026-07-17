"""视频协议插件共享的 URL 与错误文案工具。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import re
import socket
import urllib.parse
from typing import Any, List, Tuple

import httpx


_VIDEO_API_VERSION_RE = re.compile(r"/v[12]$", re.IGNORECASE)
_FAKE_IP_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="video-public-dns",
)


class UnsafePublicUrlError(ValueError):
    """远程素材 URL 不是可安全直连的公网 HTTP(S) 地址。"""


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
