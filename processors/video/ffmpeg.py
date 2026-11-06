from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Sequence


class FFmpegError(RuntimeError):
    pass


class FFmpeg:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", timeout: int = 300, semaphore: asyncio.Semaphore | None = None):
        self.ffmpeg, self.ffprobe, self.timeout = ffmpeg, ffprobe, timeout
        self.semaphore = semaphore or asyncio.Semaphore(1)

    async def _run(self, argv: Sequence[str], timeout: int | None = None) -> tuple[str, str]:
        try:
            process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as exc:
            raise FFmpegError("未找到 FFmpeg/FFprobe，请先安装并检查配置路径") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout or self.timeout)
        except asyncio.TimeoutError:
            process.kill(); await process.communicate(); raise FFmpegError("FFmpeg 处理超时")
        out, err = stdout.decode(errors="replace"), stderr.decode(errors="replace")
        if process.returncode:
            raise FFmpegError(err[-2000:] or f"FFmpeg exited {process.returncode}")
        return out, err

    async def run(self, args: Sequence[str], *, output: str | Path, timeout: int | None = None) -> Path:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(args[0]), *map(str, args[1:]), str(target)]
        async with self.semaphore:
            await self._run(argv, timeout)
        return target

    async def probe(self, path: str | Path) -> dict:
        out, _ = await self._run([self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
        try: return json.loads(out)
        except json.JSONDecodeError as exc: raise FFmpegError("FFprobe 返回无效数据") from exc

    async def video_to_gif(self, source, output, fps: int | None = None, width: int | None = None, duration: float | None = None) -> Path:
        vf = []
        if fps: vf.append(f"fps={max(1,min(60,int(fps)))}")
        if width: vf.append(f"scale={max(16,int(width))}:-1:flags=lanczos")
        args = [str(source)]
        if duration: args.extend(["-t", str(max(.1, float(duration)))])
        args.extend(["-vf", ",".join(vf or ["fps=15"])])
        return await self.run(args, output=output)

    async def gif_to_video(self, source, output, codec: str = "libx264") -> Path:
        return await self.run([str(source), "-movflags", "+faststart", "-pix_fmt", "yuv420p", "-c:v", codec], output=output)

    async def convert(self, source, output, format_name: str | None = None) -> Path:
        args = [str(source)]
        if format_name: args.extend(["-f", format_name])
        return await self.run(args, output=output)

    async def compress(self, source, output, crf: int = 28) -> Path:
        return await self.run([str(source), "-c:v", "libx264", "-crf", str(max(16,min(40,int(crf)))), "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart"], output=output)

    async def trim(self, source, output, start: float, end: float) -> Path:
        return await self.run([str(source), "-ss", str(max(0,float(start))), "-to", str(max(float(start),float(end))), "-c", "copy"], output=output)

    async def speed(self, source, output, factor: float) -> Path:
        factor = min(8.0, max(.125, float(factor)))
        video = f"setpts={1/factor:.8f}*PTS"
        parts = []
        left = factor
        while left > 2: parts.append("atempo=2.0"); left /= 2
        while left < .5: parts.append("atempo=0.5"); left /= .5
        parts.append(f"atempo={left:.8f}")
        return await self.run([str(source), "-filter_complex", f"[0:v]{video}[v];[0:a]{','.join(parts)}[a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac"], output=output)

    async def scale(self, source, output, width: int, height: int) -> Path:
        return await self.run([str(source), "-vf", f"scale={max(16,width)}:{max(16,height)}:flags=lanczos"], output=output)

    async def rotate(self, source, output, angle: int) -> Path:
        angle = angle % 360
        transpose = {90:"transpose=1", 180:"transpose=1,transpose=1", 270:"transpose=2"}.get(angle)
        if not transpose: return await self.convert(source, output)
        return await self.run([str(source), "-vf", transpose], output=output)

    async def extract_audio(self, source, output) -> Path:
        return await self.run([str(source), "-vn", "-c:a", "libmp3lame", "-q:a", "2"], output=output)

    async def mute(self, source, output) -> Path:
        return await self.run([str(source), "-c:v", "copy", "-an"], output=output)

    async def frame(self, source, output, at: float) -> Path:
        return await self.run([str(source), "-ss", str(max(0,float(at))), "-frames:v", "1"], output=output)
