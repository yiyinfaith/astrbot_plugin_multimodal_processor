"""QQ（OneBot v11）专属能力。

只有 aiocqhttp 平台会调用这里的函数；其他平台在入口处就会收到明确提示。
QQ 官方表情（商城表情 / mface）在 AstrBot 的消息解析里会被丢弃，所以这里直接读取
协议端的原始消息段（当前消息的 raw_message、引用消息走 get_msg）。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import uuid

from ..core import download as dl
from ..core.errors import MediaError, MediaTooLarge, UnsupportedPlatform
from ..core.log import log
from ..core.media import MediaRef, component_type

PLATFORM = "aiocqhttp"
BASE64_SEND_LIMIT = 30 * 1024 * 1024


def is_onebot(event: Any) -> bool:
    try:
        name = str(event.get_platform_name() or "").lower()
    except Exception:
        return False
    return name == PLATFORM and getattr(event, "bot", None) is not None


def require_onebot(event: Any, feature: str) -> None:
    if not is_onebot(event):
        raise UnsupportedPlatform(f"{feature}是 QQ 专属功能，仅支持 OneBot v11 协议端，当前平台不支持。")


async def call(event: Any, action: str, **params: Any) -> Any:
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    caller = getattr(api, "call_action", None) or getattr(bot, "call_action", None)
    if caller is None:
        raise UnsupportedPlatform("当前协议端不支持调用 OneBot 接口")
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw, dict) and raw.get("self_id"):
        params.setdefault("self_id", raw["self_id"])
    return await caller(action, **params)


def _raw_segments(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, list):
            return [seg for seg in message if isinstance(seg, dict)]
    return []


def current_segments(event: Any) -> list[dict]:
    return _raw_segments(getattr(getattr(event, "message_obj", None), "raw_message", None))


def reply_id(event: Any) -> str | None:
    for seg in current_segments(event):
        if seg.get("type") == "reply":
            value = (seg.get("data") or {}).get("id")
            if value is not None:
                return str(value)
    try:
        for component in event.get_messages():
            if component_type(component) == "reply" and getattr(component, "id", None):
                return str(component.id)
    except Exception:
        pass
    return None


@dataclass(slots=True)
class QQImage:
    url: str = ""
    file_id: str = ""
    summary: str = ""
    is_mface: bool = False
    origin: str = "message"


def mface_url(emoji_id: str) -> str:
    """QQ 商城表情原图地址（与 NapCat 的转换规则一致）。"""
    return f"https://gxh.vip.qq.com/club/item/parcel/item/{emoji_id[:2]}/{emoji_id}/raw300.gif"


def image_from_segment(seg: dict, origin: str) -> QQImage | None:
    seg_type = str(seg.get("type") or "").lower()
    data = seg.get("data") or {}
    if seg_type == "image":
        url = str(data.get("url") or "")
        file_id = str(data.get("file") or data.get("file_id") or "")
        emoji_id = str(data.get("emoji_id") or "")
        if not url and file_id.startswith(("http://", "https://")):
            url = file_id
        if not url and emoji_id:
            url = mface_url(emoji_id)
        is_mface = "/club/item/" in url or bool(emoji_id)
        if not url and not file_id:
            return None
        return QQImage(url, file_id, str(data.get("summary") or ""), is_mface, origin)
    if seg_type == "mface":
        emoji_id = str(data.get("emoji_id") or "")
        url = str(data.get("url") or "") or (mface_url(emoji_id) if emoji_id else "")
        if not url:
            return None
        return QQImage(url, "", str(data.get("summary") or ""), True, origin)
    return None


async def find_images(event: Any) -> list[QQImage]:
    """引用消息里的图片优先，其次是当前消息里的图片。"""
    found: list[QQImage] = []
    rid = reply_id(event)
    if rid:
        try:
            payload = await call(event, "get_msg", message_id=int(rid) if rid.lstrip("-").isdigit() else rid)
        except UnsupportedPlatform:
            raise
        except Exception as exc:
            log.warning(f"[多模态处理器] 读取引用消息失败：{exc}")
            payload = None
        for seg in _raw_segments(payload):
            image = image_from_segment(seg, "reply")
            if image:
                found.append(image)
    for seg in current_segments(event):
        image = image_from_segment(seg, "message")
        if image:
            found.append(image)
    if not found:  # 原始消息不可用时退回 AstrBot 解析后的组件
        try:
            for component in event.get_messages():
                ctype = component_type(component)
                chain = (getattr(component, "chain", None) or []) if ctype == "reply" else [component]
                for comp in chain:
                    if component_type(comp) == "image":
                        url = str(getattr(comp, "url", "") or "")
                        file_id = str(getattr(comp, "file", "") or "")
                        if url or file_id:
                            found.append(QQImage(url, file_id, "", "/club/item/" in url, "reply" if ctype == "reply" else "message"))
        except Exception:
            pass
    return found


def _forward_ids(event: Any) -> list[str]:
    ids: list[str] = []

    def scan(components) -> None:
        for component in components or []:
            ctype = component_type(component)
            if ctype == "forward" and getattr(component, "id", None):
                ids.append(str(component.id))
            elif ctype == "reply":
                scan(getattr(component, "chain", None))

    try:
        scan(event.get_messages())
    except Exception:
        pass
    for seg in current_segments(event):
        if seg.get("type") == "forward":
            value = (seg.get("data") or {}).get("id")
            if value and str(value) not in ids:
                ids.append(str(value))
    return ids


async def forward_images(event: Any, limit: int = 100) -> list[QQImage]:
    """读取当前消息或引用消息里的合并转发（get_forward_msg），返回其中的图片。"""
    if not is_onebot(event):
        return []
    found: list[QQImage] = []

    def collect(segments, depth: int) -> None:
        for seg in segments:
            if len(found) >= limit:
                return
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "").lower()
            if seg_type == "node" and depth < 3:
                data = seg.get("data") or {}
                collect(data.get("content") or data.get("message") or [], depth + 1)
                continue
            image = image_from_segment(seg, "forward")
            if image and not image.is_mface:
                found.append(image)

    for forward_id in _forward_ids(event):
        try:
            payload = await call(event, "get_forward_msg", message_id=forward_id)
        except Exception as exc:
            log.warning(f"[多模态处理器] 读取合并转发失败：{exc}")
            continue
        messages = []
        if isinstance(payload, dict):
            messages = payload.get("messages") or payload.get("message") or []
        for message in messages:
            if isinstance(message, dict):
                collect(message.get("content") or message.get("message") or [], 0)
    return found


async def _materialize(result: Any, dest: Path, max_bytes: int, allow_private: bool, timeout: int) -> Path | None:
    """处理 get_image / get_record / get_file 的返回：优先 base64，其次 URL，最后本机路径。"""
    if not isinstance(result, dict):
        return None
    encoded = result.get("base64")
    if isinstance(encoded, str) and encoded:
        if len(encoded) * 3 // 4 > max_bytes:
            raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
        data = await asyncio.to_thread(base64.b64decode, encoded)
        await asyncio.to_thread(dest.write_bytes, data)
        return dest
    url = str(result.get("url") or "")
    if url.startswith(("http://", "https://")):
        try:
            await dl.download(url, dest, max_bytes, timeout=timeout, allow_private=allow_private)
            return dest
        except MediaTooLarge:
            raise
        except MediaError as exc:
            log.debug(f"[多模态处理器] 协议端返回的链接下载失败：{exc}")
    local = str(result.get("file") or result.get("path") or "")
    if local.startswith("file://"):
        local = urlparse(local).path
    if local:
        path = Path(local)
        # 路径来自协议端接口返回（不是用户输入）；协议端在容器里时宿主机可能读不到
        if path.is_file():
            if path.stat().st_size > max_bytes:
                raise MediaTooLarge(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB）")
            data = await asyncio.to_thread(path.read_bytes)
            await asyncio.to_thread(dest.write_bytes, data)
            return dest
    return None


async def fetch_qq_image(event: Any, image: QQImage, workdir: Path, max_bytes: int, *, allow_private: bool, timeout: int) -> Path:
    dest = workdir / f"qq-{uuid.uuid4().hex[:10]}"
    errors: list[str] = []
    if image.url:
        try:
            await dl.download(image.url, dest, max_bytes, timeout=timeout, allow_private=allow_private,
                              headers={"Referer": "https://qun.qq.com/"} if image.is_mface else None)
            return dest
        except MediaTooLarge:
            raise
        except MediaError as exc:
            errors.append(str(exc))
    if image.file_id and not image.file_id.startswith(("http://", "https://")):
        try:
            result = await call(event, "get_image", file=image.file_id)
            path = await _materialize(result, dest, max_bytes, allow_private, timeout)
            if path:
                return path
            errors.append("协议端没有返回可读取的图片")
        except (MediaTooLarge, UnsupportedPlatform):
            raise
        except Exception as exc:
            errors.append(f"get_image 失败：{exc}")
    raise MediaError("获取 QQ 图片失败：" + ("；".join(errors) or "没有可用的图片地址"))


async def platform_fetch(event: Any, ref: MediaRef, workdir: Path, max_bytes: int, *, allow_private: bool = False, timeout: int = 60) -> Path | None:
    """通用媒体获取失败时的 QQ 兜底：通过协议端接口取图片、语音或文件。"""
    if event is None or ref.component is None or not is_onebot(event):
        return None
    comp = ref.component
    ctype = component_type(comp)
    file_id = str(getattr(comp, "file", "") or getattr(comp, "file_", "") or "")
    if not file_id or file_id.startswith(("http://", "https://", "base64://")):
        return None
    dest = workdir / f"qq-{uuid.uuid4().hex[:10]}"
    attempts: list[tuple[str, dict]] = []
    if ctype == "image":
        attempts.append(("get_image", {"file": file_id}))
    elif ctype == "record":
        attempts.append(("get_record", {"file": file_id, "out_format": "mp3"}))
    attempts.append(("get_file", {"file_id": file_id}))
    for action, params in attempts:
        try:
            result = await call(event, action, **params)
        except Exception as exc:
            log.debug(f"[多模态处理器] {action} 失败：{exc}")
            continue
        path = await _materialize(result, dest, max_bytes, allow_private, timeout)
        if path:
            return path
    return None


async def _file_segment(path: Path, name: str, as_base64: bool) -> dict:
    if as_base64:
        encoded = await asyncio.to_thread(lambda: base64.b64encode(path.read_bytes()).decode())
        file_value = "base64://" + encoded
    else:
        file_value = path.resolve().as_uri()
    return {"type": "file", "data": {"file": file_value, "name": name}}


async def send_file(event: Any, path: Path, name: str, target: str) -> None:
    """target: private（发给指令发送者）或 group（当前群）。先用 file:// 路径，失败再用 base64。"""
    group_id = event.get_group_id()
    sender_id = event.get_sender_id()
    if target == "group" and not group_id:
        target = "private"

    async def _send(segment: dict) -> None:
        if target == "group":
            await call(event, "send_group_msg", group_id=int(group_id), message=[segment])
        else:
            params: dict = {"user_id": int(sender_id), "message": [segment]}
            if group_id:
                params["group_id"] = int(group_id)  # 非好友时尝试群临时会话
            await call(event, "send_private_msg", **params)

    try:
        await _send(await _file_segment(path, name, False))
        return
    except Exception as exc:
        if path.stat().st_size > BASE64_SEND_LIMIT:
            raise
        log.info(f"[多模态处理器] 以本地路径发送文件失败，改用 base64：{exc}")
    await _send(await _file_segment(path, name, True))


async def send_image_with_summary(event: Any, path: Path, summary: str) -> None:
    """发送带外显文字（summary）的图片到当前会话。"""
    encoded = await asyncio.to_thread(lambda: base64.b64encode(path.read_bytes()).decode())
    segment = {
        "type": "image",
        "data": {"file": "base64://" + encoded, "summary": summary, "sub_type": 0},
    }
    group_id = event.get_group_id()
    if group_id:
        await call(event, "send_group_msg", group_id=int(group_id), message=[segment])
    else:
        await call(event, "send_private_msg", user_id=int(event.get_sender_id()), message=[segment])
