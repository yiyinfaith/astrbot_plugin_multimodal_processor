"""逐帧滤镜：图片转线稿、表情包做旧。动图逐帧处理并保留时长与循环。

功能设计参考 astrbot_plugin_gifcaijian（shskjw，AGPL-3.0）：
- 线稿：原插件用 FIND_EDGES 边缘检测，线条断续且噪点多；这里改为经典的
  「灰度 → 反相 → 高斯模糊 → 颜色减淡」素描算法，线条连续、明暗自然。
- 做旧：沿用「偏绿 + 反复低质量 JPEG 压缩 + 轻微模糊/锐化 + 降饱和与对比」的思路，
  随机数使用固定种子，同一张图同样的次数结果一致；动图每帧使用相同的偏色，避免闪烁。
"""

from __future__ import annotations

import io
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from ...core.errors import UsageError
from ..gif import frames as fr
from .ops import ImageResult, _map_frames, _save_like_source


def _sketch(frame: Image.Image, blur: float) -> Image.Image:
    rgba = frame.convert("RGBA")
    gray = ImageOps.grayscale(flatten_keep(rgba))
    inverted = ImageOps.invert(gray).filter(ImageFilter.GaussianBlur(blur))
    base = np.asarray(gray, dtype=np.float32)
    blurred = np.asarray(inverted, dtype=np.float32)
    dodge = np.clip(base * 255.0 / np.maximum(255.0 - blurred, 1.0), 0, 255)
    # 稍微加深线条，避免浅色图片的线稿过淡
    dodge = 255.0 - np.clip((255.0 - dodge) * 1.6, 0, 255)
    sketch = Image.fromarray(dodge.astype(np.uint8), "L").convert("RGBA")
    sketch.putalpha(rgba.getchannel("A"))
    return sketch


def flatten_keep(rgba: Image.Image) -> Image.Image:
    canvas = Image.new("RGB", rgba.size, (255, 255, 255))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def line_art(path: Path, out_base: Path, limits: fr.ImageLimits) -> ImageResult:
    anim = fr.load(path, limits)
    blur = max(2.0, min(anim.size) / 120)
    result = _map_frames(anim, lambda f: _sketch(f, blur))
    return ImageResult(_save_like_source(result, out_base))


def _age_frame(frame: Image.Image, times: int, seed: int) -> Image.Image:
    rng = random.Random(seed)
    alpha = frame.convert("RGBA").getchannel("A")
    img = flatten_keep(frame.convert("RGBA"))
    for i in range(times):
        if i % 3 == 0:  # 渐进偏绿
            r, g, b = img.split()
            boost, red, blue = rng.randint(1, 2), rng.randint(0, 1), rng.randint(0, 1)
            g = g.point(lambda x, v=boost: min(255, x + v))
            r = r.point(lambda x, v=red: max(0, x - v))
            b = b.point(lambda x, v=blue: max(0, x - v))
            img = Image.merge("RGB", (r, g, b))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=max(25, 70 - i * 3))
        buffer.seek(0)
        with Image.open(buffer) as reloaded:
            img = reloaded.convert("RGB")
        if i % 3 == 0:
            img = img.filter(ImageFilter.GaussianBlur(0.2 + (i // 3) * 0.1))
        if i % 5 == 2:
            img = img.filter(ImageFilter.SHARPEN)
        if i % 2 == 0:
            img = ImageEnhance.Color(img).enhance(0.985)
        else:
            img = ImageEnhance.Contrast(img).enhance(0.99)
        if times >= 15 and i == times // 2 and min(img.size) > 50:
            w, h = img.size
            img = img.resize((int(w * 0.8), int(h * 0.8)), Image.Resampling.BILINEAR).resize((w, h), Image.Resampling.BILINEAR)
    result = img.convert("RGBA")
    result.putalpha(alpha)
    return result


def age_level(times: int) -> str:
    if times <= 5:
        return "轻度做旧（微微泛绿）"
    if times <= 10:
        return "中度做旧（明显发绿变糊）"
    if times <= 20:
        return "重度做旧（经典老图风格）"
    return "极限做旧（赛博遗产级别）"


def age_meme(path: Path, out_base: Path, times: int, limits: fr.ImageLimits) -> ImageResult:
    if not 1 <= times <= 50:
        raise UsageError("做旧次数范围是 1～50")
    anim = fr.load(path, limits)
    seed = times * 7919 + anim.size[0] * 31 + anim.size[1]
    result = _map_frames(anim, lambda f: _age_frame(f, times, seed))
    if not result.animated:
        target = out_base.with_suffix(".jpg") if not fr.has_alpha(result.frames[0]) else out_base.with_suffix(".png")
        fmt = "JPEG" if target.suffix == ".jpg" else "PNG"
        fr.save_static(result.frames[0], target, fmt, quality=max(30, 70 - times * 3))
        return ImageResult(target, f"做旧 {times} 次：{age_level(times)}")
    return ImageResult(_save_like_source(result, out_base), f"做旧 {times} 次（{len(result.frames)} 帧）：{age_level(times)}")
