"""消息处理的纯逻辑：把 OneBot 消息段拆成「发言人 + 内容」、构造合并转发节点、解析参数。

不依赖 AstrBot 和网络。真实消息（引用、历史记录）与模拟消息都变成同一种 Node，
再由 Node.segment() 生成 OneBot 节点，发送统一交给 platforms/qq.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import re
from typing import Any, Callable, Iterable

Segment = dict
SEPARATORS = ("|", "｜")


@dataclass(slots=True)
class Node:
    """合并转发里的一条消息。真实消息带 message_id，协议端可以直接引用原消息；
    content 是同一条消息的内容副本（引用失败时用它重新构造），模拟消息只有 content。"""

    user_id: str
    nickname: str = ""
    content: list[Segment] = field(default_factory=list)
    message_id: str | None = None

    def segment(self, by_id: bool = True) -> Segment:
        if by_id and self.message_id is not None:
            return {"type": "node", "data": {"id": self.message_id}}
        return {"type": "node", "data": {"user_id": self.user_id, "nickname": self.nickname or self.user_id, "content": self.content}}


# ---------------------------------------------------------------- 消息段工具
def text_of(segments: Iterable[Segment]) -> str:
    return "".join(str((s.get("data") or {}).get("text") or "") for s in segments if s.get("type") == "text")


def text_seg(text: str) -> Segment:
    return {"type": "text", "data": {"text": text}}


def trim(segments: list[Segment]) -> list[Segment]:
    """去掉首尾空白和空文字段。"""
    out = [dict(s) for s in segments]
    while out and out[0].get("type") == "text" and not text_of([out[0]]).strip():
        out.pop(0)
    while out and out[-1].get("type") == "text" and not text_of([out[-1]]).strip():
        out.pop()
    if out and out[0].get("type") == "text":
        out[0] = text_seg(text_of([out[0]]).lstrip())
    if out and out[-1].get("type") == "text":
        out[-1] = text_seg(text_of([out[-1]]).rstrip())
    return [s for s in out if s.get("type") != "text" or text_of([s])]


def strip_command(segments: list[Segment], words: list[str], *, self_id: str = "", prefixes: Iterable[str] = ()) -> list[Segment]:
    """去掉指令之前的引用、@（@机器人，以及 QQ 回复时自动插入的 @原作者）、唤醒前缀和指令词
    （如「消息」「模拟」），返回其余消息段。其他位置的引用段也一并去掉。指令词不区分大小写。"""
    rest = [s for s in segments if s.get("type") != "reply"]
    start = next((i for i, s in enumerate(rest) if s.get("type") == "text" and text_of([s]).strip()), None)
    if start is None or any(s.get("type") not in {"at", "text"} for s in rest[:start]):
        # 指令前面有图片等其他内容：只去掉 @机器人
        while rest and rest[0].get("type") == "at" and str((rest[0].get("data") or {}).get("qq")) == str(self_id):
            rest.pop(0)
        return trim(rest)
    rest = rest[start:]
    text = text_of([rest[0]]).lstrip()
    for prefix in sorted((p for p in prefixes if p), key=len, reverse=True):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for word in words:
        text = text.lstrip()
        if text.lower().startswith(word.lower()):
            text = text[len(word):]
    return trim([text_seg(text), *rest[1:]]) if text.strip() else trim(rest[1:])


def components_to_segments(components: Iterable[Any]) -> list[Segment]:
    """协议端原始消息不可用时，把 AstrBot 消息组件转成 OneBot 消息段（文字、表情、图片、@）。"""
    out: list[Segment] = []
    for comp in components:
        kind = str(getattr(getattr(comp, "type", ""), "value", getattr(comp, "type", ""))).lower()
        if kind == "plain":
            out.append(text_seg(str(getattr(comp, "text", "") or "")))
        elif kind == "at":
            out.append({"type": "at", "data": {"qq": str(getattr(comp, "qq", "") or "")}})
        elif kind == "face":
            out.append({"type": "face", "data": {"id": str(getattr(comp, "id", ""))}})
        elif kind == "image":
            url = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "")
            if url:
                out.append({"type": "image", "data": {"file": url, "url": url}})
        elif kind == "reply":
            out.append({"type": "reply", "data": {"id": str(getattr(comp, "id", "") or "")}})
    return out


def clean(segments: Iterable[Segment], convert: Callable[[Segment], Segment | None]) -> list[Segment]:
    """只保留可以安全重新发送的消息段（convert 负责转换和过滤），并合并相邻文字。"""
    out: list[Segment] = []
    for seg in segments:
        converted = convert(seg)
        if converted is None:
            continue
        if converted.get("type") == "text" and out and out[-1].get("type") == "text":
            out[-1] = text_seg(text_of([out[-1]]) + text_of([converted]))
        else:
            out.append(converted)
    return trim(out)


# ---------------------------------------------------------------- 参数
OPTION_KEYS = {"标题": "source", "摘要": "summary"}
_OPTION_RE = re.compile(r"^\s*(标题|摘要)\s*[=＝]\s*(\S+)")


def take_options(text: str) -> tuple[dict[str, str], str]:
    """取出开头的「标题=…」「摘要=…」，返回 (选项, 其余文字)。"""
    options: dict[str, str] = {}
    while True:
        match = _OPTION_RE.match(text)
        if not match:
            return options, text.lstrip()
        options[OPTION_KEYS[match.group(1)]] = match.group(2)[:40]
        text = text[match.end():]


def take_segment_options(segments: list[Segment]) -> tuple[dict[str, str], list[Segment]]:
    if not segments or segments[0].get("type") != "text":
        return {}, segments
    options, rest = take_options(text_of([segments[0]]))
    return options, trim([text_seg(rest), *segments[1:]]) if rest else trim(segments[1:])


# ---------------------------------------------------------------- 模拟消息脚本
_SPEAKER_RE = re.compile(r"^\s*(\d{5,12}|我)(?:[:：]([^\s|｜]{1,20}))?(?=\s|$)")
_NICK_RE = re.compile(r"^\s*[:：]([^\s|｜]{1,20})(?=\s|$)")


class ScriptError(ValueError):
    pass


@dataclass(slots=True)
class Line:
    user_id: str  # 「我」会在调用方换成发送者
    nickname: str
    content: list[Segment]


def split_parts(segments: list[Segment]) -> list[list[Segment]]:
    """按 | 拆段；图片、表情、@ 归属它所在的那一段。"""
    parts: list[list[Segment]] = [[]]
    for seg in segments:
        if seg.get("type") != "text":
            parts[-1].append(seg)
            continue
        pieces = re.split("|".join(re.escape(s) for s in SEPARATORS), text_of([seg]))
        for index, piece in enumerate(pieces):
            if index:
                parts.append([])
            if piece:
                parts[-1].append(text_seg(piece))
    return [trim(part) for part in parts if trim(part)]


def parse_line(part: list[Segment], index: int) -> Line:
    """一段的开头是发言人：QQ 号、@某人 或「我」，后面可以紧跟「:昵称」，其余是内容。"""
    head = part[0]
    nickname = ""
    if head.get("type") == "at":
        user_id = str((head.get("data") or {}).get("qq") or "")
        if not user_id.isdigit():
            raise ScriptError(f"第 {index} 段的 @ 不是有效的 QQ 用户")
        rest = part[1:]
        if rest and rest[0].get("type") == "text":
            match = _NICK_RE.match(text_of([rest[0]]))
            if match:
                nickname = match.group(1)
                rest = [text_seg(text_of([rest[0]])[match.end():]), *rest[1:]]
    elif head.get("type") == "text":
        match = _SPEAKER_RE.match(text_of([head]))
        if not match:
            raise ScriptError(f"第 {index} 段要以 QQ 号、@某人 或「我」开头")
        user_id, nickname = match.group(1), match.group(2) or ""
        rest = [text_seg(text_of([head])[match.end():]), *part[1:]]
    else:
        raise ScriptError(f"第 {index} 段要以 QQ 号、@某人 或「我」开头")
    content = trim(rest)
    if not content:
        raise ScriptError(f"第 {index} 段没有内容")
    return Line(user_id, nickname, content)


def parse_script(segments: list[Segment]) -> list[Line]:
    parts = split_parts(segments)
    return [parse_line(part, i) for i, part in enumerate(parts, 1)]


# ---------------------------------------------------------------- 真实消息
def history_node(entry: dict, convert: Callable[[Segment], Segment | None]) -> Node | None:
    """get_msg / get_group_msg_history 返回的一条消息 → Node（同时保留 ID 和内容副本）。"""
    sender = entry.get("sender") if isinstance(entry.get("sender"), dict) else {}
    user_id = str(sender.get("user_id") or entry.get("user_id") or "")
    if not user_id:
        return None
    message = entry.get("message")
    content = clean(message if isinstance(message, list) else [], convert)
    message_id = entry.get("message_id")
    return Node(
        user_id=user_id,
        nickname=str(sender.get("card") or sender.get("nickname") or user_id),
        content=content,
        message_id=str(message_id) if message_id is not None else None,
    )


# ---------------------------------------------------------------- 其他参数
FACE_MAX_ID = 500
# 随机表情只从经典小黄脸里挑，避免抽到协议端不认识的编号
COMMON_FACES = tuple(range(0, 40)) + tuple(range(96, 115)) + (171, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183)


def face_ids(spec: str | None, count: int, limit: int, rng: random.Random | None = None) -> list[int]:
    """表情编号：单个数字、范围（起~止）或空（随机），每个重复 count 次，总数不超过 limit。"""
    rng = rng or random.Random()
    if count < 1:
        raise ValueError("数量至少为 1")
    if count > limit:  # 先检查再生成，避免超大数量占满内存
        raise ValueError(f"一次最多发送 {limit} 个表情")
    if not spec or spec == "随机":
        return [rng.choice(COMMON_FACES) for _ in range(count)]
    match = re.fullmatch(r"(\d{1,4})\s*[~～\-]\s*(\d{1,4})", spec)
    if match:
        start, end = sorted((int(match.group(1)), int(match.group(2))))
        base = list(range(start, end + 1))
    elif spec.isdigit():
        base = [int(spec)]
    else:
        raise ValueError("表情编号要写数字、范围（如 1~10）或「随机」")
    if any(i > FACE_MAX_ID for i in base):
        raise ValueError(f"表情编号范围是 0～{FACE_MAX_ID}")
    if len(base) * count > limit:
        raise ValueError(f"一次最多发送 {limit} 个表情")
    return [i for i in base for _ in range(count)]


MUSIC_PLATFORMS = {
    "qq": "qq", "qq音乐": "qq",
    "163": "163", "网易": "163", "网易云": "163", "网易云音乐": "163",
    "kugou": "kugou", "酷狗": "kugou",
    "kuwo": "kuwo", "酷我": "kuwo",
    "migu": "migu", "咪咕": "migu",
}
