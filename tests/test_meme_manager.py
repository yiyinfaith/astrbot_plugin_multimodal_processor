from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from mmp.core.config import Settings
from mmp.processors.meme.manager import MemeManager


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
    image = Image.new("RGB", (1024, 1024), "red")
    raw = BytesIO(); image.save(raw, "PNG")
    compressed = MemeManager._compress(raw.getvalue())
    with Image.open(BytesIO(compressed)) as result:
        assert max(result.size) <= 512
