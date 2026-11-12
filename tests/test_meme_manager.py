from io import BytesIO
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from conftest import FakeEvent, comp, image, plain, run
from mmp.core.config import Settings
from mmp.core.errors import ProcessingError, UserError
from mmp.processors.meme.manager import MemeInputError, MemeManager


def png_bytes(size=(8, 8), color="red") -> bytes:
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "PNG")
    return raw.getvalue()


def py_meme(key, keywords, *, min_images=0, max_images=0, min_texts=0, max_texts=0, defaults=(), args_model=None, result=b"GIF89a-result"):
    calls = []

    class Meme:
        pass

    def call(self, *, images, texts, args):
        calls.append((images, texts, args))
        if isinstance(result, Exception):
            raise result
        return BytesIO(result)

    Meme.__call__ = call
    meme = Meme()
    meme.key = key
    meme.keywords = list(keywords)
    meme.tags = set()
    meme.calls = calls
    meme.params_type = SimpleNamespace(
        min_images=min_images, max_images=max_images, min_texts=min_texts, max_texts=max_texts,
        default_texts=list(defaults), args_type=SimpleNamespace(args_model=args_model) if args_model else None,
    )
    return meme


def manager_with(app, *memes, engine="python"):
    manager = app.memes
    manager.settings.enable = True
    manager.status.engine = engine
    manager.status.available = True
    manager.memes = list(memes)
    manager._rebuild_index()
    return manager


def stub_avatars(app, table=None):
    """头像不走网络：按 QQ 号生成不同的图片，返回 bytes -> 号码 的对照表。"""
    mapping = {}

    async def fetch_avatar(event, uid, slot):
        data = (table or {}).get(uid, png_bytes(color=(int(uid) % 256, 0, 0)))
        mapping[data] = uid
        return data

    app.memes._fetch_avatar = fetch_avatar
    return mapping


def onebot_event(messages, **kwargs):
    infos = {"1": {"nickname": "甲", "sex": "male"}, "2": {"nickname": "乙", "sex": "female"}}

    async def call_action(action, **params):
        assert action == "get_stranger_info"
        return infos.get(str(params["user_id"]), {"nickname": "某", "sex": "other"})

    event = FakeEvent(messages, platform="aiocqhttp", sender_id="1", group_id="42", **kwargs)
    event.bot = SimpleNamespace(api=SimpleNamespace(call_action=call_action))
    event.get_self_id = lambda: "99"
    return event


class BoolModel:
    model_fields = {
        "circle": SimpleNamespace(annotation=bool),
        "direction": SimpleNamespace(annotation=str),
        "user_infos": SimpleNamespace(annotation=list),
    }


def test_meme_settings_default_python_and_disabled():
    settings = Settings.load({"image": {"meme": {"enable": True}}})
    assert settings.meme.enable is True
    assert settings.meme.need_prefix is False
    assert settings.meme.disabled_for("123") == []


def test_engine_detection_and_static_compression():
    rust_meme = type("RustMeme", (), {"generate": lambda self: None})
    python_meme = type("PythonMeme", (), {"__call__": lambda self: None})
    assert MemeManager._is_rust(SimpleNamespace(Image=object, MemeSortBy=object, Meme=rust_meme)) is True
    assert MemeManager._is_rust(SimpleNamespace(Meme=python_meme, MemeParamsType=object)) is False
    picture = Image.new("RGB", (1024, 1024), "red")
    raw = BytesIO()
    picture.save(raw, "PNG")
    compressed = MemeManager._compress(raw.getvalue())
    with Image.open(BytesIO(compressed)) as result:
        assert max(result.size) <= 512


def test_output_suffix_follows_media_signature():
    assert MemeManager.suffix(b"GIF89a") == ".gif"
    assert MemeManager.suffix(b"\x89PNG\r\n\x1a\n") == ".png"
    assert MemeManager.suffix(b"\xff\xd8\xff\xe0") == ".jpg"
    assert MemeManager.suffix(b"RIFFxxxxWEBP") == ".webp"
    assert MemeManager.suffix(b"unknown") == ".bin"


# ------------------------------------------------------------ 触发
def test_keyword_needs_a_boundary_on_both_sides(make_app):
    manager = manager_with(make_app(), py_meme("good_news", ["喜报"]), py_meme("play", ["一起玩"]))
    assert manager.match("喜报 好消息") == ("喜报", "好消息")
    assert manager.match("喜报") == ("喜报", "")
    assert manager.match("今天 喜报") == ("喜报", "今天")
    assert manager.match("今天 喜报 来了") == ("喜报", "今天 来了")
    assert manager.match("喜报来了") is None
    assert manager.match("今天喜报") is None
    assert manager.match("喜报123") is None


def test_literal_at_token_is_a_boundary_and_not_text(make_app):
    manager = manager_with(make_app(), py_meme("pet", ["摸"]))
    assert manager.match("@小明摸") == ("摸", "")
    assert manager.match("摸@小明") == ("摸", "")
    assert manager.match("@小明 摸 你好") == ("摸", "你好")


def test_longest_keyword_wins(make_app):
    manager = manager_with(make_app(), py_meme("a", ["摸"]), py_meme("b", ["摸 摸头"]))
    assert manager.match("摸 摸头 好") == ("摸 摸头", "好")


def test_at_component_counts_as_space_but_image_does_not(make_app, tmp_path):
    app = make_app()
    manager_with(app, py_meme("pet", ["摸"]))
    assert app.meme_text(FakeEvent([plain("你好"), comp("At", qq="2"), plain("摸")])) == "你好 摸"
    path = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(path)
    assert app.meme_text(FakeEvent([plain("你好"), image(path), plain("摸")])) == "你好摸"


def test_at_glued_message_triggers_through_passive(make_app):
    app = make_app()
    stub_avatars(app)
    meme = py_meme("pet", ["摸"], min_images=1, max_images=1)
    manager_with(app, meme)
    event = onebot_event([plain("今天"), comp("At", qq="2"), plain("摸")], is_at_or_wake_command=False)
    run(app.handle_passive(event))
    assert len(meme.calls) == 1 and app.sender.kinds() == ["image"]
    assert not event.stopped


def test_prefixed_message_only_triggers_at_start(make_app):
    app = make_app()
    meme = py_meme("good_news", ["喜报"], max_texts=2)
    manager_with(app, meme)
    other = FakeEvent([plain("/别的指令 喜报")], message_str="别的指令 喜报")
    run(app.handle_passive(other))
    assert meme.calls == [] and not other.stopped
    mine = FakeEvent([plain("/喜报 好")], message_str="喜报 好")
    run(app.handle_passive(mine))
    assert len(meme.calls) == 1


def test_keyword_after_unprefixed_command_word_still_triggers(make_app):
    app = make_app()
    meme = py_meme("good_news", ["喜报"], max_texts=2)
    manager_with(app, meme)
    run(app.handle_passive(FakeEvent([plain("转换 喜报")], message_str="转换 喜报", is_at_or_wake_command=False)))
    assert len(meme.calls) == 1


def test_unknown_tag_falls_back_without_touching_files(make_app, tmp_path, monkeypatch):
    package = tmp_path / "meme_generator"
    (package / "memes").mkdir(parents=True)
    tags_source = "class MemeTags:\n    known = {'已知'}\n"
    (package / "tags.py").write_text(tags_source, encoding="utf-8")
    (package / "__init__.py").write_text(
        "from meme_generator.tags import MemeTags\n"
        "class Meme:\n"
        "    key = 'x'\n"
        "    keywords = ['额外']\n"
        "    tags = MemeTags.not_declared | MemeTags.known\n"
        "def get_memes():\n"
        "    return [Meme()]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("meme_generator", "meme_generator.tags"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    app = make_app({"image": {"meme": {"enable": True}}})
    run(app.memes.initialize())
    try:
        assert app.memes.status.available, app.memes.status.reason
        assert app.memes.memes[0].tags == {"已知"}
        assert app.memes.match("额外") == ("额外", "")
        assert (package / "tags.py").read_text(encoding="utf-8") == tags_source
        assert not any(type(f).__name__ == "_TagsFinder" for f in sys.meta_path), "导入后要移除钩子"
    finally:
        for name in ("meme_generator", "meme_generator.tags"):
            sys.modules.pop(name, None)


def test_folder_name_is_not_a_trigger_but_can_be_looked_up(make_app):
    meme = py_meme("play", ["一起玩"])
    manager = manager_with(make_app(), meme)
    assert manager.match("play games") is None
    assert manager.find("play") is meme
    assert manager.search_pairs("pla") == []
    assert manager.search_pairs("玩") == [("一起玩", "play")]


def test_meme_without_keywords_falls_back_to_folder_name(make_app):
    manager = manager_with(make_app(), py_meme("custom_meme", []))
    assert manager.match("custom_meme") == ("custom_meme", "")


def test_extra_prefix_is_not_used_as_text(make_app):
    app = make_app({"image": {"meme": {"extra_prefix": "#"}}})
    meme = py_meme("good_news", ["喜报"], min_texts=1, max_texts=1)
    manager = manager_with(app, meme)
    event = FakeEvent([plain("#喜报 好消息")], is_at_or_wake_command=False)
    assert run(manager.handle_dynamic(event, "#喜报 好消息", explicit=False)) is True
    assert meme.calls[0][1] == ["好消息"]
    assert manager.match("喜报 好消息") is None


def test_keyword_repeated_in_text_is_kept(make_app):
    app = make_app()
    meme = py_meme("good_news", ["喜报"], min_texts=1, max_texts=2)
    manager = manager_with(app, meme)
    run(manager.handle_dynamic(FakeEvent([plain("喜报 喜报 来了")]), "喜报 喜报 来了"))
    assert meme.calls[0][1] == ["喜报", "来了"]


def test_generated_result_is_image_only(make_app):
    app = make_app()
    manager = manager_with(app, py_meme("good_news", ["喜报"], max_texts=1))
    event = FakeEvent([plain("喜报 好")], is_at_or_wake_command=False)
    assert run(manager.handle_dynamic(event, "喜报 好", explicit=False)) is True
    assert app.sender.kinds() == ["image"]
    assert app.sender.calls[0][2] is None
    assert app.sender.calls[0][1].suffix == ".gif"


# ------------------------------------------------------------ 文字与选项
def test_user_text_wins_over_quoted_text(make_app):
    app = make_app()
    meme = py_meme("good_news", ["喜报"], min_texts=1, max_texts=1)
    manager = manager_with(app, meme)
    reply = comp("Reply", chain=[plain("别人说的话")], id="1")
    run(manager.handle_dynamic(FakeEvent([reply, plain("喜报 我的文字")]), "喜报 我的文字"))
    run(manager.handle_dynamic(FakeEvent([reply, plain("喜报")]), "喜报"))
    assert [c[1] for c in meme.calls] == [["我的文字"], ["别人说的话"]]


def test_default_texts_are_positional(make_app):
    params = SimpleNamespace(min_texts=2, max_texts=2, default_texts=["默认一", "默认二"])
    assert MemeManager._fit_texts(["我的"], params) == ["我的", "默认二"]
    assert MemeManager._fit_texts([], params) == ["默认一", "默认二"]
    optional = SimpleNamespace(min_texts=0, max_texts=2, default_texts=["默认一", "默认二"])
    assert MemeManager._fit_texts(["我的"], optional) == ["我的"], "用户写了文字时不追加默认文字"


def test_option_values_do_not_become_text(make_app):
    app = make_app()
    meme = py_meme("m", ["表情"], max_texts=3, args_model=BoolModel)
    manager = manager_with(app, meme)
    text = "表情 甲 --direction left --circle 乙 --不是选项"
    run(manager.handle_dynamic(FakeEvent([plain(text)]), text))
    _, texts, options = meme.calls[0]
    assert texts == ["甲", "乙", "--不是选项"]
    assert options == {"direction": "left", "circle": True}


# ------------------------------------------------------------ 图片
def test_sender_goes_first_when_one_image_is_missing(make_app):
    app = make_app()
    mapping = stub_avatars(app)
    meme = py_meme("kiss", ["亲"], min_images=2, max_images=2)
    manager = manager_with(app, meme)
    event = onebot_event([plain("亲 "), comp("At", qq="2")])
    run(manager.handle_dynamic(event, "亲"))
    images, _, options = meme.calls[0]
    assert [mapping[i] for i in images] == ["1", "2"]
    assert [i["name"] for i in options["user_infos"]] == ["甲", "乙"]


def test_avatars_only_fill_up_to_minimum(make_app):
    app = make_app()
    mapping = stub_avatars(app)
    meme = py_meme("repeat", ["重复"], min_images=1, max_images=5)
    manager = manager_with(app, meme)
    run(manager.handle_dynamic(onebot_event([plain("重复")]), "重复"))
    assert [mapping[i] for i in meme.calls[0][0]] == ["1"]


def test_unknown_gender_is_normalized(make_app):
    app = make_app()
    stub_avatars(app)
    meme = py_meme("m", ["表情"])
    manager = manager_with(app, meme)
    event = onebot_event([plain("表情")], is_at_or_wake_command=False)
    event.sender_id = "7"
    run(manager.handle_dynamic(event, "表情", explicit=False))
    assert meme.calls[0][2]["user_infos"] == [{"name": "某", "gender": "unknown"}]


def test_oversized_image_is_rejected_before_decoding(make_app, tmp_path):
    app = make_app({"image": {"basic": {"max_megapixels": 1}}})
    meme = py_meme("m", ["表情"], min_images=1, max_images=1)
    manager = manager_with(app, meme)
    path = tmp_path / "big.png"
    Image.new("RGB", (2000, 2000), "red").save(path)
    with pytest.raises(UserError, match="分辨率"):
        run(manager.handle_dynamic(FakeEvent([image(path), plain("表情")]), "表情"))
    assert meme.calls == []


# ------------------------------------------------------------ 错误与放行
def test_plain_chat_with_missing_params_is_silent_and_not_stopped(make_app):
    app = make_app()
    manager = manager_with(app, py_meme("m", ["今天"], min_texts=2, max_texts=2))
    event = FakeEvent([plain("今天")], is_at_or_wake_command=False)
    assert run(manager.handle_dynamic(event, "今天", explicit=False)) is False
    assert app.sender.calls == []


def test_explicit_call_gets_a_friendly_error(make_app):
    app = make_app()
    manager_with(app, py_meme("m", ["今天"], min_images=1, max_images=1))
    event = FakeEvent([plain("/今天")], message_str="今天")
    run(app.handle_passive(event))
    assert len(app.sender.texts()) == 1 and "1 张图片" in app.sender.texts()[0]
    assert not event.stopped, "回复后不停止事件，后续插件照常处理"


def test_library_errors_are_translated(make_app):
    class MemeGeneratorException(Exception):
        def __init__(self, message):
            self.message = message

    class TextOverLength(MemeGeneratorException):
        pass

    class Other(MemeGeneratorException):
        pass

    def raiser(exc):
        def func():
            raise exc
        return func

    manager = manager_with(make_app())
    with pytest.raises(MemeInputError, match="过长"):
        run(manager._call(raiser(TextOverLength("文本过长"))))
    with pytest.raises(ProcessingError, match="坏了"):
        run(manager._call(raiser(Other("坏了"))))
    with pytest.raises(ProcessingError, match="素材缺失"):
        run(manager._call(raiser(FileNotFoundError("x.png"))))
    with pytest.raises(ValueError):
        run(manager._call(raiser(ValueError("bug"))))


@pytest.mark.parametrize("name,fields,expected", [
    ("ImageNumberMismatch", {"min": 1, "max": 2, "actual": 0}, "图片数量"),
    ("TextNumberMismatch", {"min": 1, "max": 1, "actual": 0}, "文字数量"),
    ("TextOverLength", {"text": "很长"}, "过长"),
    ("MemeFeedback", {"feedback": "换一张"}, "换一张"),
])
def test_rust_error_objects_are_input_errors(name, fields, expected):
    with pytest.raises(MemeInputError, match=expected):
        MemeManager._as_bytes(type(name, (), fields)())


def test_rust_missing_asset_message():
    with pytest.raises(ProcessingError, match="素材缺失"):
        MemeManager._as_bytes(type("ImageAssetMissing", (), {"path": "a.png"})())


# ------------------------------------------------------------ 禁用
def test_group_rules_from_webui_strings():
    s = Settings.load({"image": {"meme": {"disabled_list": ["全局"], "group_disabled_map": ["123:摸,喜报", "456：抱", "错误项"]}}}).meme
    assert s.disabled_for("123") == ["全局", "摸", "喜报"]
    assert s.disabled_for("456") == ["全局", "抱"]
    assert s.disabled_for("789") == ["全局"]
    assert s.disabled_for("") == ["全局"]
    legacy = Settings.load({"image": {"meme": {"group_disabled_map": [{"scope": "group:1", "memes": ["摸"]}]}}}).meme
    assert legacy.disabled_for("1") == ["摸"]


def test_disabled_meme_passes_through_and_covers_aliases(make_app):
    app = make_app({"image": {"meme": {"disabled_list": ["摸"]}}})
    meme = py_meme("pet", ["摸", "摸摸"])
    manager = manager_with(app, meme)
    event = FakeEvent([plain("摸摸")], group_id="42", is_at_or_wake_command=False)
    assert run(manager.handle_dynamic(event, "摸摸", explicit=False)) is False
    assert meme.calls == []


def test_disabled_by_folder_name(make_app):
    app = make_app({"image": {"meme": {"disabled_list": ["pet"]}}})
    meme = py_meme("pet", ["摸"])
    manager = manager_with(app, meme)
    assert run(manager.handle_dynamic(FakeEvent([plain("摸")]), "摸")) is False


# ------------------------------------------------------------ Rust 适配
def rust_app(make_app):
    class Option:
        def __init__(self, name):
            self.name = name

    bool_opt = type("BooleanOption", (Option,), {})
    int_opt = type("IntegerOption", (Option,), {})
    str_opt = type("StringOption", (Option,), {})
    calls = []

    class Meme:
        key = "rs"

        def generate(self, images, texts, options):
            assert all(isinstance(v, (bool, str, int, float)) for v in options.values()), options
            calls.append((images, texts, options))
            return b"GIF89a"

    meme = Meme()
    meme.info = SimpleNamespace(
        key="rs", keywords=["表情"], tags=set(),
        params=SimpleNamespace(min_images=0, max_images=0, min_texts=0, max_texts=1, default_texts=[],
                               options=[bool_opt("circle"), int_opt("count"), str_opt("name")]),
    )
    app = make_app()
    manager = manager_with(app, meme, engine="rust")
    manager.package = SimpleNamespace(Image=lambda name, data: (name, data))
    return app, manager, calls


def test_rust_options_are_scalar_and_typed(make_app):
    app, manager, calls = rust_app(make_app)
    text = "表情 --count 3 --circle"
    run(manager.handle_dynamic(onebot_event([plain(text)]), text))
    assert calls[0][2] == {"count": 3, "circle": True, "name": "甲"}


def test_rust_bad_number_is_input_error(make_app):
    app, manager, calls = rust_app(make_app)
    with pytest.raises(MemeInputError, match="数字"):
        run(manager.handle_dynamic(FakeEvent([plain("表情 --count 多")]), "表情 --count 多"))
    assert calls == []


# ------------------------------------------------------------ 开关与指令
def test_meme_commands_follow_their_own_switch(make_app):
    app = make_app({"image": {"basic": {"enable": False}, "meme": {"enable": True}}})
    manager_with(app, py_meme("play", ["一起玩"]))
    event = FakeEvent([plain("/meme搜索 玩")], message_str="meme搜索 玩")
    run(app.handle_command(event, "meme搜索"))
    assert app.sender.texts() == ["1. 一起玩  (play)"]


def test_meme_commands_disabled_by_meme_switch(make_app):
    app = make_app()
    event = FakeEvent([plain("/meme列表")], message_str="meme列表")
    run(app.handle_command(event, "meme列表"))
    assert "关闭" in app.sender.texts()[0]


def test_search_output_is_capped(make_app):
    app = make_app({"image": {"meme": {"enable": True}}})
    manager_with(app, *[py_meme(f"k{i}", [f"词{i:03d}"]) for i in range(150)])
    event = FakeEvent([plain("/meme搜索 词")], message_str="meme搜索 词")
    run(app.handle_command(event, "meme搜索"))
    lines = app.sender.texts()[0].splitlines()
    assert len(lines) == 101 and "共 150 条" in lines[-1]


def test_list_is_rendered_inside_a_task_and_named_by_real_format(make_app):
    app = make_app({"image": {"meme": {"enable": True}}})
    manager = manager_with(app, py_meme("play", ["一起玩"]))

    async def render():
        assert app.tasks._light.locked() or app.tasks._user_tasks, "渲染应占用任务名额"
        return b"\xff\xd8\xff\xe0jpeg"

    manager.render_list = render
    run(app.handle_command(FakeEvent([plain("/meme列表")], message_str="meme列表"), "meme列表"))
    assert app.sender.calls[0][1].suffix == ".jpg"


def test_detail_accepts_keyword_with_spaces(make_app):
    app = make_app({"image": {"meme": {"enable": True}}})
    meme = py_meme("m", ["两个 词"])
    meme.generate_preview = lambda: BytesIO(b"GIF89a")
    manager_with(app, meme)
    run(app.handle_command(FakeEvent([plain("/meme详情 两个 词")], message_str="meme详情 两个 词"), "meme详情"))
    assert app.sender.kinds() == ["image"] and "名称：m" in app.sender.calls[0][2]
