"""插件运行时：持有配置、任务管理、FFmpeg、发送器，负责指令分发与统一的错误处理。

main.py 里每个指令处理函数都只调用 `handle_command`；没有前缀的关键词由 `handle_passive` 处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
import logging
from pathlib import Path
import re
import tempfile
from typing import Any, Awaitable, Callable

from ..platforms import qq
from .commands import ParsedCommand, get_spec, parse_command
from .config import Settings
from .errors import (
    Busy, FeatureDisabled, MediaMissing, ToolMissing, UnsupportedPlatform, UsageError, UserError,
)
from .ffmpeg import FFmpegRunner
from .media import ANY, AUDIO, IMAGE, VIDEO, MediaFile, MediaRef, MediaResolver, at_targets, collect_refs, component_type, qq_avatar_url
from .sender import Sender
from .tasks import TaskManager, TaskSlot

log = logging.getLogger("astrbot")

Handler = Callable[["Invocation"], Awaitable[bool]]

_FEATURE_OF_CATEGORY = {"image": "image", "qq": "qq", "mirage": "mirage", "prism": "prism", "video": "video", "audio": "audio"}
_FEATURE_NAMES = {"image": "图片", "mirage": "幻影坦克", "prism": "光棱坦克", "video": "视频", "audio": "音频"}


@dataclass(slots=True)
class Invocation:
    app: "MultimodalApp"
    event: Any
    parsed: ParsedCommand
    explicit: bool  # 带前缀/唤醒触发（True）还是被动关键词触发（False）
    notes: list[str] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        return self.app.settings

    @property
    def args(self) -> tuple[str, ...]:
        return self.parsed.args

    @property
    def user_key(self) -> str:
        try:
            return f"{self.event.unified_msg_origin}#{self.event.get_sender_id()}"
        except Exception:
            return "unknown"

    async def reply(self, text: str) -> None:
        await self.app.sender.text(self.event, text)

    def refs(self, kind: str) -> list[MediaRef]:
        return [ref for ref in collect_refs(self.event) if ref.accepts(kind)]

    def image_refs(self, allow_avatar: bool = True) -> list[MediaRef]:
        refs = self.refs(IMAGE)
        if refs or not allow_avatar:
            return refs
        general = self.settings.general
        if general.enable_at_avatar and qq.is_onebot(self.event):
            return [MediaRef(None, "avatar", "avatar", name=f"avatar_{uid}", url=qq_avatar_url(uid)) for uid in at_targets(self.event)]
        return []

    async def fetch(self, ref: MediaRef, slot: TaskSlot, max_bytes: int) -> MediaFile:
        return await self.app.resolver.fetch(self.event, ref, slot.workdir, max_bytes)

    @property
    def timeout(self) -> int:
        return self.settings.general.task_timeout

    async def run_timed(self, awaitable: Awaitable):
        try:
            return await asyncio.wait_for(awaitable, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise UserError(f"处理超时（超过 {self.timeout} 秒），已中止") from exc

    async def notify_queue(self, ahead: int) -> None:
        await self.reply(f"任务已排队，前面还有 {ahead + 1} 个任务，完成后会自动发送结果。")


class MultimodalApp:
    def __init__(self, config: Any, data_dir: Path, *, astrbot_roots: list[Path] | None = None, wake_prefixes: Callable[[], list[str]] | None = None) -> None:
        self.config = config
        self.settings = Settings.load(config)
        self.data_dir = Path(data_dir)
        g = self.settings.general
        self.tasks = TaskManager(
            self.data_dir / "tasks",
            heavy_concurrency=g.heavy_concurrency,
            light_concurrency=g.light_concurrency,
            queue_limit=g.queue_limit,
            per_user_limit=g.per_user_limit,
            expire_seconds=g.temp_expire_minutes * 60,
        )
        v = self.settings.video
        self.ffmpeg = FFmpegRunner(v.ffmpeg_path, v.ffprobe_path, threads=v.threads, memory_mb=v.memory_mb)
        self.sender = Sender(debug=g.debug)
        roots = [self.data_dir, Path(tempfile.gettempdir()), *(astrbot_roots or []), *(Path(p) for p in g.extra_local_roots)]
        self.resolver = MediaResolver(
            roots,
            download_timeout=g.download_timeout,
            allow_private_urls=g.allow_private_urls,
            platform_fetch=partial(qq.platform_fetch, allow_private=g.allow_private_urls, timeout=g.download_timeout),
        )
        self._wake_prefixes = wake_prefixes or (lambda: ["/"])
        self.mirage_sessions: set[str] = set()
        from ..handlers import HANDLERS  # 延迟导入，避免循环依赖

        self.handlers: dict[str, Handler] = HANDLERS

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        removed = self.tasks.sweep(max_age=0)
        if removed:
            log.info(f"[多模态处理器] 启动时清理了 {removed} 个残留临时目录")
        self.tasks.start()
        if (self.settings.video.enable or self.settings.audio.enable) and not self.ffmpeg.available:
            log.warning("[多模态处理器] 未找到 FFmpeg/FFprobe，视频与音频功能不可用，图片功能不受影响")

    async def stop(self) -> None:
        await self.tasks.stop()

    def debug(self, message: str) -> None:
        if self.settings.general.debug:
            log.info(f"[多模态处理器][调试] {message}")

    # ------------------------------------------------------------------ 文本解析
    def command_text(self, event: Any) -> tuple[str, bool]:
        """拼接消息里的纯文本段（忽略 @ 文本），返回 (去掉唤醒前缀后的文本, 是否带前缀)。"""
        parts: list[str] = []
        try:
            for component in event.get_messages():
                if component_type(component) == "plain":
                    parts.append(str(getattr(component, "text", "") or ""))
        except Exception:
            parts = [str(getattr(event, "message_str", "") or "")]
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if not text:
            text = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "")).strip()
        for prefix in sorted((p for p in self._wake_prefixes() if p), key=len, reverse=True):
            if text.startswith(prefix):
                return text[len(prefix):].strip(), True
        return text, False

    @staticmethod
    def _astrbot_would_match(event: Any) -> bool:
        """复现 AstrBot CommandFilter 的判断：已唤醒且 message_str 以注册的指令名开头。"""
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        message = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "").strip())
        parsed = parse_command(message)
        if not parsed:
            return False
        return message == parsed.used_name or message.startswith(parsed.used_name + " ")

    # ------------------------------------------------------------------ 入口
    async def handle_command(self, event: Any, name: str) -> None:
        """由 main.py 中 @filter.command 注册的处理函数调用。"""
        text, _ = self.command_text(event)
        parsed = parse_command(text)
        spec = get_spec(name)
        if spec is None:
            return
        if parsed is None or parsed.spec is not spec:
            message = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "").strip())
            parsed = parse_command(message)
            if parsed is None or parsed.spec is not spec:
                parsed = ParsedCommand(spec, name, (), "")
        await self.run(event, parsed, explicit=True)

    async def handle_passive(self, event: Any) -> None:
        """处理 AstrBot 指令系统接不到的情况：精准关键词（无前缀）以及「@某人 /指令」。"""
        text, prefixed = self.command_text(event)
        parsed = parse_command(text)
        if parsed is None:
            return
        if self._astrbot_would_match(event):
            return  # 交给 @filter.command 注册的处理函数
        woken = bool(getattr(event, "is_at_or_wake_command", False))
        if not (prefixed or woken):
            # 精准关键词只对需要媒体的指令生效，并且必须找到媒体才会响应（见 MediaMissing 处理）
            if not self.settings.general.match_mode or not parsed.spec.needs_media:
                return
        await self.run(event, parsed, explicit=prefixed or woken)

    async def run(self, event: Any, parsed: ParsedCommand, *, explicit: bool) -> None:
        spec = parsed.spec
        inv = Invocation(self, event, parsed, explicit)
        handled = True
        try:
            self._check_enabled(spec.category, spec.handler)
            if spec.admin and not _is_admin(event):
                raise UserError("只有管理员可以使用这个指令")
            self.debug(f"{spec.name} args={parsed.args} explicit={explicit} platform={_platform(event)}")
            handled = await self.handlers[spec.handler](inv)
        except UsageError as exc:
            await self._safe_reply(event, f"{exc}\n用法：{spec.usage_text()}")
        except (MediaMissing, FeatureDisabled) as exc:
            if explicit:
                await self._safe_reply(event, str(exc))
            else:
                handled = False
        except (UserError, ToolMissing, UnsupportedPlatform, Busy) as exc:
            await self._safe_reply(event, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(f"[多模态处理器] 指令 {spec.name} 执行出错：{exc}")
            await self._safe_reply(event, "处理失败：发生了意外错误，详情请查看 AstrBot 日志。")
        if handled:
            try:
                event.stop_event()
            except Exception:
                pass

    def _check_enabled(self, category: str, handler: str) -> None:
        s = self.settings
        if category == "text":
            if not s.general.text_enable:
                raise FeatureDisabled("文本功能已在配置中关闭")
            return
        if category == "qq":
            if handler == "qq_convert" and not s.qq.enable_convert:
                raise FeatureDisabled("QQ 图片保存（/转换）已在配置中关闭")
            if handler == "qq_rename" and not s.qq.enable_rename:
                raise FeatureDisabled("修改图片外显（/外显取名）已在配置中关闭")
            return
        feature = _FEATURE_OF_CATEGORY.get(category)
        if feature and not getattr(getattr(s, feature), "enable", True):
            raise FeatureDisabled(f"{_FEATURE_NAMES[feature]}功能已在配置中关闭")

    async def _safe_reply(self, event: Any, text: str) -> None:
        try:
            await self.sender.text(event, text)
        except Exception as exc:
            log.warning(f"[多模态处理器] 回复消息失败：{exc}")

    # ------------------------------------------------------------------ 配置写回
    def save_blocked_groups(self, groups: list[str]) -> None:
        self.settings.qq.blocked_groups = list(groups)
        try:
            section = self.config.get("image")
            if not isinstance(section, dict):
                section = {}
                self.config["image"] = section
            section["qq_blocked_groups"] = list(groups)
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
        except Exception as exc:
            log.warning(f"[多模态处理器] 保存禁用群列表失败：{exc}")


def _is_admin(event: Any) -> bool:
    try:
        return bool(event.is_admin())
    except Exception:
        return False


def _platform(event: Any) -> str:
    try:
        return str(event.get_platform_name())
    except Exception:
        return "?"


__all__ = ["ANY", "AUDIO", "IMAGE", "VIDEO", "Invocation", "MultimodalApp"]
