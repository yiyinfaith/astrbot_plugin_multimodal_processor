# 第三方许可与版权说明

本插件整体以 GNU Affero General Public License v3.0 发布（见 LICENSE）。以下项目的设计或代码思路被参考、改写后并入本插件。

## astrbot_plugin_pic_toolbox

- 仓库：https://github.com/lirundong093-glitch/astrbot_plugin_pic_toolbox
- 作者：Lucy（lirundong093-glitch）
- 许可：GNU AGPL-3.0
- 使用范围：反色、旋转、翻转、半边对称、GIF 逐帧处理与调速（含帧率上限与丢帧策略）、@用户取头像、精准关键词匹配的功能设计。实现已重写（改用 Pillow 写 GIF，修正奇数尺寸对称错位、按平均帧率计算调速倍率等问题）。
- 未包含：摸头、发射、杀、撅人、鞭笞等表情包生成器及其全部图片素材、人脸检测模型与 OpenCV 依赖。

## astrbot_plugins_ConvetPicture

- 仓库：https://github.com/orchidsziyou/astrbot_plugins_ConvetPicture
- 作者：orchidsziyou
- 许可：GNU AGPL-3.0
- 使用范围：通过 OneBot 接口把 QQ 图片 / GIF / 官方表情重新发送为文件、修改图片外显文字（summary）、群聊黑名单的功能设计。实现已重写（加入平台检测、私聊优先发送、真实格式识别、下载安全检查）。

## astrbot_plugin_mirage_tank

- 仓库：https://github.com/Yuzi-Liang/astrbot_plugin_mirage_tank
- 许可：MIT，原文如下：

```
MIT License

Copyright (c) 2025 poisama

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- 使用范围：黑白 / 彩色幻影坦克的像素公式（`processors/mirage/algorithm.py`）与「依次发送表图、里图」的交互方式。改动：会话改为按发送者隔离、里图居中裁剪对齐、输出尺寸上限、黑白模式参数可配置，并修正原配置校验中 `w` 越界时误改 `b` 的问题。

## astrbot_plugin_gifcaijian

- 仓库：https://github.com/shskjw/astrbot_plugin_gifcaijian
- 作者：shskjw
- 许可：GNU AGPL-3.0
- 使用范围：视频转动图的参数写法（时间段、抽帧、缩放）、GIF 加速 / 减速、精灵图合成、多图合成（含合并转发与 @ 头像）、GIF 分解、网格裁剪与边距、图片转线稿、表情包做旧的功能设计。实现已重写：视频转动图改用 FFmpeg 调色板滤镜（不再依赖 imageio），动图读写保留透明度与循环，线稿改用素描（颜色减淡）算法，做旧使用固定随机种子，网格切图对动图逐帧切割，并加入尺寸、帧数与下载安全限制。
- 未包含：示例图片（`11/` 目录）。

## 光棱坦克原理

- 参考：https://github.com/TankFactory/Mirage_Image 、https://github.com/TankFactory/Mirage_Decode （MIT）及其中引用的百度贴吧帖子「开个贴细说新式高科技坦克」
- 使用范围：「灰度值分离 + 像素交错」的隐藏图原理。`processors/prism/algorithm.py` 为独立实现（NumPy 向量化），未复制上述仓库的代码；显形算法（自动阈值、归一化滤波补齐）为本插件自行设计。

## astrbot-plugin-pic-mirror

- 仓库：https://github.com/FenChen0211/astrbot-plugin-pic-mirror
- 使用范围：精准关键词匹配（无前缀触发）的思路，经 astrbot_plugin_pic_toolbox 间接引入；未使用其代码。
