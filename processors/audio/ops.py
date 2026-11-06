"""音频处理：组装 FFmpeg 参数。输出格式默认沿用源格式（可编码时），否则用 MP3。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.args import format_bytes, format_seconds
from ...core.errors import UsageError
from ...core.ffmpeg import FFmpegRunner, ProbeResult
from ..video.ops import atempo_chain

AUDIO_FORMATS = ("mp3", "wav", "ogg", "m4a", "flac", "opus")
_SOURCE_EXT = {".mp3": "mp3", ".wav": "wav", ".ogg": "ogg", ".m4a": "m4a", ".aac": "m4a", ".flac": "flac", ".opus": "opus"}


@dataclass(slots=True)
class AudioJob:
    runner: FFmpegRunner
    source: Path
    probe: ProbeResult
    out_dir: Path
    stem: str
    timeout: float
    bitrate: int = 128  # kbps

    @property
    def native_format(self) -> str:
        return _SOURCE_EXT.get(self.source.suffix.lower(), "mp3")

    def out(self, suffix: str, fmt: str) -> Path:
        return self.out_dir / f"{self.stem}_{suffix}.{fmt}"

    def codec(self, fmt: str, bitrate: int | None = None) -> list[str]:
        kbps = bitrate or self.bitrate
        if fmt == "mp3":
            return ["-c:a", "libmp3lame", "-b:a", f"{kbps}k"] if bitrate else ["-c:a", "libmp3lame", "-q:a", "2"]
        if fmt == "m4a":
            return ["-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart"]
        if fmt == "ogg":
            return ["-c:a", "libvorbis", "-b:a", f"{kbps}k"] if bitrate else ["-c:a", "libvorbis", "-q:a", "5"]
        if fmt == "opus":
            return ["-c:a", "libopus", "-b:a", f"{min(kbps, 256)}k"]
        if fmt == "flac":
            return ["-c:a", "flac", "-compression_level", "8"]
        return ["-c:a", "pcm_s16le"]

    def base(self) -> list[str]:
        if not self.probe.audio:
            raise UsageError("这个文件里没有音频")
        return ["-i", self.source, "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "0"]


@dataclass(slots=True)
class AudioResult:
    path: Path
    note: str | None = None


def _check_start(job: AudioJob, start: float) -> None:
    if job.probe.duration and start >= job.probe.duration:
        raise UsageError(f"开始时间超出音频长度（{format_seconds(job.probe.duration)}）")


async def convert(job: AudioJob, fmt: str) -> AudioResult:
    out = job.out("convert", fmt)
    await job.runner.run([*job.base(), *job.codec(fmt)], out, job.timeout)
    return AudioResult(out, f"已转换为 {fmt.upper()}（{format_bytes(out.stat().st_size)}）")


async def trim(job: AudioJob, start: float, end: float) -> AudioResult:
    _check_start(job, start)
    if job.probe.duration:
        end = min(end, job.probe.duration)
    fmt = job.native_format
    out = job.out("trim", fmt)
    args = ["-ss", f"{start:.3f}", *job.base(), "-t", f"{end - start:.3f}", *job.codec(fmt)]
    await job.runner.run(args, out, job.timeout)
    return AudioResult(out, f"已截取 {format_seconds(start)} ～ {format_seconds(end)}")


async def speed(job: AudioJob, factor: float, keep_pitch: bool = True) -> AudioResult:
    fmt = job.native_format
    out = job.out(f"x{factor:g}", fmt)
    if keep_pitch:
        audio_filter = atempo_chain(factor)
    else:
        rate = job.probe.audio.sample_rate if job.probe.audio and job.probe.audio.sample_rate else 44100
        audio_filter = f"asetrate={int(rate * factor)},aresample={rate}"
    await job.runner.run([*job.base(), "-af", audio_filter, *job.codec(fmt)], out, job.timeout)
    new = job.probe.duration / factor if job.probe.duration else None
    mode = "保持音调" if keep_pitch else "同时改变音调"
    return AudioResult(out, f"×{factor:g}（{mode}），时长 {format_seconds(job.probe.duration)} → {format_seconds(new)}")


async def volume(job: AudioJob, kind: str, value: float) -> AudioResult:
    fmt = job.native_format
    out = job.out("volume", fmt)
    expr = f"volume={value:.2f}dB" if kind == "db" else f"volume={value:.4f}"
    boosting = value > 0 if kind == "db" else value > 1
    if boosting:
        expr += ",alimiter=limit=0.97:level=0"
    await job.runner.run([*job.base(), "-af", expr, *job.codec(fmt)], out, job.timeout)
    label = f"{value:+g} dB" if kind == "db" else f"×{value:g}"
    return AudioResult(out, f"音量 {label}" + ("（已加限幅防止爆音）" if boosting else ""))


async def compress(job: AudioJob, kbps: int) -> AudioResult:
    fmt = job.native_format if job.native_format in {"mp3", "m4a", "ogg", "opus"} else "mp3"
    out = job.out(f"{kbps}k", fmt)
    await job.runner.run([*job.base(), *job.codec(fmt, kbps)], out, job.timeout)
    before, after = job.source.stat().st_size, out.stat().st_size
    note = f"{kbps} kbps {fmt.upper()}：{format_bytes(before)} → {format_bytes(after)}（{after / before * 100:.0f}%）"
    if after >= before:
        note += "\n原文件码率已经不高，可以尝试更低的码率。"
    return AudioResult(out, note)


async def mute_range(job: AudioJob, start: float, end: float) -> AudioResult:
    _check_start(job, start)
    fmt = job.native_format
    out = job.out("muted", fmt)
    expr = f"volume=enable='between(t,{start:.3f},{end:.3f})':volume=0"
    await job.runner.run([*job.base(), "-af", expr, *job.codec(fmt)], out, job.timeout)
    return AudioResult(out, f"已将 {format_seconds(start)} ～ {format_seconds(end)} 静音")


async def remove_silence(job: AudioJob, threshold_db: float, min_silence: float) -> AudioResult:
    fmt = job.native_format
    out = job.out("desilenced", fmt)
    expr = (
        f"silenceremove=start_periods=1:start_duration=0:start_threshold={threshold_db:g}dB:"
        f"stop_periods=-1:stop_duration={min_silence:g}:stop_threshold={threshold_db:g}dB"
    )
    await job.runner.run([*job.base(), "-af", expr, *job.codec(fmt)], out, job.timeout)
    probe = await job.runner.probe(out)
    if probe.duration <= 0.05:
        raise UsageError("整段音频都低于静音阈值，处理后没有剩余内容。可以把阈值调低，例如 -60")
    return AudioResult(out, f"已删除低于 {threshold_db:g} dB、长于 {min_silence:g} 秒的静音：{format_seconds(job.probe.duration)} → {format_seconds(probe.duration)}")


def describe(probe: ProbeResult, size: int) -> str:
    a = probe.audio
    lines = [f"格式：{probe.format_name or '未知'}", f"时长：{format_seconds(probe.duration)}", f"大小：{format_bytes(size)}"]
    if a:
        bitrate = a.bit_rate or probe.bit_rate
        lines.append(f"编码：{a.codec}，{a.sample_rate} Hz，{a.channels} 声道" + (f"，{bitrate // 1000} kbps" if bitrate else ""))
    else:
        lines.append("没有检测到音频流")
    if probe.video:
        lines.append("（文件里还包含视频画面）")
    return "\n".join(lines)
