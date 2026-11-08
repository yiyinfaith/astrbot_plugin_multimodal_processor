"""meme-generator 表情包子功能的管理指令。"""

from __future__ import annotations

from ..core.app import Invocation
from ..core.errors import FeatureDisabled, UsageError, UserError


def _require(inv: Invocation):
    manager = inv.app.memes
    if not inv.settings.meme.enable:
        raise FeatureDisabled("表情包生成已在 图片处理 → Meme Generator 中关闭")
    if not manager.status.available:
        reason = manager.status.reason or "未加载到 meme-generator"
        raise UserError(f"表情包生成不可用：{reason}")
    return manager


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


HANDLERS = {
    "meme_help": help_list,
    "meme_search": search,
    "meme_details": details,
}
