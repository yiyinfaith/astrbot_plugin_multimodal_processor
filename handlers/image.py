"""图片 / GIF 指令。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..core import args as A
from ..core.app import Invocation
from ..core.errors import MediaError, MediaMissing, MediaTooLarge, UsageError
from ..core.formats import IMAGE as KIND_IMAGE
from ..core.media import IMAGE as REF_IMAGE, MediaRef, at_targets, qq_avatar_url
from ..platforms import qq
from ..processors.gif import compose
from ..processors.gif.frames import ImageLimits
from ..processors.image import effects, ops

IMAGE_FORMATS = ("png", "jpg", "webp", "gif", "bmp", "tiff")
# QQ 等平台能直接显示的格式用图片消息发送，其余格式用文件发送
_DISPLAYABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def limits_of(inv: Invocation) -> ImageLimits:
    s = inv.settings.image
    return ImageLimits(max_pixels=s.max_pixels, max_frames=s.max_frames, memory_bytes=s.memory_bytes)


def missing_image() -> MediaMissing:
    return MediaMissing("请引用一张图片、随指令附带图片，或 @ 一位用户（取头像）后再发送指令")


async def run_image_job(
    inv: Invocation, process: Callable[[Path, Path], ops.ImageResult], *, as_file: bool = False, allow_avatar: bool = True
) -> bool:
    refs = inv.image_refs(allow_avatar)
    if not refs:
        raise missing_image()
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="image") as slot:
        media = None
        errors: list[str] = []
        for ref in refs:
            try:
                candidate = await inv.fetch(ref, slot, inv.settings.image.max_bytes)
            except MediaError as exc:
                errors.append(str(exc))
                continue
            if candidate.sniffed.kind == KIND_IMAGE:
                media = candidate
                break
            errors.append("引用的文件不是图片")
        if media is None:
            raise MediaError(errors[0] if errors else "没有找到可处理的图片")
        out_base = slot.workdir / f"{media.stem if media.name else 'image'}_out"
        result = await inv.run_timed(inv.app.tasks.run_blocking(process, media.path, out_base))
        if as_file or result.path.suffix.lower() not in _DISPLAYABLE:
            await inv.app.sender.file(inv.event, result.path, result.path.name.replace("_out", ""), result.note)
        else:
            await inv.app.sender.image(inv.event, result.path, result.note)
    return True


async def transform(inv: Invocation) -> bool:
    op = ops.TRANSFORMS[inv.parsed.spec.name]
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: ops.transform(src, out, op, limits))


async def gif_speed(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写倍率")
    factor = A.parse_float(inv.args[0], "倍率", 0.1, 10.0)
    s = inv.settings.image
    limits = limits_of(inv)
    return await run_image_job(
        inv, lambda src, out: ops.speed(src, out, factor, limits, s.gif_max_fps, s.gif_speed_allow_frame_drop)
    )


async def convert(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写目标格式")
    fmt = A.parse_format(inv.args[0], IMAGE_FORMATS)
    extra = [a.lower() for a in inv.args[1:]]
    unknown = [a for a in extra if a not in {"静态", "static", "首帧"}]
    if unknown:
        raise UsageError(f"无法识别的参数：{' '.join(unknown)}")
    static = bool(extra)
    quality = inv.settings.image.default_quality
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: ops.convert(src, out, fmt, limits, static=static, quality=quality))


async def compress(inv: Invocation) -> bool:
    quality = inv.settings.image.default_quality if not inv.args else A.parse_int(inv.args[0], "质量", 1, 100)
    max_side = A.parse_int(inv.args[1], "最长边", 16, 16384) if len(inv.args) > 1 else None
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: ops.compress(src, out, quality, limits, max_side))


async def resize(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写目标尺寸")
    spec = A.parse_size(inv.args[0])
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: ops.resize(src, out, spec, limits))


async def crop(inv: Invocation) -> bool:
    if not inv.args:
        raise UsageError("请填写裁剪区域")
    mode, values = A.parse_crop(inv.args[0])
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: ops.crop(src, out, mode, values, limits))


async def info(inv: Invocation) -> bool:
    refs = inv.image_refs()
    if not refs:
        raise missing_image()
    limits = limits_of(inv)
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="info") as slot:
        media = await inv.fetch(refs[0], slot, inv.settings.image.max_bytes)
        if media.sniffed.kind != KIND_IMAGE:
            raise MediaError("引用的文件不是图片")
        data = await inv.run_timed(inv.app.tasks.run_blocking(ops.info, media.path, limits))
    lines = [
        f"格式：{data['format']}",
        f"尺寸：{data['width']} x {data['height']}",
        f"大小：{A.format_bytes(data['size'])}",
        f"模式：{data['mode']}{'（含透明）' if data['alpha'] else ''}",
    ]
    if data["frames"] > 1:
        lines.append(f"帧数：{data['frames']}")
        if data["duration"]:
            fps = data["frames"] * 1000 / data["duration"]
            lines.append(f"总时长：{data['duration'] / 1000:.2f} 秒（平均 {fps:.1f} FPS）")
        loop = data["loop"]
        lines.append("循环：" + ("无限" if loop == 0 else loop if isinstance(loop, str) else f"{loop} 次"))
    else:
        lines.append("帧数：1（静态图）")
    await inv.reply("\n".join(lines))
    return True


HANDLERS = {
    "image_transform": transform,
    "gif_speed": gif_speed,
    "image_convert": convert,
    "image_compress": compress,
    "image_resize": resize,
    "image_crop": crop,
    "image_info": info,
}


# --------------------------------------------------------------------------- 动图工具（整合自 gifcaijian）
async def _speed_by(inv: Invocation, faster: bool) -> bool:
    times = A.parse_float(inv.args[0], "倍数", 1.0, 10.0) if inv.args else 2.0
    factor = times if faster else 1 / times
    s = inv.settings.image
    limits = limits_of(inv)
    return await run_image_job(
        inv, lambda src, out: ops.speed(src, out, factor, limits, s.gif_max_fps, s.gif_speed_allow_frame_drop)
    )


async def gif_faster(inv: Invocation) -> bool:
    return await _speed_by(inv, True)


async def gif_slower(inv: Invocation) -> bool:
    return await _speed_by(inv, False)


def _grid_args(inv: Invocation, *, need_duration: bool) -> tuple[tuple[int, int], int | None, compose.Margins]:
    rest, margins = A.take_margins(list(inv.args))
    grid = None
    duration = None
    for token in rest:
        parsed = A.parse_grid(token)
        if parsed and grid is None:
            grid = parsed
        elif need_duration and duration is None:
            duration = A.parse_frame_duration(token)
        else:
            raise UsageError(f"无法识别的参数：{token}")
    if grid is None:
        raise UsageError("请填写行列数，例如 3x4")
    return grid, duration, compose.Margins(**margins)


async def sprite_to_gif(inv: Invocation) -> bool:
    (rows, cols), duration, margins = _grid_args(inv, need_duration=True)
    frame_ms = duration or 100
    limits = limits_of(inv)
    fmt = inv.settings.image.make_format

    def process(src: Path, out: Path) -> ops.ImageResult:
        anim = compose.sprite_to_animation(src, rows, cols, frame_ms, margins, limits)
        path = compose.save_made(anim, out, fmt)
        note = f"{rows}x{cols} 网格，{len(anim.frames)} 帧，每帧 {frame_ms}ms"
        if not margins.empty:
            note += f"，已去边距（{margins.describe()}）"
        return ops.ImageResult(path, note)

    return await run_image_job(inv, process, allow_avatar=False)


async def _multi_output_job(inv: Invocation, process) -> bool:
    refs = inv.image_refs(allow_avatar=False)
    if not refs:
        raise missing_image()
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="multi") as slot:
        media = await inv.fetch(refs[0], slot, inv.settings.image.max_bytes)
        if media.sniffed.kind != KIND_IMAGE:
            raise MediaError("引用的文件不是图片")
        out_dir = slot.workdir / "out"
        out_dir.mkdir()
        paths, note = await inv.run_timed(inv.app.tasks.run_blocking(process, media.path, out_dir))
        await inv.app.sender.image_group(inv.event, paths, note)
    return True


async def grid_split(inv: Invocation) -> bool:
    (rows, cols), _, margins = _grid_args(inv, need_duration=False)
    if rows * cols < 2:
        raise UsageError("至少要切成 2 块")
    limits = limits_of(inv)

    def process(src: Path, out_dir: Path):
        paths = compose.grid_split(src, out_dir, rows, cols, margins, limits)
        note = f"已切成 {rows}x{cols} 共 {len(paths)} 块（按行从左到右）"
        if not margins.empty:
            note += f"，已去边距（{margins.describe()}）"
        return paths, note

    return await _multi_output_job(inv, process)


async def gif_split(inv: Invocation) -> bool:
    limit = A.parse_int(inv.args[0], "最多帧数", 1, 200) if inv.args else inv.settings.image.split_max_frames
    limits = limits_of(inv)

    def process(src: Path, out_dir: Path):
        paths, total = compose.split_frames(src, out_dir, limit, limits)
        note = f"共 {total} 帧" + (f"，均匀抽取了 {len(paths)} 帧" if len(paths) < total else "")
        return paths, note

    return await _multi_output_job(inv, process)


async def multi_to_gif(inv: Invocation) -> bool:
    frame_ms = A.parse_frame_duration(inv.args[0]) if inv.args else 500
    if len(inv.args) > 1:
        raise UsageError(f"无法识别的参数：{' '.join(inv.args[1:])}")
    s = inv.settings.image
    g = inv.settings.general
    onebot = qq.is_onebot(inv.event)
    refs = inv.refs(REF_IMAGE)
    if g.enable_at_avatar and onebot:
        refs += [MediaRef(None, "avatar", "avatar", name=f"avatar_{uid}", url=qq_avatar_url(uid)) for uid in at_targets(inv.event)]
    forward = await qq.forward_images(inv.event, s.multi_max_images) if onebot else []
    if not refs and not forward:
        raise MediaMissing("请发送多张图片，或引用含图片的消息 / 合并转发后再发送指令")
    limits = limits_of(inv)
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="compose") as slot:
        paths: list[Path] = []
        skipped = 0
        for ref in refs:
            if len(paths) >= s.multi_max_images:
                break
            try:
                media = await inv.fetch(ref, slot, s.max_bytes)
            except MediaTooLarge:
                raise
            except MediaError:
                skipped += 1
                continue
            if media.sniffed.kind == KIND_IMAGE:
                paths.append(media.path)
            else:
                skipped += 1
        for image in forward:
            if len(paths) >= s.multi_max_images:
                break
            try:
                path = await qq.fetch_qq_image(
                    inv.event, image, slot.workdir, s.max_bytes, allow_private=g.allow_private_urls, timeout=g.download_timeout
                )
            except MediaError:
                skipped += 1
                continue
            paths.append(path)
        if len(paths) < 2:
            raise UsageError("至少需要 2 张图片才能合成动图")

        def process() -> ops.ImageResult:
            anim = compose.compose_images(paths, frame_ms, limits, s.gif_max_side)
            out = compose.save_made(anim, slot.workdir / "composed", s.make_format)
            note = f"已合成 {len(paths)} 张图片，每帧 {frame_ms}ms，画布 {anim.size[0]}x{anim.size[1]}"
            if skipped:
                note += f"（跳过 {skipped} 个无法读取的文件）"
            return ops.ImageResult(out, note)

        result = await inv.run_timed(inv.app.tasks.run_blocking(process))
        await inv.app.sender.image(inv.event, result.path, result.note)
    return True


async def to_line_art(inv: Invocation) -> bool:
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: effects.line_art(src, out, limits))


async def age_meme(inv: Invocation) -> bool:
    times = A.parse_int(inv.args[0], "做旧次数", 1, 50) if inv.args else 5
    limits = limits_of(inv)
    return await run_image_job(inv, lambda src, out: effects.age_meme(src, out, times, limits))


HANDLERS.update({
    "gif_faster": gif_faster,
    "gif_slower": gif_slower,
    "sprite_to_gif": sprite_to_gif,
    "grid_split": grid_split,
    "gif_split": gif_split,
    "multi_to_gif": multi_to_gif,
    "line_art": to_line_art,
    "age_meme": age_meme,
})
