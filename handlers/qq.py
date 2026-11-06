"""QQ 图片保存（/转换）、修改外显（/取名）与群禁用管理。仅 OneBot v11（aiocqhttp）可用。"""

from __future__ import annotations

import logging

from ..core.app import Invocation
from ..core.errors import MediaError, MediaMissing, UserError, UsageError
from ..core.formats import IMAGE, sniff_file
from ..platforms import qq

log = logging.getLogger("astrbot")

_EXT_NAMES = {".gif": "GIF 动图", ".png": "PNG", ".jpg": "JPG", ".webp": "WEBP", ".bmp": "BMP"}


async def _fetch_first_image(inv: Invocation, slot) -> tuple:
    images = await qq.find_images(inv.event)
    if not images:
        raise MediaMissing("请引用一张图片 / GIF / 表情，或随指令附带图片")
    g = inv.settings.general
    errors: list[str] = []
    for image in images:
        if image.is_mface and not inv.settings.qq.enable_mface:
            errors.append("QQ 官方表情提取已在配置中关闭")
            continue
        try:
            path = await qq.fetch_qq_image(
                inv.event, image, slot.workdir, inv.settings.image.max_bytes,
                allow_private=g.allow_private_urls, timeout=g.download_timeout,
            )
        except MediaError as exc:
            errors.append(str(exc))
            continue
        sniffed = sniff_file(path)
        if sniffed.kind != IMAGE:
            errors.append("获取到的内容不是图片")
            path.unlink(missing_ok=True)
            continue
        final = path.with_suffix(sniffed.ext)
        path.replace(final)
        return final, image
    raise MediaError(errors[0] if errors else "没有可保存的图片")


def _group_blocked(inv: Invocation) -> bool:
    group_id = str(inv.event.get_group_id() or "")
    return bool(group_id) and group_id in inv.settings.qq.blocked_groups


async def convert(inv: Invocation) -> bool:
    qq.require_onebot(inv.event, "QQ 图片保存")
    if _group_blocked(inv):
        raise UserError("本群已禁止使用 /转换，请私聊机器人使用")
    mode = inv.settings.qq.convert_group_mode
    in_group = bool(inv.event.get_group_id())
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="qqsave") as slot:
        path, image = await _fetch_first_image(inv, slot)
        name = f"{'表情' if image.is_mface else '图片'}{path.suffix}"
        kind = _EXT_NAMES.get(path.suffix, path.suffix.upper().lstrip("."))
        if not in_group:
            await qq.send_file(inv.event, path, name, "private")
            return True
        if mode == "group":
            await qq.send_file(inv.event, path, name, "group")
            return True
        try:
            await qq.send_file(inv.event, path, name, "private")
            await inv.reply(f"已把 {kind} 文件私聊发给你。")
        except Exception as exc:
            log.info(f"[多模态处理器] 私聊发送转换文件失败：{exc}")
            if mode == "private_then_group":
                await qq.send_file(inv.event, path, name, "group")
            else:
                raise UserError("私聊发送失败：请先添加机器人为好友，或直接私聊机器人使用 /转换") from exc
    return True


async def rename(inv: Invocation) -> bool:
    qq.require_onebot(inv.event, "修改图片外显")
    text = inv.parsed.rest.strip()
    if not text:
        raise UsageError("请填写外显文字")
    if len(text) > 30:
        raise UsageError("外显文字最多 30 个字")
    summary = text if text.startswith("[") and text.endswith("]") else f"[{text}]"
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="rename") as slot:
        path, _ = await _fetch_first_image(inv, slot)
        await qq.send_image_with_summary(inv.event, path, summary)
    return True


def _target_group(inv: Invocation) -> str:
    if inv.args:
        value = inv.args[0].strip()
        if not value.isdigit():
            raise UsageError("群号必须是数字")
        return value
    group_id = str(inv.event.get_group_id() or "")
    if not group_id:
        raise UsageError("私聊使用时请填写群号")
    return group_id


async def block(inv: Invocation) -> bool:
    group_id = _target_group(inv)
    groups = list(inv.settings.qq.blocked_groups)
    if group_id in groups:
        await inv.reply(f"群 {group_id} 已在禁用列表中")
        return True
    groups.append(group_id)
    inv.app.save_blocked_groups(groups)
    await inv.reply(f"已禁止群 {group_id} 使用 /转换")
    return True


async def unblock(inv: Invocation) -> bool:
    group_id = _target_group(inv)
    groups = list(inv.settings.qq.blocked_groups)
    if group_id not in groups:
        await inv.reply(f"群 {group_id} 不在禁用列表中")
        return True
    groups.remove(group_id)
    inv.app.save_blocked_groups(groups)
    await inv.reply(f"已恢复群 {group_id} 的 /转换")
    return True


HANDLERS = {
    "qq_convert": convert,
    "qq_rename": rename,
    "qq_block": block,
    "qq_unblock": unblock,
}
