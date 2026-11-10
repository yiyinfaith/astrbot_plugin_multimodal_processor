"""视频与音频指令：统一的「找媒体 → 排队 → 下载 → ffprobe 校验 → 处理 → 发送」流程。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Awaitable, Callable

from ..core import args as A
from ..core.app import Invocation
from ..core.errors import MediaError, MediaMissing, MediaTooLarge, UsageError
from ..core.ffmpeg import ProbeResult
from ..core.formats import IMAGE
from ..core.media import AUDIO, IMAGE as REF_IMAGE, VIDEO, MediaFile
from ..processors.audio import ops as audio_ops
from ..processors.video import ops as video_ops

CRF_PRESETS = {"高": 23, "高质量": 23, "中": None, "低": 33, "低质量": 33}


def _missing(kind: str) -> MediaMissing:
    what = {"video": "视频", "audio": "音频或视频", "gif": "GIF 动图"}[kind]
    return MediaMissing(f"请引用一个{what}，或随指令附带{what}文件后再发送指令")


async def _fetch_media(inv: Invocation, slot, kind: str) -> MediaFile:
    """kind: video / audio / gif。按候选顺序下载，返回第一个格式匹配的文件。"""
    if kind == "gif":
        refs = inv.refs(REF_IMAGE)
        max_bytes = inv.settings.image.max_bytes
    elif kind == "video":
        refs = inv.refs(VIDEO)
        max_bytes = inv.settings.video.max_bytes
    else:
        refs = inv.refs(AUDIO)
        max_bytes = max(inv.settings.audio.max_bytes, inv.settings.video.max_bytes if inv.settings.video.enable else 0)
    if not refs:
        raise _missing(kind)
    errors: list[str] = []
    for ref in refs:
        try:
            media = await inv.fetch(ref, slot, max_bytes)
        except MediaTooLarge:
            raise
        except MediaError as exc:
            errors.append(str(exc))
            continue
        sniffed = media.sniffed
        if kind == "gif" and sniffed.kind == IMAGE:
            return media
        if kind == "video" and sniffed.maybe_video:
            return media
        if kind == "audio" and sniffed.maybe_audio:
            return media
        errors.append("引用的文件格式不对")
        media.path.unlink(missing_ok=True)
    raise MediaError(errors[0] if errors else "没有找到可处理的文件")


async def _probe_checked(inv: Invocation, media: MediaFile, kind: str) -> ProbeResult:
    probe = await inv.app.ffmpeg.probe(media.path)
    if kind == "video":
        if not probe.video:
            raise MediaError("这个文件没有视频画面" + ("，可以用音频指令处理" if probe.audio else ""))
        limit = inv.settings.video.max_duration
        side = max(probe.display_size)
        if side > inv.settings.video.max_input_side:
            raise MediaTooLarge(f"视频分辨率 {probe.display_size[0]}x{probe.display_size[1]} 超过限制（最长边 {inv.settings.video.max_input_side}px）")
    else:
        if not probe.audio:
            raise MediaError("这个文件里没有音频")
        limit = inv.settings.audio.max_duration
        if not probe.video and media.size > inv.settings.audio.max_bytes:
            raise MediaTooLarge(f"音频文件超过大小限制（{inv.settings.audio.max_mb} MB）")
        if probe.video and media.size > inv.settings.video.max_bytes:
            raise MediaTooLarge(f"视频文件超过大小限制（{inv.settings.video.max_mb} MB）")
    if probe.duration and probe.duration > limit:
        raise MediaTooLarge(f"时长 {A.format_seconds(probe.duration)} 超过限制（{A.format_seconds(limit)}）")
    return probe


def _video_job(inv: Invocation, media: MediaFile, probe: ProbeResult, workdir: Path) -> video_ops.VideoJob:
    v = inv.settings.video
    return video_ops.VideoJob(
        runner=inv.app.ffmpeg, source=media.path, probe=probe, out_dir=workdir,
        stem=media.stem if media.name else "video", timeout=inv.timeout,
        crf=v.default_crf, preset=v.preset, max_output_side=v.max_output_side,
    )


def _audio_job(inv: Invocation, media: MediaFile, probe: ProbeResult, workdir: Path) -> audio_ops.AudioJob:
    return audio_ops.AudioJob(
        runner=inv.app.ffmpeg, source=media.path, probe=probe, out_dir=workdir,
        stem=media.stem if media.name else "audio", timeout=inv.timeout, bitrate=inv.settings.audio.default_bitrate,
    )


async def _send(inv: Invocation, path: Path, note: str | None, kind: str) -> None:
    sender = inv.app.sender
    if kind == "image":
        await sender.image(inv.event, path, note)
    elif kind == "audio":
        await sender.audio(inv.event, path, path.name, note, inv.settings.audio.send_mode)
    else:
        mode = "file" if inv.settings.video.send_mode == "file" else "auto"
        await sender.video(inv.event, path, path.name, note, mode)


async def run_video(inv: Invocation, op: Callable[[video_ops.VideoJob], Awaitable[video_ops.VideoResult]], *, source: str = "video") -> bool:
    runner = inv.app.ffmpeg
    runner.require()
    if not (inv.refs(REF_IMAGE) if source == "gif" else inv.refs(VIDEO)):
        raise _missing(source)
    async with inv.app.tasks.task(inv.user_key, heavy=True, label="video", on_wait=inv.notify_queue) as slot:
        media = await _fetch_media(inv, slot, source)
        if source == "gif":
            media = await _prepare_gif(inv, media)
        probe = await _probe_checked(inv, media, "video")
        job = _video_job(inv, media, probe, slot.workdir)
        result = await inv.run_timed(op(job))
        await _send(inv, result.path, result.note, result.kind)
    return True


async def run_audio(inv: Invocation, op: Callable[[audio_ops.AudioJob], Awaitable[audio_ops.AudioResult]]) -> bool:
    inv.app.ffmpeg.require()
    if not inv.refs(AUDIO):
        raise _missing("audio")
    async with inv.app.tasks.task(inv.user_key, heavy=True, label="audio", on_wait=inv.notify_queue) as slot:
        media = await _fetch_media(inv, slot, "audio")
        probe = await _probe_checked(inv, media, "audio")
        job = _audio_job(inv, media, probe, slot.workdir)
        result = await inv.run_timed(op(job))
        await _send(inv, result.path, result.note, "audio")
    return True


async def _prepare_gif(inv: Invocation, media: MediaFile) -> MediaFile:
    """FFmpeg 不能解码动态 WebP / APNG 的全部帧，先用 Pillow 转成 GIF。"""
    path = media.path

    def check_and_convert() -> Path:
        from ..handlers.image import limits_of
        from ..processors.gif import frames as fr

        fmt, _, n_frames = fr.probe(path)
        if n_frames <= 1:
            raise UsageError("这是一张静态图片，GIF转视频 需要动图")
        if fmt == "GIF":
            return path
        anim = fr.load(path, limits_of(inv))
        target = path.with_suffix(".gif")
        fr.save_gif(anim.frames, anim.durations, target, anim.loop)
        return target

    converted = await inv.run_timed(inv.app.tasks.run_blocking(check_and_convert))
    return MediaFile(converted, media.sniffed, media.name or "animation", media.origin, converted.stat().st_size)


# ------------------------------------------------------------------ 视频指令
def _opt(inv: Invocation, index: int) -> str | None:
    return inv.args[index] if len(inv.args) > index else None


@dataclass(slots=True)
class GifOptions:
    fps: int | None = None
    every: int | None = None  # 每 N 帧取 1 帧
    width: int | None = None
    scale: float | None = None  # 相对原视频宽度
    start: float | None = None
    duration: float | None = None


_RANGE = re.compile(r"^(.+?)[-~～](.+)$")
_LABELS = {"fps": "帧率", "width": "宽度", "duration": "时长", "start": "开始时间"}


def parse_gif_options(args: tuple[str, ...]) -> GifOptions:
    """同时支持位置参数 `[帧率] [宽度] [时长] [开始]` 与关键字写法：
    `2s-4.5s`（时间段）、`开始 2`、`时长 3`、`fps 15` / `15fps`、`1/3`（每 3 帧取 1 帧）、`0.5` / `50%`（缩放）。"""
    opts = GifOptions()
    tokens = [A.normalize(t).lower() for t in args]
    positional: list[str] = []
    i = 0

    def next_value(name: str) -> str:
        nonlocal i
        if i + 1 >= len(tokens):
            raise UsageError(f"「{name}」后面需要填写数值")
        i += 1
        return tokens[i]

    while i < len(tokens):
        token = tokens[i]
        for word, field_name in (("开始", "start"), ("start", "start"), ("时长", "duration"), ("len", "duration")):
            if token.startswith(word):
                value = token[len(word):] or next_value(word)
                setattr(opts, field_name, A.parse_time(value, "开始时间" if field_name == "start" else "时长"))
                break
        else:
            if token.startswith("fps") or token.startswith("帧率"):
                value = token.removeprefix("fps").removeprefix("帧率") or next_value("fps")
                opts.fps = A.parse_int(value, "帧率", 1, 50)
            elif token.endswith("fps"):
                opts.fps = A.parse_int(token[:-3], "帧率", 1, 50)
            elif re.fullmatch(r"\d+/\d+", token):
                a, b = (int(v) for v in token.split("/"))
                opts.every = max(a, b)
                if not 1 <= opts.every <= 30:
                    raise UsageError("抽帧间隔范围是 1～30，例如 1/3 表示每 3 帧取 1 帧")
            elif token.endswith("%"):
                opts.scale = A.parse_float(token[:-1], "缩放比例", 1, 100) / 100
            elif re.fullmatch(r"0?\.\d+|1\.0+", token):
                opts.scale = A.parse_float(token, "缩放比例", 0.05, 1.0)
            elif (match := _RANGE.fullmatch(token)) and not token.startswith("-"):
                start = A.parse_time(match.group(1).rstrip("s"), "开始时间")
                end = A.parse_time(match.group(2).rstrip("s"), "结束时间")
                if end <= start:
                    raise UsageError("结束时间必须晚于开始时间")
                opts.start, opts.duration = start, end - start
            else:
                positional.append(token)
        i += 1
    if len(positional) > 4:
        raise UsageError(f"参数太多：{' '.join(positional[4:])}")
    names = ("fps", "width", "duration", "start")
    for name, value in zip(names, positional):
        if getattr(opts, name) is not None:
            raise UsageError(f"重复设置了{_LABELS[name]}")
        if name == "fps":
            opts.fps = A.parse_int(value, "帧率", 1, 50)
        elif name == "width":
            opts.width = A.parse_int(value, "宽度", 16, 4096)
        elif name == "duration":
            opts.duration = A.parse_time(value, "时长")
        else:
            opts.start = A.parse_time(value, "开始时间")
    return opts


async def video_to_gif(inv: Invocation) -> bool:
    v = inv.settings.video
    max_width = inv.settings.image.gif_max_side
    opts = parse_gif_options(inv.args)
    duration = opts.duration if opts.duration is not None else float(v.gif_max_duration)
    if duration <= 0:
        raise UsageError("时长必须大于 0")
    if duration > v.gif_max_duration:
        raise UsageError(f"GIF 时长最多 {v.gif_max_duration} 秒（可在配置中调整）")
    if opts.width is not None and opts.width > max_width:
        raise UsageError(f"GIF 宽度最多 {max_width} 像素（可在配置中调整）")
    start = opts.start or 0.0

    async def op(job: video_ops.VideoJob) -> video_ops.VideoResult:
        src_fps = job.probe.video.fps if job.probe.video and job.probe.video.fps > 0 else 25.0
        if opts.every:
            fps = max(1, min(50, round(src_fps / opts.every)))
        else:
            fps = min(opts.fps or v.gif_fps, max(1, round(src_fps)))
        src_w = job.probe.display_size[0] or v.gif_width
        if opts.scale is not None:
            width = max(16, round(src_w * opts.scale))
        else:
            width = opts.width or v.gif_width
        width = min(width, max_width)
        return await video_ops.to_gif(job, fps, width, duration, start)

    return await run_video(inv, op)


async def gif_to_video(inv: Invocation) -> bool:
    return await run_video(inv, video_ops.gif_to_video, source="gif")


async def video_convert(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写目标格式")
    fmt = A.parse_format(inv.args[0], video_ops.VIDEO_FORMATS)
    return await run_video(inv, lambda job: video_ops.convert(job, fmt))


async def video_compress(inv: Invocation) -> bool:
    crf = inv.settings.video.default_crf
    first = _opt(inv, 0)
    if first:
        if first in CRF_PRESETS:
            crf = CRF_PRESETS[first] or crf
        else:
            crf = A.parse_int(first, "CRF", 18, 40)
    short_side = None
    if _opt(inv, 1):
        spec = A.parse_size(inv.args[1])
        short_side = spec.short or spec.height or spec.width
        if not short_side:
            raise UsageError("分辨率请写成 720p、480p 这样的形式")
    return await run_video(inv, lambda job: video_ops.compress(job, crf, short_side))


async def video_trim(inv: Invocation) -> bool:
    if len(inv.args) < 2:
        raise UsageError("请填写开始和结束时间")
    start, end = A.parse_range(inv.args[0], inv.args[1])
    return await run_video(inv, lambda job: video_ops.trim(job, start, end))


async def video_speed(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写倍率")
    factor = A.parse_float(inv.args[0], "倍率", 0.25, 4.0)
    return await run_video(inv, lambda job: video_ops.speed(job, factor))


async def video_scale(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写目标尺寸")
    spec = A.parse_size(inv.args[0], inv.settings.video.max_output_side * 4)
    return await run_video(inv, lambda job: video_ops.scale(job, spec))


async def video_rotate(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写角度")
    angle = A.parse_angle(inv.args[0])
    return await run_video(inv, lambda job: video_ops.rotate(job, angle))


async def video_extract_audio(inv: Invocation) -> bool:
    fmt = A.parse_format(inv.args[0], video_ops.AUDIO_FORMATS) if inv.args else "mp3"
    return await run_video(inv, lambda job: video_ops.extract_audio(job, fmt))


async def video_mute(inv: Invocation) -> bool:
    return await run_video(inv, video_ops.mute)


async def video_frame(inv: Invocation) -> bool:
    at = A.parse_time(inv.args[0], "时间") if inv.args else 0.0
    return await run_video(inv, lambda job: video_ops.frame(job, at))


async def video_info(inv: Invocation) -> bool:
    inv.app.ffmpeg.require()
    if not inv.refs(VIDEO):
        raise _missing("video")
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="vinfo") as slot:
        media = await _fetch_media(inv, slot, "video")
        probe = await inv.app.ffmpeg.probe(media.path)
    await inv.reply(video_ops.describe(probe, media.size))
    return True


# ------------------------------------------------------------------ 音频指令
async def audio_convert(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写目标格式")
    fmt = A.parse_format(inv.args[0], audio_ops.AUDIO_FORMATS)
    return await run_audio(inv, lambda job: audio_ops.convert(job, fmt))


async def audio_trim(inv: Invocation) -> bool:
    if len(inv.args) < 2:
        raise UsageError("请填写开始和结束时间")
    start, end = A.parse_range(inv.args[0], inv.args[1])
    return await run_audio(inv, lambda job: audio_ops.trim(job, start, end))


async def audio_speed(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写倍率")
    factor = A.parse_float(inv.args[0], "倍率", 0.25, 4.0)
    extra = list(inv.args[1:])
    if extra and extra[0].lower() not in {"变调", "pitch"}:
        raise UsageError(f"无法识别的参数：{extra[0]}")
    keep_pitch = not extra
    return await run_audio(inv, lambda job: audio_ops.speed(job, factor, keep_pitch))


async def audio_volume(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写音量")
    kind, value = A.parse_volume(inv.args[0])
    return await run_audio(inv, lambda job: audio_ops.volume(job, kind, value))


async def audio_compress(inv: Invocation) -> bool:
    kbps = A.parse_bitrate(inv.args[0]) if inv.args else 64
    return await run_audio(inv, lambda job: audio_ops.compress(job, kbps))


async def audio_mute(inv: Invocation) -> bool:
    if len(inv.args) < 2:
        raise UsageError("请填写开始和结束时间")
    start, end = A.parse_range(inv.args[0], inv.args[1])
    return await run_audio(inv, lambda job: audio_ops.mute_range(job, start, end))


async def audio_desilence(inv: Invocation) -> bool:
    threshold = A.parse_float(inv.args[0].lower().removesuffix("db"), "静音阈值", -90, -10) if inv.args else -50.0
    min_silence = A.parse_float(inv.args[1], "最短静音时长", 0.1, 10) if len(inv.args) > 1 else 0.5
    return await run_audio(inv, lambda job: audio_ops.remove_silence(job, threshold, min_silence))


async def audio_info(inv: Invocation) -> bool:
    inv.app.ffmpeg.require()
    if not inv.refs(AUDIO):
        raise _missing("audio")
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="ainfo") as slot:
        media = await _fetch_media(inv, slot, "audio")
        probe = await inv.app.ffmpeg.probe(media.path)
    await inv.reply(audio_ops.describe(probe, media.size))
    return True


HANDLERS = {
    "video_to_gif": video_to_gif,
    "gif_to_video": gif_to_video,
    "video_convert": video_convert,
    "video_compress": video_compress,
    "video_trim": video_trim,
    "video_speed": video_speed,
    "video_scale": video_scale,
    "video_rotate": video_rotate,
    "video_extract_audio": video_extract_audio,
    "video_mute": video_mute,
    "video_frame": video_frame,
    "video_info": video_info,
    "audio_convert": audio_convert,
    "audio_trim": audio_trim,
    "audio_speed": audio_speed,
    "audio_volume": audio_volume,
    "audio_compress": audio_compress,
    "audio_mute": audio_mute,
    "audio_desilence": audio_desilence,
    "audio_info": audio_info,
}
