"""视频处理：只负责组装 FFmpeg 参数并调用 FFmpegRunner。

参数选择原则：
- 能直接封装（-c copy）就不重新编码，例如兼容编码的 MP4↔MKV、去除音轨；
- 需要编码时用 libx264 + CRF + 可配置 preset，-pix_fmt yuv420p 与 +faststart 保证各平台可播放；
- 时间裁剪、截帧把 -ss 放在 -i 之前（快速定位，转码时依然精确）；
- 视频转 GIF 在一个滤镜图里生成调色板并抖动，兼顾画质与体积。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.args import SizeSpec, format_bytes, format_seconds
from ...core.errors import UsageError
from ...core.ffmpeg import FFmpegRunner, ProbeResult

VIDEO_FORMATS = ("mp4", "webm", "mkv", "mov")
AUDIO_FORMATS = ("mp3", "m4a", "wav", "ogg", "flac", "opus")
_COPY_VIDEO = {"mp4": {"h264", "hevc", "av1", "mpeg4"}, "mov": {"h264", "hevc", "mpeg4", "prores"}, "mkv": {"h264", "hevc", "av1", "vp8", "vp9", "mpeg4"}, "webm": {"vp8", "vp9", "av1"}}
_COPY_AUDIO = {"mp4": {"aac", "mp3", "alac"}, "mov": {"aac", "mp3", "alac", "pcm_s16le"}, "mkv": {"aac", "mp3", "opus", "vorbis", "flac", "ac3", "pcm_s16le"}, "webm": {"opus", "vorbis"}}


@dataclass(slots=True)
class VideoJob:
    runner: FFmpegRunner
    source: Path
    probe: ProbeResult
    out_dir: Path
    stem: str
    timeout: float
    crf: int = 28
    preset: str = "veryfast"
    max_output_side: int = 1920

    def out(self, suffix: str, ext: str) -> Path:
        return self.out_dir / f"{self.stem}_{suffix}.{ext}"

    def h264(self, crf: int | None = None) -> list[str]:
        return ["-c:v", "libx264", "-preset", self.preset, "-crf", str(crf or self.crf), "-pix_fmt", "yuv420p"]

    def aac(self, bitrate: str = "128k") -> list[str]:
        return ["-c:a", "aac", "-b:a", bitrate] if self.probe.audio else ["-an"]

    def mp4_flags(self, ext: str) -> list[str]:
        return ["-movflags", "+faststart"] if ext in {"mp4", "mov"} else []


@dataclass(slots=True)
class VideoResult:
    path: Path
    note: str | None = None
    kind: str = "video"  # video / image / audio


def _keep_audio(job: VideoJob) -> list[str]:
    """MP4 能直接容纳的音频原样复制，其他转 AAC；没有音轨就不输出音轨。"""
    a = job.probe.audio
    if not a:
        return ["-an"]
    return ["-c:a", "copy"] if a.codec in {"aac", "mp3"} else job.aac()


def _even_scale(width: str, height: str) -> str:
    return f"scale={width}:{height}:flags=lanczos,setsar=1"


def _cap_filter(max_side: int) -> str:
    """把较长边限制在 max_side 内，保持比例且宽高为偶数（H.264 要求）。"""
    return (
        f"scale='if(gte(iw,ih),min({max_side},iw),-2)':'if(gte(iw,ih),-2,min({max_side},ih))':flags=lanczos,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
    )


def _check_window(job: VideoJob, start: float) -> None:
    duration = job.probe.duration
    if duration and start >= duration:
        raise UsageError(f"开始时间超出视频长度（{format_seconds(duration)}）")


async def to_gif(job: VideoJob, fps: int, width: int, duration: float, start: float = 0.0) -> VideoResult:
    _check_window(job, start)
    v = job.probe.video
    if not v:
        raise UsageError("这个文件没有视频画面")
    src_w = job.probe.display_size[0] or width
    width = max(16, min(width, src_w))
    out = job.out("gif", "gif")
    graph = (
        f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=256:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    )
    args = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", job.source, "-filter_complex", graph, "-loop", "0", "-an"]
    await job.runner.run(args, out, job.timeout)
    remain = max(0.0, (job.probe.duration or duration) - start)
    note = f"{fps} FPS，宽 {width}px，时长 {format_seconds(min(duration, remain))}，{format_bytes(out.stat().st_size)}"
    return VideoResult(out, note, "image")


async def gif_to_video(job: VideoJob) -> VideoResult:
    out = job.out("video", "mp4")
    vf = _cap_filter(job.max_output_side)
    args = ["-i", job.source, "-vf", vf, *job.h264(), "-an", "-movflags", "+faststart"]
    await job.runner.run(args, out, job.timeout)
    return VideoResult(out, f"已转为 MP4（{format_bytes(out.stat().st_size)}），透明区域显示为黑色")


async def convert(job: VideoJob, fmt: str) -> VideoResult:
    out = job.out("convert", fmt)
    v, a = job.probe.video, job.probe.audio
    if not v:
        raise UsageError("这个文件没有视频画面，请用音频指令处理")
    can_copy_v = v.codec in _COPY_VIDEO[fmt]
    can_copy_a = a is None or a.codec in _COPY_AUDIO[fmt]
    if can_copy_v and can_copy_a:
        args = ["-i", job.source, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", *job.mp4_flags(fmt)]
        if fmt in {"mp4", "mov"} and v.codec == "hevc":
            args += ["-tag:v", "hvc1"]
        try:
            await job.runner.run(args, out, job.timeout)
            return VideoResult(out, f"已直接封装为 {fmt.upper()}（未重新编码）")
        except Exception:
            out.unlink(missing_ok=True)
    if fmt == "webm":
        args = [
            "-i", job.source, "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libvpx-vp9", "-crf", str(min(63, job.crf + 6)), "-b:v", "0", "-row-mt", "1",
            "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p",
        ]
        args += ["-c:a", "libopus", "-b:a", "96k"] if a else ["-an"]
    else:
        args = ["-i", job.source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", *job.h264(), *job.aac(), *job.mp4_flags(fmt)]
    await job.runner.run(args, out, job.timeout)
    codec = "VP9/Opus" if fmt == "webm" else "H.264/AAC"
    return VideoResult(out, f"已重新编码为 {fmt.upper()}（{codec}，{format_bytes(out.stat().st_size)}）")


async def compress(job: VideoJob, crf: int, max_short_side: int | None = None) -> VideoResult:
    out = job.out("compressed", "mp4")
    filters = []
    if max_short_side:
        filters.append(f"scale='if(gte(iw,ih),-2,min({max_short_side},iw))':'if(gte(iw,ih),min({max_short_side},ih),-2)':flags=lanczos")
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1")
    args = ["-i", job.source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(filters), *job.h264(crf), *job.aac("96k"), "-movflags", "+faststart"]
    await job.runner.run(args, out, job.timeout)
    before, after = job.source.stat().st_size, out.stat().st_size
    note = f"CRF {crf}：{format_bytes(before)} → {format_bytes(after)}（{after / before * 100:.0f}%）"
    if after >= before:
        note += "\n原视频已经压缩得比较充分，结果没有变小，可以尝试更低的质量或更低的分辨率。"
    return VideoResult(out, note)


async def trim(job: VideoJob, start: float, end: float) -> VideoResult:
    _check_window(job, start)
    if job.probe.duration:
        end = min(end, job.probe.duration)
    out = job.out("trim", "mp4")
    args = ["-ss", f"{start:.3f}", "-i", job.source, "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", *job.h264(), *job.aac(), "-movflags", "+faststart"]
    await job.runner.run(args, out, job.timeout)
    return VideoResult(out, f"已截取 {format_seconds(start)} ～ {format_seconds(end)}")


def atempo_chain(factor: float) -> str:
    """atempo 单级只支持 0.5～2.0（新版可到 100），拆成多级保证兼容并保持音调。"""
    parts = []
    remaining = factor
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


async def speed(job: VideoJob, factor: float) -> VideoResult:
    v = job.probe.video
    if not v:
        raise UsageError("这个文件没有视频画面")
    out = job.out(f"x{factor:g}", "mp4")
    fps = v.fps if 1 <= v.fps <= 120 else 30
    out_fps = min(60.0, fps)  # 加速时丢帧保持原帧率，减速时补齐帧率，避免播放卡顿和体积暴涨
    video = f"[0:v]setpts=PTS/{factor:.6f},fps={out_fps:.3f},scale=trunc(iw/2)*2:trunc(ih/2)*2[v]"
    if job.probe.audio:
        graph = f"{video};[0:a]{atempo_chain(factor)}[a]"
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
    else:
        graph, maps = video, ["-map", "[v]", "-an"]
    args = ["-i", job.source, "-filter_complex", graph, *maps, *job.h264(), "-movflags", "+faststart"]
    await job.runner.run(args, out, job.timeout)
    new_duration = job.probe.duration / factor if job.probe.duration else None
    return VideoResult(out, f"×{factor:g}，时长 {format_seconds(job.probe.duration)} → {format_seconds(new_duration)}")


async def scale(job: VideoJob, spec: SizeSpec) -> VideoResult:
    src_w, src_h = job.probe.display_size
    if not src_w:
        raise UsageError("这个文件没有视频画面")
    w, h = spec.resolve(src_w, src_h)
    if max(w, h) > job.max_output_side:
        ratio = job.max_output_side / max(w, h)
        w, h = round(w * ratio), round(h * ratio)
        capped = True
    else:
        capped = False
    w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
    out = job.out(f"{w}x{h}", "mp4")
    args = ["-i", job.source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", _even_scale(str(w), str(h)), *job.h264(), *_keep_audio(job), "-movflags", "+faststart"]
    await job.runner.run(args, out, job.timeout)
    note = f"{src_w}x{src_h} → {w}x{h}"
    if capped:
        note += f"（已按输出上限 {job.max_output_side}px 等比缩小）"
    return VideoResult(out, note)


async def rotate(job: VideoJob, angle: int) -> VideoResult:
    if not job.probe.video:
        raise UsageError("这个文件没有视频画面")
    vf = {90: "transpose=1", 270: "transpose=2", 180: "hflip,vflip"}[angle]
    out = job.out(f"rotate{angle}", "mp4")
    args = ["-i", job.source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf + ",scale=trunc(iw/2)*2:trunc(ih/2)*2", *job.h264(), *_keep_audio(job), "-movflags", "+faststart"]
    await job.runner.run(args, out, job.timeout)
    return VideoResult(out, f"已顺时针旋转 {angle}°")


AUDIO_CODECS: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    "wav": ["-c:a", "pcm_s16le"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    "flac": ["-c:a", "flac"],
    "opus": ["-c:a", "libopus", "-b:a", "128k"],
}


async def extract_audio(job: VideoJob, fmt: str) -> VideoResult:
    a = job.probe.audio
    if not a:
        raise UsageError("这个视频没有音轨")
    out = job.out("audio", fmt)
    if fmt == "m4a" and a.codec == "aac":
        codec = ["-c:a", "copy", "-movflags", "+faststart"]
    elif fmt == "mp3" and a.codec == "mp3":
        codec = ["-c:a", "copy"]
    else:
        codec = AUDIO_CODECS[fmt]
    args = ["-i", job.source, "-map", "0:a:0", "-vn", "-sn", "-dn", *codec]
    await job.runner.run(args, out, job.timeout)
    return VideoResult(out, f"已提取音轨（{fmt.upper()}，{format_bytes(out.stat().st_size)}）", "audio")


async def mute(job: VideoJob) -> VideoResult:
    if not job.probe.video:
        raise UsageError("这个文件没有视频画面")
    ext = job.source.suffix.lstrip(".").lower()
    ext = ext if ext in {"mp4", "mkv", "webm", "mov"} else "mp4"
    out = job.out("muted", ext)
    try:
        await job.runner.run(["-i", job.source, "-map", "0:v:0", "-c:v", "copy", "-an", *job.mp4_flags(ext)], out, job.timeout)
    except Exception:
        out = job.out("muted", "mp4")
        await job.runner.run(["-i", job.source, "-map", "0:v:0", *job.h264(), "-an", "-movflags", "+faststart"], out, job.timeout)
    return VideoResult(out, "已去除声音" if job.probe.audio else "原视频本来就没有声音")


async def frame(job: VideoJob, at: float) -> VideoResult:
    if not job.probe.video:
        raise UsageError("这个文件没有视频画面")
    duration = job.probe.duration
    if duration and at > duration:
        raise UsageError(f"时间超出视频长度（{format_seconds(duration)}）")
    if duration and at >= duration - 0.05:
        at = max(0.0, duration - 0.1)
    out = job.out(f"frame_{at:.2f}s".replace(".", "_"), "png")
    args = ["-ss", f"{at:.3f}", "-i", job.source, "-frames:v", "1", "-update", "1"]
    await job.runner.run(args, out, job.timeout)
    return VideoResult(out, f"{format_seconds(at)} 处的画面", "image")


def describe(probe: ProbeResult, size: int) -> str:
    lines = [f"容器：{probe.format_name or '未知'}", f"时长：{format_seconds(probe.duration)}", f"大小：{format_bytes(size)}"]
    if probe.bit_rate:
        lines.append(f"总码率：{probe.bit_rate // 1000} kbps")
    v = probe.video
    if v:
        w, h = probe.display_size
        rot = f"，旋转 {v.rotation}°" if v.rotation else ""
        lines.append(f"视频：{v.codec}，{w}x{h}，{v.fps:.2f} FPS{rot}" + (f"，{v.bit_rate // 1000} kbps" if v.bit_rate else ""))
    a = probe.audio
    if a:
        lines.append(f"音频：{a.codec}，{a.sample_rate} Hz，{a.channels} 声道" + (f"，{a.bit_rate // 1000} kbps" if a.bit_rate else ""))
    if not v and not a:
        lines.append("没有检测到音视频流")
    return "\n".join(lines)
