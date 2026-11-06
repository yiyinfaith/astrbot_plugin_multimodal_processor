# 多模态处理器

AstrBot 本地多模态文件处理插件（AGPL-3.0）。支持图片/GIF、QQ 图片保存、幻影坦克，以及基于 FFmpeg 的视频和音频处理。

## 安装

将目录放入 AstrBot 插件目录，安装 Python 依赖，并在系统中安装 `ffmpeg`/`ffprobe`：

```bash
pip install -r requirements.txt
# Debian/Ubuntu
sudo apt install ffmpeg
```

## 指令

图片/GIF：`/反色`、`/顺时针`、`/逆时针`、`/左右翻转`、`/上下翻转`、`/左对称`、`/右对称`、`/上对称`、`/下对称`、`/调速 2`；格式与尺寸：`/图片转换 webp`、`/图片压缩 80`、`/图片缩放 800x600`、`/图片裁剪 0,0,800,600`、`/图片信息`。

QQ 图片保存：引用 QQ 图片/GIF 后发送 `/转换`；`/rename 文件名` 修改外显文件名；管理员可用 `/convert addblock 群号`、`/convert delblock 群号`、`/convert blocklist` 管理群限制。QQ/NapCat 不支持时会明确提示。

幻影坦克：`/幻影坦克`、`/彩色幻影坦克`，随后按提示发送表图和里图；发送 `取消` 可中止。也支持在命令消息中直接附带两张图片。

视频：`/视频转GIF [fps] [宽度] [时长]`、`/GIF转视频`、`/视频转换 mp4|webm|mkv`、`/视频压缩 [CRF]`、`/视频裁剪 开始 结束`、`/视频调速 倍率`、`/视频缩放 宽x高`、`/视频旋转 90|180|270`、`/提取音频`、`/视频静音`、`/提取视频帧 秒`。

音频：`/音频转MP3`、`/音频转换 mp3|wav|ogg|m4a`、`/音频裁剪 开始 结束`、`/音频调速 倍率`、`/音量 1.5`、`/音频压缩 128k`、`/音频静音 开始 结束`、`/音频信息`。

所有命令都可引用消息、直接附带媒体或使用文件消息。开启 `match_mode` 后，图片命令可省略 `/`。输出在发送完成后清理，异常和超时也会清理任务目录。GIF 默认保留动画、时长、循环和透明度；转换为静态格式时使用命令明确指定。

主要配置见 `_conf_schema.json`，视频/音频功能需要 FFmpeg。参考项目致谢：`astrbot_plugin_pic_toolbox`（GPL 系列）、`astrbot_plugins_ConvetPicture`（AGPL-3.0）和 `astrbot_plugin_mirage_tank`（MIT）；本项目未携带其素材，算法按许可证保留来源说明。

## 许可证

本项目采用 [GNU AGPL v3.0](LICENSE)。
