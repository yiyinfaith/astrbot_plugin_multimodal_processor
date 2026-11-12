"""帮助指令。"""

from __future__ import annotations

from ..core.app import Invocation
from ..core.commands import help_for, help_overview


async def show_help(inv: Invocation) -> bool:
    query = " ".join(inv.args).strip()
    if query:
        text = help_for(query)
        if text is None:
            text = f"没有找到「{query}」。可用分类：图片、表情包、QQ、幻影坦克、光棱、文本、视频、音频、消息；也可以直接写指令名。"
    else:
        ffmpeg_needed = inv.settings.video.enable or inv.settings.audio.enable
        text = help_overview(ffmpeg_ok=inv.app.ffmpeg.available or not ffmpeg_needed)
    await inv.reply(text)
    return True


HANDLERS = {"help": show_help}
