"""图片 / 动图处理（同步函数，由调用方放进线程池执行）。

所有函数接收输入路径和输出路径前缀（不含扩展名），返回 ImageResult。
动图逐帧处理并保留帧时长、循环次数和透明度；转静态格式时会在 note 里明确说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from ...core.args import SizeSpec, format_bytes
from ...core.errors import MediaError, MediaTooLarge, UsageError
from ..gif import frames as fr
from ..gif.speed import apply_speed

TRANSFORMS = {
    "反色": "invert",
    "顺时针": "cw",
    "逆时针": "ccw",
    "左右翻转": "hflip",
    "上下翻转": "vflip",
    "左对称": "left",
    "右对称": "right",
    "上对称": "top",
    "下对称": "bottom",
}


@dataclass(slots=True)
class ImageResult:
    path: Path
    note: str | None = None


def _invert(frame: Image.Image) -> Image.Image:
    rgba = frame.convert("RGBA")
    inverted = ImageOps.invert(rgba.convert("RGB"))
    inverted.putalpha(rgba.getchannel("A"))
    return inverted


def _symmetry(frame: Image.Image, keep: str) -> Image.Image:
    """保留一半并镜像到另一半。奇数尺寸时中间那一列/行保留原样，不会丢像素或错位。"""
    src = frame.convert("RGBA")
    w, h = src.size
    out = src.copy()
    if keep in {"left", "right"}:
        if w < 2:
            return out
        big, small = (w + 1) // 2, w // 2
        if keep == "left":
            half = ImageOps.mirror(src.crop((0, 0, big, h)))
            out.paste(half.crop((big - small, 0, big, h)), (big, 0))
        else:
            half = ImageOps.mirror(src.crop((small, 0, w, h)))
            out.paste(half.crop((0, 0, small, h)), (0, 0))
    else:
        if h < 2:
            return out
        big, small = (h + 1) // 2, h // 2
        if keep == "top":
            half = ImageOps.flip(src.crop((0, 0, w, big)))
            out.paste(half.crop((0, big - small, w, big)), (0, big))
        else:
            half = ImageOps.flip(src.crop((0, small, w, h)))
            out.paste(half.crop((0, 0, w, small)), (0, 0))
    return out


def transform_frame(frame: Image.Image, op: str) -> Image.Image:
    if op == "invert":
        return _invert(frame)
    if op == "cw":
        return frame.transpose(Image.Transpose.ROTATE_270)
    if op == "ccw":
        return frame.transpose(Image.Transpose.ROTATE_90)
    if op == "hflip":
        return frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if op == "vflip":
        return frame.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if op in {"left", "right", "top", "bottom"}:
        return _symmetry(frame, op)
    raise ValueError(f"未知操作：{op}")


def _save_like_source(anim: fr.Animation, out_base: Path, quality: int = 95) -> Path:
    fmt = fr.output_format_for(anim)
    target = out_base.with_suffix(fr.FORMAT_TO_EXT[fmt])
    return fr.save_animation(anim, target, fmt, quality=quality)


def _map_frames(anim: fr.Animation, func) -> fr.Animation:
    return fr.Animation([func(f) for f in anim.frames], list(anim.durations), anim.loop, anim.format, anim.source_frames, dict(anim.info))


# --------------------------------------------------------------------------- 变换
def transform(path: Path, out_base: Path, op: str, limits: fr.ImageLimits) -> ImageResult:
    anim = fr.load(path, limits)
    result = _map_frames(anim, lambda f: transform_frame(f, op))
    return ImageResult(_save_like_source(result, out_base))


def speed(path: Path, out_base: Path, factor: float, limits: fr.ImageLimits, max_fps: int, allow_drop: bool) -> ImageResult:
    anim = fr.load(path, limits)
    if not anim.animated:
        raise UsageError("调速只适用于 GIF 等动图，这张是静态图片")
    result, plan = apply_speed(anim, factor, max_fps, allow_drop)
    return ImageResult(_save_like_source(result, out_base), plan.note)


# --------------------------------------------------------------------------- 格式转换
def convert(path: Path, out_base: Path, target: str, limits: fr.ImageLimits, *, static: bool = False, quality: int = 90) -> ImageResult:
    fmt = fr.EXT_TO_FORMAT[target]
    _, _, n_frames = fr.probe(path)
    animated_source = n_frames > 1
    keep_animation = animated_source and not static and fmt in fr.ANIMATABLE
    anim = fr.load(path, limits, first_frame_only=not keep_animation)
    out = out_base.with_suffix(fr.FORMAT_TO_EXT[fmt])
    note = None
    if keep_animation:
        fr.save_animation(anim, out, fmt, quality=quality)
        if fmt == "PNG":
            note = "已转换为 APNG 动图（部分聊天软件只显示第一帧）。需要静态 PNG 请加参数「静态」。"
        elif anim.source_frames != len(anim.frames):
            note = f"注意：输出 {len(anim.frames)} 帧，原图 {anim.source_frames} 帧。"
    else:
        fr.save_static(anim.frames[0], out, fmt, quality=quality)
        if animated_source and static:
            note = f"已按要求只保留第一帧（原图共 {n_frames} 帧）。"
        elif animated_source:
            note = f"{target.upper()} 不支持动画，已只保留第一帧（原图共 {n_frames} 帧）。需要保留动画请转换为 gif、webp 或 png。"
        if fmt in {"JPEG", "BMP"} and fr.has_alpha(anim.frames[0]):
            note = (note + "\n" if note else "") + f"{target.upper()} 不支持透明，透明区域已填充为白色。"
    return ImageResult(out, note)


# --------------------------------------------------------------------------- 压缩
def _fit_within(anim: fr.Animation, max_side: int | None) -> fr.Animation:
    if not max_side:
        return anim
    w, h = anim.size
    if max(w, h) <= max_side:
        return anim
    scale = max_side / max(w, h)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return _map_frames(anim, lambda f: f.resize(size, Image.Resampling.LANCZOS))


def compress(path: Path, out_base: Path, quality: int, limits: fr.ImageLimits, max_side: int | None = None) -> ImageResult:
    source_size = path.stat().st_size
    anim = _fit_within(fr.load(path, limits), max_side)
    colors = max(16, min(255, round(255 * quality / 100)))
    fmt = anim.format
    note = None
    if anim.animated:
        fmt = anim.format if anim.format in fr.ANIMATABLE else "GIF"
        out = out_base.with_suffix(fr.FORMAT_TO_EXT[fmt])
        if fmt == "PNG":  # APNG 无损，转成动态 WebP 才有明显的压缩效果
            fmt, out = "WEBP", out_base.with_suffix(".webp")
            note = "APNG 已转为动态 WebP 以减小体积。"
        fr.save_animation(anim, out, fmt, quality=quality, colors=colors)
    else:
        frame = anim.frames[0]
        if fmt in {"JPEG", "WEBP"}:
            out = out_base.with_suffix(fr.FORMAT_TO_EXT[fmt])
            fr.save_static(frame, out, fmt, quality)
        elif fmt == "GIF":
            out = out_base.with_suffix(".gif")
            fr.save_gif([frame], [0], out, None, colors)
        else:
            out = out_base.with_suffix(".png")
            if fmt not in {"PNG"}:
                note = f"{fmt} 已转为 PNG。"
            rgba = frame.convert("RGBA")
            if quality >= 95:
                image = rgba if fr.has_alpha(rgba) else rgba.convert("RGB")
                image.save(out, format="PNG", optimize=True)
            else:
                rgba.quantize(colors=colors, method=Image.Quantize.FASTOCTREE).save(out, format="PNG", optimize=True)
    result_size = out.stat().st_size
    if result_size >= source_size and max_side is None:
        out.unlink(missing_ok=True)
        raise UsageError(f"图片已经足够精简（{format_bytes(source_size)}），按质量 {quality} 重新压缩反而更大。可以降低质量或加最长边参数。")
    ratio = result_size / source_size * 100 if source_size else 100
    summary = f"{format_bytes(source_size)} → {format_bytes(result_size)}（{ratio:.0f}%）"
    return ImageResult(out, summary + (f"\n{note}" if note else ""))


# --------------------------------------------------------------------------- 缩放 / 裁剪
def resize(path: Path, out_base: Path, spec: SizeSpec, limits: fr.ImageLimits) -> ImageResult:
    _, (w, h), n_frames = fr.probe(path)
    new_w, new_h = spec.resolve(w, h)
    try:
        fr.check_limits((new_w, new_h), n_frames, limits)
    except MediaTooLarge as exc:
        raise UsageError(f"目标尺寸 {new_w}x{new_h} 过大：{exc}") from exc
    anim = fr.load(path, limits)
    method = Image.Resampling.LANCZOS if new_w * new_h <= w * h else Image.Resampling.BICUBIC
    result = _map_frames(anim, lambda f: f.resize((new_w, new_h), method))
    return ImageResult(_save_like_source(result, out_base), f"{w}x{h} → {new_w}x{new_h}")


def crop_box(size: tuple[int, int], mode: str, values: tuple) -> tuple[int, int, int, int]:
    w, h = size
    if mode == "ratio":
        rw, rh = values
        if w * rh > h * rw:  # 原图更宽
            cw, ch = max(1, round(h * rw / rh)), h
        else:
            cw, ch = w, max(1, round(w * rh / rw))
        left, top = (w - cw) // 2, (h - ch) // 2
        return left, top, left + cw, top + ch
    x, y, cw, ch = values
    left, top = max(0, x), max(0, y)
    right, bottom = min(w, x + cw), min(h, y + ch)
    if right <= left or bottom <= top:
        raise UsageError(f"裁剪区域不在图片范围内（图片尺寸 {w}x{h}）")
    return left, top, right, bottom


def crop(path: Path, out_base: Path, mode: str, values: tuple, limits: fr.ImageLimits) -> ImageResult:
    anim = fr.load(path, limits)
    box = crop_box(anim.size, mode, values)
    result = _map_frames(anim, lambda f: f.crop(box))
    w, h = box[2] - box[0], box[3] - box[1]
    return ImageResult(_save_like_source(result, out_base), f"裁剪区域：x={box[0]}, y={box[1]}, {w}x{h}")


# --------------------------------------------------------------------------- 信息
def info(path: Path, limits: fr.ImageLimits) -> dict:
    fmt, (w, h), n_frames = fr.probe(path)
    data = {
        "format": fmt or "未知",
        "width": w,
        "height": h,
        "frames": n_frames,
        "size": path.stat().st_size,
        "duration": None,
        "loop": None,
        "alpha": False,
        "mode": "",
    }
    try:
        with Image.open(path) as image:
            data["mode"] = image.mode
            data["alpha"] = image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info
            if n_frames > 1:
                data["loop"] = image.info.get("loop") if "loop" in image.info else "不循环"
                if n_frames <= limits.max_frames:
                    total = 0
                    for index in range(n_frames):
                        image.seek(index)
                        total += int(image.info.get("duration", 0) or 0)
                    data["duration"] = total
    except (OSError, ValueError, EOFError) as exc:
        raise MediaError(f"图片已损坏或无法读取：{exc}") from exc
    return data
