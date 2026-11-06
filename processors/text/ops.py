"""文本处理（纯函数，无第三方依赖）：Markdown 增删、统计、全半角、大小写、清理、编码、哈希等。"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
import unicodedata
from urllib.parse import quote, unquote

from ...core.errors import UsageError


# ---------------------------------------------------------------- Markdown
def strip_markdown(text: str) -> str:
    """去除 Markdown 标记，保留可读的纯文本。"""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line)
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", line):  # 表格分隔行
            continue
        if re.match(r"^\s*([-*_]\s*){3,}$", line):  # 分隔线
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)  # 标题
        line = re.sub(r"^\s*>+\s?", "", line)  # 引用
        line = re.sub(r"^(\s*)[-*+]\s+\[[ xX]\]\s+", r"\1", line)  # 任务列表
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1· ", line)  # 无序列表
        if line.strip().startswith("|") or line.count("|") >= 2:  # 表格行
            line = "  ".join(c.strip() for c in line.strip().strip("|").split("|"))
        line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)  # 图片
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)  # 链接
        line = re.sub(r"<(https?://[^>]+)>", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)  # 行内代码
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)  # 粗体
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", line)  # 斜体
        line = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w])", r"\1", line)
        line = re.sub(r"~~(.+?)~~", r"\1", line)  # 删除线
        line = re.sub(r"</?[a-zA-Z][^>]*>", "", line)  # HTML 标签
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def md_code(text: str, lang: str = "") -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}{lang}\n{text}\n{fence}"


def md_quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def md_list(text: str, ordered: bool = False) -> str:
    items = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(f"{i}. {item}" if ordered else f"- {item}" for i, item in enumerate(items, 1))


def md_table(text: str) -> str:
    """把 CSV / TSV / 竖线 / 连续空格分隔的文本转成 Markdown 表格，第一行作为表头。"""
    rows_text = [line for line in text.splitlines() if line.strip()]
    if len(rows_text) < 2:
        raise UsageError("至少需要两行（第一行为表头）")
    sample = rows_text[0]
    if "\t" in sample:
        rows = [r.split("\t") for r in rows_text]
    elif "|" in sample:
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows_text]
        rows = [r for r in rows if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
    elif "," in sample or "，" in sample:
        rows = list(csv.reader(io.StringIO("\n".join(r.replace("，", ",") for r in rows_text))))
    else:
        rows = [re.split(r"\s{2,}|\s+", r.strip()) for r in rows_text]
    width = max(len(r) for r in rows)
    rows = [[c.strip().replace("|", "\\|") for c in r] + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def md_auto(text: str) -> str:
    """给普通文本加上基本 Markdown 格式：首行作标题，「名称：内容」加粗名称，网址转链接，列表符号统一。"""
    lines = text.splitlines()
    out: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if index == 0 and len(stripped) <= 40 and len(lines) > 1 and not re.search(r"[。！？.!?，,]$", stripped):
            out.append(f"## {stripped}")
            continue
        bullet = re.match(r"^([•·●◦▪\-*]|\d+[.、)）])\s*(.+)$", stripped)
        if bullet:
            number = re.match(r"\d+", bullet.group(1))
            prefix = number.group() + ". " if number else "- "
            stripped = prefix + bullet.group(2)
        stripped = re.sub(r"^((?:- |\d+\. )?)([^：:\s]{1,12})([：:])\s*", r"\1**\2**\3 ", stripped, count=1)
        stripped = re.sub(r"(?<![(<])(https?://[^\s)）]+)", r"<\1>", stripped)
        out.append(stripped)
    return "\n".join(out)


# ---------------------------------------------------------------- 统计与转换
def count(text: str) -> str:
    no_space = re.sub(r"\s", "", text)
    cjk = len(re.findall(r"[一-鿿㐀-䶿豈-﫿]", text))
    words = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))
    digits = len(re.findall(r"\d", text))
    punct = sum(1 for ch in no_space if unicodedata.category(ch).startswith("P"))
    lines = text.count("\n") + 1 if text else 0
    paragraphs = len([p for p in re.split(r"\n\s*\n", text) if p.strip()])
    return (
        f"总字符：{len(text)}（不含空白 {len(no_space)}）\n"
        f"汉字：{cjk}　英文单词：{words}　数字：{digits}　标点：{punct}\n"
        f"行数：{lines}　段落：{paragraphs}\n"
        f"UTF-8 字节：{len(text.encode('utf-8'))}"
    )


def to_halfwidth(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        out.append(chr(code))
    return "".join(out)


def to_fullwidth(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x20:
            code = 0x3000
        elif 0x21 <= code <= 0x7E:
            code += 0xFEE0
        out.append(chr(code))
    return "".join(out)


def clean_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t　]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def dedupe_lines(text: str) -> tuple[str, int]:
    seen: set[str] = set()
    out = []
    for line in text.splitlines():
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out), len(text.splitlines()) - len(out)


def sort_lines(text: str, reverse: bool = False) -> str:
    def key(line: str):
        return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", line)]

    return "\n".join(sorted((l for l in text.splitlines() if l.strip()), key=key, reverse=reverse))


def b64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def b64_decode(text: str) -> str:
    raw = re.sub(r"\s", "", text)
    raw += "=" * (-len(raw) % 4)
    try:
        data = base64.b64decode(raw.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UsageError("不是有效的 Base64 文本") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError("解码结果不是 UTF-8 文本（可能是二进制数据）") from exc


def url_encode(text: str) -> str:
    return quote(text, safe="")


def url_decode(text: str) -> str:
    return unquote(text.replace("+", " "))


def json_format(text: str, compact: bool = False) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}") from exc
    if compact:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, ensure_ascii=False, indent=2)


def hashes(text: str) -> str:
    data = text.encode("utf-8")
    return "\n".join(f"{name.upper()}：{hashlib.new(name, data).hexdigest()}" for name in ("md5", "sha1", "sha256"))
