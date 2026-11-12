"""QQ（OneBot v11）专属能力：读取原始消息段、取图，以及统一的消息发送、转发、合并转发和用户信息查询。

只有 aiocqhttp 平台会调用这里的函数；其他平台在入口处就会收到明确提示。
QQ 官方表情（商城表情 / mface）在 AstrBot 的消息解析里会被丢弃，所以这里直接读取
协议端的原始消息段（当前消息的 raw_message、引用消息走 get_msg）。

发送类接口只用 OneBot v11 标准动作和 NapCat / LLOneBot / SnowLuma 共有的扩展动作；
扩展动作失败时退回标准做法（见 forward_single、send_forward）。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
import time
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


async def call(event: Any, action: str, *, timeout: float | None = None, **params: Any) -> Any:
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    caller = getattr(api, "call_action", None) or getattr(bot, "call_action", None)
    if caller is None:
        raise UnsupportedPlatform("当前协议端不支持调用 OneBot 接口")
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw, dict) and raw.get("self_id"):
        params.setdefault("self_id", raw["self_id"])
    if timeout is None:
        return await caller(action, **params)
    return await asyncio.wait_for(caller(action, **params), timeout)


def onebot_id(value: Any) -> Any:
    """消息 ID、QQ 号、群号：纯数字（可为负）转成整数，其他保持字符串。"""
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else text


def _raw_segments(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, list):
            return [seg for seg in message if isinstance(seg, dict)]
    return []


def current_segments(event: Any) -> list[dict]:
    return _raw_segments(getattr(getattr(event, "message_obj", None), "raw_message", None))


def _raw_event(event: Any) -> dict:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    return raw if isinstance(raw, dict) else {}


def current_message_id(event: Any) -> str | None:
    value = _raw_event(event).get("message_id")
    if value is None:
        value = getattr(getattr(event, "message_obj", None), "message_id", None)
    return str(value) if value not in (None, "") else None


def sender_role(event: Any) -> str:
    """群聊中发送者的身份：owner / admin / member，取不到时为空字符串。"""
    sender = _raw_event(event).get("sender")
    return str(sender.get("role") or "") if isinstance(sender, dict) else ""


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
            payload = await call(event, "get_msg", message_id=onebot_id(rid))
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
    if target == "group" and not group_id:
        target = "private"

    async def _send(segment: dict) -> None:
        if target == "group":
            await send_message(event, [segment], group_id=group_id)
        else:  # 非好友时带上群号，尝试群临时会话
            await send_message(event, [segment], user_id=event.get_sender_id(), temp_group=group_id or None)

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
    await send_message(event, [segment])


# ---------------------------------------------------------------- 统一发送、转发与查询
SEND_TIMEOUT = 30
QUERY_TIMEOUT = 10


def _target(event: Any, group_id: Any, user_id: Any) -> tuple[str, Any]:
    """返回 ("group", 群号) 或 ("private", QQ 号)；都没给时发回当前会话。"""
    if group_id:
        return "group", onebot_id(group_id)
    if user_id:
        return "private", onebot_id(user_id)
    current_group = event.get_group_id()
    if current_group:
        return "group", onebot_id(current_group)
    return "private", onebot_id(event.get_sender_id())


async def send_message(event: Any, message: list[dict], *, group_id: Any = None, user_id: Any = None, temp_group: Any = None) -> Any:
    """发送一条由 OneBot 消息段组成的消息；不指定目标时发回当前会话。"""
    kind, target = _target(event, group_id, user_id)
    if kind == "group":
        return await call(event, "send_group_msg", timeout=SEND_TIMEOUT, group_id=target, message=message)
    params: dict = {"user_id": target, "message": message}
    if temp_group:
        params["group_id"] = onebot_id(temp_group)
    return await call(event, "send_private_msg", timeout=SEND_TIMEOUT, **params)


async def send_forward(event: Any, nodes: list[dict], *, group_id: Any = None, user_id: Any = None, meta: dict | None = None) -> Any:
    """发送合并转发。meta 可含 source（标题）、summary（底部摘要）、prompt（外显）、news（预览行），
    协议端不认识时会忽略。专用动作失败时再试 NapCat / LLOneBot 的通用动作 send_forward_msg。"""
    kind, target = _target(event, group_id, user_id)
    extra = {k: v for k, v in (meta or {}).items() if v}
    key = "group_id" if kind == "group" else "user_id"
    try:
        return await call(event, f"send_{kind}_forward_msg", timeout=SEND_TIMEOUT, **{key: target}, messages=nodes, **extra)
    except (UnsupportedPlatform, asyncio.TimeoutError):
        raise
    except Exception as exc:
        try:
            return await call(event, "send_forward_msg", timeout=SEND_TIMEOUT, message_type=kind, **{key: target}, messages=nodes, **extra)
        except Exception:
            raise exc from None


async def get_message(event: Any, message_id: Any) -> dict | None:
    try:
        result = await call(event, "get_msg", timeout=QUERY_TIMEOUT, message_id=onebot_id(message_id))
    except UnsupportedPlatform:
        raise
    except Exception as exc:
        log.info(f"[多模态处理器] get_msg {message_id} 失败：{exc}")
        return None
    return result if isinstance(result, dict) else None


async def forward_single(event: Any, message_id: Any, *, group_id: Any = None, user_id: Any = None) -> None:
    """把一条消息原样转发到目标会话。优先用扩展动作 forward_*_single_msg，
    协议端不支持时取回原消息（get_msg）重新发送。"""
    kind, target = _target(event, group_id, user_id)
    action = "forward_group_single_msg" if kind == "group" else "forward_friend_single_msg"
    key = "group_id" if kind == "group" else "user_id"
    try:
        await call(event, action, timeout=SEND_TIMEOUT, message_id=onebot_id(message_id), **{key: target})
        return
    except (UnsupportedPlatform, asyncio.TimeoutError):
        raise
    except Exception as exc:
        log.info(f"[多模态处理器] {action} 不可用，改为重新发送：{exc}")
    payload = await get_message(event, message_id)
    segments = [out for out in (sendable_segment(seg) for seg in _raw_segments(payload)) if out]
    if not segments:
        raise MediaError("读取不到这条消息的内容，可能已过期、被撤回，或含有无法重新发送的内容")
    await send_message(event, segments, **{key: target})


async def message_history(event: Any, count: int, *, anchor: Any = None) -> list[dict]:
    """当前会话最近的消息（旧 → 新）。anchor 为某条消息 ID 时返回它及更早的消息。"""
    group_id = event.get_group_id()
    params: dict = {"count": count}
    if anchor is not None:  # SnowLuma 用 message_id，NapCat / LLOneBot 用 message_seq，两个都传
        params["message_id"] = params["message_seq"] = onebot_id(anchor)
    if group_id:
        result = await call(event, "get_group_msg_history", timeout=QUERY_TIMEOUT, group_id=onebot_id(group_id), **params)
    else:
        result = await call(event, "get_friend_msg_history", timeout=QUERY_TIMEOUT, user_id=onebot_id(event.get_sender_id()), **params)
    messages = result.get("messages") if isinstance(result, dict) else None
    items = [m for m in messages or [] if isinstance(m, dict)]

    def order(m: dict) -> tuple[int, int]:
        def num(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        return num(m.get("time")), num(m.get("message_seq") or m.get("real_seq"))

    items.sort(key=order)
    return items


_NAME_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}
_NAME_TTL = 600
_NAME_CACHE_MAX = 512


async def display_name(event: Any, user_id: Any, *, group_id: Any = None, mode: str = "card") -> str:
    """转发节点显示的昵称。mode: card（群名片优先）、nickname（QQ 昵称）、id（直接用 QQ 号）。
    通过协议端查询并缓存 10 分钟；查询失败、超时或用户不存在时用 QQ 号。"""
    uid = str(user_id)
    if mode == "id":
        return uid
    key = (str(group_id or ""), uid, mode)
    now = time.monotonic()
    cached = _NAME_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]
    name = ""
    if group_id:
        try:
            info = await call(event, "get_group_member_info", timeout=QUERY_TIMEOUT, group_id=onebot_id(group_id), user_id=onebot_id(uid))
            if isinstance(info, dict):
                name = str((info.get("card") if mode == "card" else "") or info.get("nickname") or "")
        except UnsupportedPlatform:
            raise
        except Exception as exc:
            log.debug(f"[多模态处理器] 查询群成员 {uid} 失败：{exc}")
    if not name:
        try:
            info = await call(event, "get_stranger_info", timeout=QUERY_TIMEOUT, user_id=onebot_id(uid))
            if isinstance(info, dict):
                name = str(info.get("nickname") or info.get("nick") or "")
        except UnsupportedPlatform:
            raise
        except Exception as exc:
            log.debug(f"[多模态处理器] 查询用户 {uid} 失败：{exc}")
    name = name.strip() or uid
    if len(_NAME_CACHE) >= _NAME_CACHE_MAX:
        _NAME_CACHE.clear()
    _NAME_CACHE[key] = (name, now + _NAME_TTL)
    return name


async def member_role(event: Any, group_id: Any, user_id: Any) -> str | None:
    """群成员身份（owner / admin / member）；不在群里或查询失败时返回 None。"""
    try:
        info = await call(event, "get_group_member_info", timeout=QUERY_TIMEOUT, group_id=onebot_id(group_id), user_id=onebot_id(user_id), no_cache=True)
    except UnsupportedPlatform:
        raise
    except Exception:
        return None
    if not isinstance(info, dict) or not info.get("user_id"):
        return None
    return str(info.get("role") or "member")


async def _id_set(event: Any, action: str, key: str) -> set[str] | None:
    try:
        result = await call(event, action, timeout=QUERY_TIMEOUT)
    except UnsupportedPlatform:
        raise
    except Exception as exc:
        log.info(f"[多模态处理器] {action} 失败：{exc}")
        return None
    if isinstance(result, dict):
        result = result.get("data") or result.get("list") or []
    return {str(item.get(key)) for item in result or [] if isinstance(item, dict) and item.get(key)}


async def bot_in_group(event: Any, group_id: Any) -> bool | None:
    """机器人是否在该群；查询失败时返回 None。"""
    groups = await _id_set(event, "get_group_list", "group_id")
    return None if groups is None else str(group_id) in groups


async def is_friend(event: Any, user_id: Any) -> bool | None:
    """是否为机器人的好友；查询失败时返回 None。"""
    friends = await _id_set(event, "get_friend_list", "user_id")
    return None if friends is None else str(user_id) in friends


def sendable_segment(seg: dict) -> dict | None:
    """把收到的消息段转成可以再次发送的形式：只保留文字、QQ 表情、图片和 @某人（不含 @全体成员），
    图片和官方表情用 image_from_segment 解析出的地址。其他类型返回 None。"""
    seg_type = str(seg.get("type") or "").lower()
    data = seg.get("data") or {}
    if seg_type == "text":
        text = str(data.get("text") or "")
        return {"type": "text", "data": {"text": text}} if text else None
    if seg_type == "face":
        return {"type": "face", "data": {"id": str(data.get("id"))}} if data.get("id") is not None else None
    if seg_type == "at":
        qq = str(data.get("qq") or "")
        return {"type": "at", "data": {"qq": qq}} if qq.isdigit() else None
    if seg_type in {"image", "mface"}:
        image = image_from_segment(seg, "message")
        if image is None:
            return None
        out: dict = {"file": image.url or image.file_id}
        if image.summary:
            out["summary"] = image.summary
        return {"type": "image", "data": out}
    return None
