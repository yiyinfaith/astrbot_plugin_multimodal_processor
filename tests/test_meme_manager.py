from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from mmp.core.config import Settings
from mmp.processors.meme.manager import MemeManager


def test_meme_settings_default_python_and_disabled():
    settings = Settings.load({"image": {"meme": {"enable": True}}})
    assert settings.meme.enable is True
    assert settings.meme.engine == "python"
    assert settings.meme.disabled_for("123") == []


def test_engine_detection_and_static_compression():
    assert MemeManager._is_rust(SimpleNamespace(Image=object, MemeSortBy=object), "0.2.3") is True
    assert MemeManager._is_rust(SimpleNamespace(), "0.1.14") is False
    image = Image.new("RGB", (1024, 1024), "red")
    raw = BytesIO(); image.save(raw, "PNG")
    compressed = MemeManager._compress(raw.getvalue())
    with Image.open(BytesIO(compressed)) as result:
        assert max(result.size) <= 512
