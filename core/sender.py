"""按平台能力发送处理结果。

所有发送都 `await event.send(...)`，返回时平台已经读取完文件（aiocqhttp 把图片/语音转成
base64、文件/视频交给协议端读取；WebChat 在 send 内复制文件），之后任务目录才会被删除。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .errors import UnsupportedPlatform

log = logging.getLogger("astrbot")

try:  # AstrBot 运行环境
    import astrbot.api.message_components as Comp
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover - 纯处理器测试环境
    Comp = None
    MessageChain = None

# 已知的平台能力。未列出的平台按「都尝试，失败再降级」处理。
_IMAGE_ONLY = {"qq_official", "qq_official_webhook", "dingtalk", "weixin_official_account"}
_NO_VIDEO = {"webchat", "misskey"} | _IMAGE_ONLY
_NO_RECORD = {"slack", "kook", "misskey", "matrix", "mattermost", "satori"} | _IMAGE_ONLY
_NO_FILE = {"misskey", "weixin_oc"} | _IMAGE_ONLY


def platform_name(event: Any) -> str:
    try:
        return str(event.get_platform_name() or "").lower()
    except Exception:
        return ""


def is_group(event: Any) -> bool:
    try:
        return bool(event.get_group_id())
    except Exception:
        return False


class Sender:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug

    async def _send(self, event: Any, chain: list) -> None:
        if MessageChain is None:
            raise RuntimeError("AstrBot 消息组件不可用")
        await event.send(MessageChain(chain))

    async def text(self, event: Any, text: str) -> None:
        if Comp is None:
            return
        await self._send(event, [Comp.Plain(text)])

    async def image(self, event: Any, path: Path, text: str | None = None) -> None:
        chain = []
        if text:
            chain.append(Comp.Plain(text + "\n"))
        chain.append(Comp.Image.fromFileSystem(str(path)))
        await self._send(event, chain)

    async def images(self, event: Any, paths: list[Path], text: str | None = None) -> None:
        chain = [Comp.Plain(text + "\n")] if text else []
        chain.extend(Comp.Image.fromFileSystem(str(p)) for p in paths)
        await self._send(event, chain)

    async def image_group(self, event: Any, paths: list[Path], text: str | None = None, title: str = "多模态处理器") -> None:
        """发送多张图片：QQ 用合并转发（不刷屏），其他平台每条消息最多 9 张。"""
        if platform_name(event) == "aiocqhttp":
            try:
                self_id = str(event.get_self_id() or "0")
            except Exception:
                self_id = "0"
            nodes = []
            if text:
                nodes.append(Comp.Node(uin=self_id, name=title, content=[Comp.Plain(text)]))
            nodes.extend(Comp.Node(uin=self_id, name=title, content=[Comp.Image.fromFileSystem(str(p))]) for p in paths)
            try:
                await self._send(event, [Comp.Nodes(nodes=nodes)])
                return
            except Exception as exc:
                log.warning(f"[多模态处理器] 合并转发发送失败，改为分批发送：{exc}")
        for start in range(0, len(paths), 9):
            batch = paths[start:start + 9]
            await self.images(event, batch, text if start == 0 else None)

    async def file(self, event: Any, path: Path, name: str, text: str | None = None) -> None:
        platform = platform_name(event)
        if platform in _NO_FILE:
            raise UnsupportedPlatform("当前平台不支持发送文件")
        if text:
            await self.text(event, text)
        await self._send(event, [Comp.File(name=name, file=str(path))])

    async def video(self, event: Any, path: Path, name: str, text: str | None = None, mode: str = "auto") -> None:
        platform = platform_name(event)
        if mode == "file" or platform in _NO_VIDEO:
            await self.file(event, path, name, text)
            return
        if text:
            await self.text(event, text)
        try:
            await self._send(event, [Comp.Video.fromFileSystem(str(path))])
        except Exception as exc:
            log.warning(f"[多模态处理器] 视频消息发送失败，改为文件发送：{exc}")
            await self.file(event, path, name)

    async def audio(self, event: Any, path: Path, name: str, text: str | None = None, mode: str = "auto") -> None:
        """auto：群聊发语音（不占群文件），私聊发文件（保留格式、可下载）。"""
        platform = platform_name(event)
        if mode == "auto":
            mode = "media" if is_group(event) else "file"
        if mode == "file" and platform in _NO_FILE:
            mode = "media"
        if mode == "media" and platform in _NO_RECORD:
            mode = "file"
        if mode == "file":
            await self.file(event, path, name, text)
            return
        if platform in _NO_RECORD:
            raise UnsupportedPlatform("当前平台不支持发送语音或文件")
        if text:
            await self.text(event, text)
        try:
            await self._send(event, [Comp.Record.fromFileSystem(str(path))])
        except Exception as exc:
            log.warning(f"[多模态处理器] 语音发送失败，改为文件发送：{exc}")
            await self.file(event, path, name)
