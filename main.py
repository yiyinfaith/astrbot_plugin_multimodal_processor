"""多模态处理器：AstrBot 本地图片 / GIF / 视频 / 音频处理与 QQ 图片保存、幻影坦克。

AstrBot 按处理函数所在模块绑定插件，所以带装饰器的处理函数必须写在 main.py 的插件类里；
它们只负责把指令名交给 core.app.MultimodalApp，具体逻辑在 core/、handlers/、processors/。
指令名、别名、用法都定义在 core/commands.py，tests/test_commands.py 会核对两边是否一致。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_temp_path

from .core.app import MultimodalApp

PLUGIN_NAME = "multimodal_processor"


class MultimodalProcessor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_root = Path(get_astrbot_data_path())
        self.app = MultimodalApp(
            config,
            data_root / "plugin_data" / PLUGIN_NAME,
            astrbot_roots=[data_root, Path(get_astrbot_temp_path())],
            wake_prefixes=self._wake_prefixes,
        )

    def _wake_prefixes(self) -> list[str]:
        try:
            prefixes = self.context.get_config().get("wake_prefix", ["/"])
            return [str(p) for p in prefixes if p] or ["/"]
        except Exception:
            return ["/"]

    async def initialize(self):
        await self.app.start()
        logger.info("[多模态处理器] 已加载" + ("" if self.app.ffmpeg.available else "（未检测到 FFmpeg，音视频功能不可用）"))

    async def terminate(self):
        await self.app.stop()

    # ------------------------------------------------------------ @某人 + 指令
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """识别回复他人消息时 QQ 自动插入 @ 导致未被 AstrBot 唤醒的「/指令」"""
        await self.app.handle_passive(event)

    # ------------------------------------------------------------ 帮助
    @filter.command("多模态处理帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看多模态处理器的指令列表或某个指令的用法"""
        await self.app.handle_command(event, "多模态处理帮助")

    # ------------------------------------------------------------ 图片 / GIF
    @filter.command("反色")
    async def cmd_invert(self, event: AstrMessageEvent):
        """图片/GIF 反色"""
        await self.app.handle_command(event, "反色")

    @filter.command("顺时针")
    async def cmd_rotate_cw(self, event: AstrMessageEvent):
        """图片/GIF 顺时针旋转 90°"""
        await self.app.handle_command(event, "顺时针")

    @filter.command("逆时针")
    async def cmd_rotate_ccw(self, event: AstrMessageEvent):
        """图片/GIF 逆时针旋转 90°"""
        await self.app.handle_command(event, "逆时针")

    @filter.command("左右翻转")
    async def cmd_flip_h(self, event: AstrMessageEvent):
        """图片/GIF 左右翻转"""
        await self.app.handle_command(event, "左右翻转")

    @filter.command("上下翻转")
    async def cmd_flip_v(self, event: AstrMessageEvent):
        """图片/GIF 上下翻转"""
        await self.app.handle_command(event, "上下翻转")

    @filter.command("左对称")
    async def cmd_mirror_left(self, event: AstrMessageEvent):
        """保留左半边并镜像到右边"""
        await self.app.handle_command(event, "左对称")

    @filter.command("右对称")
    async def cmd_mirror_right(self, event: AstrMessageEvent):
        """保留右半边并镜像到左边"""
        await self.app.handle_command(event, "右对称")

    @filter.command("上对称")
    async def cmd_mirror_top(self, event: AstrMessageEvent):
        """保留上半边并镜像到下边"""
        await self.app.handle_command(event, "上对称")

    @filter.command("下对称")
    async def cmd_mirror_bottom(self, event: AstrMessageEvent):
        """保留下半边并镜像到上边"""
        await self.app.handle_command(event, "下对称")

    @filter.command("调速")
    async def cmd_gif_speed(self, event: AstrMessageEvent):
        """调整 GIF 播放速度，例：/调速 2"""
        await self.app.handle_command(event, "调速")

    @filter.command("图片转换")
    async def cmd_image_convert(self, event: AstrMessageEvent):
        """转换图片格式，例：/图片转换 webp"""
        await self.app.handle_command(event, "图片转换")

    @filter.command("图片压缩")
    async def cmd_image_compress(self, event: AstrMessageEvent):
        """压缩图片，例：/图片压缩 70"""
        await self.app.handle_command(event, "图片压缩")

    @filter.command("图片缩放")
    async def cmd_image_resize(self, event: AstrMessageEvent):
        """缩放图片，例：/图片缩放 800x600"""
        await self.app.handle_command(event, "图片缩放")

    @filter.command("图片裁剪")
    async def cmd_image_crop(self, event: AstrMessageEvent):
        """裁剪图片，例：/图片裁剪 1:1"""
        await self.app.handle_command(event, "图片裁剪")

    @filter.command("图片信息")
    async def cmd_image_info(self, event: AstrMessageEvent):
        """查看图片格式、尺寸、帧数"""
        await self.app.handle_command(event, "图片信息")

    @filter.command("GIF加速", alias={"加速"})
    async def cmd_gif_faster(self, event: AstrMessageEvent):
        """动图加速，例：/GIF加速 2"""
        await self.app.handle_command(event, "GIF加速")

    @filter.command("GIF减速", alias={"减速"})
    async def cmd_gif_slower(self, event: AstrMessageEvent):
        """动图减速，例：/GIF减速 2"""
        await self.app.handle_command(event, "GIF减速")

    @filter.command("精灵图转GIF", alias={"合成GIF", "合成1gif", "合成2gif"})
    async def cmd_sprite_to_gif(self, event: AstrMessageEvent):
        """精灵图按网格合成动图，例：/精灵图转GIF 4x4 0.1s"""
        await self.app.handle_command(event, "精灵图转GIF")

    @filter.command("多图合成GIF")
    async def cmd_multi_to_gif(self, event: AstrMessageEvent):
        """多张图片合成动图，例：/多图合成GIF 0.5"""
        await self.app.handle_command(event, "多图合成GIF")

    @filter.command("GIF分解")
    async def cmd_gif_split(self, event: AstrMessageEvent):
        """把动图拆成单独的帧"""
        await self.app.handle_command(event, "GIF分解")

    @filter.command("网格切图", alias={"切图", "网格裁剪"})
    async def cmd_grid_split(self, event: AstrMessageEvent):
        """把图片按行列切块，例：/网格切图 3x3"""
        await self.app.handle_command(event, "网格切图")

    @filter.command("图片转线稿", alias={"线稿"})
    async def cmd_line_art(self, event: AstrMessageEvent):
        """图片转铅笔素描线稿"""
        await self.app.handle_command(event, "图片转线稿")

    @filter.command("表情包做旧", alias={"做旧"})
    async def cmd_age_meme(self, event: AstrMessageEvent):
        """表情包做旧，例：/表情包做旧 10"""
        await self.app.handle_command(event, "表情包做旧")

    # ------------------------------------------------------------ QQ
    @filter.command("转换")
    async def cmd_qq_convert(self, event: AstrMessageEvent):
        """把引用的 QQ 图片/GIF/表情重新发送为可下载文件"""
        await self.app.handle_command(event, "转换")

    @filter.command("外显取名")
    async def cmd_qq_rename(self, event: AstrMessageEvent):
        """修改图片在 QQ 中的外显文字，例：/外显取名 好耶"""
        await self.app.handle_command(event, "外显取名")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("转换禁用", alias={"convert addblock"})
    async def cmd_qq_block(self, event: AstrMessageEvent):
        """（管理员）禁止某个群使用 /转换"""
        await self.app.handle_command(event, "转换禁用")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("转换启用", alias={"convert delblock"})
    async def cmd_qq_unblock(self, event: AstrMessageEvent):
        """（管理员）恢复某个群的 /转换"""
        await self.app.handle_command(event, "转换启用")

    # ------------------------------------------------------------ 幻影坦克
    @filter.command("幻影坦克")
    async def cmd_mirage(self, event: AstrMessageEvent):
        """生成黑白幻影坦克"""
        await self.app.handle_command(event, "幻影坦克")

    @filter.command("彩色幻影坦克", alias={"彩色幻影"})
    async def cmd_mirage_color(self, event: AstrMessageEvent):
        """生成彩色幻影坦克"""
        await self.app.handle_command(event, "彩色幻影坦克")

    # ------------------------------------------------------------ 光棱坦克
    @filter.command("光棱坦克")
    async def cmd_prism(self, event: AstrMessageEvent):
        """生成光棱坦克（拉伸暗部色阶后显示里图）"""
        await self.app.handle_command(event, "光棱坦克")

    @filter.command("光棱显形", alias={"光棱坦克显形"})
    async def cmd_prism_reveal(self, event: AstrMessageEvent):
        """还原光棱坦克里隐藏的图片"""
        await self.app.handle_command(event, "光棱显形")

    # ------------------------------------------------------------ 文本
    @filter.command("去除MD格式")
    async def cmd_text_strip_md(self, event: AstrMessageEvent):
        """去掉 Markdown 标记，转成纯文本"""
        await self.app.handle_command(event, "去除MD格式")

    @filter.command("添加MD格式")
    async def cmd_text_add_md(self, event: AstrMessageEvent):
        """给普通文本加上标题、加粗、列表、链接等 Markdown 格式"""
        await self.app.handle_command(event, "添加MD格式")

    @filter.command("MD代码块")
    async def cmd_text_md_code(self, event: AstrMessageEvent):
        """包成 Markdown 代码块"""
        await self.app.handle_command(event, "MD代码块")

    @filter.command("MD引用")
    async def cmd_text_md_quote(self, event: AstrMessageEvent):
        """每行加上 > 变成引用"""
        await self.app.handle_command(event, "MD引用")

    @filter.command("MD列表")
    async def cmd_text_md_list(self, event: AstrMessageEvent):
        """每行变成列表项"""
        await self.app.handle_command(event, "MD列表")

    @filter.command("MD表格")
    async def cmd_text_md_table(self, event: AstrMessageEvent):
        """把 CSV / TSV / 空格分隔的文本转成 Markdown 表格（首行为表头）"""
        await self.app.handle_command(event, "MD表格")

    @filter.command("字数统计")
    async def cmd_text_count(self, event: AstrMessageEvent):
        """统计字符、汉字、单词、行数、段落"""
        await self.app.handle_command(event, "字数统计")

    @filter.command("全角转半角")
    async def cmd_text_halfwidth(self, event: AstrMessageEvent):
        """全角字母数字标点转半角"""
        await self.app.handle_command(event, "全角转半角")

    @filter.command("半角转全角")
    async def cmd_text_fullwidth(self, event: AstrMessageEvent):
        """半角字符转全角"""
        await self.app.handle_command(event, "半角转全角")

    @filter.command("转大写")
    async def cmd_text_upper(self, event: AstrMessageEvent):
        """英文转大写"""
        await self.app.handle_command(event, "转大写")

    @filter.command("转小写")
    async def cmd_text_lower(self, event: AstrMessageEvent):
        """英文转小写"""
        await self.app.handle_command(event, "转小写")

    @filter.command("清理空白")
    async def cmd_text_clean(self, event: AstrMessageEvent):
        """去掉行首行尾空格、合并多余空格和空行"""
        await self.app.handle_command(event, "清理空白")

    @filter.command("文本去重")
    async def cmd_text_dedupe(self, event: AstrMessageEvent):
        """删除重复的行（保留第一次出现）"""
        await self.app.handle_command(event, "文本去重")

    @filter.command("文本排序")
    async def cmd_text_sort(self, event: AstrMessageEvent):
        """按行排序（数字按大小）"""
        await self.app.handle_command(event, "文本排序")

    @filter.command("文本反转")
    async def cmd_text_reverse(self, event: AstrMessageEvent):
        """把文字倒过来"""
        await self.app.handle_command(event, "文本反转")

    @filter.command("Base64编码")
    async def cmd_text_b64e(self, event: AstrMessageEvent):
        """Base64 编码（UTF-8）"""
        await self.app.handle_command(event, "Base64编码")

    @filter.command("Base64解码")
    async def cmd_text_b64d(self, event: AstrMessageEvent):
        """Base64 解码"""
        await self.app.handle_command(event, "Base64解码")

    @filter.command("URL编码")
    async def cmd_text_urle(self, event: AstrMessageEvent):
        """URL 编码"""
        await self.app.handle_command(event, "URL编码")

    @filter.command("URL解码")
    async def cmd_text_urld(self, event: AstrMessageEvent):
        """URL 解码"""
        await self.app.handle_command(event, "URL解码")

    @filter.command("JSON格式化")
    async def cmd_text_json(self, event: AstrMessageEvent):
        """格式化 JSON（加「压缩」则压成一行）"""
        await self.app.handle_command(event, "JSON格式化")

    @filter.command("文本哈希")
    async def cmd_text_hash(self, event: AstrMessageEvent):
        """计算 MD5 / SHA1 / SHA256"""
        await self.app.handle_command(event, "文本哈希")

    # ------------------------------------------------------------ 视频
    @filter.command("视频转GIF")
    async def cmd_video_to_gif(self, event: AstrMessageEvent):
        """视频转 GIF，例：/视频转GIF 12 480 5"""
        await self.app.handle_command(event, "视频转GIF")

    @filter.command("GIF转视频")
    async def cmd_gif_to_video(self, event: AstrMessageEvent):
        """GIF 转 MP4"""
        await self.app.handle_command(event, "GIF转视频")

    @filter.command("视频转换")
    async def cmd_video_convert(self, event: AstrMessageEvent):
        """转换视频格式，例：/视频转换 webm"""
        await self.app.handle_command(event, "视频转换")

    @filter.command("视频压缩")
    async def cmd_video_compress(self, event: AstrMessageEvent):
        """压缩视频，例：/视频压缩 低"""
        await self.app.handle_command(event, "视频压缩")

    @filter.command("视频裁剪")
    async def cmd_video_trim(self, event: AstrMessageEvent):
        """截取视频片段，例：/视频裁剪 5 12"""
        await self.app.handle_command(event, "视频裁剪")

    @filter.command("视频调速")
    async def cmd_video_speed(self, event: AstrMessageEvent):
        """视频变速，例：/视频调速 2"""
        await self.app.handle_command(event, "视频调速")

    @filter.command("视频缩放")
    async def cmd_video_scale(self, event: AstrMessageEvent):
        """调整视频分辨率，例：/视频缩放 720p"""
        await self.app.handle_command(event, "视频缩放")

    @filter.command("视频旋转")
    async def cmd_video_rotate(self, event: AstrMessageEvent):
        """旋转视频，例：/视频旋转 90"""
        await self.app.handle_command(event, "视频旋转")

    @filter.command("提取音频")
    async def cmd_video_extract_audio(self, event: AstrMessageEvent):
        """从视频中提取音轨，例：/提取音频 mp3"""
        await self.app.handle_command(event, "提取音频")

    @filter.command("视频静音")
    async def cmd_video_mute(self, event: AstrMessageEvent):
        """去掉视频的声音"""
        await self.app.handle_command(event, "视频静音")

    @filter.command("视频截图", alias={"提取视频帧", "视频截帧"})
    async def cmd_video_frame(self, event: AstrMessageEvent):
        """提取指定时间的画面，例：/视频截图 3"""
        await self.app.handle_command(event, "视频截图")

    @filter.command("视频信息")
    async def cmd_video_info(self, event: AstrMessageEvent):
        """查看视频时长、分辨率、编码"""
        await self.app.handle_command(event, "视频信息")

    # ------------------------------------------------------------ 音频
    @filter.command("音频转换", alias={"音频转MP3", "音频转WAV", "音频转OGG", "音频转M4A", "音频转FLAC"})
    async def cmd_audio_convert(self, event: AstrMessageEvent):
        """转换音频格式，例：/音频转换 wav 或 /音频转MP3"""
        await self.app.handle_command(event, "音频转换")

    @filter.command("音频裁剪")
    async def cmd_audio_trim(self, event: AstrMessageEvent):
        """截取音频片段，例：/音频裁剪 10 25"""
        await self.app.handle_command(event, "音频裁剪")

    @filter.command("音频调速")
    async def cmd_audio_speed(self, event: AstrMessageEvent):
        """音频变速（默认保持音调），例：/音频调速 1.5"""
        await self.app.handle_command(event, "音频调速")

    @filter.command("音量", alias={"音频音量"})
    async def cmd_audio_volume(self, event: AstrMessageEvent):
        """调整音量，例：/音量 +6dB"""
        await self.app.handle_command(event, "音量")

    @filter.command("音频压缩")
    async def cmd_audio_compress(self, event: AstrMessageEvent):
        """按码率压缩音频，例：/音频压缩 64k"""
        await self.app.handle_command(event, "音频压缩")

    @filter.command("音频静音")
    async def cmd_audio_mute(self, event: AstrMessageEvent):
        """把指定时间段静音，例：/音频静音 3 5"""
        await self.app.handle_command(event, "音频静音")

    @filter.command("去除静音", alias={"音频去静音"})
    async def cmd_audio_desilence(self, event: AstrMessageEvent):
        """删除音频中的静音片段"""
        await self.app.handle_command(event, "去除静音")

    @filter.command("音频信息")
    async def cmd_audio_info(self, event: AstrMessageEvent):
        """查看音频格式、时长、码率"""
        await self.app.handle_command(event, "音频信息")
