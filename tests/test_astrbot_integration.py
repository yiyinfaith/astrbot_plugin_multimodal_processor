"""依赖真实 AstrBot 源码的集成测试；本机没有 AstrBot 时跳过。

运行方式（VPS）：PYTHONPATH=/root/yiyin/AstrBot ASTRBOT_ROOT=<临时目录> .venv/bin/python -m pytest tests
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from PIL import Image

from conftest import FakeEvent, image, plain, run

pytest.importorskip("astrbot.api", reason="需要 AstrBot 运行环境")


def test_main_imports_and_registers_all_commands():
    from astrbot.api.star import Star
    from astrbot.core.star.star_handler import star_handlers_registry

    from mmp.core.commands import COMMANDS

    module = importlib.import_module("mmp.main")
    assert issubclass(module.MultimodalProcessor, Star)
    handlers = [h for h in star_handlers_registry if h.handler_module_path == "mmp.main"]
    # 每条指令一个处理函数，外加一个识别「@某人 /指令」的监听器
    assert len(handlers) == len(COMMANDS) + 1


def _dispatch_to_sessions(event) -> bool:
    """模拟 AstrBot 核心把消息交给等待中的会话（与 ProcessStage 的做法一致）。"""
    from astrbot.core.utils.session_waiter import FILTERS, USER_SESSIONS, SessionWaiter

    for session_filter in list(FILTERS):
        session_id = session_filter.filter(event)
        if session_id and session_id in USER_SESSIONS:
            asyncio.get_running_loop().create_task(SessionWaiter.trigger(session_id, event))
            return True
    return False


def test_mirage_stepwise_sessions_are_isolated_per_user(tmp_path, make_app):
    app = make_app({"image": {"mirage": {"timeout": 5}}})
    imgs = {}
    for name, size, color in (("a_front", (30, 20), (255, 255, 255)), ("a_back", (30, 20), (0, 0, 0)), ("b_front", (12, 12), (128, 128, 128))):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", size, color).save(path)
        imgs[name] = path

    def ev(user, parts):
        return FakeEvent(parts, message_str=" ".join(p.text for p in parts if p.type == "Plain"), sender_id=user, group_id="42", platform="aiocqhttp")

    async def go():
        task_a = asyncio.create_task(app.handle_command(ev("1", [plain("/幻影坦克")]), "幻影坦克"))
        task_b = asyncio.create_task(app.handle_command(ev("2", [plain("/彩色幻影坦克")]), "彩色幻影坦克"))
        await asyncio.sleep(0.2)
        assert len(app.mirage_sessions) == 2
        # B 先发一张图，只应进入 B 的会话
        assert _dispatch_to_sessions(ev("2", [image(imgs["b_front"])]))
        await asyncio.sleep(0.2)
        # 第三个人发的图不属于任何会话
        assert not _dispatch_to_sessions(ev("3", [image(imgs["a_back"])]))
        # A 依次发送表图、里图
        assert _dispatch_to_sessions(ev("1", [image(imgs["a_front"])]))
        await asyncio.sleep(0.2)
        assert _dispatch_to_sessions(ev("1", [image(imgs["a_back"])]))
        await asyncio.wait_for(task_a, 5)
        # B 取消
        assert _dispatch_to_sessions(ev("2", [plain("取消")]))
        await asyncio.wait_for(task_b, 5)

    run(go())
    images = [c for c in app.sender.calls if c[0] == "image"]
    assert len(images) == 1, app.sender.calls
    texts = app.sender.texts()
    assert any("已取消" in t for t in texts)
    assert sum("已收到表图" in t for t in texts) == 2  # A、B 各收到一次
    assert app.mirage_sessions == set()
    assert list(app.tasks.root.glob("task-*")) == []


def test_mirage_session_timeout(tmp_path, make_app):
    app = make_app({"image": {"mirage": {"timeout": 5}}})
    app.settings.mirage.timeout = 1  # 配置下限是 5 秒，测试里直接改短
    event = FakeEvent([plain("/幻影坦克")], message_str="幻影坦克", sender_id="9", group_id="1", platform="aiocqhttp")
    run(asyncio.wait_for(app.handle_command(event, "幻影坦克"), 5))
    assert any("超时" in t for t in app.sender.texts())
    assert app.mirage_sessions == set() and list(app.tasks.root.glob("task-*")) == []
