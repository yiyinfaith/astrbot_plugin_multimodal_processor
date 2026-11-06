from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import ipaddress
import mimetypes
from pathlib import Path
import re
import shutil
from urllib.parse import unquote, urlparse
import uuid

import aiohttp


class MediaError(RuntimeError):
    pass


class MediaTooLarge(MediaError):
    pass


@dataclass(slots=True)
class MediaFile:
    path: Path
    kind: str
    suffix: str
    source: str = ""


def _safe_suffix(name: str | None, default: str = ".bin") -> str:
    suffix = Path(name or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else default


def sniff_kind(path: str | Path) -> tuple[str, str]:
    p = Path(path)
    with p.open("rb") as f:
        head = f.read(64)
    if head.startswith((b"GIF87a", b"GIF89a")): return "image", ".gif"
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return "image", ".png"
    if head.startswith(b"\xff\xd8\xff"): return "image", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP": return "image", ".webp"
    if head.startswith(b"BM"): return "image", ".bmp"
    if head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        return "video", ".mp4" if brand in (b"isom", b"iso2", b"mp41", b"mp42") else ".m4a"
    if head.startswith(b"\x1aE\xdf\xa3"): return "video", ".mkv"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xff and head[1] & 0xe0 == 0xe0): return "audio", ".mp3"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE": return "audio", ".wav"
    if head.startswith(b"OggS"): return "audio", ".ogg"
    mime = mimetypes.guess_type(str(p))[0] or ""
    return (mime.split("/", 1)[0] if "/" in mime else "unknown", p.suffix.lower() or ".bin")


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MediaError("只允许 HTTP(S) 媒体地址")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise MediaError("拒绝访问内网媒体地址")
    except ValueError:
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            raise MediaError("拒绝访问本机地址")


async def _download(url: str, dest: Path, max_bytes: int, timeout: int = 60) -> None:
    _validate_url(url)
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(timeout, 15))
    headers = {"User-Agent": "AstrBot-Multimodal-Processor/1.0"}
    async with aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session:
        try:
            # 不自动跟随重定向，避免下载服务把安全的公网地址重定向到内网。
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise MediaError(f"下载失败：HTTP {response.status}")
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise MediaTooLarge("媒体超过大小限制")
                total = 0
                with dest.open("wb") as out:
                    async for chunk in response.content.iter_chunked(1024 * 128):
                        total += len(chunk)
                        if total > max_bytes:
                            raise MediaTooLarge("媒体超过大小限制")
                        out.write(chunk)
        except asyncio.TimeoutError as exc:
            raise MediaError("下载超时") from exc
        except aiohttp.ClientError as exc:
            raise MediaError(f"下载失败：{exc}") from exc


def _component_type(component: object) -> str:
    value = getattr(component, "type", "")
    return getattr(value, "value", str(value)).lower()


def _iter_components(event: object):
    try:
        components = list(event.get_messages())
    except Exception:
        components = list(getattr(event, "message_obj", None).message or []) if getattr(event, "message_obj", None) else []
    for comp in components:
        yield comp
        if _component_type(comp) == "reply":
            for nested in getattr(comp, "chain", None) or []:
                yield nested


async def _component_source(component: object, workdir: Path, index: int) -> tuple[str, str] | None:
    typ = _component_type(component)
    if typ in {"image", "video", "record", "audio", "file"}:
        if typ == "file":
            try:
                source = await component.get_file(allow_return_url=True)
            except Exception:
                source = getattr(component, "file_", "") or getattr(component, "url", "")
        else:
            source = getattr(component, "path", None) or getattr(component, "url", None) or getattr(component, "file", None)
        return (str(source), _safe_suffix(str(source))) if source else None
    return None


async def resolve_media(event: object, workdir: str | Path, max_bytes: int, *, kind: str = "any", limit: int = 1) -> list[MediaFile]:
    """Resolve images/files from current and quoted chains into task-local files."""
    root = Path(workdir); root.mkdir(parents=True, exist_ok=True)
    found: list[MediaFile] = []
    for index, component in enumerate(_iter_components(event)):
        pair = await _component_source(component, root, index)
        if not pair:
            continue
        source, suffix = pair
        target = root / f"input-{index}-{uuid.uuid4().hex[:8]}{suffix}"
        parsed = urlparse(source)
        if source.startswith("base64://"):
            try: target.write_bytes(base64.b64decode(source[9:], validate=True))
            except Exception as exc: raise MediaError("Base64媒体无效") from exc
        elif source.startswith("data:"):
            try: target.write_bytes(base64.b64decode(source.split(",", 1)[1], validate=True))
            except Exception as exc: raise MediaError("Data URL无效") from exc
        elif parsed.scheme in {"http", "https"}:
            await _download(source, target, max_bytes)
        elif parsed.scheme == "file":
            local = Path(unquote(parsed.path))
            if parsed.netloc and not local.drive: local = Path(f"//{parsed.netloc}{local}")
            if not local.exists() or not local.is_file(): continue
            if local.stat().st_size > max_bytes: raise MediaTooLarge("媒体超过大小限制")
            shutil.copyfile(local, target)
        else:
            local = Path(source)
            if not local.exists() or not local.is_file(): continue
            if local.stat().st_size > max_bytes: raise MediaTooLarge("媒体超过大小限制")
            shutil.copyfile(local, target)
        actual_kind, actual_suffix = sniff_kind(target)
        if kind != "any" and actual_kind != kind:
            continue
        found.append(MediaFile(target, actual_kind, actual_suffix, source))
        if len(found) >= limit: break
    return found


async def avatar_media(event: object, workdir: str | Path, max_bytes: int) -> list[MediaFile]:
    if not getattr(event, "bot", None): return []
    at_id = None
    for comp in _iter_components(event):
        if _component_type(comp) in {"at", "mention"}:
            at_id = getattr(comp, "qq", None) or getattr(comp, "target_id", None) or getattr(comp, "id", None)
            if callable(at_id): at_id = at_id()
            if at_id: break
    if not at_id: return []
    url = f"https://q1.qlogo.cn/g?b=qq&nk={at_id}&s=640"
    target = Path(workdir) / f"avatar-{at_id}.jpg"
    await _download(url, target, max_bytes)
    return [MediaFile(target, "image", ".jpg", url)]
