from __future__ import annotations

from dataclasses import dataclass
import shlex


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    args: tuple[str, ...]
    raw: str


def parse_command(text: str | None, *, match_mode: bool = False) -> ParsedCommand | None:
    if not text:
        return None
    raw = str(text).strip()
    if raw.startswith("/"):
        raw = raw[1:].strip()
    elif not match_mode:
        return None
    if not raw:
        return None
    try:
        bits = shlex.split(raw, posix=False)
    except ValueError:
        bits = raw.split()
    return ParsedCommand(bits[0].strip(), tuple(x.strip('"') for x in bits[1:]), str(text).strip())


HELP = (
    "图片：反色、顺时针、逆时针、左右翻转、上下翻转、左/右/上/下对称、调速 倍率、图片转换 格式、图片压缩 质量、图片缩放 宽x高、图片裁剪 x,y,w,h、图片信息\n"
    "视频：视频转GIF、GIF转视频、视频转换、视频压缩、视频裁剪、视频调速、视频缩放、视频旋转、提取音频、视频静音、提取视频帧\n"
    "音频：音频转换、音频转MP3、音频裁剪、音频调速、音量、音频压缩、音频静音、音频信息；另有 转换、rename、幻影坦克、彩色幻影坦克。"
)
