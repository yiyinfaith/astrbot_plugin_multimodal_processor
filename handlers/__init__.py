"""指令处理函数：每个模块导出 HANDLERS（handler 键 -> 协程函数），键与 core/commands.py 中的 handler 字段对应。"""

from __future__ import annotations

from . import av, helpinfo, image, meme, message, mirage, qq, text

HANDLERS = {**image.HANDLERS, **meme.HANDLERS, **qq.HANDLERS, **mirage.HANDLERS, **av.HANDLERS, **text.HANDLERS, **message.HANDLERS, **helpinfo.HANDLERS}
