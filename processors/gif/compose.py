"""动图的合成与拆分：精灵图转动图、多图合成、逐帧分解、网格切图。

功能设计参考 astrbot_plugin_gifcaijian（shskjw，AGPL-3.0），实现已重写：
统一走本插件的帧读写（保留透明度、循环、时长），并对输入尺寸、帧数做限制。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ...core.errors import MediaTooLarge, UsageError
from . import frames as fr


@dataclass(frozen=True, slots=True)
class Margins:
    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0

    @property
    def empty(self) -> bool:
        return not (self.top or self.bottom or self.left or self.right)

    def describe(self) -> str:
        return f"上{self.top} 下{self.bottom} 左{self.left} 右{self.right}"


def trim_margins(frame: Image.Image, margins: Margins) -> Image.Image:
    if margins.empty:
        return frame
    w, h = frame.size
    box = (margins.left, margins.top, w - margins.right, h - margins.bottom)
    if box[0] >= box[2] or box[1] >= box[3]:
        raise UsageError(f"边距过大：图片只有 {w}x{h}")
    return frame.crop(box)


def grid_cells(size: tuple[int, int], rows: int, cols: int) -> list[tuple[int, int, int, int]]:
    """按行优先返回每个格子的区域。余下的像素平均分给各格，不会丢掉右侧和底部的边。"""
    w, h = size
    if w < cols or h < rows:
        raise UsageError(f"图片 {w}x{h} 太小，无法切成 {rows}x{cols}")
    xs = [round(i * w / cols) for i in range(cols + 1)]
    ys = [round(j * h / rows) for j in range(rows + 1)]
    return [(xs[c], ys[r], xs[c + 1], ys[r + 1]) for r in range(rows) for c in range(cols)]


def sprite_to_animation(path: Path, rows: int, cols: int, frame_ms: int, margins: Margins, limits: fr.ImageLimits) -> fr.Animation:
    anim = fr.load(path, limits, first_frame_only=True)
    sheet = trim_margins(anim.frames[0], margins)
    cells = grid_cells(sheet.size, rows, cols)
    width = min(c[2] - c[0] for c in cells)
    height = min(c[3] - c[1] for c in cells)
    if width < 2 or height < 2:
        raise UsageError(f"单格只有 {width}x{height}，请检查行列数")
    frames = [sheet.crop((x0, y0, x0 + width, y0 + height)) for x0, y0, _, _ in cells]
    # 去掉末尾完全透明的空格子（精灵图最后一行常常不满）
    while len(frames) > 1 and frames[-1].getchannel("A").getextrema()[1] == 0:
        frames.pop()
    return fr.Animation(frames, [frame_ms] * len(frames), 0, "GIF", len(frames))


def compose_images(paths: list[Path], frame_ms: int, limits: fr.ImageLimits, max_side: int) -> fr.Animation:
    """多张图片合成动图：以最大的宽、高为画布，每张等比缩放后居中，空白处透明。"""
    needed = 0
    for path in paths:  # 所有图片会同时留在内存里，先按总量检查
        _, (width, height), _ = fr.probe(path)
        needed += width * height * 4
    if needed > limits.memory_bytes:
        raise MediaTooLarge(f"这些图片展开后约需 {needed / 1024 / 1024:.0f} MB 内存，超过限制（{limits.memory_bytes // 1024 // 1024} MB）")
    images = [fr.load(p, limits, first_frame_only=True).frames[0] for p in paths]
    canvas_w = max(i.width for i in images)
    canvas_h = max(i.height for i in images)
    if max(canvas_w, canvas_h) > max_side:
        scale = max_side / max(canvas_w, canvas_h)
        canvas_w, canvas_h = max(1, round(canvas_w * scale)), max(1, round(canvas_h * scale))
    fr.check_limits((canvas_w, canvas_h), len(images), limits)
    frames = []
    for image in images:
        ratio = min(canvas_w / image.width, canvas_h / image.height)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        resized = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        canvas.paste(resized, ((canvas_w - size[0]) // 2, (canvas_h - size[1]) // 2), resized)
        frames.append(canvas)
    return fr.Animation(frames, [frame_ms] * len(frames), 0, "GIF", len(frames))


def pick_frames(total: int, limit: int) -> list[int]:
    """帧数超过上限时均匀抽取，首尾帧都保留。"""
    if total <= limit:
        return list(range(total))
    if limit == 1:
        return [0]
    return sorted({round(i * (total - 1) / (limit - 1)) for i in range(limit)})


def split_frames(path: Path, out_dir: Path, limit: int, limits: fr.ImageLimits) -> tuple[list[Path], int]:
    anim = fr.load(path, limits)
    if not anim.animated:
        raise UsageError("这是一张静态图片，没有可以分解的帧")
    indices = pick_frames(len(anim.frames), limit)
    outputs = []
    for order, index in enumerate(indices, 1):
        target = out_dir / f"frame_{order:03d}_{index + 1:03d}.png"
        fr.save_static(anim.frames[index], target, "PNG")
        outputs.append(target)
    return outputs, len(anim.frames)


def grid_split(path: Path, out_dir: Path, rows: int, cols: int, margins: Margins, limits: fr.ImageLimits) -> list[Path]:
    """网格切图；动图的每一格仍是动图。"""
    anim = fr.load(path, limits)
    trimmed = [trim_margins(f, margins) for f in anim.frames]
    cells = grid_cells(trimmed[0].size, rows, cols)
    fmt = fr.output_format_for(anim)
    outputs = []
    for index, box in enumerate(cells, 1):
        piece = fr.Animation([f.crop(box) for f in trimmed], list(anim.durations), anim.loop, anim.format, anim.source_frames)
        target = out_dir / f"cell_{index:02d}{fr.FORMAT_TO_EXT[fmt]}"
        fr.save_animation(piece, target, fmt, quality=95)
        outputs.append(target)
    return outputs


def save_made(anim: fr.Animation, out_base: Path, fmt: str) -> Path:
    """保存新合成的动图。fmt：gif / webp / apng。"""
    if fmt == "webp":
        return fr.save_animation(anim, out_base.with_suffix(".webp"), "WEBP", quality=90)
    if fmt == "apng":
        return fr.save_animation(anim, out_base.with_suffix(".png"), "PNG")
    return fr.save_animation(anim, out_base.with_suffix(".gif"), "GIF")
