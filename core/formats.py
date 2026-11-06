"""根据文件头识别真实格式，不信任扩展名和 Content-Type。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IMAGE, VIDEO, AUDIO, AV, SILK, UNKNOWN = "image", "video", "audio", "av", "silk", "unknown"


@dataclass(frozen=True, slots=True)
class Sniffed:
    kind: str  # image / video / audio / av（音视频容器，需 ffprobe 确认）/ silk / unknown
    ext: str  # 带点的扩展名

    @property
    def is_image(self) -> bool:
        return self.kind == IMAGE

    @property
    def maybe_video(self) -> bool:
        return self.kind in {VIDEO, AV}

    @property
    def maybe_audio(self) -> bool:
        return self.kind in {AUDIO, AV, VIDEO, SILK}


_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}
_MOV_BRANDS = {b"qt  "}


def sniff_bytes(head: bytes) -> Sniffed:
    if head.startswith((b"GIF87a", b"GIF89a")):
        return Sniffed(IMAGE, ".gif")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Sniffed(IMAGE, ".png")
    if head.startswith(b"\xff\xd8\xff"):
        return Sniffed(IMAGE, ".jpg")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Sniffed(IMAGE, ".webp")
    if head.startswith(b"BM") and len(head) >= 26:
        return Sniffed(IMAGE, ".bmp")
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return Sniffed(IMAGE, ".tiff")
    if head.startswith((b"#!SILK", b"\x02#!SILK")):
        return Sniffed(SILK, ".silk")
    if head.startswith(b"#!AMR"):
        return Sniffed(AUDIO, ".amr")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AUDIO_BRANDS:
            return Sniffed(AUDIO, ".m4a")
        if brand in {b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"}:
            return Sniffed(IMAGE, ".avif" if brand.startswith(b"avi") else ".heic")
        if brand in _MOV_BRANDS:
            return Sniffed(AV, ".mov")
        return Sniffed(AV, ".mp4")
    if head.startswith(b"\x1aE\xdf\xa3"):
        return Sniffed(AV, ".webm" if b"webm" in head[:64] else ".mkv")
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return Sniffed(AUDIO, ".wav")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return Sniffed(VIDEO, ".avi")
    if head.startswith(b"OggS"):
        return Sniffed(AV, ".opus" if b"OpusHead" in head[:64] else ".ogg")
    if head.startswith(b"fLaC"):
        return Sniffed(AUDIO, ".flac")
    if head.startswith(b"FLV"):
        return Sniffed(VIDEO, ".flv")
    if head.startswith(b"ID3"):
        return Sniffed(AUDIO, ".mp3")
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:  # ADTS AAC：12 位同步字 + layer 00
            return Sniffed(AUDIO, ".aac")
        if head[1] & 0xE0 == 0xE0:  # MPEG 音频帧同步
            return Sniffed(AUDIO, ".mp3")
    if len(head) >= 189 and head[0] == 0x47 and head[188] == 0x47:
        return Sniffed(VIDEO, ".ts")
    if head[:4] == b"\x30\x26\xb2\x75":
        return Sniffed(AV, ".wmv")
    return Sniffed(UNKNOWN, ".bin")


def sniff_file(path: str | Path) -> Sniffed:
    with Path(path).open("rb") as handle:
        return sniff_bytes(handle.read(512))


IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".ts", ".m4v", ".wmv"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".flac", ".amr", ".wma"}
