"""指令注册表与统一解析。

所有指令的名称、别名、用法、示例都只在这里定义：
main.py 的装饰器、帮助文本、参数解析都读取这张表。
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
    "meme": "表情包（需要 meme-generator）",
    "qq": "QQ 图片保存",
    "mirage": "幻影坦克",
    "prism": "光棱坦克",
    "text": "文本",
    "video": "视频（需要 FFmpeg）",
    "audio": "音频（需要 FFmpeg）",
    "message": "消息处理（QQ 为主）",
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
    # ---------------- Meme Generator ----------------
    CommandSpec("meme列表", "meme_help", "meme", "查看全部表情包关键词", examples=("/meme列表",), aliases={"meme帮助": (), "meme菜单": (), "表情帮助": ()}),
    CommandSpec("meme搜索", "meme_search", "meme", "搜索表情包关键词", "<关键词片段>", examples=("/meme搜索 贴",)),
    CommandSpec("meme详情", "meme_details", "meme", "查看表情包参数和预览", "<关键词>", examples=("/meme详情 喜报",), aliases={"表情详情": (), "meme信息": ()}),
    # ---------------- QQ ----------------
    CommandSpec(
        "转换", "qq_convert", "qq", "把 QQ 图片/GIF/官方表情重新发送为可下载文件", "",
        ("引用一张图片后发送 /转换",),
        "保留原格式和动画。群聊发送方式由配置决定（默认私聊发送，避免占用群文件）。仅支持 QQ（OneBot v11）。",
    ),
    CommandSpec(
        "外显取名", "qq_rename", "qq", "修改图片在 QQ 消息列表中的外显文字", "<文字>",
        ("引用图片后发送 /外显取名 好耶", "/外显取名 [doge]"),
        "文字没有方括号时会自动加上，例如 好耶 → [好耶]。仅支持 QQ（OneBot v11）。",
    ),
    CommandSpec(
        "转换禁用", "qq_block", "qq", "（管理员）禁止某个群使用 /转换", "[群号]",
        ("在群里发送 /转换禁用", "/转换禁用 123456"),
        "不填群号时作用于当前群。", aliases={"convert addblock": ()}, admin=True,
    ),
    CommandSpec(
        "转换启用", "qq_unblock", "qq", "（管理员）恢复某个群的 /转换", "[群号]",
        ("/转换启用", "/转换启用 123456"),
        aliases={"convert delblock": ()}, admin=True,
    ),
    # ---------------- 幻影坦克 ----------------
    CommandSpec(
        "幻影坦克", "mirage", "mirage", "生成黑白幻影坦克（白底显示表图，黑底显示里图）", "",
        ("/幻影坦克 [附带两张图片]", "/幻影坦克 → 依次发送表图、里图"),
        "可以随指令直接附带两张图（第一张为表图），也可以按提示分步发送；发送「取消」中止。",
    ),
    CommandSpec(
        "彩色幻影坦克", "mirage", "mirage", "生成彩色幻影坦克", "",
        ("/彩色幻影坦克 [附带两张图片]",),
        "用法同 /幻影坦克，颜色参数在配置里调整。", aliases={"彩色幻影": ()},
    ),
    # ---------------- 光棱坦克 ----------------
    CommandSpec(
        "光棱坦克", "prism_tank", "prism", "生成光棱坦克（平时显示表图，拉伸暗部色阶后显示里图）", "[彩色|黑白]",
        ("/光棱坦克 [附带两张图片]", "/光棱坦克 黑白 → 依次发送表图、里图"),
        "用法同幻影坦克：一次附带两张图（第一张为表图）或分步发送，发送「取消」中止。不填参数时表图是否保留颜色见配置。",
    ),
    CommandSpec(
        "光棱显形", "prism_reveal", "prism", "还原光棱坦克里隐藏的图片", "[阈值 1~200]",
        ("引用光棱坦克原图发送 /光棱显形", "/光棱显形 48"),
        "阈值优先读取本插件写入的参数，其次自动检测；图片被压缩过时可能需要手动指定。",
        aliases={"光棱坦克显形": ()},
    ),
    # ---------------- 文本 ----------------
    CommandSpec("去除MD格式", "text", "text", "去掉 Markdown 标记，转成纯文本", "[文本]", ("引用一条消息发送 /去除MD格式",)),
    CommandSpec("添加MD格式", "text", "text", "给普通文本加上标题、加粗、列表、链接等 Markdown 格式", "[文本]", ("/添加MD格式 购物清单\n1. 牛奶\n2. 面包",)),
    CommandSpec("MD代码块", "text", "text", "包成 Markdown 代码块", "[语言] 文本", ("/MD代码块 python\nprint(1)",)),
    CommandSpec("MD引用", "text", "text", "每行加上 > 变成引用", "[文本]", ("/MD引用 名言一句",)),
    CommandSpec("MD列表", "text", "text", "每行变成列表项", "[有序] 文本", ("/MD列表 有序\n第一\n第二",)),
    CommandSpec("MD表格", "text", "text", "把 CSV / TSV / 空格分隔的文本转成 Markdown 表格（首行为表头）", "文本", ("/MD表格 姓名,年龄\n张三,18",)),
    CommandSpec("字数统计", "text", "text", "统计字符、汉字、单词、行数、段落", "[文本]", ("引用一条消息发送 /字数统计",)),
    CommandSpec("全角转半角", "text", "text", "全角字母数字标点转半角", "[文本]", ("/全角转半角 ＡＢＣ１２３",)),
    CommandSpec("半角转全角", "text", "text", "半角字符转全角", "[文本]", ("/半角转全角 ABC123",)),
    CommandSpec("转大写", "text", "text", "英文转大写", "[文本]", ("/转大写 hello",)),
    CommandSpec("转小写", "text", "text", "英文转小写", "[文本]", ("/转小写 HELLO",)),
    CommandSpec("清理空白", "text", "text", "去掉行首行尾空格、合并多余空格和空行", "[文本]", ("引用一条消息发送 /清理空白",)),
    CommandSpec("文本去重", "text", "text", "删除重复的行（保留第一次出现）", "[文本]", ("引用一条消息发送 /文本去重",)),
    CommandSpec("文本排序", "text", "text", "按行排序（数字按大小）", "[倒序] 文本", ("/文本排序 倒序\nb\na",)),
    CommandSpec("文本反转", "text", "text", "把文字倒过来", "[文本]", ("/文本反转 上海自来水",)),
    CommandSpec("Base64编码", "text", "text", "Base64 编码（UTF-8）", "[文本]", ("/Base64编码 你好",)),
    CommandSpec("Base64解码", "text", "text", "Base64 解码", "[文本]", ("/Base64解码 5L2g5aW9",)),
    CommandSpec("URL编码", "text", "text", "URL 编码", "[文本]", ("/URL编码 a b&c",)),
    CommandSpec("URL解码", "text", "text", "URL 解码", "[文本]", ("/URL解码 a%20b",)),
    CommandSpec("JSON格式化", "text", "text", "格式化 JSON（加「压缩」则压成一行）", "[压缩] 文本", ("/JSON格式化 {\"a\":1}",)),
    CommandSpec("文本哈希", "text", "text", "计算 MD5 / SHA1 / SHA256", "[文本]", ("/文本哈希 hello",)),
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
    # ---------------- 消息处理 ----------------
    CommandSpec(
        "消息", "message", "message", "发送各类 QQ 消息、转发、合并转发和模拟对话", "<操作> [参数]",
        ("/消息 骰子", "/消息 模拟 123456 你好 | 654321 你好呀", "引用一条消息发送 /消息 转发"),
        "不写操作时显示全部操作；/多模态处理帮助 消息 模拟 查看某个操作的用法。",
    ),
    # ---------------- 帮助 ----------------
    CommandSpec(
        "多模态处理帮助", "help", "help", "查看指令列表或某个指令的详细用法", "[分类 | 指令]",
        ("/多模态处理帮助", "/多模态处理帮助 视频", "/多模态处理帮助 视频裁剪"),
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


@dataclass(frozen=True, slots=True)
class MessageOp:
    """/消息 的一个操作。feature 对应配置里的独立开关（enable_<feature>）。"""

    name: str
    feature: str
    summary: str
    usage: str = ""
    examples: tuple[str, ...] = ()
    detail: str = ""
    qq_only: bool = True

    def usage_text(self) -> str:
        lines = [f"/消息 {self.name}{(' ' + self.usage) if self.usage else ''}  —  {self.summary}"]
        if self.detail:
            lines.append(self.detail)
        if self.examples:
            lines.append("示例：" + "；".join(self.examples))
        return "\n".join(lines)


SIMULATE_NOTICE = "模拟对话由使用者自行编写，不代表真实聊天记录；请勿用于冒充他人、伪造证据或传播谣言。"

MESSAGE_OPS: tuple[MessageOp, ...] = (
    MessageOp("文本", "send", "让机器人发送一段文字，可夹带 QQ 表情和图片", "<内容>", ("/消息 文本 大家好",),
              "内容里的 @全体成员 会被去掉。其他平台只发送文字。", qq_only=False),
    MessageOp("表情", "send", "发送 QQ 小黄脸表情", "[编号 | 起~止 | 随机] [数量]", ("/消息 表情 14 3", "/消息 表情 1~10", "/消息 表情"),
              "不写编号时随机挑一个经典表情。"),
    MessageOp("骰子", "send", "发送骰子魔法表情", examples=("/消息 骰子",), detail="其他平台改为文字掷骰。", qq_only=False),
    MessageOp("猜拳", "send", "发送猜拳魔法表情", examples=("/消息 猜拳",), detail="其他平台改为文字出拳。", qq_only=False),
    MessageOp("音乐", "card", "发送音乐分享卡片", "<qq | 网易云 | 酷狗 | 酷我 | 咪咕> <歌曲ID>", ("/消息 音乐 网易云 1495009565",),
              "卡片签名由协议端完成，签名服务不可用时会发送失败。"),
    MessageOp("推荐", "card", "发送好友或群推荐卡片", "[QQ号 | @某人 | 群 [群号]]", ("/消息 推荐 @某人", "/消息 推荐 群"),
              "不写目标时推荐自己；「群」后面不写群号时推荐当前群。"),
    MessageOp("Markdown", "card", "以合并转发的形式发送 Markdown 消息", "<内容>", ("/消息 Markdown # 标题\n正文",),
              "没有内容时取引用消息的文字。依赖协议端支持，部分协议端会发送失败。"),
    MessageOp("@", "at", "@ 群成员或全体成员", "<QQ号… | @某人… | 全体> [文字]", ("/消息 @ 123456 开会了", "/消息 @全体 今晚八点集合"),
              "只能在群聊使用。@全体成员 需要配置允许的身份，机器人也必须是群主或管理员。"),
    MessageOp("转发", "forward", "把引用的消息转发出去", "[群号… | @某人… | 我]", ("引用消息发送 /消息 转发", "引用消息发送 /消息 转发 123456", "引用消息发送 /消息 转发 我"),
              "不写目标时转发到当前会话；@某人 是转发给对方私聊，「我」是转发给自己私聊。一次最多 5 个目标，"
              "转发到其他会话需要配置允许，目标群必须是机器人所在的群，私聊目标必须是机器人的好友。"),
    MessageOp("合并", "merge", "把真实聊天消息合并成一条合并转发", "[条数] [标题=…] [摘要=…]", ("引用消息发送 /消息 合并", "/消息 合并 10", "引用消息发送 /消息 合并 5 标题=名场面"),
              "引用一条消息时：只写引用 → 合并这一条；写了条数 → 合并这一条及之前共 N 条。"
              "没有引用时合并当前会话最近 N 条（默认 10 条，不含这条指令）。需要协议端支持读取历史消息。"),
    MessageOp("模拟", "simulate", "按指定的 QQ 号和内容生成合并转发（模拟对话）", "[标题=…] QQ号 内容 | @某人 内容 | 我 内容",
              ("/消息 模拟 123456 你好 | 654321:小明 你好呀", "/消息 模拟 我 看图[图片] | @某人 好看"),
              "每段以 QQ 号、@某人 或「我」开头，可紧跟「:昵称」，段与段用 | 分隔；图片放在哪一段就属于哪条消息。"
              "引用一条消息时，被引用的真实消息排在最前面。" + SIMULATE_NOTICE),
)

_OPS_LONGEST_FIRST = sorted(MESSAGE_OPS, key=lambda op: len(op.name), reverse=True)
MESSAGE_HELP_WORDS = ("帮助",)


def match_message_op(text: str) -> tuple[MessageOp | None, str]:
    """识别 /消息 后面的操作名（不区分大小写，操作名后可以不加空格），返回 (操作, 其余文字)。"""
    stripped = text.lstrip()
    lowered = stripped.lower()
    for op in _OPS_LONGEST_FIRST:
        if lowered.startswith(op.name.lower()):
            return op, stripped[len(op.name):]
    return None, stripped


def message_help() -> str:
    spec = get_spec("消息")
    lines = [f"【{CATEGORIES['message']}】/消息 {spec.usage}  —  {spec.summary}"]
    lines.extend(f"/消息 {op.name}{(' ' + op.usage) if op.usage else ''}  —  {op.summary}" for op in MESSAGE_OPS)
    lines.append("除文本、骰子、猜拳外只支持 QQ（OneBot v11）。/多模态处理帮助 消息 模拟 查看某个操作的详细用法。")
    lines.append(SIMULATE_NOTICE)
    return "\n".join(lines)


def help_overview(ffmpeg_ok: bool = True) -> str:
    lines = ["多模态处理器 指令一览（/多模态处理帮助 <分类或指令> 查看详细用法）"]
    for key, title in CATEGORIES.items():
        if key == "message":
            lines.append(f"【{title}】/消息 " + " ".join(op.name for op in MESSAGE_OPS))
            continue
        names = [f"/{s.name}" for s in COMMANDS if s.category == key]
        lines.append(f"【{title}】" + " ".join(names))
    lines.append(_SOURCE_HINT)
    lines.append("视频/音频来源：引用或附带视频、语音、音频文件。")
    if not ffmpeg_ok:
        lines.append("⚠ 未检测到 FFmpeg，视频和音频功能暂不可用。")
    return "\n".join(lines)


_CATEGORY_KEYWORDS = {
    "图片": "image", "gif": "image", "动图": "image", "表情包": "meme", "meme": "meme", "qq": "qq", "转换": "qq",
    "幻影坦克": "mirage", "幻影": "mirage", "光棱": "prism", "文本": "text", "文字": "text", "md": "text", "视频": "video", "音频": "audio", "消息": "message", "帮助": "help",
}


def help_for(query: str) -> str | None:
    query = query.strip().lstrip("/")
    lowered = query.lower()
    if lowered.startswith("消息") and query[2:].strip():  # 「消息 模拟」：某个操作的用法
        op, rest = match_message_op(query[2:])
        if op and not rest.strip():
            return op.usage_text()
    spec = get_spec(query)
    if spec and spec.category == "message":
        return message_help()
    if spec:
        return spec.usage_text()
    category = _CATEGORY_KEYWORDS.get(lowered)
    if category == "message":
        return message_help()
    if not category:
        op, rest = match_message_op(query)
        return op.usage_text() if op and not rest.strip() else None
    lines = [f"【{CATEGORIES[category]}】"]
    for spec in COMMANDS:
        if spec.category == category:
            lines.append(f"/{spec.name}{(' ' + spec.usage) if spec.usage else ''}  —  {spec.summary}")
    if category in {"image", "mirage", "prism"}:
        lines.append(_SOURCE_HINT)
    if category == "text":
        lines.append("文本来源：指令后面的文字（可换行），没有时取引用消息的文字或引用 / 附带的文本文件（txt、md、json、csv 等）。")
    return "\n".join(lines)
