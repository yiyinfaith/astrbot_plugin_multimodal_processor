"""把 AstrBot 传入的嵌套配置整理成带默认值、已校验范围的设置对象。

_conf_schema.json 里每个分组对应这里的一个 dataclass，字段名保持一致。
越界数值会被收回到允许范围内，类型错误回退为默认值，不会让插件加载失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any


def _num(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return min(high, max(low, result))


def _int(value: Any, default: int, low: int, high: int) -> int:
    return int(_num(value, default, low, high))


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _str(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _choice(value: Any, default: str, options: set[str]) -> str:
    text = _str(value, default)
    return text if text in options else default


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


@dataclass(slots=True)
class GeneralSettings:
    enable_at_avatar: bool = True
    task_timeout: int = 180
    heavy_concurrency: int = 1
    light_concurrency: int = 2
    queue_limit: int = 5
    per_user_limit: int = 2
    download_timeout: int = 60
    allow_private_urls: bool = False
    extra_local_roots: list[str] = field(default_factory=list)
    temp_expire_minutes: int = 60
    debug: bool = False
    text_enable: bool = True
    text_max_chars: int = 20000
    text_file_threshold: int = 3000

    @classmethod
    def load(cls, raw: dict) -> "GeneralSettings":
        return cls(
            enable_at_avatar=_bool(raw.get("enable_at_avatar"), True),
            task_timeout=_int(raw.get("task_timeout"), 180, 10, 3600),
            heavy_concurrency=_int(raw.get("heavy_concurrency"), 1, 1, 8),
            light_concurrency=_int(raw.get("light_concurrency"), 2, 1, 16),
            queue_limit=_int(raw.get("queue_limit"), 5, 0, 100),
            per_user_limit=_int(raw.get("per_user_limit"), 2, 1, 20),
            download_timeout=_int(raw.get("download_timeout"), 60, 5, 600),
            allow_private_urls=_bool(raw.get("allow_private_urls"), False),
            extra_local_roots=_str_list(raw.get("extra_local_roots")),
            temp_expire_minutes=_int(raw.get("temp_expire_minutes"), 60, 5, 7 * 24 * 60),
            debug=_bool(raw.get("debug"), False),
            text_enable=_bool(raw.get("text_enable"), True),
            text_max_chars=_int(raw.get("text_max_chars"), 20000, 100, 1_000_000),
            text_file_threshold=_int(raw.get("text_file_threshold"), 3000, 200, 100_000),
        )


@dataclass(slots=True)
class ImageSettings:
    enable: bool = True
    max_mb: int = 20
    max_megapixels: int = 40
    max_frames: int = 600
    memory_mb: int = 512
    gif_max_fps: int = 50
    gif_speed_allow_frame_drop: bool = False
    gif_max_side: int = 1024
    default_quality: int = 85
    make_format: str = "gif"
    split_max_frames: int = 50
    multi_max_images: int = 30

    @classmethod
    def load(cls, raw: dict) -> "ImageSettings":
        return cls(
            enable=_bool(raw.get("enable"), True),
            max_mb=_int(raw.get("max_mb"), 20, 1, 512),
            max_megapixels=_int(raw.get("max_megapixels"), 40, 1, 500),
            max_frames=_int(raw.get("max_frames"), 600, 1, 10000),
            memory_mb=_int(raw.get("memory_mb"), 512, 32, 16384),
            gif_max_fps=_int(raw.get("gif_max_fps"), 50, 1, 100),
            gif_speed_allow_frame_drop=_bool(raw.get("gif_speed_allow_frame_drop"), False),
            gif_max_side=_int(raw.get("gif_max_side"), 1024, 16, 8192),
            default_quality=_int(raw.get("default_quality"), 85, 1, 100),
            make_format=_choice(raw.get("make_format"), "gif", {"gif", "webp", "apng"}),
            split_max_frames=_int(raw.get("split_max_frames"), 50, 1, 200),
            multi_max_images=_int(raw.get("multi_max_images"), 30, 2, 100),
        )

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024

    @property
    def max_pixels(self) -> int:
        return self.max_megapixels * 1_000_000

    @property
    def memory_bytes(self) -> int:
        return self.memory_mb * 1024 * 1024


@dataclass(slots=True)
class MemeSettings:
    """meme-generator 运行时设置，不在主 requirements 中强制重装。"""

    enable: bool = False
    need_prefix: bool = False
    extra_prefix: str = ""
    compress_image: bool = True
    timeout: int = 15
    disabled_list: list[str] = field(default_factory=list)
    group_disabled_map: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, raw: dict) -> "MemeSettings":
        return cls(
            enable=_bool(raw.get("enable"), False),
            need_prefix=_bool(raw.get("need_prefix"), False),
            extra_prefix=_str(raw.get("extra_prefix"), "") if raw.get("extra_prefix") else "",
            compress_image=_bool(raw.get("compress_image"), True),
            timeout=_int(raw.get("timeout"), 15, 1, 300),
            disabled_list=_str_list(raw.get("disabled_list")),
            group_disabled_map=_group_rules(raw.get("group_disabled_map")),
        )

    def disabled_for(self, group_id: str | None) -> list[str]:
        """全局禁用 + 该群禁用的关键词。"""
        result = list(self.disabled_list)
        if group_id:
            for item in self.group_disabled_map:
                if item["scope"] == group_id:
                    result.extend(m for m in item["memes"] if m not in result)
        return result


def _group_rules(value: Any) -> list[dict]:
    """配置界面的列表项是字符串「群号:关键词1,关键词2」；也兼容字典写法。"""
    pairs: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                pairs.append((item.get("scope"), item.get("memes")))
            elif isinstance(item, str):
                scope, _, memes = item.replace("：", ":").strip().removeprefix("group:").partition(":")
                pairs.append((scope, memes))
    rules: list[dict] = []
    for scope, memes in pairs:
        group_id = str(scope or "").strip().removeprefix("group:")
        keywords = _str_list(memes)
        if group_id and keywords:
            rules.append({"scope": group_id, "memes": keywords})
    return rules


QQ_CONVERT_MODES = {"private", "group", "private_then_group"}


@dataclass(slots=True)
class QQSettings:
    enable_convert: bool = True
    enable_rename: bool = True
    enable_mface: bool = True
    convert_group_mode: str = "private"
    blocked_groups: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, raw: dict) -> "QQSettings":
        return cls(
            enable_convert=_bool(raw.get("enable_convert"), True),
            enable_rename=_bool(raw.get("enable_rename"), True),
            enable_mface=_bool(raw.get("enable_mface"), True),
            convert_group_mode=_choice(raw.get("convert_group_mode"), "private", QQ_CONVERT_MODES),
            blocked_groups=_str_list(raw.get("blocked_groups")),
        )


@dataclass(slots=True)
class MirageSettings:
    enable: bool = True
    timeout: int = 60
    max_mb: int = 10
    max_side: int = 2048
    gray_front_ratio: float = 0.5
    gray_back_ratio: float = 0.5
    a: float = 0.5
    b: float = 20.0
    w: float = 0.1

    @classmethod
    def load(cls, raw: dict) -> "MirageSettings":
        return cls(
            enable=_bool(raw.get("enable"), True),
            timeout=_int(raw.get("timeout"), 60, 5, 3600),
            max_mb=_int(raw.get("max_mb"), 10, 1, 512),
            max_side=_int(raw.get("max_side"), 2048, 64, 8192),
            gray_front_ratio=_num(raw.get("gray_front_ratio"), 0.5, 0.1, 1.0),
            gray_back_ratio=_num(raw.get("gray_back_ratio"), 0.5, 0.1, 1.0),
            a=_num(raw.get("a"), 0.5, 0.1, 3.0),
            b=_num(raw.get("b"), 20.0, -50.0, 50.0),
            w=_num(raw.get("w"), 0.1, 0.0, 1.0),
        )

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024


@dataclass(slots=True)
class PrismSettings:
    enable: bool = True
    timeout: int = 60
    max_mb: int = 10
    max_side: int = 1200
    colored: bool = True
    inner_limit: int = 32
    cover_limit: int = 64
    rate: int = 2

    @classmethod
    def load(cls, raw: dict) -> "PrismSettings":
        inner = _int(raw.get("inner_limit"), 32, 4, 120)
        cover = _int(raw.get("cover_limit"), 64, 8, 250)
        if cover <= inner:  # 表图色阶端点必须高于里图，否则回退默认值
            inner, cover = 32, 64
        return cls(
            enable=_bool(raw.get("enable"), True),
            timeout=_int(raw.get("timeout"), 60, 5, 3600),
            max_mb=_int(raw.get("max_mb"), 10, 1, 512),
            max_side=_int(raw.get("max_side"), 1200, 64, 8192),
            colored=_bool(raw.get("colored"), True),
            inner_limit=inner,
            cover_limit=cover,
            rate=_int(raw.get("rate"), 2, 2, 8),
        )

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024


SEND_MODES = {"auto", "media", "file"}
PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"}


@dataclass(slots=True)
class VideoSettings:
    enable: bool = True
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    max_mb: int = 100
    max_duration: int = 300
    max_input_side: int = 3840
    max_output_side: int = 1920
    default_crf: int = 28
    preset: str = "veryfast"
    threads: int = 2
    memory_mb: int = 1536
    gif_fps: int = 12
    gif_width: int = 480
    gif_max_duration: int = 15
    send_mode: str = "auto"

    @classmethod
    def load(cls, raw: dict) -> "VideoSettings":
        return cls(
            enable=_bool(raw.get("enable"), True),
            ffmpeg_path=_str(raw.get("ffmpeg_path"), "ffmpeg"),
            ffprobe_path=_str(raw.get("ffprobe_path"), "ffprobe"),
            max_mb=_int(raw.get("max_mb"), 100, 1, 4096),
            max_duration=_int(raw.get("max_duration"), 300, 1, 6 * 3600),
            max_input_side=_int(raw.get("max_input_side"), 3840, 64, 16384),
            max_output_side=_int(raw.get("max_output_side"), 1920, 64, 8192),
            default_crf=_int(raw.get("default_crf"), 28, 18, 40),
            preset=_choice(raw.get("preset"), "veryfast", PRESETS),
            threads=_int(raw.get("threads"), 2, 0, 64),
            memory_mb=_int(raw.get("memory_mb"), 1536, 0, 65536),
            gif_fps=_int(raw.get("gif_fps"), 12, 1, 50),
            gif_width=_int(raw.get("gif_width"), 480, 16, 4096),
            gif_max_duration=_int(raw.get("gif_max_duration"), 15, 1, 300),
            send_mode=_choice(raw.get("send_mode"), "auto", SEND_MODES),
        )

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024


@dataclass(slots=True)
class AudioSettings:
    enable: bool = True
    max_mb: int = 50
    max_duration: int = 1800
    default_bitrate: int = 128
    send_mode: str = "auto"

    @classmethod
    def load(cls, raw: dict) -> "AudioSettings":
        return cls(
            enable=_bool(raw.get("enable"), True),
            max_mb=_int(raw.get("max_mb"), 50, 1, 2048),
            max_duration=_int(raw.get("max_duration"), 1800, 1, 24 * 3600),
            default_bitrate=_int(raw.get("default_bitrate"), 128, 16, 320),
            send_mode=_choice(raw.get("send_mode"), "auto", SEND_MODES),
        )

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024


USER_LEVELS = {"all", "group_admin", "admin"}
AT_ALL_LEVELS = {"off", "group_admin", "admin"}
CROSS_CHAT_LEVELS = {"off", "admin", "member"}
NICKNAME_MODES = {"card", "nickname", "id"}


@dataclass(slots=True)
class MessageSettings:
    enable: bool = True
    permission: str = "all"
    cooldown: int = 3
    enable_send: bool = True
    enable_card: bool = True
    enable_at: bool = True
    at_all_permission: str = "admin"
    enable_forward: bool = True
    cross_chat_permission: str = "admin"
    enable_merge: bool = True
    enable_simulate: bool = True
    simulate_permission: str = "all"
    simulate_mark: bool = True
    protected_ids: list[str] = field(default_factory=list)
    nickname_mode: str = "card"
    forward_title: str = ""
    max_nodes: int = 30
    max_chars: int = 2000
    max_segments: int = 20

    @classmethod
    def load(cls, raw: dict) -> "MessageSettings":
        return cls(
            enable=_bool(raw.get("enable"), True),
            permission=_choice(raw.get("permission"), "all", USER_LEVELS),
            cooldown=_int(raw.get("cooldown"), 3, 0, 600),
            enable_send=_bool(raw.get("enable_send"), True),
            enable_card=_bool(raw.get("enable_card"), True),
            enable_at=_bool(raw.get("enable_at"), True),
            at_all_permission=_choice(raw.get("at_all_permission"), "admin", AT_ALL_LEVELS),
            enable_forward=_bool(raw.get("enable_forward"), True),
            cross_chat_permission=_choice(raw.get("cross_chat_permission"), "admin", CROSS_CHAT_LEVELS),
            enable_merge=_bool(raw.get("enable_merge"), True),
            enable_simulate=_bool(raw.get("enable_simulate"), True),
            simulate_permission=_choice(raw.get("simulate_permission"), "all", USER_LEVELS),
            simulate_mark=_bool(raw.get("simulate_mark"), True),
            protected_ids=[x for x in _str_list(raw.get("protected_ids")) if x.isdigit()],
            nickname_mode=_choice(raw.get("nickname_mode"), "card", NICKNAME_MODES),
            forward_title=(_str(raw.get("forward_title"), "") if raw.get("forward_title") else "")[:40],
            max_nodes=_int(raw.get("max_nodes"), 30, 1, 100),
            max_chars=_int(raw.get("max_chars"), 2000, 10, 20000),
            max_segments=_int(raw.get("max_segments"), 20, 1, 100),
        )


@dataclass(slots=True)
class Settings:
    general: GeneralSettings = field(default_factory=GeneralSettings)
    image: ImageSettings = field(default_factory=ImageSettings)
    qq: QQSettings = field(default_factory=QQSettings)
    mirage: MirageSettings = field(default_factory=MirageSettings)
    prism: PrismSettings = field(default_factory=PrismSettings)
    video: VideoSettings = field(default_factory=VideoSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    meme: MemeSettings = field(default_factory=MemeSettings)
    message: MessageSettings = field(default_factory=MessageSettings)

    @classmethod
    def load(cls, config: Any | None) -> "Settings":
        def group(name: str) -> dict:
            if not config:
                return {}
            try:
                value = config.get(name)
            except AttributeError:
                return {}
            return value if isinstance(value, dict) else {}

        image_raw = group("image")
        return cls(
            general=GeneralSettings.load({**group("general"), **{"text_" + k: v for k, v in group("text").items()}}),
            image=ImageSettings.load({**_sub(image_raw, "basic"), **_sub(image_raw, "gif")}),
            qq=QQSettings.load(_sub(image_raw, "qq")),
            mirage=MirageSettings.load(_sub(image_raw, "mirage")),
            prism=PrismSettings.load(_sub(image_raw, "prism")),
            video=VideoSettings.load(group("video")),
            audio=AudioSettings.load(group("audio")),
            meme=MemeSettings.load(_sub(image_raw, "meme")),
            message=MessageSettings.load(group("message")),
        )


# 「图片处理」分组在配置界面里再分成几个小框：基础、动图、QQ、幻影坦克、光棱坦克
IMAGE_BOXES = {"basic": "image", "gif": "image", "qq": "qq", "mirage": "mirage", "prism": "prism", "meme": "meme"}
GIF_FIELDS = {"gif_max_fps", "gif_speed_allow_frame_drop", "gif_max_side", "make_format", "split_max_frames", "multi_max_images"}


def _sub(raw: dict, key: str) -> dict:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value if isinstance(value, dict) else {}


def setting_names() -> dict[str, dict[str, tuple[str, str]]]:
    """配置界面每个分组的键（小框里的写成「框.键」）-> (设置对象, 字段名)，供测试核对 _conf_schema.json。"""
    image: dict[str, tuple[str, str]] = {}
    for f in fields(ImageSettings):
        box = "gif" if f.name in GIF_FIELDS else "basic"
        image[f"{box}.{f.name}"] = ("image", f.name)
    for box, cls in (("qq", QQSettings), ("mirage", MirageSettings), ("prism", PrismSettings), ("meme", MemeSettings)):
        image.update({f"{box}.{f.name}": (box, f.name) for f in fields(cls)})
    return {
        "general": {f.name: ("general", f.name) for f in fields(GeneralSettings) if not f.name.startswith("text_")},
        "image": image,
        "text": {f.name[5:]: ("general", f.name) for f in fields(GeneralSettings) if f.name.startswith("text_")},
        "video": {f.name: ("video", f.name) for f in fields(VideoSettings)},
        "audio": {f.name: ("audio", f.name) for f in fields(AudioSettings)},
        "message": {f.name: ("message", f.name) for f in fields(MessageSettings)},
    }
