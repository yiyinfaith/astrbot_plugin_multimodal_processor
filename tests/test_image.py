"""图片 / GIF 处理：动画帧、时长、循环、透明度、奇数尺寸、资源限制、调速。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from mmp.core.args import SizeSpec
from mmp.core.errors import MediaTooLarge, UsageError
from mmp.processors.gif import frames as fr
from mmp.processors.gif.speed import plan_speed
from mmp.processors.image import ops

LIMITS = fr.ImageLimits()
COLORS = [(255, 0, 0), (0, 200, 0), (0, 0, 255), (240, 200, 0)]


def make_gif(path: Path, *, size=(31, 21), durations=(40, 80, 120, 60), loop: int | None = 0, transparent=True) -> Path:
    frames = []
    for index, color in enumerate(COLORS[: len(durations)]):
        frame = Image.new("RGBA", size, (*color, 255))
        if transparent:
            # 左上角和中心各有一块透明区域，位置随帧变化，确保每帧不同；
            # 中心那块在任何半边对称后都会保留
            w, h = size
            for x0, y0 in ((index, 0), (w // 2 - 2 + index % 2, h // 2 - 2)):
                for x in range(x0, x0 + 5):
                    for y in range(y0, y0 + 5):
                        frame.putpixel((x, y), (0, 0, 0, 0))
        frames.append(frame)
    params = {"save_all": True, "append_images": frames[1:], "duration": list(durations), "disposal": 2}
    if loop is not None:
        params["loop"] = loop
    if transparent:
        # 交给本插件的写入器生成真正带透明索引的 GIF
        anim = fr.Animation(frames, list(durations), loop, "GIF")
        fr.save_animation(anim, path, "GIF")
    else:
        frames[0].convert("RGB").save(path, format="GIF", **params)
    return path


def read(path: Path) -> fr.Animation:
    return fr.load(path, LIMITS)


def test_gif_reader_writer_roundtrip_keeps_everything(tmp_path):
    src = make_gif(tmp_path / "a.gif", loop=3)
    anim = read(src)
    assert anim.format == "GIF" and len(anim.frames) == 4
    assert anim.durations == [40, 80, 120, 60]
    assert anim.loop == 3
    assert anim.frames[0].getpixel((0, 0))[3] == 0
    assert anim.frames[0].getpixel((20, 15)) == (255, 0, 0, 255)


@pytest.mark.parametrize("name", list(ops.TRANSFORMS))
def test_every_transform_keeps_animation_timing_loop_and_alpha(tmp_path, name):
    src = make_gif(tmp_path / "in.gif", loop=0)
    result = ops.transform(src, tmp_path / "out", ops.TRANSFORMS[name], LIMITS)
    assert result.path.suffix == ".gif"
    anim = read(result.path)
    assert len(anim.frames) == 4
    assert anim.durations == [40, 80, 120, 60]
    assert anim.loop == 0
    assert any(f.getchannel("A").getextrema()[0] == 0 for f in anim.frames), "透明度丢失"
    expected_size = (21, 31) if name in {"顺时针", "逆时针"} else (31, 21)
    assert anim.size == expected_size


def test_gif_without_loop_extension_stays_play_once(tmp_path):
    src = make_gif(tmp_path / "once.gif", loop=None, transparent=False)
    with Image.open(src) as image:
        assert "loop" not in image.info
    result = ops.transform(src, tmp_path / "out", "hflip", LIMITS)
    with Image.open(result.path) as image:
        assert "loop" not in image.info


def test_invert_keeps_alpha_and_inverts_color(tmp_path):
    src = tmp_path / "a.png"
    img = Image.new("RGBA", (4, 4), (10, 20, 30, 128))
    img.save(src)
    out = ops.transform(src, tmp_path / "o", "invert", LIMITS).path
    with Image.open(out) as result:
        assert result.format == "PNG"
        assert result.convert("RGBA").getpixel((1, 1)) == (245, 235, 225, 128)


def test_symmetry_odd_width_mirrors_exactly(tmp_path):
    src = tmp_path / "row.png"
    img = Image.new("RGB", (5, 3))
    for x in range(5):
        for y in range(3):
            img.putpixel((x, y), (x * 50, y * 80, 0))
    img.save(src)

    def column_values(path):
        with Image.open(path) as im:
            im = im.convert("RGB")
            return [im.getpixel((x, 0))[0] for x in range(im.width)]

    left = column_values(ops.transform(src, tmp_path / "l", "left", LIMITS).path)
    right = column_values(ops.transform(src, tmp_path / "r", "right", LIMITS).path)
    assert left == [0, 50, 100, 50, 0]
    assert right == [200, 150, 100, 150, 200]

    def row_values(path):
        with Image.open(path) as im:
            im = im.convert("RGB")
            return [im.getpixel((0, y))[1] for y in range(im.height)]

    assert row_values(ops.transform(src, tmp_path / "t", "top", LIMITS).path) == [0, 80, 0]
    assert row_values(ops.transform(src, tmp_path / "b", "bottom", LIMITS).path) == [160, 80, 160]


def test_static_jpeg_keeps_format(tmp_path):
    src = tmp_path / "a.jpg"
    Image.new("RGB", (16, 8), (200, 10, 10)).save(src, quality=95)
    out = ops.transform(src, tmp_path / "o", "cw", LIMITS).path
    with Image.open(out) as result:
        assert result.format == "JPEG" and result.size == (8, 16)


def test_convert_animated_explicitly_handles_static_targets(tmp_path):
    src = make_gif(tmp_path / "a.gif")
    webp = ops.convert(src, tmp_path / "w", "webp", LIMITS)
    with Image.open(webp.path) as image:
        assert image.format == "WEBP" and image.n_frames == 4
    apng = ops.convert(src, tmp_path / "p", "png", LIMITS)
    with Image.open(apng.path) as image:
        assert image.n_frames == 4
    assert "APNG" in apng.note
    jpg = ops.convert(src, tmp_path / "j", "jpg", LIMITS)
    with Image.open(jpg.path) as image:
        assert image.format == "JPEG" and getattr(image, "n_frames", 1) == 1
    assert "第一帧" in jpg.note and "透明" in jpg.note
    static = ops.convert(src, tmp_path / "s", "gif", LIMITS, static=True)
    with Image.open(static.path) as image:
        assert getattr(image, "n_frames", 1) == 1
    assert "第一帧" in static.note


def test_convert_back_from_webp_to_gif_keeps_frames(tmp_path):
    src = make_gif(tmp_path / "a.gif")
    webp = ops.convert(src, tmp_path / "w", "webp", LIMITS).path
    gif = ops.convert(webp, tmp_path / "g", "gif", LIMITS).path
    anim = read(gif)
    assert len(anim.frames) == 4 and anim.durations == [40, 80, 120, 60]


def test_compress_resize_crop(tmp_path):
    src = tmp_path / "noise.png"
    import random

    random.seed(1)
    img = Image.new("RGB", (300, 200))
    img.putdata([(random.randrange(256), random.randrange(256), random.randrange(256)) for _ in range(300 * 200)])
    img.save(src)
    small = ops.compress(src, tmp_path / "c", 60, LIMITS)
    assert small.path.stat().st_size < src.stat().st_size and "→" in small.note

    resized = ops.resize(src, tmp_path / "r", SizeSpec(width=150), LIMITS)
    with Image.open(resized.path) as image:
        assert image.size == (150, 100)
    percent = ops.resize(src, tmp_path / "p", SizeSpec(percent=50), LIMITS)
    with Image.open(percent.path) as image:
        assert image.size == (150, 100)

    square = ops.crop(src, tmp_path / "sq", "ratio", (1, 1), LIMITS)
    with Image.open(square.path) as image:
        assert image.size == (200, 200)
    boxed = ops.crop(src, tmp_path / "bx", "box", (250, 150, 100, 100), LIMITS)
    with Image.open(boxed.path) as image:
        assert image.size == (50, 50)  # 超出部分截到边界
    with pytest.raises(UsageError):
        ops.crop(src, tmp_path / "bad", "box", (400, 400, 10, 10), LIMITS)


def test_compress_animated_gif_keeps_frames(tmp_path):
    src = make_gif(tmp_path / "a.gif", size=(120, 90))
    result = ops.compress(src, tmp_path / "c", 40, LIMITS, max_side=60)
    anim = read(result.path)
    assert len(anim.frames) == 4 and max(anim.size) == 60


def test_limits_reject_huge_inputs(tmp_path):
    src = make_gif(tmp_path / "a.gif", size=(50, 50))
    with pytest.raises(MediaTooLarge):
        fr.load(src, fr.ImageLimits(max_frames=2))
    with pytest.raises(MediaTooLarge):
        fr.load(src, fr.ImageLimits(max_pixels=100))
    with pytest.raises(MediaTooLarge):
        fr.load(src, fr.ImageLimits(memory_bytes=50 * 50 * 4 * 3))
    with pytest.raises(UsageError):
        ops.resize(src, tmp_path / "r", SizeSpec(width=20000, height=20000), LIMITS)


def test_info_reports_animation(tmp_path):
    src = make_gif(tmp_path / "a.gif", loop=0)
    data = ops.info(src, LIMITS)
    assert data["format"] == "GIF" and data["frames"] == 4 and data["duration"] == 300 and data["loop"] == 0


def test_speed_plans():
    # 普通倍率：全部帧保留，时长按倍率缩短并对齐到 10ms
    plan = plan_speed([100, 100, 100], 2.0)
    assert plan.durations == [50, 50, 50] and plan.actual == 2.0 and plan.note is None
    # 超出 50 FPS：按最短帧回退
    plan = plan_speed([40, 100, 100], 4.0)
    assert plan.actual == 2.0 and plan.durations[0] == 20 and "×2" in plan.note
    # 已经 50 FPS 的动图不能再加速
    plan = plan_speed([20, 20], 2.0)
    assert plan.actual == 1.0 and "保持原速" in plan.note
    # 丢帧：总时长准确，每帧不少于 20ms
    plan = plan_speed([20] * 30, 3.0, allow_drop=True)
    assert all(d >= 20 for d in plan.durations)
    assert abs(sum(plan.durations) - 600 / 3) <= 20
    assert len(plan.indices) < 30 and plan.indices[0] == 0
    # 0ms 帧按浏览器的 100ms 理解
    assert plan_speed([0, 0], 2.0).durations == [50, 50]
    # 减速
    assert plan_speed([40, 60], 0.5).durations == [80, 120]


def test_speed_on_gif_and_static_error(tmp_path):
    src = make_gif(tmp_path / "a.gif")
    result = ops.speed(src, tmp_path / "s", 2.0, LIMITS, 50, False)
    anim = read(result.path)
    assert anim.durations == [20, 40, 60, 30] and len(anim.frames) == 4
    static = tmp_path / "s.png"
    Image.new("RGB", (4, 4)).save(static)
    with pytest.raises(UsageError):
        ops.speed(static, tmp_path / "x", 2.0, LIMITS, 50, False)


def test_animated_webp_transform_stays_webp(tmp_path):
    frames = [Image.new("RGBA", (20, 20), (*c, 200)) for c in COLORS]
    src = tmp_path / "a.webp"
    frames[0].save(src, format="WEBP", save_all=True, append_images=frames[1:], duration=[50, 60, 70, 80], loop=0, lossless=True)
    out = ops.transform(src, tmp_path / "o", "vflip", LIMITS).path
    with Image.open(out) as image:
        assert image.format == "WEBP" and image.n_frames == 4
    anim = read(out)
    assert anim.durations == [50, 60, 70, 80]
    assert anim.frames[0].getpixel((5, 5))[3] < 255
