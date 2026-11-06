"""安全下载：只允许 http(s)，拦截内网地址（含 DNS 解析结果与每一跳重定向），流式写盘并限制大小。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from .errors import MediaError, MediaTooLarge

USER_AGENT = "Mozilla/5.0 (compatible; AstrBot-MultimodalProcessor/1.0)"
MAX_REDIRECTS = 5
CHUNK = 256 * 1024


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class _PublicOnlyResolver(AbstractResolver):
    """包装默认解析器：任何一个解析结果是非公网地址都拒绝，防止 DNS 指向内网。"""

    def __init__(self) -> None:
        self._inner = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        for item in results:
            if not is_public_ip(item["host"]):
                raise MediaError("拒绝访问内网或保留地址")
        return results

    async def close(self) -> None:
        await self._inner.close()


def validate_url(url: str, allow_private: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MediaError("只支持 http/https 媒体链接")
    if parsed.username or parsed.password:
        raise MediaError("不支持带账号密码的链接")
    if allow_private:
        return
    host = parsed.hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise MediaError("拒绝访问本机或内网地址")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return  # 域名：由解析器在连接时检查
    if not is_public_ip(host):
        raise MediaError("拒绝访问内网或保留地址")


async def download(
    url: str,
    dest: Path,
    max_bytes: int,
    *,
    timeout: int = 60,
    allow_private: bool = False,
    headers: dict[str, str] | None = None,
) -> int:
    """下载到 dest，返回字节数。超过 max_bytes 立即中断并删除半成品。"""
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(15, timeout), sock_read=min(30, timeout))
    connector = aiohttp.TCPConnector(resolver=None if allow_private else _PublicOnlyResolver(), limit=4)
    merged = {"User-Agent": USER_AGENT, **(headers or {})}
    current = url
    try:
        async with aiohttp.ClientSession(timeout=client_timeout, connector=connector, headers=merged) as session:
            for _ in range(MAX_REDIRECTS + 1):
                validate_url(current, allow_private)
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise MediaError("下载失败：重定向缺少目标地址")
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise MediaError(f"下载失败：HTTP {response.status}")
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
                    total = 0
                    with dest.open("wb") as out:
                        async for chunk in response.content.iter_chunked(CHUNK):
                            total += len(chunk)
                            if total > max_bytes:
                                raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
                            out.write(chunk)
                    if total == 0:
                        raise MediaError("下载到的文件为空")
                    return total
            raise MediaError("下载失败：重定向次数过多")
    except (MediaError, MediaTooLarge):
        dest.unlink(missing_ok=True)
        raise
    except asyncio.TimeoutError as exc:
        dest.unlink(missing_ok=True)
        raise MediaError("下载超时") from exc
    except aiohttp.ClientError as exc:
        dest.unlink(missing_ok=True)
        cause = exc.__cause__ or exc
        if isinstance(cause, MediaError):
            raise cause from None
        raise MediaError(f"下载失败：{type(exc).__name__}") from exc
