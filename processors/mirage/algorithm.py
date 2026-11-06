"""幻影坦克：生成一张 RGBA PNG，白色背景下显示表图，黑色背景下显示里图。

算法来自 astrbot_plugin_mirage_tank（MIT，© 2025 poisama），在此基础上：
- 里图按「覆盖 + 居中裁剪」对齐表图尺寸，不再拉伸变形；
- 输出尺寸受 max_side 限制，避免超大图片占满内存；
- 黑白模式的明暗压缩比例可配置（默认值与原插件一致）。

原理：设表图灰度 A、里图灰度 B，输出像素 P 与透明度 α 满足
  白底显示：P·α + 255·(1-α) = A'     黑底显示：P·α = B'
其中 A' 被压到亮区 [255·(1-fr), 255]，B' 被压到暗区 [0, 255·br]，保证 α 在 0~1 之间。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from ...core.errors import MediaError

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


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


def _prepare(front_path: str | Path, back_path: str | Path, max_side: int) -> tuple[np.ndarray, np.ndarray]:
    front = _open_rgb(front_path)
    back = _open_rgb(back_path)
    if max(front.size) > max_side:
        scale = max_side / max(front.size)
        front = front.resize((max(1, round(front.width * scale)), max(1, round(front.height * scale))), Image.Resampling.LANCZOS)
    back = ImageOps.fit(back, front.size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    return np.asarray(front, dtype=np.float32), np.asarray(back, dtype=np.float32)


def gray_tank(front: np.ndarray, back: np.ndarray, front_ratio: float = 0.5, back_ratio: float = 0.5) -> np.ndarray:
    a = front @ _LUMA
    b = back @ _LUMA
    shown_white = 255.0 - (255.0 - a) * front_ratio  # 表图压到亮区
    shown_black = b * back_ratio  # 里图压到暗区
    alpha = np.clip(255.0 - shown_white + shown_black, 1.0, 255.0)
    pixel = np.clip(shown_black * 255.0 / alpha, 0.0, 255.0)
    rgba = np.empty(a.shape + (4,), dtype=np.uint8)
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = np.round(pixel).astype(np.uint8)
    rgba[..., 3] = np.round(alpha).astype(np.uint8)
    return rgba


def color_tank(front: np.ndarray, back: np.ndarray, a: float = 0.5, b: float = 20.0, w: float = 0.1) -> np.ndarray:
    """彩色模式（与原插件公式一致）：a 为里图亮度缩放，b 为亮度偏移，w 为里图颜色权重。"""
    front_gray = front @ _LUMA
    back_gray = a * (back @ _LUMA) + b
    alpha = np.clip(255.0 - front_gray + back_gray, 1.0, 255.0)[..., None]
    base = (1.0 - w) * front + w * back
    pixel = np.clip((base - (255.0 - alpha)) / (alpha / 255.0), 0.0, 255.0)
    return np.concatenate([np.round(pixel), np.round(alpha)], axis=2).astype(np.uint8)


def generate(
    front_path: str | Path,
    back_path: str | Path,
    output: str | Path,
    *,
    mode: str = "gray",
    max_side: int = 2048,
    gray_front_ratio: float = 0.5,
    gray_back_ratio: float = 0.5,
    a: float = 0.5,
    b: float = 20.0,
    w: float = 0.1,
) -> Path:
    front, back = _prepare(front_path, back_path, max_side)
    if mode == "color":
        rgba = color_tank(front, back, a, b, w)
    else:
        rgba = gray_tank(front, back, gray_front_ratio, gray_back_ratio)
    Image.fromarray(rgba, "RGBA").save(output, format="PNG", compress_level=6)
    return Path(output)


def preview(path: str | Path, background: tuple[int, int, int]) -> Image.Image:
    """把幻影坦克图叠在指定背景色上，返回实际看到的画面（测试与调试用）。"""
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, background)
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas
