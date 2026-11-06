from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from urllib.parse import urlparse


class UnsupportedPlatform(RuntimeError):
    pass


class QQCapability:
    """OneBot/NapCat 可选能力，通用平台不会触发这些方法。"""
    def __init__(self, config: Any, data_dir: str | Path | None = None):
        self.config = config
        self.block_path = Path(data_dir or Path("data") / "plugins" / "multimodal_processor") / "blocklist.json"
        self.block_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.block_path.read_text(encoding="utf-8"))
            self.blocked = {str(x) for x in data.get("group_id", [])}
        except (OSError, json.JSONDecodeError):
            self.blocked = set()

    def _save(self) -> None:
        self.block_path.write_text(json.dumps({"group_id": sorted(self.blocked)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_group(self, group_id: str | int) -> bool:
        value = str(group_id)
        if value in self.blocked: return False
        self.blocked.add(value); self._save(); return True

    def remove_group(self, group_id: str | int) -> bool:
        value = str(group_id)
        if value not in self.blocked: return False
        self.blocked.remove(value); self._save(); return True

    def is_blocked(self, group_id: str | int | None) -> bool:
        return bool(group_id and str(group_id) in self.blocked)

    @staticmethod
    def is_qq_event(event: object) -> bool:
        platform = str(getattr(getattr(event, "platform_meta", None), "name", "")).lower()
        return "qq" in platform or "onebot" in platform or hasattr(event, "bot") and hasattr(getattr(event, "bot"), "api")

    async def get_image_path(self, event: object, component: object) -> str:
        bot = getattr(event, "bot", None); api = getattr(bot, "api", None)
        file_id = getattr(component, "file", None)
        if not api or not hasattr(api, "call_action") or not file_id:
            raise UnsupportedPlatform("当前平台不支持 QQ 图片提取")
        try:
            response = await api.call_action("get_image", file_id=file_id)
            path = response.get("file") if isinstance(response, dict) else None
            if path and Path(path).exists(): return str(path)
        except Exception as exc:
            raise UnsupportedPlatform(f"QQ 图片提取失败：{exc}") from exc
        raise UnsupportedPlatform("QQ 未返回本地图片路径")

    async def get_reply_image_path(self, event: object, reply_id: str | int) -> str:
        """通过 OneBot get_msg 读取引用消息，兼容 Reply.chain 缺失的 NapCat 事件。"""
        bot = getattr(event, "bot", None); api = getattr(bot, "api", None)
        if not api or not hasattr(api, "call_action"):
            raise UnsupportedPlatform("当前平台不支持 QQ 引用消息提取")
        try:
            response = await api.call_action("get_msg", message_id=reply_id)
            messages = response.get("message", []) if isinstance(response, dict) else []
            for item in messages:
                if isinstance(item, dict) and str(item.get("type", "")).lower() in {"image", "face"}:
                    data = item.get("data", {}) or {}
                    file_id = data.get("file") or data.get("url")
                    if not file_id:
                        continue
                    return await self.get_image_path(event, SimpleNamespace(file=file_id))
        except UnsupportedPlatform:
            raise
        except Exception as exc:
            raise UnsupportedPlatform(f"QQ 引用消息提取失败：{exc}") from exc
        raise UnsupportedPlatform("引用消息中没有可保存的图片")

    async def send_file(self, event: object, path: str | Path, name: str | None = None) -> None:
        bot = getattr(event, "bot", None); api = getattr(bot, "api", None)
        if not api or not hasattr(api, "call_action"):
            raise UnsupportedPlatform("当前平台不支持 QQ 文件发送")
        target = Path(path).resolve(); file_uri = target.as_uri(); filename = name or target.name
        message_type = str(getattr(event, "get_message_type", lambda: "")()).lower()
        if "group" in message_type:
            group_id = getattr(event, "get_group_id", lambda: None)()
            if self.is_blocked(group_id): raise UnsupportedPlatform("该群已禁用 QQ 图片保存")
            enabled = bool(self.config.get("qq_group_file_enabled", False) if hasattr(self.config, "get") else getattr(self.config, "qq_group_file_enabled", False))
            if not enabled: raise UnsupportedPlatform("为避免污染群文件，群聊文件发送已关闭，请私聊使用")
            await api.call_action("send_group_msg", group_id=group_id, message=[{"type":"file","data":{"file":file_uri,"name":filename}}])
        else:
            user_id = getattr(event, "get_sender_id", lambda: None)()
            await api.call_action("send_private_msg", user_id=user_id, message=[{"type":"file","data":{"file":file_uri,"name":filename}}])
