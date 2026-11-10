"""可选的 meme-generator 适配层。

不复制 meme-generator 的内置或用户扩展素材，也不在插件安装时执行 pip 重装。
运行时按 API 形状适配 Python 0.1.x 和 Rust 0.2.x；AstrBot 的依赖版本由
宿主环境决定，用户自定义的 site-packages/meme_generator/memes 会被原样使用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, get_args

from PIL import Image

from ...core.errors import MediaError, MediaTooLarge, ProcessingError, UserError
from ...core.formats import IMAGE
from ...core.log import log
from ...core.media import IMAGE as IMAGE_REF, MediaRef, at_targets, collect_refs, component_type, qq_avatar_url
from ...platforms import qq
from ..gif import frames as fr

_TRUE_WORDS = {"true", "1", "yes", "on"}
_FALSE_WORDS = {"false", "0", "no", "off"}
_MISSING_ASSET = "表情素材缺失，请按 README 下载 meme-generator 资源"


class MemeInputError(UserError):
    """用户给的图片、文字或选项不符合该表情的要求。

    没有带前缀或 @ 的普通聊天碰巧以关键词开头时，这类错误不回复，消息继续交给其他插件。
    """


@dataclass(slots=True)
class MemeStatus:
    available: bool = False
    engine: str = "auto"
    version: str = ""
    reason: str = ""


class MemeManager:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.settings = app.settings.meme
        self.status = MemeStatus()
        self.package: Any = None
        self.memes: list[Any] = []
        self._keywords: list[str] = []
        self._keyword_map: dict[str, Any] = {}
        self._key_map: dict[str, Any] = {}

    async def initialize(self) -> None:
        if not self.settings.enable:
            self.status.reason = "表情包生成未启用"
            return
        try:  # 加载上千个表情要几十秒，放进线程避免卡住 AstrBot
            package = await asyncio.to_thread(importlib.import_module, "meme_generator")
        except Exception as exc:
            self.status.reason = "未安装 meme-generator；请按 README 手动安装匹配版本"
            log.warning("[多模态处理器] %s: %s", self.status.reason, exc)
            return
        self.package = package
        self.status.version = self._version(package)
        self.status.engine = "rust" if self._is_rust(package) else "python"
        try:
            self.memes = await asyncio.to_thread(lambda: list(package.get_memes()))
            self._rebuild_index()
            self.status.available = bool(self.memes)
            if not self.memes:
                self.status.reason = "meme-generator 没有加载到任何表情包"
        except Exception as exc:
            self.status.reason = f"读取 meme-generator 表情包失败：{exc}"
            log.exception("[多模态处理器] meme-generator 初始化失败")

    @staticmethod
    def _version(package: Any) -> str:
        value = getattr(package, "__version__", "")
        if not value and hasattr(package, "get_version"):
            try:
                value = package.get_version()
            except Exception:
                value = ""
        return str(value or "")

    @staticmethod
    def _is_rust(package: Any) -> bool:
        """按 API 形状识别实现，不把版本号写死。

        Python 版暴露 ``Meme.__call__``/``MemeParamsType``；Rust 版暴露
        ``Image``、``MemeSortBy``，且 Meme 类提供 ``generate``。版本号只用于
        日志显示，避免 0.1.12、0.1.14 或其他兼容发行版被误判。
        """
        meme_cls = getattr(package, "Meme", None)
        return bool(
            hasattr(package, "Image")
            and hasattr(package, "MemeSortBy")
            and meme_cls is not None
            and hasattr(meme_cls, "generate")
        )

    @staticmethod
    def _info(meme: Any) -> Any:
        return getattr(meme, "info", meme)

    def _params(self, meme: Any) -> Any:
        info = self._info(meme)
        return info.params_type if self.status.engine == "python" else info.params

    def _key(self, meme: Any) -> str:
        return str(getattr(meme, "key", "") or getattr(self._info(meme), "key", ""))

    def _keywords_for(self, meme: Any) -> list[str]:
        """触发词只用表情声明的关键词；英文文件夹名很多是常见单词，不参与触发（没有关键词时才用它）。"""
        keywords = [str(x) for x in (getattr(self._info(meme), "keywords", None) or []) if str(x).strip()]
        return list(dict.fromkeys(keywords)) or [self._key(meme)]

    def _rebuild_index(self) -> None:
        self._keyword_map.clear()
        self._key_map.clear()
        keywords: list[str] = []
        for meme in self.memes:
            key = self._key(meme)
            if key:
                self._key_map.setdefault(key.casefold(), meme)
            for keyword in self._keywords_for(meme):
                if keyword:
                    self._keyword_map.setdefault(keyword.casefold(), meme)
                    keywords.append(keyword)
        self._keywords = sorted(dict.fromkeys(keywords), key=lambda x: (-len(x), x.casefold()))

    def find(self, keyword: str) -> Any | None:
        """按触发词查找，也接受 /meme搜索 返回的英文文件夹名。"""
        lowered = keyword.strip().casefold()
        return self._keyword_map.get(lowered) or self._key_map.get(lowered)

    def match(self, text: str) -> tuple[str, str] | None:
        """整条消息必须以关键词开头（后面是空白或结束），返回 (关键词, 关键词之后的文字)。"""
        value = text.strip()
        if self.settings.extra_prefix:
            if not value.startswith(self.settings.extra_prefix):
                return None
            value = value[len(self.settings.extra_prefix):].strip()
        for keyword in self._keywords:  # 长词优先，兼容带空格的关键词
            if value == keyword or value.startswith(keyword + " "):
                return keyword, value[len(keyword):].strip()
        return None

    def search_pairs(self, text: str) -> list[tuple[str, str]]:
        q = text.strip().casefold()
        if not q:
            return []
        return [(keyword, self._key(self.find(keyword))) for keyword in self._keywords if q in keyword.casefold()]

    def _is_disabled(self, event: Any, meme: Any) -> bool:
        """禁用列表里写该表情的任意关键词或英文文件夹名，都禁用整个表情。"""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        disabled = set(self.settings.disabled_for(group_id))
        return bool(disabled & {self._key(meme), *self._keywords_for(meme)})

    def _meme_description(self, meme: Any) -> str:
        info = self._info(meme)
        params = self._params(meme)
        keywords = getattr(info, "keywords", []) or []
        tags = getattr(info, "tags", []) or []
        lines = [f"名称：{self._key(meme)}", f"别名：{list(keywords)}"]
        min_images, max_images = int(getattr(params, "min_images", 0)), int(getattr(params, "max_images", 0))
        min_texts, max_texts = int(getattr(params, "min_texts", 0)), int(getattr(params, "max_texts", 0))
        if max_images:
            lines.append(f"所需图片：{min_images}张" if min_images == max_images else f"所需图片：{min_images}~{max_images}张")
        if max_texts:
            lines.append(f"所需文本：{min_texts}段" if min_texts == max_texts else f"所需文本：{min_texts}~{max_texts}段")
        defaults = getattr(params, "default_texts", []) or []
        if defaults:
            lines.append(f"默认文本：{defaults}")
        if tags:
            lines.append(f"标签：{sorted(tags)}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 调用 meme-generator
    async def _call(self, func: Callable, *args: Any, **kwargs: Any) -> bytes:
        """在线程里调用 meme-generator，把两种实现的错误统一成给用户看的提示。"""
        try:
            result = await asyncio.to_thread(func, *args, **kwargs)
        except FileNotFoundError as exc:
            log.warning("[多模态处理器] meme-generator 素材缺失：%s", exc)
            raise ProcessingError(_MISSING_ASSET) from exc
        except Exception as exc:
            names = {cls.__name__ for cls in type(exc).__mro__}
            if "MemeGeneratorException" not in names:
                raise
            message = str(getattr(exc, "message", "") or exc)
            if names & {"ParamsMismatch", "TextOverLength", "MemeFeedback"}:
                raise MemeInputError(message) from exc
            raise ProcessingError(f"表情包生成失败：{message}") from exc
        return self._as_bytes(result)

    @staticmethod
    def _as_bytes(data: Any) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, BytesIO):
            return data.getvalue()
        if isinstance(data, bytearray):
            return bytes(data)
        if data is None:
            raise ProcessingError("meme-generator 没有返回图片")
        # Rust 版用返回值表示错误
        kind = type(data).__name__
        if kind == "ImageNumberMismatch":
            raise MemeInputError(f"图片数量不符，应为 {_span(data.min, data.max)} 张")
        if kind == "TextNumberMismatch":
            raise MemeInputError(f"文字数量不符，应为 {_span(data.min, data.max)} 段")
        if kind == "TextOverLength":
            raise MemeInputError(f"文字「{str(data.text)[:10]}」过长")
        if kind == "MemeFeedback":
            raise MemeInputError(str(data.feedback))
        if kind == "ImageAssetMissing":
            log.warning("[多模态处理器] meme-generator 素材缺失：%s", data.path)
            raise ProcessingError(_MISSING_ASSET)
        raise ProcessingError(f"表情包生成失败：{getattr(data, 'error', None) or kind}")

    async def details(self, keyword: str) -> tuple[str, bytes] | None:
        meme = self.find(keyword)
        if not meme:
            return None
        return self._meme_description(meme), await self._call(meme.generate_preview)

    async def render_list(self) -> bytes:
        if self.status.engine == "python":
            from meme_generator.utils import MemeProperties, render_meme_list  # type: ignore

            items = [(m, MemeProperties()) for m in self.memes]
            return await self._call(render_meme_list, items, text_template="{index}.{keywords}", add_category_icon=True)
        from meme_generator.tools import MemeProperties, MemeSortBy, render_meme_list  # type: ignore

        props = {str(m.key): MemeProperties() for m in self.memes}
        return await self._call(
            render_meme_list, meme_properties=props, exclude_memes=[], sort_by=MemeSortBy.KeywordsPinyin,
            sort_reverse=False, text_template="{index}. {keywords}", add_category_icon=True,
        )

    @staticmethod
    def suffix(data: bytes) -> str:
        """按真实文件头取扩展名，平台据此判断 MIME。"""
        if data[:6] in {b"GIF87a", b"GIF89a"}:
            return ".gif"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        return ".bin"

    async def generate(self, event: Any, keyword: str, rest: str, slot: Any) -> bytes:
        meme = self.find(keyword)
        if not meme:
            raise UserError(f"未找到表情包关键词：{keyword}")
        images, texts, options = await self._collect_params(event, meme, rest, slot)
        if self.status.engine == "python":
            return await self._call(meme, images=images, texts=texts, args=options)
        wrapped = [self.package.Image(name=f"image_{i}", data=image) for i, image in enumerate(images)]
        return await self._call(meme.generate, wrapped, texts, options)

    # ------------------------------------------------------------ 收集参数
    async def _collect_params(self, event: Any, meme: Any, rest: str, slot: Any) -> tuple[list[bytes], list[str], dict[str, Any]]:
        params = self._params(meme)
        kinds = self._option_kinds(params)
        images, avatar_ids = await self._collect_images(event, params, slot)
        texts, options = self._parse_tokens(rest.split(), kinds)
        if not texts:  # 用户没写文字时才用引用消息的文字
            texts = self._reply_tokens(event)
        texts = self._fit_texts(texts, params)
        min_images, max_images = int(getattr(params, "min_images", 0)), int(getattr(params, "max_images", 0))
        if not min_images <= len(images) <= max_images:
            raise MemeInputError(f"这个表情需要 {_span(min_images, max_images)} 张图片，可以引用图片、附带图片或 @ 用户")
        min_texts, max_texts = int(getattr(params, "min_texts", 0)), int(getattr(params, "max_texts", 0))
        if not min_texts <= len(texts) <= max_texts:
            raise MemeInputError(f"这个表情需要 {_span(min_texts, max_texts)} 段文字，用空格分隔")
        if qq.is_onebot(event):
            await self._add_user_options(event, options, kinds, avatar_ids)
        return images, texts, options

    def _read_image(self, path: Path) -> bytes:
        s = self.app.settings.image
        _, size, frames = fr.probe(path)
        fr.check_limits(size, frames, fr.ImageLimits(s.max_pixels, s.max_frames, s.memory_bytes))
        return path.read_bytes()

    async def _fetch_image(self, event: Any, ref: MediaRef, slot: Any) -> bytes | None:
        """取不到或不是图片时返回 None，由后面的数量检查决定如何提示；超过限制直接报错。"""
        try:
            media = await self.app.resolver.fetch(event, ref, slot.workdir, self.app.settings.image.max_bytes)
            if media.sniffed.kind != IMAGE:
                return None
            return await asyncio.to_thread(self._read_image, media.path)
        except MediaTooLarge:
            raise
        except MediaError as exc:
            log.debug("[多模态处理器] 表情包图片获取失败：%s", exc)
            return None

    async def _fetch_avatar(self, event: Any, uid: str, slot: Any) -> bytes | None:
        ref = MediaRef(None, "avatar", "avatar", name=f"avatar_{uid}", url=qq_avatar_url(uid))
        return await self._fetch_image(event, ref, slot)

    async def _collect_images(self, event: Any, params: Any, slot: Any) -> tuple[list[bytes], list[str]]:
        """返回 (图片, 头像对应的 QQ 号)。

        顺序：引用/附带的图片 → @ 的用户。仍不够最少张数时，把发送者、机器人的头像插到最前面：
        两张图的表情约定第一张是「动作发出者」，这样「亲 @某人」才是发送者亲对方。
        """
        min_images, max_images = int(getattr(params, "min_images", 0)), int(getattr(params, "max_images", 0))
        images: list[bytes] = []
        avatar_ids: list[str] = []
        if not max_images:
            return images, avatar_ids
        for ref in collect_refs(event):
            if len(images) >= max_images:
                break
            if ref.accepts(IMAGE_REF) and (data := await self._fetch_image(event, ref, slot)):
                images.append(data)
        if not qq.is_onebot(event):
            return images, avatar_ids
        for uid in at_targets(event):
            if len(images) >= max_images:
                break
            if data := await self._fetch_avatar(event, uid, slot):
                images.append(data)
                avatar_ids.append(uid)
        for uid in (str(event.get_sender_id()), str(event.get_self_id())):
            if len(images) >= min_images:
                break
            if data := await self._fetch_avatar(event, uid, slot):
                images.insert(0, data)
                avatar_ids.insert(0, uid)
        return images, avatar_ids

    def _option_kinds(self, params: Any) -> dict[str, str]:
        """该表情支持的选项 -> 类型（bool / int / float / str；Python 版非布尔统一为 value，交给 pydantic）。"""
        if self.status.engine == "python":
            model = getattr(getattr(params, "args_type", None), "args_model", None)
            fields = getattr(model, "model_fields", None) or getattr(model, "__fields__", None) or {}
            kinds: dict[str, str] = {}
            for name, info in fields.items():
                if name != "user_infos":
                    annotation = getattr(info, "annotation", None) or getattr(info, "outer_type_", None)
                    kinds[name] = "bool" if annotation is bool or bool in get_args(annotation) else "value"
            return kinds
        names = {"BooleanOption": "bool", "IntegerOption": "int", "FloatOption": "float", "StringOption": "str"}
        return {str(o.name): names.get(type(o).__name__, "str") for o in (getattr(params, "options", None) or [])}

    @staticmethod
    def _coerce(name: str, value: Any, kind: str) -> Any:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            lowered = str(value).lower()
            if lowered in _TRUE_WORDS or lowered in _FALSE_WORDS:
                return lowered in _TRUE_WORDS
            raise MemeInputError(f"选项 --{name} 只能是 true 或 false")
        if kind in {"int", "float"}:
            try:
                return int(value) if kind == "int" else float(value)
            except (TypeError, ValueError) as exc:
                raise MemeInputError(f"选项 --{name} 需要填数字") from exc
        return value

    def _parse_tokens(self, tokens: list[str], kinds: dict[str, str]) -> tuple[list[str], dict[str, Any]]:
        """拆出文字和表情选项（--key value、--key=value、--开关）；该表情不支持的 --xxx 按普通文字处理。"""
        texts: list[str] = []
        options: dict[str, Any] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            name, sep, value = token[2:].partition("=") if token.startswith("--") else ("", "", "")
            name = name.replace("-", "_")
            if name not in kinds:
                texts.append(token)
                continue
            if sep:
                options[name] = self._coerce(name, value, kinds[name])
            elif kinds[name] != "bool" and index < len(tokens) and not tokens[index].startswith("--"):
                options[name] = self._coerce(name, tokens[index], kinds[name])
                index += 1
            elif kinds[name] == "bool":
                options[name] = True
            else:
                raise MemeInputError(f"选项 --{name} 需要填写值")
        return texts, options

    @staticmethod
    def _reply_tokens(event: Any) -> list[str]:
        tokens: list[str] = []
        try:
            components = list(event.get_messages())
        except Exception:
            return tokens
        for component in components:
            if component_type(component) == "reply":
                for item in getattr(component, "chain", None) or []:
                    if component_type(item) == "plain":
                        tokens.extend(str(getattr(item, "text", "") or "").split())
        return tokens

    @staticmethod
    def _fit_texts(texts: list[str], params: Any) -> list[str]:
        """没写文字用默认文字；写少了按位置补默认文字（最多补到最少段数）；写多了截断。"""
        min_texts, max_texts = int(getattr(params, "min_texts", 0)), int(getattr(params, "max_texts", 0))
        defaults = list(getattr(params, "default_texts", []) or [])
        if not texts and min_texts <= len(defaults) <= max_texts:
            return defaults
        if len(texts) < min_texts:
            texts = [*texts, *defaults[len(texts):min_texts]]
        return texts[:max_texts]

    async def _user_info(self, event: Any, uid: str) -> dict[str, str] | None:
        try:
            info = await qq.call(event, "get_stranger_info", user_id=int(uid))
        except Exception:
            return None
        info = info or {}
        gender = str(info.get("sex", "unknown"))
        return {"name": str(info.get("nickname", "")), "gender": gender if gender in {"male", "female"} else "unknown"}

    async def _add_user_options(self, event: Any, options: dict[str, Any], kinds: dict[str, str], avatar_ids: list[str]) -> None:
        """补充昵称、性别。Python 版用 user_infos（与头像顺序一致）；Rust 版只接受标量选项。"""
        targets = avatar_ids or at_targets(event)[:1] or [str(event.get_sender_id())]
        infos = [info for info in await asyncio.gather(*(self._user_info(event, uid) for uid in targets)) if info]
        if not infos:
            return
        if self.status.engine == "python":
            options["user_infos"] = infos
            return
        for name in ("name", "gender"):
            if name in kinds and name not in options:
                options[name] = infos[-1][name]

    # ------------------------------------------------------------ 关键词触发
    @staticmethod
    def _compress(data: bytes) -> bytes:
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format == "GIF" or getattr(image, "n_frames", 1) > 1:
                    return data
                if max(image.size) <= 512:
                    return data
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                out = BytesIO()
                fmt = image.format or "PNG"
                if fmt in {"JPEG", "JPG"}:
                    image.convert("RGB").save(out, "JPEG", quality=85)
                else:
                    image.save(out, fmt)
                return out.getvalue()
        except Exception:
            return data

    async def handle_dynamic(self, event: Any, text: str, explicit: bool = True) -> bool:
        """返回 True 表示已处理。explicit 为 False 表示普通聊天（没带前缀或 @）。"""
        if not self.status.available or not self.settings.enable:
            return False
        matched = self.match(text)
        if not matched:
            return False
        keyword, rest = matched
        meme = self.find(keyword)
        if meme is None or self._is_disabled(event, meme):
            return False
        user_key = f"{getattr(event, 'unified_msg_origin', '?')}#{getattr(event, 'get_sender_id', lambda: '?')()}"
        try:
            async with self.app.tasks.task(user_key, heavy=False, label="meme") as slot:
                data = await asyncio.wait_for(self.generate(event, keyword, rest, slot), timeout=self.settings.timeout)
                if self.settings.compress_image:
                    data = await asyncio.to_thread(self._compress, data)
                output = slot.workdir / f"meme_{re.sub(r'[^a-zA-Z0-9_-]', '_', keyword)[:30]}{self.suffix(data)}"
                await asyncio.to_thread(output.write_bytes, data)
                await self.app.sender.image(event, output)
        except MemeInputError:
            if explicit:
                raise
            return False
        return True


def _span(low: int, high: int) -> str:
    return f"{low}" if low == high else f"{low}~{high}"


__all__ = ["MemeInputError", "MemeManager", "MemeStatus"]
