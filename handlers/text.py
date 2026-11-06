"""文本指令：文本来自指令后面的内容（保留换行），没有时取引用消息的文字，或引用 / 附带的文本文件。"""

from __future__ import annotations

import re

from ..core.app import Invocation
from ..core.errors import MediaError, MediaMissing, UsageError
from ..core.media import collect_refs, component_type
from ..processors.text import ops

TEXT_FILE_EXTS = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log", ".yaml", ".yml", ".ini", ".xml", ".html", ".py", ".js"}


def _raw_rest(inv: Invocation) -> str:
    """指令名之后的原始文本（保留换行和缩进）。"""
    parts = []
    for component in inv.event.get_messages():
        if component_type(component) == "plain":
            parts.append(str(getattr(component, "text", "") or ""))
    text = "".join(parts).lstrip()
    for prefix in sorted((p for p in inv.app._wake_prefixes() if p), key=len, reverse=True):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    name = inv.parsed.used_name
    if text.lower().startswith(name.lower()):
        text = text[len(name):]
    text = text.lstrip(" \t　")
    return text[1:] if text.startswith("\n") else text


async def _source_text(inv: Invocation, rest: str) -> str:
    if rest.strip():
        return rest
    for component in inv.event.get_messages():
        if component_type(component) == "reply":
            quoted = str(getattr(component, "message_str", "") or getattr(component, "text", "") or "")
            if quoted.strip():
                return quoted
    limit = inv.settings.general.text_max_chars * 4
    for ref in collect_refs(inv.event):
        name = (ref.name or "").lower()
        if ref.hint != "file" or not any(name.endswith(ext) for ext in TEXT_FILE_EXTS):
            continue
        async with inv.app.tasks.task(inv.user_key, heavy=False, label="text") as slot:
            media = await inv.fetch(ref, slot, limit)
            data = media.path.read_bytes()
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise MediaError("文本文件编码无法识别（支持 UTF-8 / GBK）")
    raise MediaMissing("请在指令后面写上文字，或引用一条文字消息 / 文本文件")


def _take_option(text: str, options: set[str]) -> tuple[str | None, str]:
    """如果第一行只有一个选项词（如语言名、「有序」），把它取出来。"""
    first, _, remain = text.partition("\n")
    word = first.strip()
    if remain and word.lower() in options:
        return word.lower(), remain
    if remain == "" and " " in first:
        head, _, tail = first.strip().partition(" ")
        if head.lower() in options:
            return head.lower(), tail
    return None, text


_LANG_RE = re.compile(r"^[a-z0-9+#.\-]{1,15}$")


async def run_text(inv: Invocation) -> bool:
    name = inv.parsed.spec.name
    rest = _raw_rest(inv)
    option = None
    if name == "MD代码块":
        first, _, remain = rest.partition("\n")
        if remain and _LANG_RE.fullmatch(first.strip().lower()):
            option, rest = first.strip().lower(), remain
    elif name in {"MD列表", "文本排序", "JSON格式化"}:
        option, rest = _take_option(rest, {"有序", "倒序", "压缩"})
    text = await _source_text(inv, rest)
    limit = inv.settings.general.text_max_chars
    if len(text) > limit:
        raise UsageError(f"文本太长（{len(text)} 字），最多 {limit} 字，可在配置中调整")
    note = None
    if name == "去除MD格式":
        result = ops.strip_markdown(text)
    elif name == "添加MD格式":
        result = ops.md_auto(text)
    elif name == "MD代码块":
        result = ops.md_code(text, option or "")
    elif name == "MD引用":
        result = ops.md_quote(text)
    elif name == "MD列表":
        result = ops.md_list(text, ordered=option == "有序")
    elif name == "MD表格":
        result = ops.md_table(text)
    elif name == "字数统计":
        result = ops.count(text)
    elif name == "全角转半角":
        result = ops.to_halfwidth(text)
    elif name == "半角转全角":
        result = ops.to_fullwidth(text)
    elif name == "转大写":
        result = text.upper()
    elif name == "转小写":
        result = text.lower()
    elif name == "清理空白":
        result = ops.clean_whitespace(text)
    elif name == "文本去重":
        result, removed = ops.dedupe_lines(text)
        note = f"已删除 {removed} 行重复内容"
    elif name == "文本排序":
        result = ops.sort_lines(text, reverse=option == "倒序")
    elif name == "文本反转":
        result = text[::-1]
    elif name == "Base64编码":
        result = ops.b64_encode(text)
    elif name == "Base64解码":
        result = ops.b64_decode(text)
    elif name == "URL编码":
        result = ops.url_encode(text)
    elif name == "URL解码":
        result = ops.url_decode(text)
    elif name == "JSON格式化":
        result = ops.json_format(text, compact=option == "压缩")
    elif name == "文本哈希":
        result = ops.hashes(text)
    else:  # pragma: no cover
        raise UsageError("未知的文本指令")
    if not result.strip():
        raise UsageError("处理结果为空")
    await _send(inv, result, note)
    return True


async def _send(inv: Invocation, result: str, note: str | None) -> None:
    threshold = inv.settings.general.text_file_threshold
    if len(result) <= threshold:
        await inv.reply(f"{note}\n{result}" if note else result)
        return
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="textout") as slot:
        path = slot.workdir / "result.txt"
        path.write_text(result, encoding="utf-8")
        try:
            await inv.app.sender.file(inv.event, path, "result.txt", note or f"结果较长（{len(result)} 字），以文件发送")
        except Exception:
            await inv.reply((note + "\n" if note else "") + result[:threshold] + f"\n……（共 {len(result)} 字，已截断）")


HANDLERS = {"text": run_text}
