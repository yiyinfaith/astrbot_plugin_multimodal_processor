"""消息处理（/消息）：消息段解析、节点构造、权限、转发与合并转发。协议端用记录调用的假 bot 模拟。"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from conftest import FakeEvent, comp, plain, run

from mmp.core.commands import help_for, match_message_op
from mmp.core.config import Settings
from mmp.platforms import qq
from mmp.processors.message import nodes as nd
from mmp.processors.message.nodes import Node, ScriptError

BOT = "99999"


def t(text):
    return {"type": "text", "data": {"text": text}}


def at(q):
    return {"type": "at", "data": {"qq": str(q)}}


def img(url):
    return {"type": "image", "data": {"file": "x.jpg", "url": url}}


class FakeBot:
    """记录 call_action；responses 里的值可以是返回值、异常或函数。"""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.api = SimpleNamespace(call_action=self.call_action)

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        value = self.responses.get(action)
        if callable(value) and not isinstance(value, type):
            value = value(**params)
        if isinstance(value, Exception):
            raise value
        return value

    def actions(self):
        return [c[0] for c in self.calls]

    def last(self, action):
        return next(p for a, p in reversed(self.calls) if a == action)


def qq_event(segments, *, sender="10001", group="42", role="member", admin=False, message_id=777, responses=None, platform="aiocqhttp"):
    """构造一条 QQ 消息事件：raw_message 是协议端原始消息段，get_messages 是对应的 AstrBot 组件。"""
    comps = []
    for seg in segments:
        kind, data = seg["type"], seg.get("data") or {}
        if kind == "text":
            comps.append(plain(data["text"]))
        elif kind == "at":
            comps.append(comp("At", qq=data["qq"], name=""))
        elif kind == "reply":
            comps.append(comp("Reply", id=data["id"], chain=[], message_str=""))
        elif kind == "image":
            comps.append(comp("Image", url=data.get("url", ""), file=data.get("file", "")))
    text = " ".join(c.text for c in comps if c.type == "Plain")
    event = FakeEvent(comps, message_str=text.strip().lstrip("/"), sender_id=sender, group_id=group, platform=platform, admin=admin)
    event.message_obj = SimpleNamespace(
        raw_message={"message_id": message_id, "message": segments, "sender": {"user_id": int(sender), "role": role}, "self_id": int(BOT)},
        message_id=str(message_id),
    )
    event.bot = FakeBot(responses)
    event.get_sender_name = lambda: "发送者"
    return event


@pytest.fixture(autouse=True)
def _reset_state():
    from mmp.handlers import message as handler

    handler._last_use.clear()
    qq._NAME_CACHE.clear()
    handler.SEND_INTERVAL = 0
    yield


@pytest.fixture
def app(make_app):
    return make_app({"message": {"cooldown": 0}})


def send(app, event):
    run(app.handle_command(event, "消息"))
    return event.bot


# ---------------------------------------------------------------- 纯解析
def test_strip_command_drops_reply_auto_at_prefix_and_words():
    segs = [{"type": "reply", "data": {"id": "5"}}, at(123), t(" /消息 模拟 111111 你好"), img("http://a/1.png")]
    assert nd.strip_command(segs, ["消息", "模拟"], self_id=BOT, prefixes=["/"]) == [t("111111 你好"), img("http://a/1.png")]
    # @机器人唤醒、操作名后不加空格、不区分大小写
    segs = [at(BOT), t(" 消息 markdown# 标题")]
    assert nd.strip_command(segs, ["消息", "Markdown"], self_id=BOT) == [t("# 标题")]
    # 指令后的 @ 保留（是参数）
    segs = [t("/消息 转发 "), at(222)]
    assert nd.strip_command(segs, ["消息", "转发"], self_id=BOT, prefixes=["/"]) == [at(222)]


def test_parse_script_speakers_nicknames_and_images():
    segs = [t("111111 你好"), img("http://a/1.png"), t(" | "), at(222222), t(" :小明 嗯 | 我:本人 看图 ｜ 333333 再见 |")]
    lines = nd.parse_script(segs)
    assert [(l.user_id, l.nickname) for l in lines] == [("111111", ""), ("222222", "小明"), ("我", "本人"), ("333333", "")]
    assert lines[0].content == [t("你好"), img("http://a/1.png")]  # 图片属于它所在的那一段
    assert lines[1].content == [t("嗯")]
    assert nd.text_of(lines[3].content) == "再见"


@pytest.mark.parametrize("segs, message", [
    ([t("你好 | 111111 嗯")], "第 1 段要以 QQ 号"),
    ([t("111111 | 222222 嗯")], "第 1 段没有内容"),
    ([t("123 太短的号")], "第 1 段要以 QQ 号"),
    ([at("all"), t(" 大家好")], "第 1 段的 @ 不是有效的 QQ 用户"),
])
def test_parse_script_errors(segs, message):
    with pytest.raises(ScriptError, match=message):
        nd.parse_script(segs)


def test_options_and_face_ids_and_music():
    assert nd.take_options("标题=名场面 摘要＝看看 10 其他") == ({"source": "名场面", "summary": "看看"}, "10 其他")
    assert nd.take_segment_options([t("标题=甲 111111 你好")]) == ({"source": "甲"}, [t("111111 你好")])
    assert nd.face_ids("14", 3, 20) == [14, 14, 14]
    assert nd.face_ids("3~1", 2, 20) == [1, 1, 2, 2, 3, 3]
    ids = nd.face_ids(None, 5, 20, random.Random(1))
    assert len(ids) == 5 and set(ids) <= set(nd.COMMON_FACES)
    with pytest.raises(ValueError, match="最多"):
        nd.face_ids("1~30", 1, 20)
    with pytest.raises(ValueError, match="范围"):
        nd.face_ids("999", 1, 20)
    assert nd.MUSIC_PLATFORMS["网易云"] == "163"


def test_sendable_segment_filters_unsafe_types():
    segs = [t("a"), at("all"), at(123), {"type": "face", "data": {"id": 14}}, {"type": "record", "data": {"file": "x"}},
            {"type": "mface", "data": {"emoji_id": "abcdef", "summary": "[好耶]"}}, {"type": "json", "data": {"data": "{}"}}]
    cleaned = nd.clean(segs, qq.sendable_segment)
    assert [s["type"] for s in cleaned] == ["text", "at", "face", "image"]
    assert cleaned[1]["data"]["qq"] == "123"  # @全体成员 被去掉
    assert cleaned[3]["data"]["file"].startswith("https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/")


def test_node_segment_by_id_and_by_content():
    node = Node("1", "甲", [t("hi")], message_id="-5")
    assert node.segment() == {"type": "node", "data": {"id": "-5"}}
    assert node.segment(by_id=False) == {"type": "node", "data": {"user_id": "1", "nickname": "甲", "content": [t("hi")]}}
    entry = {"message_id": 8, "sender": {"user_id": 5, "nickname": "昵称", "card": "名片"}, "message": [t("x"), {"type": "reply", "data": {"id": 1}}]}
    built = nd.history_node(entry, qq.sendable_segment)
    assert (built.user_id, built.nickname, built.message_id, built.content) == ("5", "名片", "8", [t("x")])


def test_components_fallback_when_raw_message_missing():
    comps = [plain("/消息 文本 你好"), comp("Face", id=14), comp("Image", url="http://a/1.png", file=""), comp("At", qq="5")]
    segs = nd.components_to_segments(comps)
    assert [s["type"] for s in segs] == ["text", "face", "image", "at"]


def test_op_matching_and_help():
    assert match_message_op(" @全体 开会")[0].name == "@"
    assert match_message_op("MARKDOWN # a")[0].name == "Markdown"
    assert match_message_op("随便")[0] is None
    assert "模拟对话由使用者自行编写" in help_for("消息")
    assert help_for("消息 模拟").startswith("/消息 模拟")
    assert help_for("转发").startswith("/消息 转发")
    assert help_for("文本").startswith("【文本】")  # 与文本分类同名时，分类优先


def test_settings_defaults_and_clamp():
    m = Settings.load({"message": {"permission": "x", "max_nodes": 999, "protected_ids": "123, abc ,456", "forward_title": "  "}}).message
    assert m.permission == "all" and m.max_nodes == 100 and m.protected_ids == ["123", "456"] and m.forward_title == ""
    d = Settings().message
    assert d.enable and d.cross_chat_permission == "admin" and d.at_all_permission == "admin" and d.simulate_mark


# ---------------------------------------------------------------- 基础消息
def test_text_sends_segments_without_at_all(app):
    bot = send(app, qq_event([t("/消息 文本 大家好 "), at("all"), {"type": "face", "data": {"id": 14}}, img("http://a/1.png")]))
    params = bot.last("send_group_msg")
    assert params["group_id"] == 42
    assert [s["type"] for s in params["message"]] == ["text", "face", "image"]
    assert params["message"][0]["data"]["text"] == "大家好 "


def test_text_on_other_platform_and_dice_fallback(app):
    event = FakeEvent([plain("/消息 文本 你好")], message_str="消息 文本 你好", platform="webchat")
    run(app.handle_command(event, "消息"))
    event2 = FakeEvent([plain("/消息 骰子")], message_str="消息 骰子", platform="telegram")
    run(app.handle_command(event2, "消息"))
    event3 = FakeEvent([plain("/消息 模拟 111111 你好")], message_str="消息 模拟 111111 你好", platform="telegram")
    run(app.handle_command(event3, "消息"))
    texts = app.sender.texts()
    assert texts[0] == "你好" and texts[1].startswith("🎲") and "仅支持 OneBot v11" in texts[2]


def test_face_dice_rps_music_contact(app):
    bot = send(app, qq_event([t("/消息 表情 1~3 2")]))
    assert [s["data"]["id"] for s in bot.last("send_group_msg")["message"]] == ["1", "1", "2", "2", "3", "3"]
    assert send(app, qq_event([t("/消息 骰子")])).last("send_group_msg")["message"] == [{"type": "dice", "data": {}}]
    assert send(app, qq_event([t("/消息 猜拳")])).last("send_group_msg")["message"] == [{"type": "rps", "data": {}}]
    music = send(app, qq_event([t("/消息 音乐 网易云 1495009565")])).last("send_group_msg")["message"][0]
    assert music == {"type": "music", "data": {"type": "163", "id": "1495009565"}}
    contact = send(app, qq_event([t("/消息 推荐 "), at(123456)])).last("send_group_msg")["message"][0]
    assert contact == {"type": "contact", "data": {"type": "qq", "id": "123456"}}
    group = send(app, qq_event([t("/消息 推荐 群")], group="654321")).last("send_group_msg")["message"][0]
    assert group == {"type": "contact", "data": {"type": "group", "id": "654321"}}
    private = send(app, qq_event([t("/消息 骰子")], group=""))
    assert private.last("send_private_msg")["user_id"] == 10001


def test_markdown_nested_forward(app):
    bot = send(app, qq_event([t("/消息 Markdown # 标题\n正文")]))
    params = bot.last("send_group_forward_msg")
    outer = params["messages"][0]["data"]
    inner = outer["content"][0]["data"]
    assert inner["content"] == [{"type": "markdown", "data": {"content": "# 标题\n正文"}}]
    assert params["source"] == "Markdown 消息"


def test_limits_and_unknown_ops(app):
    send(app, qq_event([t("/消息 表情 1~50")]))
    send(app, qq_event([t("/消息 文本 " + "字" * 2001)]))
    send(app, qq_event([t("/消息 乱写")]))
    texts = app.sender.texts()
    assert "最多发送 20 个表情" in texts[0] and "超过上限 2000 字" in texts[1] and "没有「乱写」这个操作" in texts[2]
    send(app, qq_event([t("/消息")]))
    assert "【消息处理" in app.sender.texts()[-1]


# ---------------------------------------------------------------- 开关、权限、防循环
def test_switches_and_permissions(make_app):
    app = make_app({"message": {"enable": False}})
    bot = send(app, qq_event([t("/消息 骰子")]))
    assert "已在配置中关闭" in app.sender.texts()[-1] and not bot.calls

    app = make_app({"message": {"enable_simulate": False, "cooldown": 0}})
    bot = send(app, qq_event([t("/消息 模拟 111111 你好")]))
    assert "/消息 模拟 已在配置中关闭" in app.sender.texts()[-1] and not bot.calls

    app = make_app({"message": {"permission": "group_admin", "cooldown": 0}})
    send(app, qq_event([t("/消息 骰子")], role="member"))
    assert "只有群主、群管理员和 Bot 管理员" in app.sender.texts()[-1]
    bot = send(app, qq_event([t("/消息 骰子")], role="admin"))
    assert "send_group_msg" in bot.actions()


def test_ignores_messages_sent_by_bot_itself(app):
    bot = send(app, qq_event([t("/消息 骰子")], sender=BOT))
    assert not bot.calls and not app.sender.calls


def test_cooldown(make_app):
    app = make_app({"message": {"cooldown": 60}})
    send(app, qq_event([t("/消息 骰子")]))
    bot = send(app, qq_event([t("/消息 骰子")]))
    assert "操作太频繁" in app.sender.texts()[-1] and not bot.calls
    bot = send(app, qq_event([t("/消息 骰子")], admin=True))  # Bot 管理员不受限制
    assert "send_group_msg" in bot.actions()


# ---------------------------------------------------------------- @
def test_at_members_and_text(app):
    bot = send(app, qq_event([t("/消息 @ 123456 "), at(222222), t(" 开会了 "), at(333333)]))
    message = bot.last("send_group_msg")["message"]
    assert message[:2] == [at(123456), at(222222)]
    assert message[2] == t(" 开会了 ") and message[3] == at(333333)


def test_at_shortcut_without_op_name(app):
    bot = send(app, qq_event([t("/消息 "), at(222222), t(" 来")]))
    assert bot.last("send_group_msg")["message"] == [at(222222), t(" 来")]


def test_at_all_needs_permission_and_bot_admin(app):
    send(app, qq_event([t("/消息 @全体 集合")]))
    assert "只有Bot 管理员可以 @全体成员" in app.sender.texts()[-1]
    responses = {"get_group_member_info": {"user_id": 99999, "role": "member"}}
    send(app, qq_event([t("/消息 @全体 集合")], admin=True, responses=responses))
    assert "机器人不是群主或管理员" in app.sender.texts()[-1]
    responses = {"get_group_member_info": {"user_id": 99999, "role": "admin"}, "get_group_at_all_remain": {"can_at_all": True}}
    bot = send(app, qq_event([t("/消息 @全体 集合")], admin=True, responses=responses))
    assert bot.last("send_group_msg")["message"] == [at("all"), t(" 集合")]
    send(app, qq_event([t("/消息 @ 1")], group=""))
    assert "只能在群聊" in app.sender.texts()[-1]


# ---------------------------------------------------------------- 转发
REPLY = {"type": "reply", "data": {"id": "-555"}}


def test_forward_to_current_chat(app):
    bot = send(app, qq_event([REPLY, at(1234567), t(" /消息 转发")]))  # QQ 回复时自动插入的 @ 不是目标
    assert bot.calls == [("forward_group_single_msg", {"message_id": -555, "group_id": 42, "self_id": 99999})]
    assert app.sender.texts() == []


def test_forward_falls_back_to_resend(app):
    responses = {"forward_group_single_msg": RuntimeError("unknown action"),
                 "get_msg": {"message": [t("原文"), img("http://a/1.png"), {"type": "record", "data": {}}]}}
    bot = send(app, qq_event([REPLY, t("/消息 转发")], responses=responses))
    assert bot.last("send_group_msg")["message"] == [t("原文"), {"type": "image", "data": {"file": "http://a/1.png"}}]


def test_forward_cross_chat_permissions(app):
    send(app, qq_event([REPLY, t("/消息 转发 654321")]))
    assert "只有 Bot 管理员可以转发到其他群" in app.sender.texts()[-1]
    responses = {"get_group_list": [{"group_id": 111111}]}
    bot = send(app, qq_event([REPLY, t("/消息 转发 654321")], admin=True, responses=responses))
    assert "机器人不在群 654321" in app.sender.texts()[-1] and "forward_group_single_msg" not in bot.actions()
    responses = {"get_group_list": [{"group_id": 654321}], "get_friend_list": [{"user_id": 10001}]}
    bot = send(app, qq_event([REPLY, t("/消息 转发 654321 我")], admin=True, responses=responses))
    assert [c[0] for c in bot.calls if "forward" in c[0]] == ["forward_group_single_msg", "forward_friend_single_msg"]
    assert "已转发到：群 654321、10001 的私聊" in app.sender.texts()[-1]


def test_forward_member_mode(make_app):
    app = make_app({"message": {"cross_chat_permission": "member", "cooldown": 0}})
    responses = {"get_group_list": [{"group_id": 654321}], "get_group_member_info": RuntimeError("not member")}
    send(app, qq_event([REPLY, t("/消息 转发 654321")], responses=responses))
    assert "你不在群 654321" in app.sender.texts()[-1]
    send(app, qq_event([REPLY, t("/消息 转发 "), at(222222)]))
    assert "只能转发到自己的私聊" in app.sender.texts()[-1]
    send(app, qq_event([t("/消息 转发")]))
    assert "请引用要转发的消息" in app.sender.texts()[-1]


# ---------------------------------------------------------------- 合并与模拟
HISTORY = {"messages": [
    {"message_id": 3, "time": 3, "sender": {"user_id": 3, "nickname": "丙"}, "message": [t("三")]},
    {"message_id": 1, "time": 1, "sender": {"user_id": 1, "nickname": "甲"}, "message": [t("一")]},
    {"message_id": 777, "time": 4, "sender": {"user_id": 10001, "nickname": "我"}, "message": [t("/消息 合并 3")]},
    {"message_id": 2, "time": 2, "sender": {"user_id": 2, "card": "乙"}, "message": [t("二")]},
]}


def test_merge_recent_history_excludes_command(app):
    bot = send(app, qq_event([t("/消息 合并 3 标题=名场面")], responses={"get_group_msg_history": HISTORY}))
    assert bot.last("get_group_msg_history")["count"] == 4
    params = bot.last("send_group_forward_msg")
    assert [n["data"]["id"] for n in params["messages"]] == ["1", "2", "3"]
    assert params["source"] == "名场面" and "summary" not in params


def test_merge_quoted_and_content_fallback(app):
    def forward(**params):
        if "id" in params["messages"][0]["data"]:
            return RuntimeError("node id unsupported")
        return {"message_id": 1}

    responses = {"get_msg": {"message_id": -555, "sender": {"user_id": 5, "nickname": "原作者"}, "message": [t("引用的话")]},
                 "send_group_forward_msg": forward, "send_forward_msg": RuntimeError("no")}
    bot = send(app, qq_event([REPLY, t("/消息 合并")], responses=responses))
    sent = [p for a, p in bot.calls if a == "send_group_forward_msg"]
    assert sent[0]["messages"] == [{"type": "node", "data": {"id": "-555"}}]
    assert sent[-1]["messages"] == [{"type": "node", "data": {"user_id": "5", "nickname": "原作者", "content": [t("引用的话")]}}]


def test_merge_history_unsupported(app):
    send(app, qq_event([t("/消息 合并 5")], responses={"get_group_msg_history": RuntimeError("unknown action")}))
    assert "不支持读取历史消息" in app.sender.texts()[-1]


def test_simulate_builds_nodes_with_names_and_mark(app):
    responses = {
        "get_group_member_info": lambda user_id, **_: {"user_id": user_id, "card": "名片" if user_id == 111111 else "", "nickname": f"昵称{user_id}"},
        "get_msg": {"message_id": -555, "sender": {"user_id": 5, "nickname": "原作者"}, "message": [t("真的")]},
    }
    segs = [REPLY, t("/消息 模拟 标题=对话 111111 你好"), img("http://a/1.png"), t(" | 222222:小明 嗯 | 我 好")]
    bot = send(app, qq_event(segs, responses=responses))
    params = bot.last("send_group_forward_msg")
    nodes = params["messages"]
    assert nodes[0] == {"type": "node", "data": {"id": "-555"}}  # 引用的真实消息在最前
    assert nodes[1]["data"]["nickname"] == "名片" and nodes[1]["data"]["user_id"] == "111111"
    assert [s["type"] for s in nodes[1]["data"]["content"]] == ["text", "image"]
    assert nodes[2]["data"]["nickname"] == "小明"
    assert nodes[3]["data"]["user_id"] == "10001" and nodes[3]["data"]["nickname"] == "昵称10001"
    assert params["source"] == "对话" and params["summary"] == "模拟对话，非真实聊天记录"


def test_simulate_name_lookup_failure_falls_back_to_id(app):
    responses = {"get_group_member_info": RuntimeError("x"), "get_stranger_info": RuntimeError("y")}
    bot = send(app, qq_event([t("/消息 模拟 111111 你好")], responses=responses))
    assert bot.last("send_group_forward_msg")["messages"][0]["data"]["nickname"] == "111111"


def test_simulate_protection_limits_and_errors(make_app):
    app = make_app({"message": {"protected_ids": ["111111"], "max_nodes": 2, "cooldown": 0, "simulate_mark": False, "nickname_mode": "id"}})
    send(app, qq_event([t("/消息 模拟 111111 你好")]))
    assert "不允许被模拟" in app.sender.texts()[-1]
    bot = send(app, qq_event([t("/消息 模拟 111111 你好")], admin=True))
    assert "summary" not in bot.last("send_group_forward_msg")
    send(app, qq_event([t("/消息 模拟 1234567 a | 1234567 b | 1234567 c")]))
    assert "最多 2 条消息" in app.sender.texts()[-1]
    send(app, qq_event([t("/消息 模拟 你好")]))
    assert "第 1 段要以 QQ 号" in app.sender.texts()[-1] and "用法：/消息 模拟" in app.sender.texts()[-1]
    app = make_app({"message": {"simulate_permission": "admin", "cooldown": 0}})
    send(app, qq_event([t("/消息 模拟 1234567 你好")]))
    assert "只有Bot 管理员可以使用模拟对话" in app.sender.texts()[-1]


def test_protocol_errors_are_reported(app):
    class ActionFailed(Exception):
        result = {"wording": "消息发送失败"}

    responses = {"send_group_forward_msg": ActionFailed(), "send_forward_msg": ActionFailed()}
    send(app, qq_event([t("/消息 模拟 1234567 你好")], responses=responses))
    assert "发送失败，协议端返回：消息发送失败" in app.sender.texts()[-1]


def test_send_file_and_summary_share_send_message():
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.png"
        path.write_bytes(b"x")
        event = qq_event([t("/转换")])
        run(qq.send_file(event, path, "a.png", "private"))
        assert event.bot.last("send_private_msg")["user_id"] == 10001 and event.bot.last("send_private_msg")["group_id"] == 42
        run(qq.send_image_with_summary(event, path, "[好耶]"))
        assert event.bot.last("send_group_msg")["message"][0]["data"]["summary"] == "[好耶]"


# ---------------------------------------------------------------- 审计修复的回归测试
def test_usage_errors_do_not_start_cooldown(make_app):
    app = make_app({"message": {"cooldown": 60}})
    send(app, qq_event([t("/消息 表情 abc")]))
    assert "表情编号要写数字" in app.sender.texts()[-1]
    bot = send(app, qq_event([t("/消息 骰子")]))  # 改正后马上重发不受冷却影响
    assert "send_group_msg" in bot.actions()
    bot = send(app, qq_event([t("/消息 骰子")]))  # 真正发出后才开始冷却
    assert "操作太频繁" in app.sender.texts()[-1] and not bot.calls


def test_contact_rejects_extra_targets(app):
    for text in ("/消息 推荐 123456 789012", "/消息 推荐 群 123456 789012"):
        bot = send(app, qq_event([t(text)]))
        assert "一次只能推荐一个" in app.sender.texts()[-1] and not bot.calls


def test_forward_reports_partial_failures(app):
    responses = {"get_group_list": [{"group_id": 654321}], "get_friend_list": [{"user_id": 10001}],
                 "forward_group_single_msg": RuntimeError("unknown action"), "get_msg": None}
    bot = send(app, qq_event([REPLY, t("/消息 转发 654321 我")], admin=True, responses=responses))
    assert "forward_friend_single_msg" in bot.actions()  # 第一个目标失败后继续转发
    reply = app.sender.texts()[-1]
    assert "已转发到：10001 的私聊" in reply and "转发失败：群 654321（读取不到这条消息的内容" in reply


def test_text_drops_at_when_at_disabled(make_app):
    app = make_app({"message": {"enable_at": False, "cooldown": 0}})
    bot = send(app, qq_event([t("/消息 文本 你好 "), at(123456)]))
    assert bot.last("send_group_msg")["message"] == [t("你好")]


def test_at_all_unknown_bot_role(app):
    responses = {"get_group_member_info": RuntimeError("timeout")}
    send(app, qq_event([t("/消息 @全体 集合")], admin=True, responses=responses))
    assert "无法确认机器人在本群的身份" in app.sender.texts()[-1]


def test_huge_random_face_count_is_rejected_before_generating():
    with pytest.raises(ValueError, match="最多"):
        nd.face_ids(None, 10**12, 20)


def test_help_keyword_message_processing():
    assert help_for("消息处理").startswith("【消息处理")
