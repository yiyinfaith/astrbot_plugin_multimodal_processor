"""指令注册表与统一解析。

所有指令的名称、别名、用法、示例都只在这里定义：
main.py 的装饰器、被动关键词匹配、帮助文本、参数解析都读取这张表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    handler: str
    category: str
    summary: str
    usage: str = ""
    examples: tuple[str, ...] = ()
    detail: str = ""
    # 别名 -> 预置参数（例如 音频转MP3 -> ("mp3",)）
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    admin: bool = False
    # 被动关键词模式下是否需要先找到媒体才响应（帮助、管理类指令不需要）
    needs_media: bool = True

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases.keys())

    def usage_text(self) -> str:
        lines = [f"/{self.name}{(' ' + self.usage) if self.usage else ''}  —  {self.summary}"]
        if self.detail:
            lines.append(self.detail)
        if self.aliases:
            lines.append("别名：" + "、".join(f"/{a}" for a in self.aliases))
        if self.examples:
            lines.append("示例：" + "；".join(self.examples))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    spec: CommandSpec
    used_name: str
    args: tuple[str, ...]
    rest: str  # 指令名之后的原始文本（保留空格），供 /外显取名 等自由文本使用

    @property
    def name(self) -> str:
        return self.spec.name


CATEGORIES: dict[str, str] = {
    "image": "图片 / GIF",
    "qq": "QQ 图片保存",
    "mirage": "幻影坦克",
    "prism": "光棱坦克",
    "video": "视频（需要 FFmpeg）",
    "audio": "音频（需要 FFmpeg）",
    "help": "帮助",
}

_SOURCE_HINT = "图片来源：引用图片、随指令附带图片，或 @ 一位 QQ 用户取头像。"

COMMANDS: tuple[CommandSpec, ...] = (
    # ---------------- 图片 / GIF ----------------
    CommandSpec("反色", "image_transform", "image", "图片/GIF 反色，保留透明度", examples=("引用图片发送 /反色", "/反色 @某人")),
    CommandSpec("顺时针", "image_transform", "image", "顺时针旋转 90°", examples=("/顺时针",)),
    CommandSpec("逆时针", "image_transform", "image", "逆时针旋转 90°", examples=("/逆时针",)),
    CommandSpec("左右翻转", "image_transform", "image", "水平镜像翻转", examples=("/左右翻转",)),
    CommandSpec("上下翻转", "image_transform", "image", "垂直翻转", examples=("/上下翻转",)),
    CommandSpec("左对称", "image_transform", "image", "保留左半边并镜像到右边", examples=("/左对称 @某人",)),
    CommandSpec("右对称", "image_transform", "image", "保留右半边并镜像到左边", examples=("/右对称",)),
    CommandSpec("上对称", "image_transform", "image", "保留上半边并镜像到下边", examples=("/上对称",)),
    CommandSpec("下对称", "image_transform", "image", "保留下半边并镜像到上边", examples=("/下对称",)),
    CommandSpec(
        "调速", "gif_speed", "image", "调整 GIF/动图播放速度", "<倍率 0.1~10>",
        ("/调速 2", "/调速 0.5"),
        "帧率超过上限时默认自动回退到不超限的最高倍率；开启「允许丢帧」后改为均匀丢帧。",
    ),
    CommandSpec(
        "图片转换", "image_convert", "image", "转换图片格式", "<png|jpg|webp|gif|bmp|tiff> [静态]",
        ("/图片转换 webp", "/图片转换 png 静态"),
        "动图转 gif/webp/png 默认保留动画（png 输出为 APNG）；加「静态」只取第一帧。jpg/bmp/tiff 不支持动画，会明确提示只保留第一帧。",
    ),
    CommandSpec(
        "图片压缩", "image_compress", "image", "压缩图片体积", "[质量 1~100] [最长边像素]",
        ("/图片压缩", "/图片压缩 70", "/图片压缩 60 1280"),
        "JPG/WEBP 按质量重新编码；PNG/GIF 减少颜色数；可选同时把最长边缩小到指定像素。",
    ),
    CommandSpec(
        "图片缩放", "image_resize", "image", "按尺寸或比例缩放", "<宽x高 | 宽 | x高 | 百分比>",
        ("/图片缩放 800x600", "/图片缩放 512", "/图片缩放 50%"),
        "只给宽或高时按原比例计算另一边。",
    ),
    CommandSpec(
        "图片裁剪", "image_crop", "image", "按区域或比例裁剪", "<x,y,宽,高 | 宽:高>",
        ("/图片裁剪 0,0,300,300", "/图片裁剪 1:1", "/图片裁剪 16:9"),
        "比例裁剪从画面中心取最大区域；区域超出画面时自动截到边界。",
    ),
    CommandSpec("图片信息", "image_info", "image", "查看格式、尺寸、帧数、时长、大小", examples=("引用图片发送 /图片信息",)),
    CommandSpec("GIF加速", "gif_faster", "image", "动图加速", "[倍数 1~10，默认 2]", ("/GIF加速", "/加速 1.5"), "等同于 /调速 倍数。", aliases={"加速": ()}),
    CommandSpec("GIF减速", "gif_slower", "image", "动图减速", "[倍数 1~10，默认 2]", ("/GIF减速", "/减速 3"), "等同于 /调速 1/倍数。", aliases={"减速": ()}),
    CommandSpec(
        "精灵图转GIF", "sprite_to_gif", "image", "把精灵图（雪碧图）按网格拆成帧合成动图", "<行x列> [每帧时长] [边距…]",
        ("/精灵图转GIF 4x4", "/精灵图转GIF 6x6 0.05s", "/合成gif 8x8 80ms 边距上10 左5"),
        "每帧时长可写 0.1、0.1s、100ms 或 10fps，默认 100ms；边距写法：边距10（四周）、边距上20、边距左5，可叠加。末尾完全透明的空格子会被去掉。",
        aliases={"合成GIF": (), "合成1gif": (), "合成2gif": ()},
    ),
    CommandSpec(
        "多图合成GIF", "multi_to_gif", "image", "把多张图片合成动图", "[每帧时长，默认 0.5s]",
        ("随指令附带多张图片发送 /多图合成GIF", "引用合并转发后发送 /多图合成GIF 0.2", "/多图合成GIF 5fps @甲 @乙"),
        "图片来源：引用消息、当前消息、合并转发（QQ）里的图片，以及 @ 用户的头像；按出现顺序排列，以最大宽高为画布居中放置。",
    ),
    CommandSpec(
        "GIF分解", "gif_split", "image", "把动图拆成单独的帧", "[最多帧数]",
        ("引用 GIF 发送 /GIF分解", "/GIF分解 20"),
        "QQ 以合并转发发送；帧数超过上限时均匀抽取，首尾帧保留。",
    ),
    CommandSpec(
        "网格切图", "grid_split", "image", "把图片按行列切成多块", "<行x列> [边距…]",
        ("/网格切图 3x3", "/切图 2x5 边距上20"),
        "动图切出来的每一块仍是动图；QQ 以合并转发发送。", aliases={"切图": (), "网格裁剪": ()},
    ),
    CommandSpec("图片转线稿", "line_art", "image", "把图片转成铅笔素描风格的线稿，支持动图", examples=("引用图片发送 /图片转线稿",), aliases={"线稿": ()}),
    CommandSpec(
        "表情包做旧", "age_meme", "image", "模拟反复转发后发绿、变糊的老图效果，支持动图", "[次数 1~50，默认 5]",
        ("/表情包做旧", "/做旧 15"), "1～5 轻度，5～10 中度，10～20 重度，20 以上极限。", aliases={"做旧": ()},
    ),
    # ---------------- QQ ----------------
    CommandSpec(
        "转换", "qq_convert", "qq", "把 QQ 图片/GIF/官方表情重新发送为可下载文件", "",
        ("引用一张图片后发送 /转换",),
        "保留原格式和动画。群聊发送方式由配置决定（默认私聊发送，避免占用群文件）。仅支持 QQ（OneBot v11 / NapCat）。",
    ),
    CommandSpec(
        "外显取名", "qq_rename", "qq", "修改图片在 QQ 消息列表中的外显文字", "<文字>",
        ("引用图片后发送 /外显取名 好耶", "/外显取名 [doge]"),
        "文字没有方括号时会自动加上，例如 好耶 → [好耶]。仅支持 QQ（OneBot v11 / NapCat）。",
    ),
    CommandSpec(
        "转换禁用", "qq_block", "qq", "（管理员）禁止某个群使用 /转换", "[群号]",
        ("在群里发送 /转换禁用", "/转换禁用 123456"),
        "不填群号时作用于当前群。", aliases={"convert addblock": ()}, admin=True, needs_media=False,
    ),
    CommandSpec(
        "转换启用", "qq_unblock", "qq", "（管理员）恢复某个群的 /转换", "[群号]",
        ("/转换启用", "/转换启用 123456"),
        aliases={"convert delblock": ()}, admin=True, needs_media=False,
    ),
    # ---------------- 幻影坦克 ----------------
    CommandSpec(
        "幻影坦克", "mirage", "mirage", "生成黑白幻影坦克（白底显示表图，黑底显示里图）", "",
        ("/幻影坦克 [附带两张图片]", "/幻影坦克 → 依次发送表图、里图"),
        "可以随指令直接附带两张图（第一张为表图），也可以按提示分步发送；发送「取消」中止。",
        needs_media=False,
    ),
    CommandSpec(
        "彩色幻影坦克", "mirage", "mirage", "生成彩色幻影坦克", "",
        ("/彩色幻影坦克 [附带两张图片]",),
        "用法同 /幻影坦克，颜色参数在配置里调整。", aliases={"彩色幻影": ()}, needs_media=False,
    ),
    # ---------------- 光棱坦克 ----------------
    CommandSpec(
        "光棱坦克", "prism_tank", "prism", "生成光棱坦克（平时显示表图，拉伸暗部色阶后显示里图）", "[彩色|黑白]",
        ("/光棱坦克 [附带两张图片]", "/光棱坦克 黑白 → 依次发送表图、里图"),
        "用法同幻影坦克：一次附带两张图（第一张为表图）或分步发送，发送「取消」中止。不填参数时表图是否保留颜色见配置。",
        needs_media=False,
    ),
    CommandSpec(
        "光棱显形", "prism_reveal", "prism", "还原光棱坦克里隐藏的图片", "[阈值 1~200]",
        ("引用光棱坦克原图发送 /光棱显形", "/光棱显形 48"),
        "阈值优先读取本插件写入的参数，其次自动检测；图片被压缩过时可能需要手动指定。",
        aliases={"光棱坦克显形": ()},
    ),
    # ---------------- 视频 ----------------
    CommandSpec(
        "视频转GIF", "video_to_gif", "video", "视频片段转 GIF", "[帧率] [宽度] [时长] [开始时间]",
        ("/视频转GIF", "/视频转GIF 15 360 5", "/视频转GIF 2s-4.5s fps15 50%", "/视频转GIF 开始 1:05 时长 3 1/3"),
        "也支持关键字写法：2s-4.5s（时间段）、开始 2、时长 3、fps15、1/3（每 3 帧取 1 帧）、0.5 或 50%（按原宽缩放）。"
        "默认帧率、宽度、最长时长见配置；帧率不超过原视频。",
    ),
    CommandSpec("GIF转视频", "gif_to_video", "video", "GIF/动图转 MP4", examples=("引用 GIF 发送 /GIF转视频",)),
    CommandSpec(
        "视频转换", "video_convert", "video", "转换视频格式", "<mp4|webm|mkv|mov>",
        ("/视频转换 webm", "/视频转换 mkv"),
        "编码兼容时直接封装转换（不重新编码），否则重新编码为 H.264/AAC 或 VP9/Opus。",
    ),
    CommandSpec(
        "视频压缩", "video_compress", "video", "压缩视频体积", "[高|中|低|CRF 18~40] [分辨率]",
        ("/视频压缩", "/视频压缩 低", "/视频压缩 30 720p"),
        "高/中/低 分别对应 CRF 23/默认值/33，CRF 越大体积越小。",
    ),
    CommandSpec("视频裁剪", "video_trim", "video", "截取时间段", "<开始> <结束>", ("/视频裁剪 5 12", "/视频裁剪 1:05 1:30"), "时间可写 秒数、分:秒、时:分:秒 或 1分5秒。"),
    CommandSpec("视频调速", "video_speed", "video", "加速或减速，音画同步", "<倍率 0.25~4>", ("/视频调速 2", "/视频调速 0.5"), "音频用 atempo 保持音调。"),
    CommandSpec("视频缩放", "video_scale", "video", "调整分辨率", "<宽x高 | 宽 | 720p | 50%>", ("/视频缩放 720p", "/视频缩放 1280x720", "/视频缩放 50%"), "只给一边时等比缩放。"),
    CommandSpec("视频旋转", "video_rotate", "video", "旋转视频", "<90|180|270|-90>", ("/视频旋转 90", "/视频旋转 -90"), "90 为顺时针。"),
    CommandSpec("提取音频", "video_extract_audio", "video", "从视频中提取音轨", "[mp3|m4a|wav|ogg|flac|opus]", ("/提取音频", "/提取音频 m4a")),
    CommandSpec("视频静音", "video_mute", "video", "去掉视频的声音", examples=("/视频静音",)),
    CommandSpec("视频截图", "video_frame", "video", "提取指定时间的画面为 PNG", "[时间，默认 0]", ("/视频截图 3", "/提取视频帧 1:20"), aliases={"提取视频帧": (), "视频截帧": ()}),
    CommandSpec("视频信息", "video_info", "video", "查看时长、分辨率、帧率、编码、码率", examples=("/视频信息",)),
    # ---------------- 音频 ----------------
    CommandSpec(
        "音频转换", "audio_convert", "audio", "转换音频格式", "<mp3|wav|ogg|m4a|flac|opus>",
        ("/音频转换 wav", "/音频转MP3"),
        aliases={"音频转MP3": ("mp3",), "音频转WAV": ("wav",), "音频转OGG": ("ogg",), "音频转M4A": ("m4a",), "音频转FLAC": ("flac",)},
    ),
    CommandSpec("音频裁剪", "audio_trim", "audio", "截取时间段", "<开始> <结束>", ("/音频裁剪 10 25", "/音频裁剪 1:00 1:30")),
    CommandSpec("音频调速", "audio_speed", "audio", "加速或减速，默认保持音调", "<倍率 0.25~4> [变调]", ("/音频调速 1.5", "/音频调速 0.8 变调"), "加「变调」时像磁带变速一样同时改变音调。"),
    CommandSpec("音量", "audio_volume", "audio", "调整音量", "<倍数 | 百分比 | ±分贝>", ("/音量 1.5", "/音量 50%", "/音量 +6dB"), "放大时自动加限幅，避免爆音。", aliases={"音频音量": ()}),
    CommandSpec("音频压缩", "audio_compress", "audio", "按码率压缩（默认 64k）", "[码率 16~320k]", ("/音频压缩", "/音频压缩 64k")),
    CommandSpec("音频静音", "audio_mute", "audio", "把指定时间段静音", "<开始> <结束>", ("/音频静音 3 5",)),
    CommandSpec("去除静音", "audio_desilence", "audio", "删除音频中的静音片段", "[阈值 dB，默认 -50] [最短静音秒数，默认 0.5]", ("/去除静音", "/去除静音 -40 1"), aliases={"音频去静音": ()}),
    CommandSpec("音频信息", "audio_info", "audio", "查看格式、时长、码率、采样率", examples=("/音频信息",)),
    # ---------------- 帮助 ----------------
    CommandSpec(
        "多模态处理帮助", "help", "help", "查看指令列表或某个指令的详细用法", "[分类 | 指令]",
        ("/多模态处理帮助", "/多模态处理帮助 视频", "/多模态处理帮助 视频裁剪"),
        needs_media=False,
    ),
)

_BY_NAME: dict[str, CommandSpec] = {}
for _spec in COMMANDS:
    for _name in _spec.all_names:
        if _name.lower() in _BY_NAME:
            raise RuntimeError(f"重复的指令名：{_name}")
        _BY_NAME[_name.lower()] = _spec
# 长名字优先匹配，避免「转换」抢先匹配「转换禁用」
_NAMES_LONGEST_FIRST = sorted(
    ((n, s) for s in COMMANDS for n in s.all_names), key=lambda item: len(item[0]), reverse=True
)


def get_spec(name: str) -> CommandSpec | None:
    return _BY_NAME.get(name.strip().lower())


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def parse_command(text: str | None) -> ParsedCommand | None:
    """从已去掉唤醒前缀的文本中识别指令。指令名后必须是空白或文本结束。"""
    if not text:
        return None
    raw = text.strip()
    lowered = raw.lower()
    for name, spec in _NAMES_LONGEST_FIRST:
        key = name.lower()
        if not lowered.startswith(key):
            continue
        tail = raw[len(name):]
        if tail and not tail[0].isspace():
            continue
        rest = tail.strip()
        args = tuple(spec.aliases.get(name, ())) + tuple(_split(rest))
        return ParsedCommand(spec, name, args, rest)
    return None


def help_overview(ffmpeg_ok: bool = True) -> str:
    lines = ["多模态处理器 指令一览（/多模态处理帮助 <分类或指令> 查看详细用法）"]
    for key, title in CATEGORIES.items():
        names = [f"/{s.name}" for s in COMMANDS if s.category == key]
        lines.append(f"【{title}】" + " ".join(names))
    lines.append(_SOURCE_HINT)
    lines.append("视频/音频来源：引用或附带视频、语音、音频文件。")
    if not ffmpeg_ok:
        lines.append("⚠ 未检测到 FFmpeg，视频和音频功能暂不可用。")
    return "\n".join(lines)


_CATEGORY_KEYWORDS = {
    "图片": "image", "gif": "image", "动图": "image", "qq": "qq", "转换": "qq",
    "幻影坦克": "mirage", "幻影": "mirage", "光棱": "prism", "视频": "video", "音频": "audio", "帮助": "help",
}


def help_for(query: str) -> str | None:
    query = query.strip().lstrip("/")
    spec = get_spec(query)
    if spec:
        return spec.usage_text()
    category = _CATEGORY_KEYWORDS.get(query.lower())
    if not category:
        return None
    lines = [f"【{CATEGORIES[category]}】"]
    for spec in COMMANDS:
        if spec.category == category:
            lines.append(f"/{spec.name}{(' ' + spec.usage) if spec.usage else ''}  —  {spec.summary}")
    if category in {"image", "mirage", "prism"}:
        lines.append(_SOURCE_HINT)
    return "\n".join(lines)
