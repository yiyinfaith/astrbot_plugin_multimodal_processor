"""动图（GIF / 动态 WebP / APNG）与静态图的统一读写。

读取：逐帧展开为完整的 RGBA 帧（Pillow 会按 disposal 合成增量帧），同时记录每帧时长与循环次数。
写入：GIF 每帧单独量化调色板，透明像素统一映射到 255 号索引，disposal=2，保留时长与循环信息；
没有循环扩展块的原图（只播放一次）写出时同样不加循环扩展块。
解码前先按像素数、帧数、内存预算检查，超出直接拒绝，避免大图把内存撑爆。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

from ...core.errors import MediaError, MediaTooLarge

ANIMATABLE = {"GIF", "WEBP", "PNG"}
STATIC_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"}
EXT_TO_FORMAT = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "gif": "GIF", "bmp": "BMP", "tiff": "TIFF", "tif": "TIFF"}
FORMAT_TO_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}
MAX_GIF_DURATION = 655350  # GIF 帧延时字段上限（百分之一秒 * 65535）


@dataclass(slots=True)
class ImageLimits:
    max_pixels: int = 40_000_000
    max_frames: int = 600
    memory_bytes: int = 512 * 1024 * 1024


@dataclass(slots=True)
class Animation:
    frames: list[Image.Image]
    durations: list[int]
    loop: int | None  # None = 原图没有循环信息（只播放一次）
    format: str  # 源格式：GIF / WEBP / PNG / JPEG ...
    source_frames: int = 1
    info: dict = field(default_factory=dict)

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1

    @property
    def size(self) -> tuple[int, int]:
        return self.frames[0].size

    @property
    def total_duration(self) -> int:
        return sum(self.durations)


def probe(path: str | Path) -> tuple[str, tuple[int, int], int]:
    """只读文件头，返回 (格式, 尺寸, 帧数)。"""
    try:
        with Image.open(path) as image:
            return (image.format or "").upper(), image.size, int(getattr(image, "n_frames", 1) or 1)
    except UnidentifiedImageError as exc:
        raise MediaError("无法识别的图片格式") from exc
    except Image.DecompressionBombError as exc:
        raise MediaTooLarge("图片分辨率过大") from exc
    except (OSError, ValueError) as exc:
        raise MediaError(f"图片已损坏或无法读取：{exc}") from exc


def check_limits(size: tuple[int, int], frames: int, limits: ImageLimits) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise MediaError("图片尺寸无效")
    if width * height > limits.max_pixels:
        raise MediaTooLarge(f"图片分辨率 {width}x{height} 超过限制（{limits.max_pixels // 1_000_000} 百万像素）")
    if frames > limits.max_frames:
        raise MediaTooLarge(f"动图有 {frames} 帧，超过限制（{limits.max_frames} 帧）")
    needed = width * height * 4 * frames
    if needed > limits.memory_bytes:
        raise MediaTooLarge(
            f"动图展开后约需 {needed / 1024 / 1024:.0f} MB 内存，超过限制（{limits.memory_bytes // 1024 // 1024} MB）"
        )


def load(path: str | Path, limits: ImageLimits, *, first_frame_only: bool = False) -> Animation:
    fmt, size, n_frames = probe(path)
    check_limits(size, 1 if first_frame_only else n_frames, limits)
    try:
        with Image.open(path) as image:
            loop = image.info.get("loop") if "loop" in image.info else None
            info = {k: v for k, v in image.info.items() if k in {"loop", "duration", "transparency", "background"}}
            frames: list[Image.Image] = []
            durations: list[int] = []
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                frames.append(frame.convert("RGBA"))
                durations.append(int(frame.info.get("duration", image.info.get("duration", 100)) or 0))
                if first_frame_only:
                    break
                if index + 1 > limits.max_frames:  # n_frames 不可信时的兜底
                    raise MediaTooLarge(f"动图帧数超过限制（{limits.max_frames} 帧）")
    except (MediaError, MediaTooLarge):
        raise
    except Image.DecompressionBombError as exc:
        raise MediaTooLarge("图片分辨率过大") from exc
    except (OSError, ValueError, EOFError) as exc:
        raise MediaError(f"图片已损坏或无法读取：{exc}") from exc
    if not frames:
        raise MediaError("图片没有可用的帧")
    return Animation(frames, durations, loop, fmt, n_frames, info)


def has_alpha(frame: Image.Image) -> bool:
    if frame.mode != "RGBA":
        return False
    return frame.getchannel("A").getextrema()[0] < 255


def _quantize(frame: Image.Image, colors: int = 255) -> tuple[Image.Image, bool]:
    """RGBA → P。不透明像素量化到最多 colors 色，透明像素（alpha<128）使用 255 号索引。"""
    colors = max(2, min(255, colors))
    alpha = frame.getchannel("A")
    transparent = alpha.getextrema()[0] < 128
    rgb = frame.convert("RGB")
    method = Image.Quantize.MEDIANCUT if frame.width * frame.height <= 600_000 else Image.Quantize.FASTOCTREE
    paletted = rgb.quantize(colors=colors, method=method, dither=Image.Dither.FLOYDSTEINBERG)
    palette = (paletted.getpalette() or [])[: colors * 3]
    palette += [0] * (768 - len(palette))
    paletted.putpalette(palette)
    if transparent:
        mask = alpha.point(lambda a: 255 if a < 128 else 0, "L")
        paletted.paste(255, mask=mask)
    return paletted, transparent


def save_gif(frames: list[Image.Image], durations: list[int], path: str | Path, loop: int | None, colors: int = 255) -> Path:
    if not frames:
        raise MediaError("没有可保存的帧")
    converted = [_quantize(frame.convert("RGBA"), colors) for frame in frames]
    images = [item[0] for item in converted]
    any_transparent = any(item[1] for item in converted)
    params: dict = {
        "save_all": True,
        "append_images": images[1:],
        "duration": [max(0, min(MAX_GIF_DURATION, int(d))) for d in durations],
        "disposal": 2,
        "optimize": False,
    }
    if loop is not None:
        params["loop"] = int(loop)
    if any_transparent:
        params["transparency"] = 255
    images[0].save(path, format="GIF", **params)
    return Path(path)


def flatten(frame: Image.Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """去掉透明通道（JPEG/BMP 不支持透明），透明区域铺白色。"""
    rgba = frame.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, background)
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def save_static(frame: Image.Image, path: str | Path, fmt: str, quality: int = 90) -> Path:
    fmt = fmt.upper()
    quality = max(1, min(100, int(quality)))
    if fmt == "JPEG":
        flatten(frame).save(path, format="JPEG", quality=quality, optimize=True)
    elif fmt == "WEBP":
        frame.convert("RGBA").save(path, format="WEBP", quality=quality, method=4)
    elif fmt == "GIF":
        save_gif([frame], [0], path, None)
    elif fmt == "BMP":
        flatten(frame).save(path, format="BMP")
    elif fmt == "TIFF":
        frame.convert("RGBA").save(path, format="TIFF", compression="tiff_deflate")
    else:
        image = frame.convert("RGBA")
        if not has_alpha(image):
            image = image.convert("RGB")
        image.save(path, format="PNG", compress_level=6)
    return Path(path)


def save_animation(anim: Animation, path: str | Path, fmt: str, quality: int = 90, colors: int = 255) -> Path:
    fmt = fmt.upper()
    if not anim.animated:
        return save_static(anim.frames[0], path, fmt, quality)
    if fmt == "GIF":
        return save_gif(anim.frames, anim.durations, path, anim.loop, colors)
    durations = [max(1, int(d)) for d in anim.durations]
    # 原图只播放一次时，WebP/APNG 也写成播放 1 次；否则沿用原循环次数（0 = 无限循环）
    loop = 1 if anim.loop is None else int(anim.loop)
    first, rest = anim.frames[0], anim.frames[1:]
    if fmt == "WEBP":
        first.save(path, format="WEBP", save_all=True, append_images=rest, duration=durations,
                   loop=loop, quality=max(1, min(100, quality)), method=4)
    elif fmt == "PNG":
        first.save(path, format="PNG", save_all=True, append_images=rest, duration=durations,
                   loop=loop, disposal=1, blend=0)
    else:
        raise MediaError(f"{fmt} 不支持动画")
    return Path(path)


def output_format_for(anim: Animation) -> str:
    """变换类操作的输出格式：动图保持原容器，静态图保持原格式（其他格式用 PNG）。"""
    if anim.animated:
        return anim.format if anim.format in ANIMATABLE else "GIF"
    return anim.format if anim.format in STATIC_FORMATS else "PNG"
