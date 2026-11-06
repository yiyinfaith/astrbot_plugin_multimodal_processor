"""光棱坦克：基于「灰度值分离 + 像素交错」的伪隐写图。

制作：里图色阶压缩到暗区 [0, inner_limit]，表图压缩到亮区 [cover_limit, 255]；
满足 (x + y) % rate == 0 的像素放里图，其余放表图。正常查看是略暗的表图，
用图像软件把暗部色阶拉开（或使用 /光棱显形）即可看到里图。

显形：把暗区（各通道最大值 ≤ 阈值）的像素拉伸回 0~255，其余位置视为空缺，
再用归一化盒式滤波按邻近像素补齐。阈值优先读取本插件写入的 PNG 文本块，
其次从亮度直方图中寻找里图与表图之间的空档，最后使用默认值。

原理参考 TankFactory/Mirage_Image 与百度贴吧「开个贴细说新式高科技坦克」，此处为独立实现。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps, PngImagePlugin

from ...core.errors import MediaError, UsageError

META_KEY = "multimodal_prism"


def _open_rgb(path: str | Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            image.seek(0)
            rgba = image.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise MediaError(f"图片无法读取：{exc}") from exc
    canvas = Image.new("RGB", rgba.size, (255, 255, 255))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def generate(
    cover_path: str | Path,
    inner_path: str | Path,
    output: str | Path,
    *,
    colored: bool = True,
    max_side: int = 1200,
    inner_limit: int = 32,
    cover_limit: int = 64,
    rate: int = 2,
) -> Path:
    if not (0 < inner_limit < cover_limit < 255):
        raise UsageError("光棱坦克参数错误：需要 0 < 里图色阶端点 < 表图色阶端点 < 255")
    if rate < 2:
        raise UsageError("光棱坦克参数错误：隐藏率至少为 2")
    inner = _open_rgb(inner_path)
    inner.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    cover = ImageOps.fit(_open_rgb(cover_path), inner.size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    if not colored:
        cover = cover.convert("L").convert("RGB")
    inner_arr = np.asarray(inner, dtype=np.float32)
    cover_arr = np.asarray(cover, dtype=np.float32)
    inner_px = inner_arr * (inner_limit / 255.0)
    cover_px = cover_arr * ((255.0 - cover_limit) / 255.0) + cover_limit
    h, w = inner_arr.shape[:2]
    yy, xx = np.indices((h, w))
    mask = ((xx + yy) % rate == 0)[..., None]
    out = np.where(mask, inner_px, cover_px)
    result = Image.fromarray(np.clip(np.round(out), 0, 255).astype(np.uint8), "RGB")
    info = PngImagePlugin.PngInfo()
    info.add_text(META_KEY, f"inner_limit={inner_limit};cover_limit={cover_limit};rate={rate}")
    result.save(output, format="PNG", pnginfo=info, compress_level=6)
    return Path(output)


def _meta_threshold(image: Image.Image) -> int | None:
    text = (getattr(image, "text", None) or image.info or {}).get(META_KEY)
    if not text:
        return None
    for part in str(text).split(";"):
        key, _, value = part.partition("=")
        if key.strip() == "inner_limit" and value.strip().isdigit():
            return int(value)
    return None


def detect_threshold(values: np.ndarray) -> int | None:
    """在 1~200 之间寻找最长的「几乎没有像素」的亮度空档，返回空档起点前的值作为阈值。"""
    hist = np.bincount(values.ravel(), minlength=256)
    noise = max(1, int(values.size * 0.0005))
    best_start, best_len, start = None, 0, None
    for v in range(1, 201):
        if hist[v] <= noise:
            if start is None:
                start = v
            length = v - start + 1
            if length > best_len:
                best_start, best_len = start, length
        else:
            start = None
    if best_start is None or best_len < 8:
        return None
    dark = hist[:best_start].sum()
    if dark < values.size * 0.05:  # 暗区像素太少，不像光棱坦克
        return None
    return best_start - 1


def reveal(path: str | Path, output: str | Path, *, threshold: int | None = None, default_threshold: int = 32) -> tuple[Path, int, str]:
    """返回 (输出路径, 使用的阈值, 阈值来源)。"""
    try:
        with Image.open(path) as image:
            meta = _meta_threshold(image)
            image.seek(0)
            rgba = image.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise MediaError(f"图片无法读取：{exc}") from exc
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.uint8)
    brightness = rgb.max(axis=2)
    if threshold is not None:
        source = "手动指定"
    elif meta is not None:
        threshold, source = meta, "图片内记录的参数"
    else:
        detected = detect_threshold(brightness)
        threshold, source = (detected, "自动检测") if detected is not None else (default_threshold, "默认值")
    threshold = max(1, min(254, int(threshold)))
    mask = (brightness <= threshold).astype(np.float32)
    if mask.mean() < 0.01:
        raise UsageError("这张图里几乎没有暗区像素，可能不是光棱坦克，或者被压缩过。可以尝试手动指定阈值，例如 /光棱显形 48")
    stretched = np.clip(rgb.astype(np.float32) * (255.0 / threshold), 0, 255) * mask[..., None]
    # 归一化盒式滤波：用邻近的里图像素填补交错留下的空隙
    radius = 1 if mask.mean() > 0.3 else 2
    weights = Image.fromarray((mask * 255).astype(np.uint8), "L").filter(ImageFilter.BoxBlur(radius))
    weight_arr = np.asarray(weights, dtype=np.float32)[..., None] / 255.0
    channels = []
    for c in range(3):
        layer = Image.fromarray(np.round(stretched[..., c]).astype(np.uint8), "L").filter(ImageFilter.BoxBlur(radius))
        channels.append(np.asarray(layer, dtype=np.float32))
    blurred = np.stack(channels, axis=2)
    filled = np.where(weight_arr > 0.02, blurred / np.maximum(weight_arr, 1e-3), 0)
    out = np.where(mask[..., None] > 0, stretched, filled)
    Image.fromarray(np.clip(np.round(out), 0, 255).astype(np.uint8), "RGB").save(output, format="PNG")
    return Path(output), threshold, source
