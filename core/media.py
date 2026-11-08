"""统一的媒体获取：从当前消息、引用消息或 @ 头像中找到媒体，并安全地复制到任务目录。

查找顺序：引用消息里的媒体 → 当前消息附带的媒体 → @ 用户头像（仅图片）。
远程地址走 download.download（拦截内网）；本地路径只允许位于 AstrBot 数据目录、
临时目录或配置的额外目录内，防止通过伪造路径读取任意文件。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import unquote, urlparse
import uuid

from . import download as dl
from .errors import MediaError, MediaTooLarge
from .formats import AUDIO_EXTS, IMAGE_EXTS, SILK, VIDEO_EXTS, Sniffed, sniff_file

IMAGE, VIDEO, AUDIO, ANY = "image", "video", "audio", "any"


def component_type(component: Any) -> str:
    value = getattr(component, "type", "")
    return str(getattr(value, "value", value)).lower()


@dataclass(slots=True)
class MediaRef:
    """一个候选媒体来源（尚未下载）。"""

    component: Any
    hint: str  # image / video / audio / file / avatar
    origin: str  # reply / message / avatar
    name: str = ""
    url: str = ""  # 头像等直接给出的地址

    def accepts(self, kind: str) -> bool:
        if kind == ANY or self.hint == "file":
            return True
        if self.hint == "avatar":
            return kind == IMAGE
        if kind == AUDIO:
            return self.hint in {AUDIO, VIDEO}  # 视频也能当音频源
        return self.hint == kind


@dataclass(slots=True)
class MediaFile:
    path: Path
    sniffed: Sniffed
    name: str = ""
    origin: str = ""
    size: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def stem(self) -> str:
        stem = Path(self.name).stem if self.name else ""
        stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", stem).strip(" ._")
        return stem[:60] or "output"


def _plain_components(event: Any) -> list[Any]:
    try:
        return list(event.get_messages())
    except Exception:
        message_obj = getattr(event, "message_obj", None)
        return list(getattr(message_obj, "message", None) or [])


def _ref_from_component(component: Any, origin: str) -> MediaRef | None:
    ctype = component_type(component)
    if ctype == "image":
        return MediaRef(component, IMAGE, origin)
    if ctype == "video":
        return MediaRef(component, VIDEO, origin)
    if ctype == "record":
        return MediaRef(component, AUDIO, origin)
    if ctype == "file":
        name = str(getattr(component, "name", "") or "")
        ext = Path(name).suffix.lower()
        hint = IMAGE if ext in IMAGE_EXTS else VIDEO if ext in VIDEO_EXTS else AUDIO if ext in AUDIO_EXTS else "file"
        return MediaRef(component, hint, origin, name=name)
    return None


def _walk(components: list[Any], origin: str, out: list[MediaRef], depth: int = 0) -> None:
    """展开合并转发节点（Node / Nodes）里的媒体。"""
    for component in components:
        ctype = component_type(component)
        if ctype == "nodes" and depth < 3:
            for node in getattr(component, "nodes", None) or []:
                _walk(list(getattr(node, "content", None) or []), origin, out, depth + 1)
            continue
        if ctype == "node" and depth < 3:
            _walk(list(getattr(component, "content", None) or []), origin, out, depth + 1)
            continue
        ref = _ref_from_component(component, origin)
        if ref:
            out.append(ref)


def collect_refs(event: Any) -> list[MediaRef]:
    """按优先级列出消息中的所有媒体候选（不含头像）：引用消息在前，当前消息在后。"""
    components = _plain_components(event)
    reply_refs: list[MediaRef] = []
    message_refs: list[MediaRef] = []
    for component in components:
        if component_type(component) == "reply":
            _walk(list(getattr(component, "chain", None) or []), "reply", reply_refs)
            continue
        _walk([component], "message", message_refs)
    return reply_refs + message_refs


def at_targets(event: Any) -> list[str]:
    """消息里被 @ 的用户（排除机器人自己和全体成员）。"""
    try:
        self_id = str(event.get_self_id())
    except Exception:
        self_id = ""
    targets: list[str] = []
    for component in _plain_components(event):
        if component_type(component) != "at":
            continue
        qq = str(getattr(component, "qq", "") or "").strip()
        if qq and qq != "all" and qq != self_id and qq.isdigit() and qq not in targets:
            targets.append(qq)
    return targets


def qq_avatar_url(user_id: str) -> str:
    return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"


PlatformFetch = Callable[[Any, MediaRef, Path, int], Awaitable[Path | None]]


class MediaResolver:
    def __init__(
        self,
        allowed_roots: Iterable[str | Path],
        *,
        download_timeout: int = 60,
        allow_private_urls: bool = False,
        platform_fetch: PlatformFetch | None = None,
    ) -> None:
        roots: list[Path] = []
        for root in allowed_roots:
            try:
                resolved = Path(root).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if resolved not in roots:
                roots.append(resolved)
        self.allowed_roots = roots
        self.download_timeout = download_timeout
        self.allow_private_urls = allow_private_urls
        self.platform_fetch = platform_fetch

    # ------------------------------------------------------------ 本地路径
    def local_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if not resolved.is_file():
            return False
        return any(resolved == root or root in resolved.parents for root in self.allowed_roots)

    def _copy_local(self, source: Path, dest: Path, max_bytes: int) -> int:
        if not self.local_allowed(source):
            raise MediaError("媒体文件位于不允许读取的位置")
        size = source.stat().st_size
        if size > max_bytes:
            raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
        if size == 0:
            raise MediaError("媒体文件为空")
        shutil.copyfile(source, dest)
        return size

    # ------------------------------------------------------------ 单个来源
    @staticmethod
    def _candidates(ref: MediaRef) -> list[str]:
        if ref.url:
            return [ref.url]
        comp = ref.component
        values: list[str] = []
        if component_type(comp) == "file":
            for attr in ("url", "file_"):
                value = getattr(comp, attr, "") or ""
                if value:
                    values.append(str(value))
        else:
            for attr in ("url", "file", "path"):
                value = getattr(comp, attr, "") or ""
                if value:
                    values.append(str(value))
        result: list[str] = []
        for value in values:
            if value not in result:
                result.append(value)
        return result

    async def _from_source(self, source: str, dest: Path, max_bytes: int) -> int:
        if source.startswith("base64://") or source.startswith("data:"):
            payload = source[9:] if source.startswith("base64://") else source.split(",", 1)[-1]
            if len(payload) * 3 // 4 > max_bytes:
                raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
            try:
                data = await asyncio.to_thread(base64.b64decode, payload, validate=False)
            except (binascii.Error, ValueError) as exc:
                raise MediaError("Base64 媒体数据无效") from exc
            if not data:
                raise MediaError("媒体数据为空")
            await asyncio.to_thread(dest.write_bytes, data)
            return len(data)
        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            return await dl.download(
                source, dest, max_bytes, timeout=self.download_timeout, allow_private=self.allow_private_urls
            )
        if parsed.scheme == "file":
            local = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", local):
                local = local[1:]
            return await asyncio.to_thread(self._copy_local, Path(local), dest, max_bytes)
        if parsed.scheme and len(parsed.scheme) > 1:
            raise MediaError(f"不支持的媒体地址类型：{parsed.scheme}")
        return await asyncio.to_thread(self._copy_local, Path(source), dest, max_bytes)

    async def fetch(self, event: Any, ref: MediaRef, workdir: Path, max_bytes: int) -> MediaFile:
        dest = workdir / f"input-{uuid.uuid4().hex[:12]}"
        errors: list[str] = []
        size = 0
        for source in self._candidates(ref):
            try:
                size = await self._from_source(source, dest, max_bytes)
                break
            except MediaTooLarge:
                raise
            except MediaError as exc:
                errors.append(str(exc))
                dest.unlink(missing_ok=True)
        else:
            path = None
            if self.platform_fetch is not None:
                try:
                    path = await self.platform_fetch(event, ref, workdir, max_bytes)
                except MediaTooLarge:
                    raise
                except MediaError as exc:
                    errors.append(str(exc))
            if path is None:
                raise MediaError(errors[-1] if errors else "无法获取媒体文件")
            dest = path
            size = dest.stat().st_size
            if size > max_bytes:
                raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
        sniffed = sniff_file(dest)
        if sniffed.kind == SILK:
            dest, sniffed = await self._decode_silk(ref, dest, workdir)
        final = dest.with_name(dest.stem + sniffed.ext)
        dest.replace(final)
        return MediaFile(final, sniffed, ref.name, ref.origin, size)

    async def _decode_silk(self, ref: MediaRef, path: Path, workdir: Path) -> tuple[Path, Sniffed]:
        """QQ 语音是 SILK 编码，FFmpeg 无法直接解码，借用 AstrBot 自带的转换。"""
        converter = getattr(ref.component, "convert_to_file_path", None)
        if converter is None:
            raise MediaError("无法解码 QQ 语音（SILK）")
        try:
            converted = Path(await converter())
        except Exception as exc:
            raise MediaError("无法解码 QQ 语音（SILK）") from exc
        target = workdir / f"voice-{uuid.uuid4().hex[:8]}.wav"
        try:
            await asyncio.to_thread(shutil.copyfile, converted, target)
        finally:
            try:
                if converted.resolve().parent != workdir.resolve():
                    converted.unlink(missing_ok=True)
            except OSError:
                pass
        path.unlink(missing_ok=True)
        return target, sniff_file(target)

    async def fetch_url(self, url: str, workdir: Path, max_bytes: int, name: str = "") -> MediaFile:
        ref = MediaRef(None, IMAGE, "avatar", name=name, url=url)
        return await self.fetch(None, ref, workdir, max_bytes)
