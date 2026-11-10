"""meme-generator 表情包子功能的管理指令。"""

from __future__ import annotations

from ..core.app import Invocation
from ..core.errors import UsageError, UserError

SEARCH_LIMIT = 100


def _require(inv: Invocation):
    manager = inv.app.memes
    if not manager.status.available:
        reason = manager.status.reason or "未加载到 meme-generator"
        raise UserError(f"表情包生成不可用：{reason}")
    return manager


async def help_list(inv: Invocation) -> bool:
    manager = _require(inv)
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="meme-list") as slot:
        rendered = await inv.run_timed(manager.render_list())
        output = slot.workdir / f"meme-list{manager.suffix(rendered)}"
        output.write_bytes(rendered)
        await inv.app.sender.image(inv.event, output)
    return True


async def search(inv: Invocation) -> bool:
    manager = _require(inv)
    if not inv.args:
        raise UsageError("请填写要搜索的关键词片段")
    result = manager.search_pairs(" ".join(inv.args))
    if not result:
        await inv.reply("没有找到匹配的表情包关键词")
        return True
    lines = [f"{i}. {keyword}  ({key})" for i, (keyword, key) in enumerate(result[:SEARCH_LIMIT], 1)]
    if len(result) > SEARCH_LIMIT:
        lines.append(f"……共 {len(result)} 条，只显示前 {SEARCH_LIMIT} 条，请缩小搜索范围")
    await inv.reply("\n".join(lines))
    return True


async def details(inv: Invocation) -> bool:
    manager = _require(inv)
    if not inv.args:
        raise UsageError("请填写表情包关键词")
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="meme-detail") as slot:
        result = await inv.run_timed(manager.details(" ".join(inv.args)))
        if not result:
            raise UserError("未找到相关表情包关键词")
        description, preview = result
        output = slot.workdir / f"meme-preview{manager.suffix(preview)}"
        output.write_bytes(preview)
        await inv.app.sender.image(inv.event, output, description)
    return True


HANDLERS = {
    "meme_help": help_list,
    "meme_search": search,
    "meme_details": details,
}
