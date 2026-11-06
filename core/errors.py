"""插件内统一使用的异常类型。

UserError 及其子类的 message 会原样回复给用户，其他异常只写日志、回复通用提示。
"""

from __future__ import annotations


class UserError(Exception):
    """可以直接展示给用户的错误。"""


class UsageError(UserError):
    """指令参数错误，回复时会附带该指令的用法。"""


class MediaMissing(UserError):
    """消息里没有找到所需的媒体。"""


class MediaError(UserError):
    """媒体下载、读取或格式校验失败。"""


class MediaTooLarge(MediaError):
    """媒体超过大小、时长、分辨率或内存限制。"""


class UnsupportedPlatform(UserError):
    """当前平台不支持该功能。"""


class FeatureDisabled(UserError):
    """功能在配置中被关闭。"""


class ToolMissing(UserError):
    """FFmpeg / FFprobe 不可用。"""


class ProcessingError(UserError):
    """处理过程失败（例如 FFmpeg 返回错误）。"""


class Busy(UserError):
    """任务队列已满或用户同时提交了过多任务。"""
