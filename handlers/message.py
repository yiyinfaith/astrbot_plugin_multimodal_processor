"""/消息：消息处理。发送文本、表情、骰子、猜拳、音乐与推荐卡片、Markdown、@，转发、合并真实消息和模拟对话。

消息段的解析和节点构造在 processors/message/nodes.py，OneBot 接口调用都在 platforms/qq.py。
真实消息（引用、历史记录）和模拟消息都构造成同一种 Node，用同一个 _send_nodes 发送。
除文本、骰子、猜拳外只支持 QQ（OneBot v11），其他平台在入口处明确提示。
"""

from __future__ import annotations

import asyncio
import random
import re
import time

from ..core.app import Invocation
from ..core.commands import MESSAGE_HELP_WORDS, MESSAGE_OPS, MessageOp, match_message_op, message_help
from ..core.errors import FeatureDisabled, ProcessingError, UnsupportedPlatform, UserError
from ..core.log import log
from ..platforms import qq
from ..processors.message import nodes as nd
from ..processors.message.nodes import Node, ScriptError
from .text import quoted_text

MAX_TARGETS = 5
DEFAULT_MERGE_COUNT = 10
SEND_INTERVAL = 1.0  # 连续发往多个会话时的间隔，避免协议端风控
SIMULATE_MARK = "模拟对话，非真实聊天记录"
LEVEL_NAMES = {"group_admin": "群主、群管理员和 Bot 管理员", "admin": "Bot 管理员"}
_AT_ALL_WORDS = {"全体", "全体成员", "all"}
_OPS = {op.name: op for op in MESSAGE_OPS}

_last_use: dict[str, float] = {}


def _usage(op: MessageOp, message: str) -> UserError:
    return UserError(f"{message}\n用法：{op.usage_text()}")


def _describe(exc: Exception) -> str:
    result = getattr(exc, "result", None)
    if isinstance(result, dict):
        text = result.get("wording") or result.get("message") or result.get("msg")
        if text:
            return str(text)[:120]
    return (str(exc) or type(exc).__name__)[:120]


async def _allowed(inv: Invocation, level: str) -> bool:
    if level == "all" or inv.is_admin:
        return True
    if level == "group_admin":
        return qq.sender_role(inv.event) in {"owner", "admin"}
    return False


def _check_cooldown(inv: Invocation) -> None:
    seconds = inv.settings.message.cooldown
    if seconds <= 0 or inv.is_admin:
        return
    now = time.monotonic()
    last = _last_use.get(inv.user_key)
    if last is not None and now - last < seconds:
        raise UserError(f"操作太频繁，请 {seconds - int(now - last)} 秒后再试")
    if len(_last_use) > 1000:
        _last_use.clear()
    _last_use[inv.user_key] = now


def _segments(inv: Invocation, words: list[str]) -> list[dict]:
    """指令之后的消息段（QQ 用协议端原始消息段，其他平台由 AstrBot 组件转换）。"""
    raw = qq.current_segments(inv.event) or nd.components_to_segments(inv.event.get_messages())
    return nd.strip_command(raw, words, self_id=str(inv.event.get_self_id()), prefixes=inv.app._wake_prefixes())


def _check_size(op: MessageOp, inv: Invocation, contents: list[list[dict]]) -> None:
    s = inv.settings.message
    chars = sum(len(nd.text_of(c)) for c in contents)
    if chars > s.max_chars:
        raise _usage(op, f"内容共 {chars} 字，超过上限 {s.max_chars} 字")
    for content in contents:
        extra = sum(1 for seg in content if seg.get("type") != "text")
        if extra > s.max_segments:
            raise _usage(op, f"一条消息最多 {s.max_segments} 个表情 / @ / 图片")


# ---------------------------------------------------------------- 入口
async def message(inv: Invocation) -> bool:
    event = inv.event
    if str(event.get_sender_id()) == str(event.get_self_id()):
        return True  # 机器人自己发出的消息（例如转发出去的指令）不再处理，防止循环
    op, rest = match_message_op(inv.parsed.rest)
    words = [inv.parsed.used_name] + ([op.name] if op else [])
    if op is None:
        head = _segments(inv, words)
        if head and head[0].get("type") == "at":  # 「/消息 @某人 文字」省略了操作名 @
            op = _OPS["@"]
        elif rest.strip() and rest.split()[0] not in MESSAGE_HELP_WORDS:
            raise UserError(f"没有「{rest.split()[0]}」这个操作。\n{message_help()}")
        else:
            await inv.reply(message_help())
            return True
    s = inv.settings.message
    if not getattr(s, f"enable_{op.feature}"):
        raise FeatureDisabled(f"/消息 {op.name} 已在配置中关闭")
    if not await _allowed(inv, s.permission):
        raise UserError(f"只有{LEVEL_NAMES[s.permission]}可以使用 /消息")
    if op.qq_only:
        qq.require_onebot(event, f"/消息 {op.name} ")
    _check_cooldown(inv)
    segs = _segments(inv, words)
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="message"):
        await inv.run_timed(_guard(op, inv, segs))
    return True


async def _guard(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    """把协议端的报错、超时转成用户能看懂的提示。"""
    try:
        await HANDLERS_BY_OP[op.name](op, inv, segs)
    except (UserError, asyncio.CancelledError):
        raise
    except asyncio.TimeoutError as exc:
        raise UserError("协议端响应超时，请稍后再试") from exc
    except Exception as exc:
        log.warning(f"[多模态处理器] /消息 {op.name} 失败：{exc!r}")
        raise ProcessingError(f"发送失败，协议端返回：{_describe(exc)}") from exc


# ---------------------------------------------------------------- 基础消息
async def op_text(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    if qq.is_onebot(inv.event):
        content = nd.clean(segs, qq.sendable_segment)
        if not content:
            raise _usage(op, "请写上要发送的内容")
        _check_size(op, inv, [content])
        await qq.send_message(inv.event, content)
        return
    text = nd.text_of(segs).strip()
    if not text:
        raise _usage(op, "请写上要发送的内容")
    _check_size(op, inv, [[nd.text_seg(text)]])
    await inv.reply(text)


async def op_face(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    tokens = nd.text_of(segs).split()
    if len(tokens) > 2:
        raise _usage(op, "参数太多")
    count = 1
    if len(tokens) == 2:
        if not tokens[1].isdigit():
            raise _usage(op, "数量要写数字")
        count = int(tokens[1])
    try:
        ids = nd.face_ids(tokens[0] if tokens else None, count, inv.settings.message.max_segments)
    except ValueError as exc:
        raise _usage(op, str(exc)) from exc
    await qq.send_message(inv.event, [{"type": "face", "data": {"id": str(i)}} for i in ids])


async def op_dice(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    if qq.is_onebot(inv.event):
        await qq.send_message(inv.event, [{"type": "dice", "data": {}}])
    else:
        await inv.reply(f"🎲 掷出了 {random.randint(1, 6)} 点")


async def op_rps(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    if qq.is_onebot(inv.event):
        await qq.send_message(inv.event, [{"type": "rps", "data": {}}])
    else:
        await inv.reply(random.choice(("✊ 石头", "✌️ 剪刀", "✋ 布")))


# ---------------------------------------------------------------- 卡片
async def op_music(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    tokens = nd.text_of(segs).split()
    if len(tokens) != 2:
        raise _usage(op, "请写上平台和歌曲 ID")
    platform = nd.MUSIC_PLATFORMS.get(tokens[0].lower())
    if platform is None:
        raise _usage(op, f"不支持的平台「{tokens[0]}」")
    if not re.fullmatch(r"[A-Za-z0-9]{1,40}", tokens[1]):
        raise _usage(op, "歌曲 ID 只能是字母和数字")
    await qq.send_message(inv.event, [{"type": "music", "data": {"type": platform, "id": tokens[1]}}])


async def op_contact(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    event = inv.event
    tokens = nd.text_of(segs).split()
    ats = [str((s.get("data") or {}).get("qq")) for s in segs if s.get("type") == "at"]
    if tokens and tokens[0] == "群":
        kind = "group"
        target = tokens[1] if len(tokens) > 1 else str(event.get_group_id() or "")
        if not target:
            raise _usage(op, "私聊里推荐群要写群号")
    elif ats:
        kind, target = "qq", ats[0]
    elif tokens:
        kind, target = "qq", tokens[0]
    else:
        kind, target = "qq", str(event.get_sender_id())
    if not re.fullmatch(r"\d{5,12}", target) or len(tokens) > 2:
        raise _usage(op, "QQ 号或群号要写 5～12 位数字")
    await qq.send_message(event, [{"type": "contact", "data": {"type": kind, "id": target}}])


async def op_markdown(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    event = inv.event
    text = nd.text_of(segs).strip() or quoted_text(event).strip()
    if not text:
        raise _usage(op, "请写上 Markdown 内容，或引用一条文字消息")
    _check_size(op, inv, [[nd.text_seg(text)]])
    self_id = str(event.get_self_id())
    name = str(getattr(event, "get_sender_name", lambda: "")() or self_id)
    inner = Node(self_id, name, [{"type": "markdown", "data": {"content": text}}])
    # QQ 只在合并转发里渲染普通账号发出的 Markdown，再套一层转发兼容新版客户端
    outer = Node(self_id, name, [inner.segment()])
    await qq.send_forward(event, [outer.segment()], meta={"source": "Markdown 消息", "summary": "[Markdown 消息]", "prompt": "[Markdown 消息]"})


# ---------------------------------------------------------------- @
async def op_at(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    event = inv.event
    s = inv.settings.message
    group_id = str(event.get_group_id() or "")
    if not group_id:
        raise UserError("@ 只能在群聊里使用")
    self_id = str(event.get_self_id())
    targets: list[str] = []
    at_all = False
    content: list[dict] = []
    leading = True
    for seg in segs:
        if not leading:
            content.append(seg)
            continue
        kind = seg.get("type")
        if kind == "at":
            value = str((seg.get("data") or {}).get("qq") or "")
            if value == "all":
                at_all = True
            elif value.isdigit() and value != self_id and value not in targets:
                targets.append(value)
            continue
        if kind == "text":
            text = nd.text_of([seg])
            while True:
                match = re.match(r"\s*(全体成员|全体|all|\d{5,12})(?=\s|$)", text, re.I)
                if not match:
                    break
                word = match.group(1)
                if word.lower() in _AT_ALL_WORDS:
                    at_all = True
                elif word != self_id and word not in targets:
                    targets.append(word)
                text = text[match.end():]
            if text.strip():
                leading = False
                content.append(nd.text_seg(text))
            continue
        leading = False
        content.append(seg)
    if not targets and not at_all:
        raise _usage(op, "请写上要 @ 的 QQ 号、@某人 或「全体」")
    body = nd.clean(content, qq.sendable_segment)
    message = ([{"type": "at", "data": {"qq": "all"}}] if at_all else []) + [{"type": "at", "data": {"qq": t}} for t in targets]
    if len(message) > s.max_segments:
        raise _usage(op, f"一次最多 @ {s.max_segments} 人")
    if body:
        if body[0].get("type") == "text":
            body[0] = nd.text_seg(" " + nd.text_of([body[0]]))
        else:
            body.insert(0, nd.text_seg(" "))
    _check_size(op, inv, [message + body])
    if at_all:
        await _check_at_all(inv, group_id)
    await qq.send_message(event, message + body)


async def _check_at_all(inv: Invocation, group_id: str) -> None:
    level = inv.settings.message.at_all_permission
    if level == "off":
        raise FeatureDisabled("@全体成员 已在配置中关闭")
    if not await _allowed(inv, level):
        raise UserError(f"只有{LEVEL_NAMES[level]}可以 @全体成员")
    role = await qq.member_role(inv.event, group_id, inv.event.get_self_id())
    if role not in {"owner", "admin"}:
        raise UserError("机器人不是群主或管理员，无法 @全体成员")
    try:  # NapCat / SnowLuma 的扩展接口；不支持时跳过
        remain = await qq.call(inv.event, "get_group_at_all_remain", timeout=qq.QUERY_TIMEOUT, group_id=qq.onebot_id(group_id))
    except UnsupportedPlatform:
        raise
    except Exception:
        return
    if isinstance(remain, dict) and remain.get("can_at_all") is False:
        raise UserError("今天的 @全体成员 次数已用完")


# ---------------------------------------------------------------- 转发
async def op_forward(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    event = inv.event
    rid = qq.reply_id(event)
    if not rid:
        raise _usage(op, "请引用要转发的消息")
    sender = str(event.get_sender_id())
    targets: list[tuple[str, str]] = []
    for seg in segs:
        if seg.get("type") == "at":
            value = str((seg.get("data") or {}).get("qq") or "")
            if value.isdigit():
                targets.append(("private", value))
            continue
        if seg.get("type") != "text":
            raise _usage(op, "目标只能是群号、@某人 或「我」")
        for word in nd.text_of([seg]).split():
            if word == "我":
                targets.append(("private", sender))
            elif re.fullmatch(r"\d{5,12}", word):
                targets.append(("group", word))
            else:
                raise _usage(op, f"看不懂的目标「{word}」")
    targets = list(dict.fromkeys(targets))
    if len(targets) > MAX_TARGETS:
        raise _usage(op, f"一次最多转发到 {MAX_TARGETS} 个会话")
    current = ("group", str(event.get_group_id())) if event.get_group_id() else ("private", sender)
    targets = targets or [current]
    for target in targets:
        if target != current:
            await _check_cross_chat(inv, *target)
    done: list[str] = []
    failed: list[str] = []
    for index, (kind, target) in enumerate(targets):
        if index:
            await asyncio.sleep(SEND_INTERVAL)
        label = f"群 {target}" if kind == "group" else f"{target} 的私聊"
        try:
            await qq.forward_single(event, rid, **({"group_id": target} if kind == "group" else {"user_id": target}))
            done.append(label)
        except (UserError, UnsupportedPlatform):
            raise
        except Exception as exc:
            log.warning(f"[多模态处理器] 转发到 {label} 失败：{exc!r}")
            failed.append(f"{label}（{_describe(exc)}）")
    if targets == [current] and not failed:
        return
    lines = []
    if done:
        lines.append("已转发到：" + "、".join(done))
    if failed:
        lines.append("转发失败：" + "、".join(failed))
    await inv.reply("\n".join(lines))


async def _check_cross_chat(inv: Invocation, kind: str, target: str) -> None:
    event = inv.event
    level = inv.settings.message.cross_chat_permission
    if level == "off":
        raise UserError("只能转发到当前会话，转发到其他群或私聊已在配置中关闭")
    admin = inv.is_admin
    if level == "admin" and not admin:
        raise UserError("只有 Bot 管理员可以转发到其他群或私聊")
    sender = str(event.get_sender_id())
    if kind == "group":
        inside = await qq.bot_in_group(event, target)
        if inside is None:
            raise UserError(f"无法确认机器人是否在群 {target}，已取消转发")
        if not inside:
            raise UserError(f"机器人不在群 {target}")
        if not admin and await qq.member_role(event, target, sender) is None:
            raise UserError(f"你不在群 {target}，不能转发过去")
        return
    if not admin and target != sender:
        raise UserError("只能转发到自己的私聊")
    friend = await qq.is_friend(event, target)
    if friend is None:
        raise UserError(f"无法确认 {target} 是否为机器人的好友，已取消转发")
    if not friend:
        raise UserError(f"{target} 不是机器人的好友，无法私聊转发")


# ---------------------------------------------------------------- 合并转发（真实消息 / 模拟对话）
async def _send_nodes(inv: Invocation, nodes: list[Node], meta: dict) -> None:
    """优先让协议端按消息 ID 引用真实消息（保留原图和原发送者）；不支持时改用内容副本。"""
    event = inv.event
    try:
        await qq.send_forward(event, [n.segment(by_id=True) for n in nodes], meta=meta)
        return
    except (UserError, UnsupportedPlatform, asyncio.TimeoutError):
        raise
    except Exception as exc:
        if not any(n.message_id is not None for n in nodes):
            raise
        log.info(f"[多模态处理器] 按消息 ID 合并转发失败，改用内容副本：{exc!r}")
    usable = [n for n in nodes if n.content]
    if not usable:
        raise UserError("这些消息无法合并转发（内容为空或类型不支持）")
    await qq.send_forward(event, [n.segment(by_id=False) for n in usable], meta=meta)


async def op_merge(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    event = inv.event
    s = inv.settings.message
    options: dict[str, str] = {}
    count: int | None = None
    words: list[str] = []
    for token in nd.text_of(segs).split():  # 条数、标题=…、摘要=… 顺序不限，其余文字当标题
        found, rest = nd.take_options(token)
        if found and not rest:
            options.update(found)
        elif token.isdigit() and count is None and not words:
            count = int(token)
        else:
            words.append(token)
    if count is not None and not 1 <= count <= s.max_nodes:
        raise _usage(op, f"条数范围是 1～{s.max_nodes}")
    if words:
        options.setdefault("source", " ".join(words)[:40])
    rid = qq.reply_id(event)
    if rid and count in (None, 1):
        entry = await qq.get_message(event, rid)
        if entry is None:
            raise UserError("读取不到引用的消息，可能已过期或被撤回")
        entries = [entry]
    else:
        wanted = count or min(DEFAULT_MERGE_COUNT, s.max_nodes)
        try:
            entries = await qq.message_history(event, wanted + (0 if rid else 1), anchor=rid)
        except (UnsupportedPlatform, asyncio.TimeoutError):
            raise
        except Exception as exc:
            log.info(f"[多模态处理器] 读取历史消息失败：{exc!r}")
            raise UserError("当前协议端不支持读取历史消息；可以引用一条消息只合并这一条") from exc
        own_id = qq.current_message_id(event)
        entries = [e for e in entries if str(e.get("message_id")) != own_id][-wanted:]
    built = [nd.history_node(e, qq.sendable_segment) for e in entries]
    nodes = [n for n in built if n is not None]
    if not nodes:
        raise UserError("没有可以合并的消息")
    await _send_nodes(inv, nodes, {"source": options.get("source") or s.forward_title, "summary": options.get("summary", "")})


async def op_simulate(op: MessageOp, inv: Invocation, segs: list[dict]) -> None:
    event = inv.event
    s = inv.settings.message
    if not await _allowed(inv, s.simulate_permission):
        raise UserError(f"只有{LEVEL_NAMES[s.simulate_permission]}可以使用模拟对话")
    options, body = nd.take_segment_options(segs)
    try:
        lines = nd.parse_script(body)
    except ScriptError as exc:
        raise _usage(op, str(exc)) from exc
    if not lines:
        raise _usage(op, "请写上对话内容")
    nodes: list[Node] = []
    rid = qq.reply_id(event)
    if rid:  # 引用的真实消息排在最前面
        entry = await qq.get_message(event, rid)
        node = nd.history_node(entry, qq.sendable_segment) if entry else None
        if node is None:
            raise UserError("读取不到引用的消息，可能已过期或被撤回")
        nodes.append(node)
    if len(nodes) + len(lines) > s.max_nodes:
        raise _usage(op, f"最多 {s.max_nodes} 条消息")
    sender = str(event.get_sender_id())
    group_id = event.get_group_id() or None
    resolved: list[tuple[str, str, list[dict]]] = []
    for index, line in enumerate(lines, 1):
        user_id = sender if line.user_id == "我" else line.user_id
        if user_id in s.protected_ids and not inv.is_admin:
            raise UserError(f"QQ {user_id} 不允许被模拟")
        content = nd.clean(line.content, qq.sendable_segment)
        if not content:
            raise _usage(op, f"第 {index} 段没有可以发送的内容")
        resolved.append((user_id, line.nickname, content))
    _check_size(op, inv, [content for _, _, content in resolved])
    # 没写昵称的 QQ 号并发查询一次（有缓存），避免逐条等待
    lookup = list(dict.fromkeys(uid for uid, nickname, _ in resolved if not nickname))
    names = dict(zip(lookup, await asyncio.gather(*(qq.display_name(event, uid, group_id=group_id, mode=s.nickname_mode) for uid in lookup))))
    nodes.extend(Node(uid, nickname or names[uid], content) for uid, nickname, content in resolved)
    summary = SIMULATE_MARK if s.simulate_mark else options.get("summary", "")
    await _send_nodes(inv, nodes, {"source": options.get("source") or s.forward_title, "summary": summary})


HANDLERS_BY_OP = {
    "文本": op_text, "表情": op_face, "骰子": op_dice, "猜拳": op_rps,
    "音乐": op_music, "推荐": op_contact, "Markdown": op_markdown,
    "@": op_at, "转发": op_forward, "合并": op_merge, "模拟": op_simulate,
}
assert set(HANDLERS_BY_OP) == set(_OPS), "MESSAGE_OPS 与处理函数不一致"

HANDLERS = {"message": message}
