from __future__ import annotations

import asyncio
from pathlib import Path
import numpy as np
from PIL import Image


def _fit(front: Image.Image, back: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    front = front.convert("RGB")
    back = back.convert("RGB").resize(front.size, Image.Resampling.LANCZOS)
    return np.asarray(front, dtype=np.float32), np.asarray(back, dtype=np.float32)


def _gray(front: np.ndarray, back: np.ndarray) -> np.ndarray:
    a = 0.299 * front[:,:,0] + 0.587 * front[:,:,1] + 0.114 * front[:,:,2]
    b = 0.299 * back[:,:,0] + 0.587 * back[:,:,1] + 0.114 * back[:,:,2]
    alpha = np.clip(255.0 - a + b, 1, 255)
    pixel = np.clip((b * 255.0 / alpha), 0, 255)
    rgb = np.repeat(pixel[:, :, None], 3, axis=2)
    return np.concatenate([rgb, alpha[:, :, None]], axis=2).astype(np.uint8)


def _color(front: np.ndarray, back: np.ndarray, a: float, b: float, w: float) -> np.ndarray:
    front_gray = .299 * front[:,:,0] + .587 * front[:,:,1] + .114 * front[:,:,2]
    back_gray = .299 * back[:,:,0] + .587 * back[:,:,1] + .114 * back[:,:,2]
    back_gray = a * back_gray + b
    alpha = np.clip(255.0 - front_gray + back_gray, 1, 255)[:, :, None]
    base = (1.0 - w) * front + w * back
    rgb = np.clip((base - (255.0 - alpha)) / (alpha / 255.0), 0, 255)
    return np.concatenate([rgb, alpha], axis=2).astype(np.uint8)


def generate_mirage_sync(front_path: str | Path, back_path: str | Path, output: str | Path,
                         mode: str = "gray", a: float = .5, b: float = 20, w: float = .1) -> Path:
    with Image.open(front_path) as f, Image.open(back_path) as back:
        front_arr, back_arr = _fit(f, back)
    rgba = _gray(front_arr, back_arr) if mode == "gray" else _color(front_arr, back_arr, a, b, w)
    Image.fromarray(rgba, "RGBA").save(output, format="PNG")
    return Path(output)


async def generate_mirage(front_path: str | Path, back_path: str | Path, output: str | Path,
                          mode: str = "gray", a: float = .5, b: float = 20, w: float = .1) -> Path:
    return await asyncio.to_thread(generate_mirage_sync, front_path, back_path, output, mode, a, b, w)
