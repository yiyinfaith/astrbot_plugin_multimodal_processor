"""参数解析与幻影坦克算法。"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from mmp.core import args as A
from mmp.core.errors import UsageError
from mmp.processors.mirage.algorithm import generate, preview


@pytest.mark.parametrize(
    "text, seconds",
    [("90", 90), ("1.5", 1.5), ("1:30", 90), ("01:02:03.5", 3723.5), ("1分30秒", 90), ("2m5s", 125), ("１：０５", 65), ("3秒", 3)],
)
def test_parse_time(text, seconds):
    assert A.parse_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "-5", "1:xx"])
def test_parse_time_rejects(bad):
    with pytest.raises(UsageError):
        A.parse_time(bad)


def test_parse_range_requires_order():
    assert A.parse_range("5", "1:00") == (5, 60)
    with pytest.raises(UsageError):
        A.parse_range("10", "5")


def test_parse_size_variants():
    assert A.parse_size("800x600").resolve(100, 100) == (800, 600)
    assert A.parse_size("800×600").resolve(100, 100) == (800, 600)
    assert A.parse_size("400").resolve(800, 600) == (400, 300)
    assert A.parse_size("x300").resolve(800, 600) == (400, 300)
    assert A.parse_size("50%").resolve(800, 600) == (400, 300)
    assert A.parse_size("720p").resolve(1920, 1080) == (1280, 720)
    assert A.parse_size("720p").resolve(1080, 1920) == (720, 1280)  # 竖屏按短边
    for bad in ("abc", "0x10", "99999x1", "10%%"):
        with pytest.raises(UsageError):
            A.parse_size(bad)


def test_misc_parsers():
    assert A.parse_angle("-90") == 270 and A.parse_angle("90°") == 90 and A.parse_angle("180") == 180
    with pytest.raises(UsageError):
        A.parse_angle("45")
    assert A.parse_bitrate("64k") == 64 and A.parse_bitrate("128kbps") == 128
    with pytest.raises(UsageError):
        A.parse_bitrate("5000k")
    assert A.parse_volume("+6dB") == ("db", 6) and A.parse_volume("150%") == ("factor", 1.5) and A.parse_volume("0.5") == ("factor", 0.5)
    assert A.parse_format("JPEG", ("png", "jpg")) == "jpg"
    with pytest.raises(UsageError):
        A.parse_format("exe", ("png", "jpg"))
    assert A.parse_crop("1:1") == ("ratio", (1, 1)) and A.parse_crop("1,2,3,4") == ("box", (1, 2, 3, 4))
    with pytest.raises(UsageError):
        A.parse_crop("1,2,3")
    assert A.parse_float("2x", "倍率", 0.3, 5) == 2.0
    with pytest.raises(UsageError):
        A.parse_float("nan", "倍率", 0.3, 5)


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32)


def _gradient(size, horizontal: bool) -> Image.Image:
    w, h = size
    arr = np.tile(np.linspace(0, 255, w if horizontal else h, dtype=np.float32), (h, 1) if horizontal else (w, 1))
    if not horizontal:
        arr = arr.T
    return Image.fromarray(arr.astype(np.uint8), "L").convert("RGB")


def test_gray_mirage_shows_front_on_white_and_back_on_black(tmp_path):
    front, back = tmp_path / "f.png", tmp_path / "b.png"
    _gradient((64, 48), True).save(front)
    _gradient((64, 48), False).save(back)
    out = generate(front, back, tmp_path / "m.png", mode="gray")
    with Image.open(out) as image:
        assert image.mode == "RGBA" and image.format == "PNG" and image.size == (64, 48)
    on_white = _gray(preview(out, (255, 255, 255)))
    on_black = _gray(preview(out, (0, 0, 0)))
    expected_white = 255 - (255 - _gray(_gradient((64, 48), True))) * 0.5
    expected_black = _gray(_gradient((64, 48), False)) * 0.5
    assert np.abs(on_white - expected_white).mean() < 2.5
    assert np.abs(on_black - expected_black).mean() < 2.5
    # 白底看到的应与表图相关、与里图无关
    assert np.corrcoef(on_white.ravel(), _gray(_gradient((64, 48), True)).ravel())[0, 1] > 0.99
    assert np.corrcoef(on_black.ravel(), _gray(_gradient((64, 48), False)).ravel())[0, 1] > 0.99


def test_color_mirage_rgba_and_fit_without_stretch(tmp_path):
    front, back = tmp_path / "f.png", tmp_path / "b.png"
    Image.new("RGB", (300, 200), (250, 240, 230)).save(front)
    Image.new("RGB", (50, 400), (20, 30, 40)).save(back)  # 比例完全不同
    out = generate(front, back, tmp_path / "m.png", mode="color", max_side=150)
    with Image.open(out) as image:
        assert image.mode == "RGBA" and image.size == (150, 100)
        alpha = np.asarray(image.getchannel("A"))
    assert alpha.min() >= 1 and alpha.max() <= 255


def test_mirage_accepts_transparent_and_animated_inputs(tmp_path):
    front, back = tmp_path / "f.png", tmp_path / "b.gif"
    Image.new("RGBA", (40, 40), (255, 0, 0, 0)).save(front)
    frames = [Image.new("RGB", (40, 40), c) for c in ((0, 0, 0), (255, 255, 255))]
    frames[0].save(back, save_all=True, append_images=frames[1:])
    out = generate(front, back, tmp_path / "m.png")
    with Image.open(out) as image:
        assert image.mode == "RGBA"


def test_mirage_rejects_oversized_image_before_decoding(tmp_path, make_app):
    from PIL import Image as PILImage

    from conftest import FakeEvent, image, plain, run

    app = make_app({"image": {"basic": {"max_megapixels": 1}}})
    path = tmp_path / "big.png"
    PILImage.new("RGB", (2000, 2000), "white").save(path)
    event = FakeEvent([image(path), image(path), plain("/幻影坦克")], message_str="幻影坦克")
    run(app.handle_command(event, "幻影坦克"))
    assert "分辨率" in app.sender.texts()[0]
