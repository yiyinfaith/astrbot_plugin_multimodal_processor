from pathlib import Path

from PIL import Image

from core.commands import parse_command
from core.tasks import TaskManager
from processors.image.gif import read_frames
from processors.image.operations import image_info, process_image
from processors.mirage.algorithm import generate_mirage_sync


def make_gif(path: Path) -> None:
    frames = []
    for color in ((255, 0, 0, 128), (0, 255, 0, 64), (0, 0, 255, 255)):
        image = Image.new("RGBA", (24, 16), color)
        frames.append(image)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[40, 80, 120], loop=2, disposal=2)


def test_gif_transform_preserves_animation_and_alpha(tmp_path):
    source = tmp_path / "in.gif"; output = tmp_path / "out.gif"
    make_gif(source)
    process_image(source, output, operation="hflip", max_fps=50)
    info = image_info(output)
    assert info["frames"] == 3 and info["loop"] == 2
    assert read_frames(output).durations == [40, 80, 120]


def test_mirage_outputs_rgba(tmp_path):
    front = tmp_path / "front.png"; back = tmp_path / "back.png"; out = tmp_path / "mirage.png"
    Image.new("RGB", (20, 20), "white").save(front)
    Image.new("RGB", (10, 10), "black").save(back)
    generate_mirage_sync(front, back, out, mode="color")
    with Image.open(out) as image:
        assert image.mode == "RGBA" and image.size == (20, 20)


def test_command_parser_and_task_cleanup(tmp_path):
    parsed = parse_command('/视频裁剪 "1.5" 3')
    assert parsed and parsed.name == "视频裁剪" and parsed.args == ("1.5", "3")
    manager = TaskManager(tmp_path)

    async def run():
        async with manager.task() as work:
            (work / "x").write_text("x")
            assert work.exists()
        assert not work.exists()

    import asyncio
    asyncio.run(run())
