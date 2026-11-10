"""带英文的指令和参数，大小写都要生效。"""

from __future__ import annotations

import pytest

from conftest import FakeEvent, plain, run
from mmp.core.commands import COMMANDS, parse_command

ENGLISH_NAMES = [(spec, name) for spec in COMMANDS for name in spec.all_names if name.lower() != name.upper()]


def variants(name: str) -> list[str]:
    return [v for v in dict.fromkeys((name.lower(), name.upper(), name.swapcase(), name.capitalize())) if v != name]


def test_every_english_command_is_covered():
    names = {name for _, name in ENGLISH_NAMES}
    assert {"GIF加速", "MD列表", "Base64编码", "URL解码", "JSON格式化", "meme列表", "音频转MP3", "convert addblock"} <= names


@pytest.mark.parametrize("spec,name", ENGLISH_NAMES, ids=[name for _, name in ENGLISH_NAMES])
def test_all_case_variants_parse_to_the_same_command(spec, name):
    for variant in variants(name):
        parsed = parse_command(f"{variant} 参数")
        assert parsed is not None and parsed.spec is spec, variant
        assert parsed.args == (*spec.aliases.get(name, ()), "参数"), variant


@pytest.mark.parametrize("spec,name", ENGLISH_NAMES, ids=[name for _, name in ENGLISH_NAMES])
def test_case_variants_run_once_through_the_listener(make_app, spec, name):
    """AstrBot 的指令过滤器区分大小写，其他写法要由插件的监听器接住。"""
    app = make_app({"image": {"meme": {"enable": True}}})
    calls = []

    async def handler(inv):
        calls.append(inv.parsed.spec.name)
        return True

    app.handlers = {key: handler for key in app.handlers}
    for variant in variants(name):
        calls.clear()
        event = FakeEvent([plain(f"/{variant}")], message_str=variant, admin=True)
        run(app.handle_passive(event))
        assert calls == [spec.name], variant
        assert event.stopped, variant


def test_exact_case_is_left_to_astrbot_filter(make_app):
    app = make_app()
    event = FakeEvent([plain("/MD引用 你好")], message_str="MD引用 你好")
    run(app.handle_passive(event))
    assert app.sender.calls == [] and not event.stopped


@pytest.mark.parametrize("command", ["/md引用 你好", "/Md引用 你好", "/mD引用 你好"])
def test_text_body_is_extracted_for_any_case(make_app, command):
    app = make_app()
    run(app.handle_passive(FakeEvent([plain(command)], message_str=command[1:])))
    assert app.sender.texts() == ["> 你好"]


@pytest.mark.parametrize("word", ["pitch", "PITCH", "Pitch", "变调"])
def test_audio_speed_pitch_word_ignores_case(make_app, monkeypatch, word):
    import mmp.handlers.av as av

    async def run_audio(inv, op):
        return True

    monkeypatch.setattr(av, "run_audio", run_audio)
    app = make_app()
    text = f"音频调速 1.5 {word}"
    run(app.handle_command(FakeEvent([plain("/" + text)], message_str=text), "音频调速"))
    assert app.sender.texts() == []
