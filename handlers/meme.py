"""meme-generator 表情包子功能的管理指令。"""

from __future__ import annotations

from pathlib import Path
import re

from ..core.app import Invocation
from ..core.errors import FeatureDisabled, ProcessingError, UsageError, UserError


MENU = (
    "表情包生成指令：\n"
    "/meme帮助（或 /meme列表） 查看关键词\n"
    "/meme搜索 关键词 搜索触发词\n"
    "/meme详情 关键词 查看参数和预览\n"
    "/禁用meme 关键词、/启用meme 关键词 管理本群黑名单\n"
    "/meme黑名单 查看本群黑名单\n"
    "直接发送「关键词 [文本]」即可生成，是否需要前缀由配置决定。"
)


def _require(inv: Invocation):
    manager = inv.app.memes
    if not inv.settings.meme.enable:
        raise FeatureDisabled("表情包生成已在 图片处理 → Meme Generator 中关闭")
    if not manager.status.available:
        reason = manager.status.reason or "未加载到 meme-generator"
        raise UserError(f"表情包生成不可用：{reason}")
    return manager


async def menu(inv: Invocation) -> bool:
    _require(inv)
    await inv.reply(MENU)
    return True


async def help_list(inv: Invocation) -> bool:
    manager = _require(inv)
    rendered = await manager.render_list()
    if not rendered:
        raise UserError("表情包列表渲染失败，请检查 meme-generator 的资源和字体")
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="meme-list") as slot:
        output = slot.workdir / "meme-list.png"
        output.write_bytes(rendered)
        await inv.app.sender.image(inv.event, output)
    return True


async def search(inv: Invocation) -> bool:
    manager = _require(inv)
    if not inv.args: raise UsageError("请填写要搜索的关键词片段")
    result = manager.search_pairs(" ".join(inv.args))
    await inv.reply("\n".join(f"{i}. {keyword}  ({key})" for i, (keyword, key) in enumerate(result, 1)) if result else "没有找到匹配的表情包关键词")
    return True


async def details(inv: Invocation) -> bool:
    manager = _require(inv)
    if not inv.args: raise UsageError("请填写表情包关键词")
    result = await manager.details(inv.args[0])
    if not result: raise UserError("未找到相关表情包关键词")
    description, preview = result
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="meme-detail") as slot:
        output = slot.workdir / "meme-preview.png"
        output.write_bytes(preview)
        await inv.app.sender.image(inv.event, output, description)
    return True


async def disable(inv: Invocation) -> bool:
    manager = _require(inv)
    if not inv.args: raise UsageError("请填写要禁用的表情包关键词")
    keyword = inv.args[0]
    if not manager.is_keyword(keyword): raise UserError(f"不存在表情包关键词：{keyword}")
    disabled = manager.disabled_for(inv.event)
    if keyword in disabled:
        await inv.reply(f"表情包 {keyword} 已经在禁用列表中")
        return True
    disabled.append(keyword); inv.app.save_meme_config()
    await inv.reply(f"已禁用本群表情包：{keyword}")
    return True


async def enable(inv: Invocation) -> bool:
    manager = _require(inv)
    if not inv.args: raise UsageError("请填写要启用的表情包关键词")
    keyword = inv.args[0]
    disabled = manager.disabled_for(inv.event)
    if keyword not in disabled:
        await inv.reply(f"表情包 {keyword} 不在本群禁用列表中")
        return True
    disabled.remove(keyword); inv.app.save_meme_config()
    await inv.reply(f"已启用本群表情包：{keyword}")
    return True


async def blacklist(inv: Invocation) -> bool:
    manager = _require(inv)
    disabled = manager.disabled_for(inv.event)
    await inv.reply("本群禁用的表情包：" + ("、".join(disabled) if disabled else "无"))
    return True


HANDLERS = {
    "meme_menu": menu,
    "meme_help": help_list,
    "meme_search": search,
    "meme_details": details,
    "meme_disable": disable,
    "meme_enable": enable,
    "meme_blacklist": blacklist,
}
