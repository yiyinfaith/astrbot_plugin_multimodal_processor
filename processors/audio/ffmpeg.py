from __future__ import annotations

from pathlib import Path
from ..video.ffmpeg import FFmpeg


class AudioFFmpeg(FFmpeg):
    async def convert_audio(self, source, output, bitrate: str | None = None) -> Path:
        args = [str(source)]
        if bitrate: args.extend(["-b:a", bitrate])
        return await self.run(args, output=output)

    async def trim_audio(self, source, output, start: float, end: float) -> Path:
        return await self.run([str(source), "-ss", str(max(0,float(start))), "-to", str(max(float(start),float(end))), "-vn"], output=output)

    async def speed_audio(self, source, output, factor: float) -> Path:
        factor = min(8.0, max(.125, float(factor))); parts=[]; left=factor
        while left > 2: parts.append("atempo=2.0"); left /= 2
        while left < .5: parts.append("atempo=0.5"); left /= .5
        parts.append(f"atempo={left:.8f}")
        return await self.run([str(source), "-filter:a", ",".join(parts)], output=output)

    async def volume(self, source, output, factor: float) -> Path:
        return await self.run([str(source), "-filter:a", f"volume={max(.05,min(8,float(factor))):.4f}"], output=output)

    async def compress_audio(self, source, output, bitrate: str = "128k") -> Path:
        return await self.run([str(source), "-b:a", bitrate], output=output)

    async def mute_segment(self, source, output, start: float, end: float) -> Path:
        return await self.run([str(source), "-af", f"volume=enable='between(t,{max(0,float(start))},{max(float(start),float(end))})':volume=0"], output=output)

    async def info(self, source) -> dict:
        data = await self.probe(source); fmt = data.get("format", {}); stream = next((x for x in data.get("streams", []) if x.get("codec_type") == "audio"), {})
        return {"format": fmt.get("format_name"), "duration": fmt.get("duration"), "bit_rate": stream.get("bit_rate") or fmt.get("bit_rate"), "codec": stream.get("codec_name"), "sample_rate": stream.get("sample_rate")}
