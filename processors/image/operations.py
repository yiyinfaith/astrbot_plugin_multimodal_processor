from __future__ import annotations

from pathlib import Path
import io
from PIL import Image, ImageChops, ImageOps

from .gif import GIFFrames, read_frames, save_gif


def _transform(frame: Image.Image, op: str) -> Image.Image:
    if op == "invert":
        rgba = frame.convert("RGBA"); rgb = ImageOps.invert(rgba.convert("RGB")); rgb.putalpha(rgba.getchannel("A")); return rgb
    if op == "cw": return frame.rotate(-90, expand=True)
    if op == "ccw": return frame.rotate(90, expand=True)
    if op == "hflip": return ImageOps.mirror(frame)
    if op == "vflip": return ImageOps.flip(frame)
    if op in {"right", "top", "bottom"}:
        return _symmetry(frame, op)
    return frame


def _symmetry(frame: Image.Image, direction: str) -> Image.Image:
    src = frame.convert("RGBA"); w, h = src.size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if direction == "left":
        half = src.crop((0, 0, (w + 1)//2, h)); out.paste(half, (0,0)); out.paste(ImageOps.mirror(half).crop((0,0,w//2,h)), (w - w//2, 0))
    elif direction == "right":
        half = src.crop((w//2, 0, w, h)); out.paste(half, (w//2,0)); out.paste(ImageOps.mirror(half).crop((0,0,(w+1)//2,h)), (0,0))
    elif direction == "top":
        half = src.crop((0, 0, w, (h+1)//2)); out.paste(half, (0,0)); out.paste(ImageOps.flip(half).crop((0,0,w,h//2)), (0,h-h//2))
    else:
        half = src.crop((0, h//2, w, h)); out.paste(half, (0,h//2)); out.paste(ImageOps.flip(half).crop((0,0,w,(h+1)//2)), (0,0))
    return out


def _speed(data: GIFFrames, factor: float, max_fps: int, allow_drop: bool) -> GIFFrames:
    factor = min(5.0, max(0.3, factor)); durations = [max(1, round(d / factor)) for d in data.durations]
    min_ms = max(20, int(1000 / max(1, max_fps)))
    frames = list(data.frames)
    if allow_drop and any(d < min_ms for d in durations):
        step = max(1, round(min_ms / max(1, min(durations))))
        frames = frames[::step]; durations = durations[::step]
    else:
        durations = [max(min_ms, d) for d in durations]
    return GIFFrames(frames, durations, data.loop, data.info)


def process_image(path: str | Path, output: str | Path, *, operation: str = "", output_format: str | None = None,
                  quality: int = 85, width: int | None = None, height: int | None = None,
                  crop: tuple[int, int, int, int] | None = None, speed: float | None = None,
                  keep_animation: bool = True, max_fps: int = 50, allow_frame_drop: bool = False) -> Path:
    source = Path(path); target = Path(output)
    with Image.open(source) as probe:
        animated = getattr(probe, "n_frames", 1) > 1 and probe.format == "GIF"
    data = read_frames(source) if animated else None
    if data and keep_animation and (operation or speed or width or height or crop):
        frames = data.frames
        if operation: frames = [_transform(f, operation) if operation not in {"left","right","top","bottom"} else _symmetry(f, operation) for f in frames]
        if crop: frames = [f.crop(crop) for f in frames]
        if width or height:
            frames = [f.resize((width or round(f.width * (height / f.height)), height or round(f.height * (width / f.width))), Image.Resampling.LANCZOS) for f in frames]
        data.frames = frames
        if speed: data = _speed(data, speed, max_fps, allow_frame_drop)
        if (output_format or "gif").lower().lstrip(".") == "gif": return save_gif(data, target, max_fps=max_fps)
    with Image.open(source) as image:
        image = image.convert("RGBA")
        if operation: image = _transform(image, operation) if operation not in {"left","right","top","bottom"} else _symmetry(image, operation)
        if crop: image = image.crop(crop)
        if width or height:
            new_size = (width or round(image.width * height / image.height), height or round(image.height * width / image.width))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        fmt = (output_format or target.suffix.lstrip(".") or "png").lower()
        if fmt in {"jpg", "jpeg"}: image = image.convert("RGB"); fmt = "JPEG"
        elif fmt == "webp": fmt = "WEBP"
        elif fmt == "bmp": fmt = "BMP"
        elif fmt == "gif": fmt = "GIF"; image = image.convert("P")
        else: fmt = "PNG"
        kwargs = {"quality": max(1, min(100, quality))} if fmt in {"JPEG", "WEBP"} else {}
        image.save(target, format=fmt, **kwargs)
    return target


def image_info(path: str | Path) -> dict:
    with Image.open(path) as image:
        return {"format": image.format, "width": image.width, "height": image.height,
                "mode": image.mode, "frames": getattr(image, "n_frames", 1),
                "duration_ms": image.info.get("duration"), "loop": image.info.get("loop"),
                "size_bytes": Path(path).stat().st_size}
