"""指令注册表、main.py 装饰器、配置 schema、元数据之间的一致性。"""

from __future__ import annotations

import ast
import json
import re

from conftest import ROOT

from mmp.core.commands import COMMANDS, get_spec, help_for, help_overview, parse_command
from mmp.core.config import Settings, setting_names
from mmp.handlers import HANDLERS


def _decorated_commands() -> dict[str, tuple[str, set[str], bool]]:
    """解析 main.py：函数名 -> (指令名, 别名集合, 是否管理员)。"""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    found: dict[str, tuple[str, set[str], bool]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        name, aliases, admin = None, set(), False
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            target = ast.unparse(deco.func)
            if target == "filter.command":
                name = deco.args[0].value
                for kw in deco.keywords:
                    if kw.arg == "alias":
                        aliases = {elt.value for elt in kw.value.elts}
            if target == "filter.permission_type":
                admin = "ADMIN" in ast.unparse(deco.args[0])
        if name:
            found[node.name] = (name, aliases, admin)
    return found


def test_every_spec_registered_in_main_with_same_aliases():
    decorated = {name: (aliases, admin) for name, aliases, admin in _decorated_commands().values()}
    assert set(decorated) == {spec.name for spec in COMMANDS}
    for spec in COMMANDS:
        aliases, admin = decorated[spec.name]
        assert aliases == set(spec.aliases), spec.name
        assert admin == spec.admin, spec.name


def test_main_handlers_pass_their_own_command_name():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for func, (name, _, _) in _decorated_commands().items():
        body = re.search(rf"async def {func}\(.*?\n(.*?)(?=\n    @|\Z)", source, re.S).group(1)
        assert f'handle_command(event, "{name}")' in body, func


def test_every_handler_key_exists():
    assert {spec.handler for spec in COMMANDS} <= set(HANDLERS)


def test_parse_command_longest_match_and_aliases():
    assert parse_command("转换禁用 123").spec.name == "转换禁用"
    assert parse_command("转换禁用列表") is None  # 已删除的指令
    assert parse_command("转换").spec.name == "转换"
    assert parse_command("转换一下") is None  # 指令名后必须是空白
    assert parse_command("音频转MP3").args == ("mp3",)
    assert parse_command("音频转mp3").args == ("mp3",)  # 不区分大小写
    assert parse_command("视频转gif 12 480").args == ("12", "480")
    parsed = parse_command("外显取名  好耶 呀 ")
    assert parsed.spec.name == "外显取名" and parsed.rest == "好耶 呀"
    assert parse_command("多模态处理帮助 视频").spec.name == "多模态处理帮助"
    for old in ("多模态帮助", "多模态", "rename 好耶", "取名 好耶", "外显 好耶"):  # 旧名都已停用
        assert parse_command(old) is None
    assert parse_command('图片转换 "png" 静态').args == ("png", "静态")
    assert parse_command("convert addblock 123").spec.name == "转换禁用"
    assert parse_command("随便聊聊") is None


def test_help_texts_cover_everything():
    overview = help_overview()
    for spec in COMMANDS:
        assert f"/{spec.name}" in overview
        assert spec.summary in help_for(spec.name)
    for word in ("图片", "QQ", "幻影坦克", "视频", "音频"):
        assert help_for(word)
    assert "FFmpeg" in help_overview(ffmpeg_ok=False)


def test_media_commands_have_examples():
    for spec in COMMANDS:
        assert spec.examples, f"{spec.name} 缺少示例"


def test_schema_matches_settings_dataclasses():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    names = setting_names()
    assert set(schema) == set(names)
    defaults = Settings()
    for group, fields in names.items():
        items = schema[group]["items"]
        assert set(items) == fields, group
        for key, item in items.items():
            assert "description" in item and "type" in item
            expected = getattr(getattr(defaults, group), key)
            assert item["default"] == expected, f"{group}.{key}"
            if "options" in item:
                assert item["default"] in item["options"]


def test_settings_clamp_and_fallback():
    s = Settings.load({
        "general": {"heavy_concurrency": 99, "task_timeout": "abc", "match_mode": "true"},
        "video": {"default_crf": 5, "preset": "bogus", "ffmpeg_path": ""},
        "qq": {"blocked_groups": "123, 456，123", "convert_group_mode": "x"},
        "mirage": {"w": 7},
    })
    assert s.general.heavy_concurrency == 8
    assert s.general.task_timeout == 180
    assert s.general.match_mode is True
    assert s.video.default_crf == 18
    assert s.video.preset == "veryfast"
    assert s.video.ffmpeg_path == "ffmpeg"
    assert s.qq.blocked_groups == ["123", "456"]
    assert s.qq.convert_group_mode == "private"
    assert s.mirage.w == 1.0
    assert Settings.load(None).image.enable is True


def test_metadata_and_requirements():
    meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert re.search(r"^name: multimodal_processor$", meta, re.M)
    assert re.search(r"^display_name: 多模态处理器$", meta, re.M)
    assert "https://github.com/yiyinfaith/astrbot_plugin_multimodal_processor" in meta
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for dep in ("pillow", "numpy", "aiohttp"):
        assert dep in reqs
    for banned in ("opencv", "requests", "openai"):
        assert banned not in reqs
    assert (ROOT / "logo.png").exists()


def test_no_leftover_meme_generators_or_old_names():
    banned = ["petpet", "behead", "摸头", "抽你", "操你", "pic_toolbox", "lbpcascade"]
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{path.name} 含有 {word}"
    assert not (ROOT / "resource").exists()
    assert get_spec("摸头") is None
