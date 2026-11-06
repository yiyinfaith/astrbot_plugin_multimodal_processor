from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil
import uuid

try:
    import astrbot.api.message_components as Comp
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.event.filter import EventMessageType
    from astrbot.api.star import Context, Star, register
    from astrbot.core.utils.session_waiter import SessionFilter, SessionController, session_waiter
except ImportError:  # pragma: no cover - allows pure processor tests without AstrBot installed
    Comp = None; logger = None; AstrMessageEvent = object
    filter = EventMessageType = Context = Star = register = None
    SessionFilter = SessionController = session_waiter = None

try:
    from .core.commands import HELP, ParsedCommand, parse_command
    from .core.config import Settings
    from .core.media import MediaError, MediaTooLarge, MediaFile, avatar_media, resolve_media, sniff_kind
    from .core.tasks import TaskManager
    from .platform_ext.qq import QQCapability, UnsupportedPlatform
    from .processors.audio.ffmpeg import AudioFFmpeg
    from .processors.image.operations import image_info, process_image
    from .processors.mirage.algorithm import generate_mirage
    from .processors.video.ffmpeg import FFmpeg, FFmpegError
except ImportError:  # direct import by lightweight plugin scanners/tests
    from core.commands import HELP, ParsedCommand, parse_command
    from core.config import Settings
    from core.media import MediaError, MediaTooLarge, MediaFile, avatar_media, resolve_media, sniff_kind
    from core.tasks import TaskManager
    import importlib.util as _importlib_util
    _qq_spec = _importlib_util.spec_from_file_location("_multimodal_qq", Path(__file__).with_name("platform_ext") / "qq.py")
    _qq_module = _importlib_util.module_from_spec(_qq_spec); _qq_spec.loader.exec_module(_qq_module)
    QQCapability, UnsupportedPlatform = _qq_module.QQCapability, _qq_module.UnsupportedPlatform
    from processors.audio.ffmpeg import AudioFFmpeg
    from processors.image.operations import image_info, process_image
    from processors.mirage.algorithm import generate_mirage
    from processors.video.ffmpeg import FFmpeg, FFmpegError


IMAGE_OPS = {"反色": "invert", "顺时针": "cw", "逆时针": "ccw", "左右翻转": "hflip", "上下翻转": "vflip",
             "左对称": "left", "右对称": "right", "上对称": "top", "下对称": "bottom"}
VIDEO_COMMANDS = {"视频转GIF", "GIF转视频", "视频转换", "视频压缩", "视频裁剪", "视频调速", "视频缩放", "视频旋转", "提取音频", "视频静音", "提取视频帧"}
AUDIO_COMMANDS = {"音频转换", "音频转MP3", "音频裁剪", "音频调速", "音量", "音频压缩", "音频静音", "音频信息"}


def _num(value: str, default: float = 0) -> float:
    try: return float(value)
    except (TypeError, ValueError): return default


def _size(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{1,5})[x×](\d{1,5})", value.lower())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _safe_name(value: str, suffix: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00\r\n]+", "_", value).strip(" .")[:80] or "图片"
    if not value.lower().endswith(suffix.lower()): value += suffix
    return value


class _UserSessionFilter(SessionFilter if SessionFilter else object):
    def __init__(self, session_id: str): self.session_id = session_id
    def filter(self, event):
        try:
            key = f"{event.get_session_id()}:{event.get_sender_id()}"
            return self.session_id if key == self.session_id else ""
        except Exception: return self.session_id


if register:
    @register("multimodal_processor", "yiyinfaith", "多模态处理器", "1.0.0")
    class MultimodalProcessor(Star):
        def __init__(self, context: Context, config=None):
            super().__init__(context)
            self.settings = Settings.from_config(config or {})
            self.raw_config = config or {}
            root = Path("data")
            if context and hasattr(context, "get_config"):
                try:
                    context_config = context.get_config() or {}
                    if isinstance(context_config, dict) and context_config.get("data_dir"):
                        root = Path(context_config["data_dir"])
                except Exception:
                    pass
            self.tasks = TaskManager(root / "plugins" / "multimodal_processor" / "temp", self.settings.heavy_concurrency, self.settings.process_timeout)
            self.tasks.cleanup_stale()
            self.qq = QQCapability(self.raw_config, root / "plugins" / "multimodal_processor")
            semaphore = asyncio.Semaphore(self.settings.heavy_concurrency)
            self.ffmpeg = FFmpeg(self.settings.ffmpeg_path, self.settings.ffprobe_path, self.settings.process_timeout, semaphore)
            self.audio = AudioFFmpeg(self.settings.ffmpeg_path, self.settings.ffprobe_path, self.settings.process_timeout, semaphore)

        async def _plain(self, event, text: str):
            if hasattr(event, "plain_result") and hasattr(event, "send"):
                await event.send(event.plain_result(text))
            elif hasattr(event, "set_result") and hasattr(event, "plain_result"):
                event.set_result(event.plain_result(text))

        async def _send_paths(self, event, paths: list[Path], *, text: str | None = None):
            chain = []
            if text and Comp: chain.append(Comp.Plain(text))
            for path in paths:
                suffix = path.suffix.lower()
                if not Comp: continue
                if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    chain.append(Comp.Image.fromFileSystem(str(path)))
                elif suffix in {".mp4", ".webm", ".mkv", ".avi", ".mov"}:
                    chain.append(Comp.Video.fromFileSystem(str(path)))
                elif suffix in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}:
                    chain.append(Comp.Record.fromFileSystem(str(path)))
                else:
                    chain.append(Comp.File(name=path.name, file=str(path)))
            if hasattr(event, "send") and hasattr(event, "chain_result"):
                await event.send(event.chain_result(chain))
            elif hasattr(event, "set_result") and hasattr(event, "chain_result"):
                event.set_result(event.chain_result(chain))

        async def _resolve(self, event, work, *, kind="any", limit=1):
            maximum = {"image": self.settings.max_image_mb, "video": self.settings.max_video_mb, "audio": self.settings.max_audio_mb}.get(kind, self.settings.max_image_mb)
            files = await resolve_media(event, work, maximum * 1024 * 1024, kind=kind, limit=limit)
            if not files and kind in {"image", "any"} and self.settings.enable_at_avatar:
                files = await avatar_media(event, work, self.settings.max_image_mb * 1024 * 1024)
            return files

        async def _image_command(self, event, command: ParsedCommand):
            if not self.settings.enable_image: return await self._plain(event, "图片功能已关闭")
            async with self.tasks.task() as work:
                files = await self._resolve(event, work, kind="image")
                if not files: return await self._plain(event, "请引用、附带图片/GIF，或 @ 一位用户")
                source = files[0].path; name = command.name
                output = work / f"output-{uuid.uuid4().hex}"
                kwargs = {"max_fps": self.settings.gif_max_fps, "allow_frame_drop": self.settings.gif_speed_allow_frame_drop}
                if name in IMAGE_OPS:
                    kwargs["operation"] = IMAGE_OPS[name]; output = output.with_suffix(source.suffix or ".png")
                elif name == "调速":
                    if not command.args: return await self._plain(event, "用法：/调速 2")
                    kwargs["speed"] = _num(command.args[0], 1); output = output.with_suffix(".gif")
                elif name == "图片转换":
                    if not command.args: return await self._plain(event, "用法：/图片转换 webp [静态]")
                    fmt = command.args[0].lower().lstrip("."); kwargs.update(output_format=fmt, keep_animation="静态" not in command.args)
                    output = output.with_suffix("." + fmt)
                elif name == "图片压缩":
                    kwargs["quality"] = int(max(1, min(100, _num(command.args[0], 80) if command.args else 80))); output = output.with_suffix(source.suffix or ".jpg")
                elif name == "图片缩放":
                    parsed = _size(command.args[0]) if command.args else None
                    if not parsed: return await self._plain(event, "用法：/图片缩放 800x600")
                    kwargs.update(width=parsed[0], height=parsed[1]); output = output.with_suffix(source.suffix or ".png")
                elif name == "图片裁剪":
                    if len(command.args) != 1 or len(command.args[0].split(",")) != 4: return await self._plain(event, "用法：/图片裁剪 x,y,w,h")
                    try: x,y,w,h = (int(v) for v in command.args[0].split(",")); kwargs["crop"] = (x,y,x+w,y+h)
                    except ValueError: return await self._plain(event, "裁剪参数必须是整数")
                    output = output.with_suffix(source.suffix or ".png")
                elif name == "图片信息":
                    info = await asyncio.to_thread(image_info, source); return await self._plain(event, "格式 {format}，尺寸 {width}×{height}，帧数 {frames}，大小 {size_bytes} 字节".format(**info))
                else: return await self._plain(event, HELP)
                await asyncio.to_thread(process_image, source, output, **kwargs)
                await self._send_paths(event, [output])
                if hasattr(event, "stop_event"): event.stop_event()

        async def _qq_convert(self, event, command: ParsedCommand):
            if not self.qq.is_qq_event(event): return await self._plain(event, "/转换 和 /rename 仅支持 QQ/NapCat/OneBot 平台")
            group_id = getattr(event, "get_group_id", lambda: None)()
            if self.qq.is_blocked(group_id): return await self._plain(event, "该群已禁止使用 QQ 图片保存，请私聊使用")
            component = next((c for c in getattr(event, "get_messages", lambda: [])() if str(getattr(getattr(c,"type",None),"value",getattr(c,"type", ""))).lower() == "image"), None)
            reply = next((c for c in getattr(event, "get_messages", lambda: [])() if str(getattr(getattr(c,"type",None),"value",getattr(c,"type", ""))).lower() == "reply"), None)
            if component is None:
                if reply and getattr(reply, "chain", None): component = next((c for c in reply.chain if str(getattr(getattr(c,"type",None),"value",getattr(c,"type", ""))).lower() == "image"), None)
            if component is None and reply is None: return await self._plain(event, "请引用或直接发送 QQ 图片/GIF")
            async with self.tasks.task() as work:
                try:
                    if component is not None:
                        local = Path(await self.qq.get_image_path(event, component))
                    else:
                        local = Path(await self.qq.get_reply_image_path(event, reply.id))
                    actual_kind, suffix = sniff_kind(local)
                    target = work / ("qq-image" + suffix); shutil.copyfile(local, target)
                    name = command.args[0] if command.name == "rename" and command.args else f"图片{suffix}"
                    await self.qq.send_file(event, target, _safe_name(name, suffix))
                except UnsupportedPlatform as exc: await self._plain(event, str(exc))
                except Exception as exc: await self._plain(event, f"QQ 图片保存失败：{exc}")
            if hasattr(event, "stop_event"): event.stop_event()

        async def _video_command(self, event, command: ParsedCommand):
            if not self.settings.enable_video: return await self._plain(event, "视频功能已关闭")
            async with self.tasks.task(heavy=True) as work:
                files = await self._resolve(event, work, kind="video")
                if not files: return await self._plain(event, "请附带或引用视频文件")
                source = files[0].path; name = command.name; output = work / ("output" + ({"视频转GIF":".gif","GIF转视频":".mp4","提取音频":".mp3","提取视频帧":".png"}.get(name, source.suffix or ".mp4")))
                try:
                    probe = await self.ffmpeg.probe(source)
                    duration = float((probe.get("format") or {}).get("duration") or 0)
                    if duration > self.settings.max_duration: return await self._plain(event, f"视频时长超过限制（{self.settings.max_duration} 秒）")
                    if name == "视频转GIF": output = await self.ffmpeg.video_to_gif(source, output, int(_num(command.args[0],15)) if command.args else 15, int(_num(command.args[1])) if len(command.args)>1 else None, _num(command.args[2]) if len(command.args)>2 else None)
                    elif name == "GIF转视频": output = await self.ffmpeg.gif_to_video(source, output)
                    elif name == "视频转换":
                        fmt = (command.args[0].lower().lstrip(".") if command.args else "mp4"); output = output.with_suffix("."+fmt); output = await self.ffmpeg.convert(source, output, fmt)
                    elif name == "视频压缩": output = await self.ffmpeg.compress(source, output, int(_num(command.args[0],28)) if command.args else 28)
                    elif name == "视频裁剪" and len(command.args)>=2: output = await self.ffmpeg.trim(source, output, _num(command.args[0]), _num(command.args[1]))
                    elif name == "视频调速" and command.args: output = await self.ffmpeg.speed(source, output, _num(command.args[0],1))
                    elif name == "视频缩放" and command.args and _size(command.args[0]):
                        w,h = _size(command.args[0]); output = await self.ffmpeg.scale(source, output, w, h)
                    elif name == "视频旋转" and command.args: output = await self.ffmpeg.rotate(source, output, int(_num(command.args[0],90)))
                    elif name == "提取音频": output = await self.ffmpeg.extract_audio(source, output)
                    elif name == "视频静音": output = await self.ffmpeg.mute(source, output)
                    elif name == "提取视频帧" and command.args: output = await self.ffmpeg.frame(source, output, _num(command.args[0]))
                    else: return await self._plain(event, "参数不足，请发送 /多模态帮助 查看用法")
                    await self._send_paths(event, [Path(output)])
                except FFmpegError as exc: await self._plain(event, str(exc))
            if hasattr(event, "stop_event"): event.stop_event()

        async def _audio_command(self, event, command: ParsedCommand):
            if not self.settings.enable_audio: return await self._plain(event, "音频功能已关闭")
            async with self.tasks.task(heavy=True) as work:
                files = await self._resolve(event, work, kind="audio")
                if not files: return await self._plain(event, "请附带或引用音频文件")
                source = files[0].path; name = command.name; output = work / ("output" + ({"音频转MP3":".mp3"}.get(name, source.suffix or ".mp3")))
                try:
                    probe = await self.audio.probe(source)
                    duration = float((probe.get("format") or {}).get("duration") or 0)
                    if duration > self.settings.max_duration: return await self._plain(event, f"音频时长超过限制（{self.settings.max_duration} 秒）")
                    if name in {"音频转换", "音频转MP3"}:
                        fmt = "mp3" if name == "音频转MP3" else (command.args[0].lower().lstrip(".") if command.args else "mp3"); output = output.with_suffix("."+fmt); output = await self.audio.convert_audio(source, output)
                    elif name == "音频裁剪" and len(command.args)>=2: output = await self.audio.trim_audio(source, output, _num(command.args[0]), _num(command.args[1]))
                    elif name == "音频调速" and command.args: output = await self.audio.speed_audio(source, output, _num(command.args[0],1))
                    elif name == "音量" and command.args: output = await self.audio.volume(source, output, _num(command.args[0],1))
                    elif name == "音频压缩": output = await self.audio.compress_audio(source, output, command.args[0] if command.args else "128k")
                    elif name == "音频静音" and len(command.args)>=2: output = await self.audio.mute_segment(source, output, _num(command.args[0]), _num(command.args[1]))
                    elif name == "音频信息":
                        info = await self.audio.info(source); return await self._plain(event, "格式 {format}，时长 {duration}s，码率 {bit_rate}，编码 {codec}".format(**info))
                    else: return await self._plain(event, "参数不足，请发送 /多模态帮助 查看用法")
                    await self._send_paths(event, [Path(output)])
                except FFmpegError as exc: await self._plain(event, str(exc))
            if hasattr(event, "stop_event"): event.stop_event()

        async def _mirage(self, event, mode: str):
            if not self.settings.enable_mirage: return await self._plain(event, "幻影坦克功能已关闭")
            if hasattr(event, "get_session_id"):
                session_id = f"{event.get_session_id()}:{getattr(event, 'get_sender_id', lambda: '')()}"
            else:
                session_id = str(uuid.uuid4())
            # Direct two-image form first.
            async with self.tasks.task() as work:
                files = await self._resolve(event, work, kind="image", limit=2)
                if len(files) >= 2:
                    out = work / "mirage.png"; await generate_mirage(files[0].path, files[1].path, out, mode, self.settings.mirage_a, self.settings.mirage_b, self.settings.mirage_w); await self._send_paths(event, [out]); return
            if not session_waiter: return await self._plain(event, "当前 AstrBot 版本不支持分步会话，请同时附带两张图片")
            session_dir = self.tasks.root / f"mirage-{uuid.uuid4().hex}"; session_dir.mkdir(parents=True, exist_ok=True)
            try:
                await self._plain(event, f"请在 {self.settings.mirage_timeout} 秒内发送表图；发送‘取消’可中止")
                @session_waiter(timeout=self.settings.mirage_timeout, record_history_chains=False)
                async def waiter(controller, incoming):
                    if str(getattr(incoming, "message_str", "")).strip() == "取消": controller.stop(); return
                    received = await resolve_media(incoming, session_dir, self.settings.mirage_max_mb*1024*1024, kind="image")
                    if not received: await self._plain(incoming, "请发送图片"); return
                    if not hasattr(controller, "front"):
                        controller.front = received[0].path; await self._plain(incoming, "收到表图，请发送里图"); controller.keep(self.settings.mirage_timeout, reset_timeout=True); return
                    out = session_dir / "mirage.png"; await generate_mirage(controller.front, received[0].path, out, mode, self.settings.mirage_a, self.settings.mirage_b, self.settings.mirage_w); await self._send_paths(incoming, [out]); controller.stop()
                await waiter(event, session_filter=_UserSessionFilter(session_id))
            except TimeoutError: await self._plain(event, "幻影坦克会话已超时并取消")
            finally: shutil.rmtree(session_dir, ignore_errors=True)

        @filter.event_message_type(EventMessageType.ALL)
        async def handle_all_commands(self, event: AstrMessageEvent):
            command = parse_command(getattr(event, "message_str", ""), match_mode=self.settings.match_mode)
            if not command: return
            name = command.name
            try:
                if name in {"多模态帮助", "图片帮助", "帮助"}: await self._plain(event, HELP)
                elif name in IMAGE_OPS or name in {"调速", "图片转换", "图片压缩", "图片缩放", "图片裁剪", "图片信息"}: await self._image_command(event, command)
                elif name in {"转换", "rename"}: await self._qq_convert(event, command)
                elif name in VIDEO_COMMANDS: await self._video_command(event, command)
                elif name in AUDIO_COMMANDS: await self._audio_command(event, command)
                elif name in {"幻影坦克", "彩色幻影坦克"}: await self._mirage(event, "color" if name.startswith("彩色") else "gray")
                elif name == "convert" and command.args and command.args[0] in {"addblock", "delblock", "blocklist"}:
                    if not getattr(event, "is_admin", lambda: False)(): return await self._plain(event, "需要管理员权限")
                    op = command.args[0]
                    if op == "blocklist": await self._plain(event, "禁用群：" + (", ".join(sorted(self.qq.blocked)) or "无"))
                    elif len(command.args) > 1: await self._plain(event, "已" + ("添加" if op == "addblock" and self.qq.add_group(command.args[1]) else "移除" if op == "delblock" and self.qq.remove_group(command.args[1]) else "更新") + "群聊限制")
            except (MediaError, MediaTooLarge) as exc: await self._plain(event, str(exc))
            except Exception as exc:
                if logger: logger.exception("multimodal processor command failed: %s", exc)
                await self._plain(event, "处理失败，请检查输入文件或插件日志")
            if hasattr(event, "stop_event") and name in (set(IMAGE_OPS) | {"调速", "图片转换", "图片压缩", "图片缩放", "图片裁剪", "图片信息", "转换", "rename"} | VIDEO_COMMANDS | AUDIO_COMMANDS | {"幻影坦克", "彩色幻影坦克"}): event.stop_event()

        async def terminate(self):
            self.tasks.cleanup_stale(0)
else:
    class MultimodalProcessor:  # pragma: no cover
        """Import placeholder when AstrBot is not installed."""
        pass

