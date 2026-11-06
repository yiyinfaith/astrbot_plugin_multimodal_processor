"""指令参数的解析与校验：时间、尺寸、倍率、码率等。

所有函数在输入非法时抛出 UsageError，调用方统一回复用法提示。
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .errors import UsageError

_FULLWIDTH = str.maketrans("０１２３４５６７８９．：，×Ｘｘ％", "0123456789.:,xxx%")


def normalize(text: str) -> str:
    return text.translate(_FULLWIDTH).strip()


def parse_float(text: str, name: str, low: float, high: float) -> float:
    raw = normalize(text).lower().rstrip("x倍")
    try:
        value = float(raw)
    except ValueError as exc:
        raise UsageError(f"{name}必须是数字，收到：{text}") from exc
    if value != value or not (low <= value <= high):
        raise UsageError(f"{name}范围是 {low:g}～{high:g}，收到：{text}")
    return value


def parse_int(text: str, name: str, low: int, high: int) -> int:
    raw = normalize(text)
    if not re.fullmatch(r"[+-]?\d+", raw):
        raise UsageError(f"{name}必须是整数，收到：{text}")
    value = int(raw)
    if not (low <= value <= high):
        raise UsageError(f"{name}范围是 {low}～{high}，收到：{text}")
    return value


_CN_TIME = re.compile(r"^(?:(\d+(?:\.\d+)?)(?:小时|时|h))?(?:(\d+(?:\.\d+)?)(?:分钟|分|m|min))?(?:(\d+(?:\.\d+)?)(?:秒|s)?)?$")


def parse_time(text: str, name: str = "时间") -> float:
    """解析时间点：支持 `90`、`1.5`、`1:30`、`01:02:03.5`、`1分30秒`、`2m5s`。"""
    raw = normalize(text).lower().replace("：", ":")
    if not raw:
        raise UsageError(f"{name}不能为空")
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
            raise UsageError(f"{name}格式不正确：{text}（示例 1:30 或 00:01:30）")
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    match = _CN_TIME.fullmatch(raw)
    if not match or not any(match.groups()):
        raise UsageError(f"{name}格式不正确：{text}（示例 90、1:30、1分30秒）")
    hours, minutes, secs = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + secs


def parse_range(start_text: str, end_text: str) -> tuple[float, float]:
    start = parse_time(start_text, "开始时间")
    end = parse_time(end_text, "结束时间")
    if end <= start:
        raise UsageError("结束时间必须晚于开始时间")
    return start, end


def format_seconds(value: float | None) -> str:
    if value is None:
        return "未知"
    value = max(0.0, float(value))
    minutes, seconds = divmod(value, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:05.2f}"
    if minutes:
        return f"{minutes}:{seconds:05.2f}"
    return f"{seconds:.2f} 秒"


def format_bytes(size: int | float | None) -> str:
    if size is None:
        return "未知"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


@dataclass(frozen=True, slots=True)
class SizeSpec:
    """目标尺寸：width/height 为 None 表示按比例自动计算；percent 为百分比；short 为短边（720p 写法）。"""

    width: int | None = None
    height: int | None = None
    percent: float | None = None
    short: int | None = None

    def resolve(self, src_w: int, src_h: int) -> tuple[int, int]:
        if self.short is not None:
            if src_w >= src_h:
                w, h = src_w * self.short / src_h, self.short
            else:
                w, h = self.short, src_h * self.short / src_w
        elif self.percent is not None:
            w = round(src_w * self.percent / 100)
            h = round(src_h * self.percent / 100)
        elif self.width and self.height:
            w, h = self.width, self.height
        elif self.width:
            w = self.width
            h = round(src_h * self.width / src_w)
        else:
            assert self.height
            h = self.height
            w = round(src_w * self.height / src_h)
        return max(1, int(round(w))), max(1, int(round(h)))


_P_SIZES = {"240p": 240, "360p": 360, "480p": 480, "540p": 540, "720p": 720, "1080p": 1080, "1440p": 1440, "2k": 1440, "4k": 2160, "2160p": 2160}


def parse_size(text: str, max_side: int = 16384) -> SizeSpec:
    """`800x600`、`800`（宽，等比）、`x600`（高，等比）、`50%`、`720p`（短边）。"""
    raw = normalize(text).lower().replace("*", "x")
    if raw in _P_SIZES:
        return SizeSpec(short=_P_SIZES[raw])
    if raw.endswith("%"):
        percent = parse_float(raw[:-1], "缩放比例", 1, 1000)
        return SizeSpec(percent=percent)
    match = re.fullmatch(r"(\d{1,5})?x(\d{1,5})?", raw)
    if match and any(match.groups()):
        w = int(match.group(1)) if match.group(1) else None
        h = int(match.group(2)) if match.group(2) else None
    elif re.fullmatch(r"\d{1,5}", raw):
        w, h = int(raw), None
    else:
        raise UsageError(f"尺寸格式不正确：{text}（示例 800x600、800、x600、50%、720p）")
    for value in (w, h):
        if value is not None and not (1 <= value <= max_side):
            raise UsageError(f"尺寸范围是 1～{max_side} 像素")
    return SizeSpec(width=w, height=h)


def parse_angle(text: str) -> int:
    raw = normalize(text).lower().rstrip("°度")
    aliases = {"顺时针": 90, "逆时针": 270, "左": 270, "右": 90, "倒": 180}
    if raw in aliases:
        return aliases[raw]
    try:
        value = int(raw)
    except ValueError as exc:
        raise UsageError(f"角度只能是 90、180、270 或 -90，收到：{text}") from exc
    value %= 360
    if value not in (90, 180, 270):
        raise UsageError(f"角度只能是 90、180、270 或 -90，收到：{text}")
    return value


def parse_bitrate(text: str, low: int = 16, high: int = 320) -> int:
    """返回 kbps。接受 `128`、`128k`、`128kbps`。"""
    raw = normalize(text).lower()
    raw = re.sub(r"(kbps|kb/s|kbit|k)$", "", raw)
    return parse_int(raw, "码率", low, high)


def parse_volume(text: str) -> tuple[str, float]:
    """返回 ("factor", 倍数) 或 ("db", 分贝)。支持 `1.5`、`150%`、`+6dB`、`-3db`。"""
    raw = normalize(text).lower().replace(" ", "")
    if raw.endswith("db"):
        return "db", parse_float(raw[:-2], "音量分贝", -60, 30)
    if raw.endswith("%"):
        return "factor", parse_float(raw[:-1], "音量百分比", 0, 1000) / 100
    return "factor", parse_float(raw, "音量倍数", 0, 10)


_FORMAT_ALIASES = {"jpeg": "jpg", "tif": "tiff", "mpeg4": "mp4", "matroska": "mkv", "aac": "m4a", "oga": "ogg"}


def parse_format(text: str, allowed: tuple[str, ...]) -> str:
    raw = normalize(text).lower().lstrip(".")
    raw = _FORMAT_ALIASES.get(raw, raw)
    if raw not in allowed:
        raise UsageError(f"不支持的格式：{text}，可选：{'、'.join(allowed)}")
    return raw


def parse_grid(text: str, max_each: int = 10) -> tuple[int, int] | None:
    """`3x4` / `3*4` / `3×4` → (行, 列)；不是网格写法返回 None。"""
    raw = normalize(text).lower().replace("*", "x")
    match = re.fullmatch(r"(\d{1,3})x(\d{1,3})", raw)
    if not match:
        return None
    rows, cols = int(match.group(1)), int(match.group(2))
    if not (1 <= rows <= max_each and 1 <= cols <= max_each):
        raise UsageError(f"行数和列数范围是 1～{max_each}")
    return rows, cols


_MARGIN = re.compile(r"^边距([上下左右])?边?(\d{1,5})$")


def take_margins(tokens: list[str]) -> tuple[list[str], dict[str, int]]:
    """从参数里取出 `边距10`、`边距上20`、`边距 左 5` 等写法，返回 (剩余参数, 边距)。"""
    margins = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    names = {"上": "top", "下": "bottom", "左": "left", "右": "right"}
    joined: list[str] = []
    i = 0
    while i < len(tokens):  # 合并「边距 上 20」这类被空格分开的写法
        token = normalize(tokens[i])
        if token.startswith("边距"):
            while not re.search(r"\d$", token) and i + 1 < len(tokens):
                i += 1
                token += normalize(tokens[i])
        joined.append(token)
        i += 1
    rest: list[str] = []
    for token in joined:
        match = _MARGIN.fullmatch(token)
        if match:
            amount = int(match.group(2))
            if match.group(1):
                margins[names[match.group(1)]] += amount
            else:
                for key in margins:
                    margins[key] += amount
        elif token.startswith("边距"):
            raise UsageError(f"边距写法不正确：{token}（示例 边距10、边距上20）")
        else:
            rest.append(token)
    return rest, margins


def parse_frame_duration(text: str) -> int:
    """每帧时长：`0.1`、`0.1s`（秒），`100ms`，`10fps`。返回毫秒。"""
    raw = normalize(text).lower().replace(" ", "")
    if raw.endswith("fps") or raw.endswith("帧"):
        fps = parse_float(raw.rstrip("fps").rstrip("帧"), "帧率", 0.1, 100)
        return max(10, round(1000 / fps))
    if raw.endswith("ms"):
        return parse_int(raw[:-2], "每帧毫秒数", 10, 60000)
    seconds = parse_float(raw.rstrip("s秒"), "每帧秒数", 0.01, 60)
    return max(10, round(seconds * 1000))


def parse_crop(text: str) -> tuple[str, tuple]:
    """返回 ("box", (x, y, w, h)) 或 ("ratio", (rw, rh))。"""
    raw = normalize(text).replace("，", ",")
    if re.fullmatch(r"\d{1,5}:\d{1,5}", raw):
        rw, rh = (int(v) for v in raw.split(":"))
        if rw <= 0 or rh <= 0:
            raise UsageError("裁剪比例必须大于 0")
        return "ratio", (rw, rh)
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) == 4 and all(re.fullmatch(r"\d{1,5}", p) for p in parts):
        x, y, w, h = (int(p) for p in parts)
        if w <= 0 or h <= 0:
            raise UsageError("裁剪宽高必须大于 0")
        return "box", (x, y, w, h)
    raise UsageError(f"裁剪参数格式不正确：{text}（示例 10,20,300,200 或 1:1）")
