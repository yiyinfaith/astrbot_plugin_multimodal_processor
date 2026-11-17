"""meme-generator 表情包子功能的管理指令。"""

from __future__ import annotations

import asyncio
import shutil

from ..core.app import Invocation
from ..core.errors import ProcessingError, UsageError, UserError

SEARCH_LIMIT = 100


def _require(inv: Invocation):
    manager = inv.app.memes
    if not manager.status.available:
        reason = manager.status.reason or "未加载到 meme-generator"
        raise UserError(f"表情包生成不可用：{reason}")
    return manager


async def help_list(inv: Invocation) -> bool:
    """优先发送安装 / 重载后预先渲染好的图片；还没有时才现场渲染。"""
    manager = _require(inv)
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="meme-list") as slot:
        cached = manager.cached_list()
        if cached is None:
            # shield：等待超时也不打断渲染，渲染完成后图片照样保存，下次直接发送
            try:
                result = await asyncio.wait_for(asyncio.shield(manager.start_list_refresh(force=False)), timeout=inv.timeout)
            except asyncio.TimeoutError as exc:
                raise ProcessingError("表情包较多，列表图片还在渲染，完成后再发送 /表情包列表 即可立即获取") from exc
            cached = manager.cached_list()
            if cached is None:
                raise ProcessingError(f"表情包列表渲染失败：{result.error or '没有生成图片'}")
        output = slot.workdir / cached.name  # 复制一份再发送，发送途中刷新也不受影响
        await asyncio.to_thread(shutil.copyfile, cached, output)
        await inv.app.sender.image(inv.event, output)
    return True


def describe_refresh(result, has_cache: bool) -> str:
    if result.previous is None:
        lines = [f"首次生成表情包列表，共 {result.count} 个表情。"]
    elif not result.changed:
        lines = [f"表情包资源没有更新，共 {result.count} 个表情。"]
    else:
        parts = []
        if result.added:
            parts.append(f"新增 {len(result.added)} 个")
        if result.removed:
            parts.append(f"移除 {len(result.removed)} 个")
        detail = "、".join(parts) or "关键词或参数有变化"
        lines = [f"表情包资源有更新：{detail}，现在共 {result.count} 个表情。"]
        if result.added:
            shown = "、".join(result.added[:10])
            lines.append(f"新增：{shown}" + (f" 等 {len(result.added)} 个" if len(result.added) > 10 else ""))
        if result.removed:
            shown = "、".join(result.removed[:10])
            lines.append(f"移除：{shown}" + (f" 等 {len(result.removed)} 个" if len(result.removed) > 10 else ""))
    if result.rendered:
        lines.append(f"列表图片重新渲染成功，用时 {result.seconds:.1f} 秒。")
    else:
        lines.append(f"列表图片渲染失败：{result.error}" + ("（仍保留之前的图片）" if has_cache else ""))
    return "\n".join(lines)


async def refresh_list(inv: Invocation) -> bool:
    """手动检查表情包资源并重新渲染列表，不受处理超时限制。"""
    manager = _require(inv)
    result = await asyncio.shield(manager.start_list_refresh(force=True))
    await inv.reply(describe_refresh(result, manager.cached_list() is not None))
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
    "meme_refresh": refresh_list,
    "meme_search": search,
    "meme_details": details,
}
