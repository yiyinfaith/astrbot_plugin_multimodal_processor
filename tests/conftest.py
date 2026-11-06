"""测试公共设施。

插件目录本身没有 __init__.py，这里把它注册成名为 `mmp` 的包，插件内部的相对导入即可正常工作。
不依赖 AstrBot：消息事件和消息组件用简单的假对象模拟，发送动作由 FakeSender 记录。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "mmp" not in sys.modules:
    package = types.ModuleType("mmp")
    package.__path__ = [str(ROOT)]
    sys.modules["mmp"] = package

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="需要 ffmpeg/ffprobe")


def run(coro):
    return asyncio.run(coro)


def comp(kind: str, **fields: Any) -> SimpleNamespace:
    """模拟 AstrBot 消息组件：type 字段与真实组件一样是类型名（Image / Plain / Reply ...）。"""
    return SimpleNamespace(type=kind, **fields)


def plain(text: str) -> SimpleNamespace:
    return comp("Plain", text=text)


def image(path: Path) -> SimpleNamespace:
    return comp("Image", file=str(path), url="", path=str(path))


@dataclass
class FakeEvent:
    messages: list
    message_str: str = ""
    sender_id: str = "10001"
    group_id: str = ""
    platform: str = "webchat"
    is_at_or_wake_command: bool = True
    admin: bool = False
    stopped: bool = False
    sent: list = field(default_factory=list)
    message_obj: Any = None

    @property
    def unified_msg_origin(self) -> str:
        kind = "GroupMessage" if self.group_id else "FriendMessage"
        return f"{self.platform}:{kind}:{self.group_id or self.sender_id}"

    def get_messages(self):
        return list(self.messages)

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return "99999"

    def get_platform_name(self):
        return self.platform

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True


class FakeSender:
    """记录发送动作；发送时检查文件仍然存在（确认清理发生在发送之后）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def text(self, event, text):
        self.calls.append(("text", text))

    async def _media(self, kind, path, note=None):
        assert Path(path).exists(), "发送时文件已被删除"
        self.calls.append((kind, Path(path), note, Path(path).stat().st_size))

    async def image(self, event, path, text=None):
        await self._media("image", path, text)

    async def images(self, event, paths, text=None):
        for p in paths:
            await self._media("image", p, text)

    async def image_group(self, event, paths, text=None, title=""):
        assert paths and all(Path(p).exists() for p in paths), "发送时文件已被删除"
        self.calls.append(("group", [Path(p) for p in paths], text, len(paths)))

    async def file(self, event, path, name, text=None):
        await self._media("file", path, text)

    async def video(self, event, path, name, text=None, mode="auto"):
        await self._media("video", path, text)

    async def audio(self, event, path, name, text=None, mode="auto"):
        await self._media("audio", path, text)

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]

    def texts(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "text"]


@pytest.fixture
def make_app(tmp_path):
    from mmp.core.app import MultimodalApp

    def factory(config: dict | None = None):
        app = MultimodalApp(config or {}, tmp_path / "data", astrbot_roots=[tmp_path])
        app.sender = FakeSender()
        return app

    return factory
