"""FFmpeg / FFprobe 的异步调用。

- 只用参数列表启动子进程（create_subprocess_exec），从不经过 shell，用户输入只会作为已校验的数值出现。
- 超时或任务被取消时杀掉整个进程组，不留僵尸进程。
- Linux 上通过 RLIMIT_AS 限制单个 FFmpeg 进程的内存，并降低调度优先级。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import signal
import sys
from typing import Sequence

from .errors import MediaError, ProcessingError, ToolMissing

_STDERR_LIMIT = 4000


@dataclass(slots=True)
class StreamInfo:
    codec_type: str
    codec: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bit_rate: int = 0
    rotation: int = 0
    attached_pic: bool = False  # 音频文件内嵌的封面图，不算视频画面


@dataclass(slots=True)
class ProbeResult:
    format_name: str
    duration: float
    size: int
    bit_rate: int
    streams: list[StreamInfo] = field(default_factory=list)

    @property
    def video(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "video" and not s.attached_pic), None)

    @property
    def audio(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "audio"), None)

    @property
    def display_size(self) -> tuple[int, int]:
        v = self.video
        if not v:
            return 0, 0
        if v.rotation % 180:
            return v.height, v.width
        return v.width, v.height


def _to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _fps(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def parse_probe(data: dict) -> ProbeResult:
    fmt = data.get("format") or {}
    streams = []
    for raw in data.get("streams") or []:
        rotation = 0
        tags = raw.get("tags") or {}
        if "rotate" in tags:
            rotation = _to_int(tags.get("rotate"))
        for side in raw.get("side_data_list") or []:
            if "rotation" in side:
                rotation = _to_int(side.get("rotation"))
        streams.append(
            StreamInfo(
                codec_type=str(raw.get("codec_type") or ""),
                codec=str(raw.get("codec_name") or ""),
                width=_to_int(raw.get("width")),
                height=_to_int(raw.get("height")),
                fps=_fps(raw.get("avg_frame_rate")) or _fps(raw.get("r_frame_rate")),
                sample_rate=_to_int(raw.get("sample_rate")),
                channels=_to_int(raw.get("channels")),
                bit_rate=_to_int(raw.get("bit_rate")),
                rotation=abs(rotation) % 360,
                attached_pic=bool((raw.get("disposition") or {}).get("attached_pic")),
            )
        )
    duration = float(fmt.get("duration") or 0) if str(fmt.get("duration") or "").replace(".", "", 1).isdigit() else 0.0
    return ProbeResult(
        format_name=str(fmt.get("format_name") or ""),
        duration=duration,
        size=_to_int(fmt.get("size")),
        bit_rate=_to_int(fmt.get("bit_rate")),
        streams=streams,
    )


def _resolve_binary(path: str) -> str | None:
    if os.path.sep in path or (os.altsep and os.altsep in path):
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which(path)


class FFmpegRunner:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", *, threads: int = 2, memory_mb: int = 1536) -> None:
        self.ffmpeg_cmd = ffmpeg
        self.ffprobe_cmd = ffprobe
        self.threads = max(0, int(threads))
        self.memory_mb = max(0, int(memory_mb))

    @property
    def ffmpeg(self) -> str | None:
        return _resolve_binary(self.ffmpeg_cmd)

    @property
    def ffprobe(self) -> str | None:
        return _resolve_binary(self.ffprobe_cmd)

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def require(self) -> tuple[str, str]:
        ffmpeg, ffprobe = self.ffmpeg, self.ffprobe
        if not ffmpeg or not ffprobe:
            missing = "、".join(n for n, p in (("FFmpeg", ffmpeg), ("FFprobe", ffprobe)) if not p)
            raise ToolMissing(f"未找到 {missing}，视频和音频功能不可用。请安装 FFmpeg 或在配置中填写可执行文件路径。")
        return ffmpeg, ffprobe

    def _preexec(self):
        if sys.platform.startswith("win"):
            return None
        memory = self.memory_mb

        def limit() -> None:  # 在子进程中执行
            try:
                os.nice(5)
            except OSError:
                pass
            if memory:
                try:
                    import resource

                    limit_bytes = memory * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
                except (ImportError, ValueError, OSError):
                    pass

        return limit

    async def _exec(self, argv: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
        kwargs: dict = {}
        if not sys.platform.startswith("win"):
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = self._preexec()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **kwargs,
            )
        except FileNotFoundError as exc:
            raise ToolMissing("未找到 FFmpeg，请安装或检查配置中的路径") from exc
        except PermissionError as exc:
            raise ToolMissing("FFmpeg 没有执行权限，请检查配置中的路径") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except BaseException:
            await self._kill(process)
            raise
        return process.returncode or 0, stdout, stderr

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if not sys.platform.startswith("win"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

    async def probe(self, path: str | Path, timeout: float = 30) -> ProbeResult:
        _, ffprobe = self.require()
        argv = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
        try:
            code, out, err = await self._exec(argv, timeout)
        except asyncio.TimeoutError as exc:
            raise MediaError("读取媒体信息超时") from exc
        if code != 0:
            raise MediaError("无法识别的音视频文件")
        try:
            return parse_probe(json.loads(out.decode("utf-8", "replace") or "{}"))
        except (json.JSONDecodeError, ValueError) as exc:
            raise MediaError("无法读取媒体信息") from exc

    async def run(self, args: Sequence[str], output: Path, timeout: float) -> Path:
        """执行 ffmpeg。args 不含 ffmpeg 本身和输出路径。"""
        ffmpeg, _ = self.require()
        argv = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        if self.threads:
            argv += ["-filter_threads", str(self.threads)]  # 全局选项：滤镜线程数
        argv += [str(a) for a in args]
        if self.threads:
            argv += ["-threads", str(self.threads)]  # 输出选项：编码线程数
        argv.append(str(output))
        try:
            code, _, err = await self._exec(argv, timeout)
        except asyncio.TimeoutError as exc:
            output.unlink(missing_ok=True)
            raise ProcessingError(f"处理超时（{timeout:.0f} 秒），已中止") from exc
        if code != 0 or not output.exists() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            message = err.decode("utf-8", "replace").strip()[-_STDERR_LIMIT:]
            raise ProcessingError(_friendly_error(message, code))
        return output


def _friendly_error(stderr: str, code: int) -> str:
    lowered = stderr.lower()
    if "cannot allocate memory" in lowered or "out of memory" in lowered or code in (-9, 137):
        return "处理失败：内存不足（可在配置中调低分辨率或调高 FFmpeg 内存上限）"
    if "invalid data found" in lowered or "could not find codec" in lowered or "moov atom not found" in lowered:
        return "处理失败：文件已损坏或编码不受支持"
    if "does not contain any stream" in lowered or "matches no streams" in lowered or "output file is empty" in lowered:
        return "处理失败：文件里没有可处理的音视频流"
    last = stderr.splitlines()[-1] if stderr else f"FFmpeg 退出码 {code}"
    return f"处理失败：{last[:300]}"
