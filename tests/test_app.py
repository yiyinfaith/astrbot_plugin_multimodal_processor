"""指令分发与统一错误处理：用假事件跑完整流程（不依赖 AstrBot）。"""

from __future__ import annotations

import asyncio

from PIL import Image

from conftest import FakeEvent, comp, image, plain, run

from mmp.handlers.mirage import session_key


def make_png(path, size=(20, 10), color=(200, 30, 30, 255)):
    Image.new("RGBA", size, color).save(path)
    return path


def task_dirs(app):
    return list(app.tasks.root.glob("task-*"))


def test_explicit_command_processes_and_cleans_up(tmp_path, make_app):
    app = make_app()
    src = make_png(tmp_path / "a.png")
    event = FakeEvent([plain("/反色"), image(src)], message_str="反色")
    run(app.handle_command(event, "反色"))
    assert app.sender.kinds() == ["image"]
    sent_path = app.sender.calls[0][1]
    assert not sent_path.exists(), "发送完成后应删除临时文件"
    assert task_dirs(app) == []
    assert event.stopped


def test_quoted_image_is_preferred(tmp_path, make_app):
    app = make_app()
    quoted = make_png(tmp_path / "q.png", size=(30, 10))
    attached = make_png(tmp_path / "b.png", size=(5, 5))
    reply = comp("Reply", id="1", chain=[image(quoted)])
    event = FakeEvent([reply, plain("/图片信息"), image(attached)], message_str="图片信息")
    run(app.handle_command(event, "图片信息"))
    assert "30 x 10" in app.sender.texts()[0]


def test_missing_media_explicit_vs_passive(tmp_path, make_app):
    app = make_app({"general": {"match_mode": True}})
    explicit = FakeEvent([plain("/反色")], message_str="反色")
    run(app.handle_command(explicit, "反色"))
    assert "请引用一张图片" in app.sender.texts()[0] and explicit.stopped

    app.sender.calls.clear()
    chat = FakeEvent([plain("反色")], message_str="反色", group_id="1", is_at_or_wake_command=False)
    run(app.handle_passive(chat))
    assert app.sender.calls == [] and not chat.stopped, "被动模式没有媒体时不应回复，也不应拦截消息"


def test_match_mode_switch(tmp_path, make_app):
    src = make_png(tmp_path / "a.png")
    off = make_app()
    event = FakeEvent([plain("左右翻转"), image(src)], message_str="左右翻转", group_id="1", is_at_or_wake_command=False)
    run(off.handle_passive(event))
    assert off.sender.calls == []

    on = make_app({"general": {"match_mode": True}})
    event = FakeEvent([plain("左右翻转"), image(src)], message_str="左右翻转", group_id="1", is_at_or_wake_command=False)
    run(on.handle_passive(event))
    assert on.sender.kinds() == ["image"] and event.stopped
    # 精准匹配：带其他文字不会触发
    on.sender.calls.clear()
    chat = FakeEvent([plain("左右翻转好玩"), image(src)], message_str="左右翻转好玩", group_id="1", is_at_or_wake_command=False)
    run(on.handle_passive(chat))
    assert on.sender.calls == []
    # 帮助等不需要媒体的指令不参与无前缀匹配
    helpme = FakeEvent([plain("多模态处理帮助")], message_str="多模态处理帮助", group_id="1", is_at_or_wake_command=False)
    run(on.handle_passive(helpme))
    assert on.sender.calls == []


def test_reply_with_auto_at_still_recognizes_prefixed_command(tmp_path, make_app):
    """QQ 回复别人时会自动插入 @，AstrBot 不会唤醒，被动处理器要接住。"""
    app = make_app()
    src = make_png(tmp_path / "a.png")
    reply = comp("Reply", id="1", chain=[image(src)])
    event = FakeEvent(
        [reply, comp("At", qq="12345"), plain(" /上下翻转")],
        message_str="@张三(12345) /上下翻转", group_id="1", is_at_or_wake_command=False,
    )
    run(app.handle_passive(event))
    assert app.sender.kinds() == ["image"]


def test_passive_skips_when_astrbot_command_will_handle(tmp_path, make_app):
    app = make_app({"general": {"match_mode": True}})
    src = make_png(tmp_path / "a.png")
    event = FakeEvent([plain("/反色"), image(src)], message_str="反色", is_at_or_wake_command=True)
    run(app.handle_passive(event))
    assert app.sender.calls == [] and not event.stopped


def test_usage_error_reports_usage(make_app):
    app = make_app()
    event = FakeEvent([plain("/调速 abc")], message_str="调速 abc")
    run(app.handle_command(event, "调速"))
    text = app.sender.texts()[0]
    assert "倍率必须是数字" in text and "用法：/调速" in text


def test_feature_disabled_and_platform_checks(tmp_path, make_app):
    app = make_app({"image": {"enable": False}})
    src = make_png(tmp_path / "a.png")
    run(app.handle_command(FakeEvent([plain("/反色"), image(src)], message_str="反色"), "反色"))
    assert "已在配置中关闭" in app.sender.texts()[0]

    app = make_app()
    run(app.handle_command(FakeEvent([plain("/转换"), image(src)], message_str="转换", platform="telegram"), "转换"))
    assert "QQ 专属功能" in app.sender.texts()[0]
    run(app.handle_command(FakeEvent([plain("/外显取名 好耶"), image(src)], message_str="外显取名 好耶", platform="webchat"), "外显取名"))
    assert "QQ 专属功能" in app.sender.texts()[1]
    run(app.handle_command(FakeEvent([plain("/外显取名 好耶"), image(src)], message_str="外显取名 好耶", platform="telegram"), "外显取名"))
    assert "QQ 专属功能" in app.sender.texts()[2]


def test_admin_commands_and_config_writeback(make_app):
    class Config(dict):
        saved = 0

        def save_config(self):
            Config.saved += 1

    config = Config({"image": {"qq_blocked_groups": []}})
    app = make_app(config)
    run(app.handle_command(FakeEvent([plain("/转换禁用")], message_str="转换禁用", group_id="555", admin=True), "转换禁用"))
    assert config["image"]["qq_blocked_groups"] == ["555"] and Config.saved == 1
    # 非管理员经由被动路径也会被拦下
    app.sender.calls.clear()
    run(app.handle_passive(FakeEvent([plain("/转换启用 555")], message_str="", group_id="1", is_at_or_wake_command=False)))
    assert "管理员" in app.sender.texts()[0]
    run(app.handle_command(FakeEvent([plain("/转换启用 555")], message_str="转换启用 555", admin=True), "转换启用"))
    assert config["image"]["qq_blocked_groups"] == []


def test_broken_image_does_not_crash(tmp_path, make_app):
    app = make_app()
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 10)
    event = FakeEvent([plain("/反色"), image(bad)], message_str="反色")
    run(app.handle_command(event, "反色"))
    assert "无法读取" in app.sender.texts()[0] or "损坏" in app.sender.texts()[0]
    assert task_dirs(app) == []


def test_non_image_file_rejected(tmp_path, make_app):
    app = make_app()
    txt = tmp_path / "a.png"
    txt.write_text("not an image")
    run(app.handle_command(FakeEvent([plain("/反色"), image(txt)], message_str="反色"), "反色"))
    assert "不是图片" in app.sender.texts()[0]


def test_concurrent_requests_all_cleaned(tmp_path, make_app):
    app = make_app({"general": {"light_concurrency": 2}})
    srcs = [make_png(tmp_path / f"{i}.png", size=(40 + i, 30)) for i in range(6)]

    async def go():
        events = [FakeEvent([plain("/顺时针"), image(s)], message_str="顺时针", sender_id=str(i)) for i, s in enumerate(srcs)]
        await asyncio.gather(*(app.handle_command(e, "顺时针") for e in events))

    run(go())
    assert app.sender.kinds().count("image") == 6
    assert task_dirs(app) == []


def test_mirage_direct_two_images(tmp_path, make_app):
    app = make_app()
    front = make_png(tmp_path / "f.png", size=(30, 20), color=(255, 255, 255, 255))
    back = make_png(tmp_path / "b.png", size=(10, 40), color=(0, 0, 0, 255))
    event = FakeEvent([plain("/彩色幻影坦克"), image(front), image(back)], message_str="彩色幻影坦克", group_id="7")
    run(app.handle_command(event, "彩色幻影坦克"))
    kind, path, note, _ = app.sender.calls[0]
    assert kind == "image" and "彩色幻影坦克" in note
    assert task_dirs(app) == [] and app.mirage_sessions == set()


def test_mirage_sessions_isolated_per_user_in_same_group():
    a = FakeEvent([], group_id="7", sender_id="1")
    b = FakeEvent([], group_id="7", sender_id="2")
    a2 = FakeEvent([], group_id="7", sender_id="1")
    other_group = FakeEvent([], group_id="8", sender_id="1")
    assert session_key(a) != session_key(b)
    assert session_key(a) == session_key(a2)
    assert session_key(a) != session_key(other_group)


def test_ffmpeg_missing_is_reported_without_affecting_images(tmp_path, make_app):
    app = make_app({"video": {"ffmpeg_path": str(tmp_path / "nope" / "ffmpeg"), "ffprobe_path": str(tmp_path / "nope" / "ffprobe")}})
    video = comp("Video", file=str(tmp_path / "v.mp4"), url="", path="")
    run(app.handle_command(FakeEvent([plain("/视频静音"), video], message_str="视频静音"), "视频静音"))
    assert "未找到 FFmpeg" in app.sender.texts()[0]
    run(app.handle_command(FakeEvent([plain("/多模态处理帮助")], message_str="多模态处理帮助"), "多模态处理帮助"))
    assert "未检测到 FFmpeg" in app.sender.texts()[1]
    src = make_png(tmp_path / "a.png")
    run(app.handle_command(FakeEvent([plain("/反色"), image(src)], message_str="反色"), "反色"))
    assert app.sender.kinds()[-1] == "image"


def test_help_queries(make_app):
    app = make_app()
    run(app.handle_command(FakeEvent([plain("/多模态处理帮助 视频裁剪")], message_str="多模态处理帮助 视频裁剪"), "多模态处理帮助"))
    assert "/视频裁剪 <开始> <结束>" in app.sender.texts()[0]
    run(app.handle_command(FakeEvent([plain("/多模态处理帮助 不存在")], message_str="多模态处理帮助 不存在"), "多模态处理帮助"))
    assert "没有找到" in app.sender.texts()[1]
