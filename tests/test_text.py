"""文本模块。"""

from __future__ import annotations

import pytest

from conftest import FakeEvent, comp, plain, run

from mmp.core.errors import UsageError
from mmp.processors.text import ops


def test_strip_and_add_markdown():
    md = "# 标题\n\n**粗体** 和 *斜体*，[链接](http://a.com)，`code`\n- 项目一\n> 引用\n```py\nx = 1\n```\n| a | b |\n|---|---|\n| 1 | 2 |"
    plain_text = ops.strip_markdown(md)
    for token in ("#", "**", "](", "`", "```", "---", "> "):
        assert token not in plain_text
    for word in ("标题", "粗体", "斜体", "链接", "code", "· 项目一", "x = 1", "1  2"):
        assert word in plain_text
    auto = ops.md_auto("购物清单\n1、牛奶\n• 面包\n地址：https://a.com")
    assert auto.splitlines() == ["## 购物清单", "1. 牛奶", "- 面包", "**地址**： <https://a.com>"]


def test_md_helpers():
    assert ops.md_code("a```b", "py").startswith("````py")
    assert ops.md_quote("a\n\nb") == "> a\n>\n> b"
    assert ops.md_list("x\n\ny", ordered=True) == "1. x\n2. y"
    assert ops.md_table("姓名,年龄\n张三,18") == "| 姓名 | 年龄 |\n| --- | --- |\n| 张三 | 18 |"
    assert ops.md_table("a\tb\n1\t2|3").endswith("| 1 | 2\\|3 |")
    with pytest.raises(UsageError):
        ops.md_table("只有一行")


def test_conversions():
    assert ops.to_halfwidth("ＡＢＣ１２３　！") == "ABC123 !"
    assert ops.to_fullwidth("AB 1") == "ＡＢ　１"
    assert ops.clean_whitespace("  a   b  \n\n\n\n c ") == "a b\n\nc"
    assert ops.dedupe_lines("a\nb\na\nc") == ("a\nb\nc", 1)
    assert ops.sort_lines("item10\nitem2\nitem1") == "item1\nitem2\nitem10"
    assert ops.b64_decode(ops.b64_encode("你好")) == "你好"
    assert ops.b64_decode("5L2g5aW9") == "你好"  # 缺少补位也能解
    with pytest.raises(UsageError):
        ops.b64_decode("!!!")
    assert ops.url_decode(ops.url_encode("a b&c/中")) == "a b&c/中"
    assert ops.json_format('{"a":[1,2]}', compact=True) == '{"a":[1,2]}'
    with pytest.raises(UsageError):
        ops.json_format("{bad}")
    assert "SHA256：2cf24dba" in ops.hashes("hello")
    stats = ops.count("你好 world\n\n123。")
    assert "汉字：2" in stats and "英文单词：1" in stats and "段落：2" in stats


def test_text_commands_end_to_end(make_app):
    app = make_app()
    event = FakeEvent([plain("/MD代码块 python\nprint(1)\n  x")], message_str="MD代码块 python print(1) x")
    run(app.handle_command(event, "MD代码块"))
    assert app.sender.texts()[-1] == "```python\nprint(1)\n  x\n```", "应保留换行和缩进"
    reply = comp("Reply", id="1", chain=[], message_str="**你好**")
    run(app.handle_command(FakeEvent([reply, plain("/去除MD格式")], message_str="去除MD格式"), "去除MD格式"))
    assert app.sender.texts()[-1] == "你好"
    run(app.handle_command(FakeEvent([plain("/文本排序 倒序\nb\na\nc")], message_str="文本排序 倒序 b a c"), "文本排序"))
    assert app.sender.texts()[-1] == "c\nb\na"
    run(app.handle_command(FakeEvent([plain("/转大写")], message_str="转大写"), "转大写"))
    assert "请在指令后面写上文字" in app.sender.texts()[-1]
    long_app = make_app({"general": {"text_file_threshold": 200}})
    run(long_app.handle_command(FakeEvent([plain("/转大写 " + "a" * 500)], message_str="转大写"), "转大写"))
    assert long_app.sender.kinds()[-1] == "file"
    off = make_app({"general": {"text_enable": False}})
    run(off.handle_command(FakeEvent([plain("/转大写 a")], message_str="转大写 a"), "转大写"))
    assert "文本功能已在配置中关闭" in off.sender.texts()[0]
