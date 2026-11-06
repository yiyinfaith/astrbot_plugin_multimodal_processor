"""整合自 gifcaijian 的动图工具，以及光棱坦克。"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from conftest import FakeEvent, comp, image, plain, run

from mmp.core import args as A
from mmp.core.errors import UsageError
from mmp.handlers.av import parse_gif_options
from mmp.processors.gif import compose
from mmp.processors.gif import frames as fr
from mmp.processors.image import effects
from mmp.processors.prism import algorithm as prism

LIMITS = fr.ImageLimits()
COLORS = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255), (0, 255, 255, 255), (255, 0, 255, 255)]


def sprite_sheet(path, rows=2, cols=3, cell=(10, 8), empty_last=False):
    sheet = Image.new("RGBA", (cols * cell[0], rows * cell[1]), (0, 0, 0, 0))
    for index in range(rows * cols):
        if empty_last and index == rows * cols - 1:
            continue
        r, c = divmod(index, cols)
        sheet.paste(Image.new("RGBA", cell, COLORS[index % len(COLORS)]), (c * cell[0], r * cell[1]))
    sheet.save(path)
    return path


# ---------------------------------------------------------------- 参数
def test_grid_margin_and_duration_parsers():
    assert A.parse_grid("3x4") == (3, 4) and A.parse_grid("3×4") == (3, 4) and A.parse_grid("2*5") == (2, 5)
    assert A.parse_grid("abc") is None
    with pytest.raises(UsageError):
        A.parse_grid("11x2")
    rest, margins = A.take_margins(["4x4", "边距10", "边距上", "20", "0.1s", "边距左5"])
    assert rest == ["4x4", "0.1s"]
    assert margins == {"top": 30, "bottom": 10, "left": 15, "right": 10}
    with pytest.raises(UsageError):
        A.take_margins(["边距中10"])
    assert A.parse_frame_duration("0.1") == 100
    assert A.parse_frame_duration("0.05s") == 50
    assert A.parse_frame_duration("80ms") == 80
    assert A.parse_frame_duration("10fps") == 100
    with pytest.raises(UsageError):
        A.parse_frame_duration("abc")


def test_video_to_gif_options_both_styles():
    o = parse_gif_options(("15", "360", "5", "1:05"))
    assert (o.fps, o.width, o.duration, o.start) == (15, 360, 5, 65)
    o = parse_gif_options(("2s-4.5s", "fps", "15", "0.5"))
    assert (o.start, o.duration, o.fps, o.scale) == (2, 2.5, 15, 0.5)
    o = parse_gif_options(("开始", "2s", "时长3", "1/3", "50%"))
    assert (o.start, o.duration, o.every, o.scale) == (2, 3, 3, 0.5)
    o = parse_gif_options(("12fps", "1:00-1:04"))
    assert (o.fps, o.start, o.duration) == (12, 60, 4)
    with pytest.raises(UsageError):
        parse_gif_options(("5-2",))
    with pytest.raises(UsageError):
        parse_gif_options(("fps10", "12"))  # 帧率重复
    with pytest.raises(UsageError):
        parse_gif_options(("1", "2", "3", "4", "5"))


# ---------------------------------------------------------------- 合成与拆分
def test_sprite_to_gif_with_margins_and_empty_tail(tmp_path):
    src = tmp_path / "sheet.png"
    sheet = sprite_sheet(tmp_path / "raw.png", empty_last=True)
    with Image.open(sheet) as raw:
        padded = Image.new("RGBA", (raw.width + 7, raw.height + 4), (255, 255, 255, 255))
        padded.paste(raw, (5, 4))
        padded.save(src)
    margins = compose.Margins(top=4, left=5, right=2)
    anim = compose.sprite_to_animation(src, 2, 3, 80, margins, LIMITS)
    assert len(anim.frames) == 5, "末尾空格子应被去掉"
    assert anim.size == (10, 8) and anim.durations == [80] * 5
    assert anim.frames[1].getpixel((5, 4)) == COLORS[1]
    out = compose.save_made(anim, tmp_path / "out", "gif")
    assert fr.load(out, LIMITS).durations == [80] * 5
    with pytest.raises(UsageError):
        compose.sprite_to_animation(src, 2, 3, 80, compose.Margins(left=100), LIMITS)


def test_grid_cells_cover_whole_image():
    cells = compose.grid_cells((10, 7), 2, 3)
    assert cells[0] == (0, 0, 3, 4) and cells[-1][2:] == (10, 7)
    assert sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in cells) == 70


def test_grid_split_keeps_animation(tmp_path):
    frames = [Image.new("RGBA", (30, 20), c) for c in COLORS[:3]]
    src = tmp_path / "a.gif"
    fr.save_animation(fr.Animation(frames, [50, 60, 70], 0, "GIF"), src, "GIF")
    out_dir = tmp_path / "cells"
    out_dir.mkdir()
    paths = compose.grid_split(src, out_dir, 2, 3, compose.Margins(), LIMITS)
    assert len(paths) == 6
    piece = fr.load(paths[4], LIMITS)
    assert piece.size == (10, 10) and len(piece.frames) == 3 and piece.durations == [50, 60, 70]


def test_split_frames_samples_evenly(tmp_path):
    frames = [Image.new("RGB", (8, 8), (i * 20, 0, 0)) for i in range(10)]
    src = tmp_path / "a.gif"
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=50, loop=0)
    out_dir = tmp_path / "f"
    out_dir.mkdir()
    paths, total = compose.split_frames(src, out_dir, 4, LIMITS)
    assert total == 10 and len(paths) == 4
    assert compose.pick_frames(10, 4) == [0, 3, 6, 9]
    static = tmp_path / "s.png"
    Image.new("RGB", (4, 4)).save(static)
    with pytest.raises(UsageError):
        compose.split_frames(static, out_dir, 4, LIMITS)


def test_compose_images_centers_on_largest_canvas(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (40, 20), (255, 0, 0)).save(a)
    Image.new("RGB", (10, 30), (0, 0, 255)).save(b)
    anim = compose.compose_images([a, b], 200, LIMITS, 1024)
    assert anim.size == (40, 30) and anim.durations == [200, 200]
    second = anim.frames[1]
    assert second.getpixel((20, 15))[:3] == (0, 0, 255) and second.getpixel((1, 15))[3] == 0
    for fmt, ext in (("gif", ".gif"), ("webp", ".webp"), ("apng", ".png")):
        out = compose.save_made(anim, tmp_path / f"m_{fmt}", fmt)
        with Image.open(out) as img:
            assert out.suffix == ext and img.n_frames == 2


# ---------------------------------------------------------------- 滤镜
def test_line_art_and_age_meme(tmp_path):
    src = tmp_path / "a.png"
    img = Image.new("RGB", (60, 40), (250, 250, 250))
    for x in range(10, 50):
        img.putpixel((x, 20), (0, 0, 0))
    img.save(src)
    sketch = effects.line_art(src, tmp_path / "s", LIMITS).path
    with Image.open(sketch) as s:
        gray = np.asarray(s.convert("L"))
    assert gray[20, 30] < 120 and gray[5, 5] > 230, "线条应为深色，背景接近白色"

    aged = effects.age_meme(src, tmp_path / "o", 10, LIMITS)
    assert aged.path.suffix == ".jpg" and "中度" in aged.note
    again = effects.age_meme(src, tmp_path / "o2", 10, LIMITS)
    assert aged.path.read_bytes() == again.path.read_bytes(), "固定种子，结果可复现"

    frames = [Image.new("RGBA", (20, 20), c) for c in COLORS[:3]]
    gif = tmp_path / "a.gif"
    fr.save_animation(fr.Animation(frames, [40, 50, 60], 0, "GIF"), gif, "GIF")
    aged_gif = fr.load(effects.age_meme(gif, tmp_path / "g", 3, LIMITS).path, LIMITS)
    assert len(aged_gif.frames) == 3 and aged_gif.durations == [40, 50, 60]
    # 线稿：每帧画一条位置不同的线（纯色帧的线稿全是白色，会被 GIF 写入时合并）
    lined = []
    for index in range(3):
        frame = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
        for y in range(20):
            frame.putpixel((4 + index * 5, y), (0, 0, 0, 255))
        lined.append(frame)
    lined_gif = tmp_path / "lined.gif"
    fr.save_animation(fr.Animation(lined, [40, 50, 60], 0, "GIF"), lined_gif, "GIF")
    sketch_anim = fr.load(effects.line_art(lined_gif, tmp_path / "l", LIMITS).path, LIMITS)
    assert len(sketch_anim.frames) == 3 and sketch_anim.durations == [40, 50, 60]
    with pytest.raises(UsageError):
        effects.age_meme(src, tmp_path / "x", 99, LIMITS)


# ---------------------------------------------------------------- 光棱坦克
def _gradient(size, horizontal):
    w, h = size
    line = np.linspace(0, 255, w if horizontal else h, dtype=np.float32)
    arr = np.tile(line, (h, 1)) if horizontal else np.tile(line[:, None], (1, w))
    return Image.fromarray(arr.astype(np.uint8), "L").convert("RGB")


def test_prism_roundtrip(tmp_path):
    cover, inner = tmp_path / "cover.png", tmp_path / "inner.png"
    _gradient((80, 60), True).save(cover)
    _gradient((80, 60), False).save(inner)
    out = prism.generate(cover, inner, tmp_path / "p.png", inner_limit=32, cover_limit=64, rate=2)
    with Image.open(out) as image:
        assert image.size == (80, 60) and image.text.get(prism.META_KEY)
        arr = np.asarray(image.convert("L"), dtype=np.float32)
    yy, xx = np.indices(arr.shape)
    hidden = (xx + yy) % 2 == 0
    assert arr[hidden].max() <= 32 and arr[~hidden].min() >= 64
    # 平时看到的是表图：与表图高度相关
    assert np.corrcoef(arr[~hidden], np.asarray(_gradient((80, 60), True).convert("L"))[~hidden])[0, 1] > 0.99

    revealed, used, source = prism.reveal(out, tmp_path / "r.png")
    assert used == 32 and source == "图片内记录的参数"
    with Image.open(revealed) as r:
        got = np.asarray(r.convert("L"), dtype=np.float32)
    expected = np.asarray(_gradient((80, 60), False).convert("L"), dtype=np.float32)
    assert np.abs(got - expected).mean() < 12
    assert np.corrcoef(got.ravel(), expected.ravel())[0, 1] > 0.98


def test_prism_auto_detect_without_metadata(tmp_path):
    cover, inner = tmp_path / "c.png", tmp_path / "i.png"
    _gradient((64, 64), True).save(cover)
    _gradient((64, 64), False).save(inner)
    made = prism.generate(cover, inner, tmp_path / "p.png", inner_limit=40, cover_limit=90, rate=3)
    stripped = tmp_path / "stripped.png"
    with Image.open(made) as img:
        img.convert("RGB").save(stripped)  # 去掉文本块
    _, used, source = prism.reveal(stripped, tmp_path / "r.png")
    assert source == "自动检测" and 38 <= used <= 41
    _, used, source = prism.reveal(stripped, tmp_path / "r2.png", threshold=50)
    assert used == 50 and source == "手动指定"
    plain_img = tmp_path / "white.png"
    Image.new("RGB", (20, 20), (255, 255, 255)).save(plain_img)
    with pytest.raises(UsageError):
        prism.reveal(plain_img, tmp_path / "r3.png")
    with pytest.raises(UsageError):
        prism.generate(cover, inner, tmp_path / "bad.png", inner_limit=80, cover_limit=60)


# ---------------------------------------------------------------- 端到端
def test_new_commands_end_to_end(tmp_path, make_app):
    app = make_app()
    sheet = sprite_sheet(tmp_path / "sheet.png")
    run(app.handle_command(FakeEvent([plain("/合成1gif 2x3 0.1s"), image(sheet)], message_str="合成1gif 2x3 0.1s"), "精灵图转GIF"))
    run(app.handle_command(FakeEvent([plain("/切图 2x3"), image(sheet)], message_str="切图 2x3"), "网格切图"))
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (20, 20), "red").save(a)
    Image.new("RGB", (20, 20), "blue").save(b)
    reply = comp("Reply", id="1", chain=[image(a)])
    node = comp("Nodes", nodes=[comp("Node", content=[image(b)])])
    run(app.handle_command(FakeEvent([reply, plain("/多图合成GIF 5fps"), node], message_str="多图合成GIF 5fps"), "多图合成GIF"))
    run(app.handle_command(FakeEvent([plain("/光棱坦克 黑白"), image(a), image(b)], message_str="光棱坦克 黑白"), "光棱坦克"))
    run(app.handle_command(FakeEvent([plain("/加速"), image(sheet)], message_str="加速"), "GIF加速"))
    assert app.sender.kinds()[:2] == ["image", "group"]
    assert app.sender.kinds()[2:4] == ["image", "image"]
    assert "静态图片" in app.sender.texts()[-1]
    assert "已合成 2 张图片，每帧 200ms" in app.sender.calls[2][2]
    assert list(app.tasks.root.glob("task-*")) == []

    run(app.handle_command(FakeEvent([plain("/多图合成GIF"), image(a)], message_str="多图合成GIF"), "多图合成GIF"))
    assert "至少需要 2 张" in app.sender.texts()[-1]
    run(app.handle_command(FakeEvent([plain("/精灵图转GIF"), image(sheet)], message_str="精灵图转GIF"), "精灵图转GIF"))
    assert "行列数" in app.sender.texts()[-1]
    disabled = make_app({"image": {"prism": {"enable": False}}})
    run(disabled.handle_command(FakeEvent([plain("/光棱显形"), image(a)], message_str="光棱显形"), "光棱显形"))
    assert "光棱坦克功能已在配置中关闭" in disabled.sender.texts()[0]
