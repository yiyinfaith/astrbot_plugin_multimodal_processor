from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageOps


@dataclass(slots=True)
class GIFFrames:
    frames: list[Image.Image]
    durations: list[int]
    loop: int
    info: dict


def read_frames(path: str | Path) -> GIFFrames:
    with Image.open(path) as source:
        if source.format != "GIF" and getattr(source, "n_frames", 1) <= 1:
            return GIFFrames([source.convert("RGBA")], [int(source.info.get("duration", 100) or 100)], 0, dict(source.info))
        frames: list[Image.Image] = []
        durations: list[int] = []
        for index in range(getattr(source, "n_frames", 1)):
            source.seek(index)
            frame = source.convert("RGBA").copy()
            frames.append(frame)
            durations.append(max(10, int(source.info.get("duration", 100) or 100)))
        return GIFFrames(frames, durations, int(source.info.get("loop", 0) or 0), dict(source.info))


def save_gif(data: GIFFrames, path: str | Path, *, max_fps: int = 50) -> Path:
    if not data.frames:
        raise ValueError("GIF没有帧")
    minimum = max(20, int(1000 / max(1, max_fps)))
    durations = [max(minimum, int(d)) for d in data.durations]
    first = data.frames[0].convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
    rest = [frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=255) for frame in data.frames[1:]]
    first.save(path, format="GIF", save_all=True, append_images=rest, duration=durations,
               loop=data.loop, disposal=2, optimize=False)
    return Path(path)


def apply_frames(path: str | Path, operation, output: str | Path, *, max_fps: int = 50) -> Path:
    data = read_frames(path)
    frames = [operation(frame) for frame in data.frames]
    data.frames = frames
    return save_gif(data, output, max_fps=max_fps)


def resize_frames(data: GIFFrames, width: int | None = None, height: int | None = None) -> GIFFrames:
    if not width and not height: return data
    result = []
    for frame in data.frames:
        w, h = frame.size
        if width and height: size = (width, height)
        elif width: size = (width, max(1, round(h * width / w)))
        else: size = (max(1, round(w * height / h)), height)
        result.append(frame.resize(size, Image.Resampling.LANCZOS))
    return GIFFrames(result, list(data.durations), data.loop, dict(data.info))
