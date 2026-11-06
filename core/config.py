from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULTS: dict[str, Any] = {
    "enable_image": True, "enable_video": True, "enable_audio": True,
    "enable_mirage": True, "match_mode": False, "enable_at_avatar": True,
    "max_image_mb": 20, "max_video_mb": 200, "max_audio_mb": 50,
    "max_duration": 600, "process_timeout": 300, "heavy_concurrency": 1,
    "ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "gif_max_fps": 50,
    "gif_max_width": 1920, "gif_speed_allow_frame_drop": False,
    "mirage_timeout": 60, "mirage_max_mb": 10, "mirage_a": 0.5,
    "mirage_b": 20.0, "mirage_w": 0.1, "qq_private_file_first": True,
    "qq_group_file_enabled": False, "qq_blocklist": "", "debug": False,
}


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, result))


@dataclass(slots=True)
class Settings:
    enable_image: bool = True
    enable_video: bool = True
    enable_audio: bool = True
    enable_mirage: bool = True
    match_mode: bool = False
    enable_at_avatar: bool = True
    max_image_mb: int = 20
    max_video_mb: int = 200
    max_audio_mb: int = 50
    max_duration: int = 600
    process_timeout: int = 300
    heavy_concurrency: int = 1
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    gif_max_fps: int = 50
    gif_max_width: int = 1920
    gif_speed_allow_frame_drop: bool = False
    mirage_timeout: int = 60
    mirage_max_mb: int = 10
    mirage_a: float = 0.5
    mirage_b: float = 20.0
    mirage_w: float = 0.1
    qq_private_file_first: bool = True
    qq_group_file_enabled: bool = False
    qq_blocklist: str = ""
    debug: bool = False

    @classmethod
    def from_config(cls, config: Any | None) -> "Settings":
        values = dict(DEFAULTS)
        if config:
            try:
                values.update({k: config.get(k, v) for k, v in values.items()})
            except AttributeError:
                values.update({k: config[k] for k in values if k in config})
        values["max_image_mb"] = int(_number(values["max_image_mb"], 20, 1, 1024))
        values["max_video_mb"] = int(_number(values["max_video_mb"], 200, 1, 4096))
        values["max_audio_mb"] = int(_number(values["max_audio_mb"], 50, 1, 1024))
        values["max_duration"] = int(_number(values["max_duration"], 600, 1, 86400))
        values["process_timeout"] = int(_number(values["process_timeout"], 300, 5, 3600))
        values["heavy_concurrency"] = int(_number(values["heavy_concurrency"], 1, 1, 8))
        values["gif_max_fps"] = int(_number(values["gif_max_fps"], 50, 1, 100))
        values["gif_max_width"] = int(_number(values["gif_max_width"], 1920, 16, 8192))
        values["mirage_timeout"] = int(_number(values["mirage_timeout"], 60, 5, 3600))
        values["mirage_max_mb"] = int(_number(values["mirage_max_mb"], 10, 1, 1024))
        values["mirage_a"] = _number(values["mirage_a"], .5, .1, 3)
        values["mirage_b"] = _number(values["mirage_b"], 20, -50, 50)
        values["mirage_w"] = _number(values["mirage_w"], .1, 0, 1)
        for key in ("ffmpeg_path", "ffprobe_path", "qq_blocklist"):
            values[key] = str(values.get(key) or DEFAULTS[key])
        return cls(**{k: values[k] for k in cls.__dataclass_fields__})

    @property
    def blocked_groups(self) -> set[str]:
        return {x.strip() for x in self.qq_blocklist.split(",") if x.strip()}

