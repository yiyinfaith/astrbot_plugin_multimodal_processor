"""可选的 meme-generator 适配层。

不复制 meme-generator 的内置或用户扩展素材，也不在插件安装时执行 pip 重装。
运行时按 API 形状适配 Python 0.1.x 和 Rust 0.2.x；AstrBot 的依赖版本由
宿主环境决定，用户自定义的 site-packages/meme_generator/memes 会被原样使用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import importlib
from importlib.machinery import PathFinder, SourceFileLoader
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, get_args

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


def _at_token(token: str) -> bool:
    return token.startswith("@") and len(token) > 1


def _left_boundary(value: str, index: int) -> bool:
    """关键词左边是消息开头、空白，或紧贴一个 @xxx。"""
    if index <= 0 or value[index - 1].isspace():
        return True
    return _at_token(value[:index].rsplit(None, 1)[-1])


def _right_boundary(value: str, end: int) -> bool:
    """关键词右边是消息结尾、空白，或紧贴一个 @xxx。"""
    if end >= len(value) or value[end].isspace():
        return True
    return _at_token(value[end:].split(None, 1)[0])


class _LenientTags(type):
    """额外表情引用了 MemeTags 里没有的标签时返回空集合，不让它导入失败。"""

    missing: set[str] = set()

    def __getattr__(cls, name: str) -> set[str]:
        if name.startswith("__"):
            raise AttributeError(name)
        _LenientTags.missing.add(name)
        return set()


class _TagsLoader(SourceFileLoader):
    def exec_module(self, module: Any) -> None:
        super().exec_module(module)
        tags = getattr(module, "MemeTags", None)
        if isinstance(tags, type) and not isinstance(tags, _LenientTags):
            module.MemeTags = _LenientTags("MemeTags", (tags,), {})


class _TagsFinder:
    """只在导入 meme_generator 期间生效。表情在包初始化时就加载，所以要在导入时替换，不修改磁盘文件。"""

    @staticmethod
    def find_spec(name: str, path: Any = None, target: Any = None) -> Any:
        if name != "meme_generator.tags":
            return None
        spec = PathFinder.find_spec(name, path, target)
        if spec is not None and isinstance(spec.loader, SourceFileLoader):
            spec.loader = _TagsLoader(spec.loader.name, spec.loader.path)
        return spec


def _import_package() -> Any:
    finder = _TagsFinder()
    sys.meta_path.insert(0, finder)
    try:
        return importlib.import_module("meme_generator")
    finally:
        sys.meta_path.remove(finder)


@dataclass(slots=True)
class MemeStatus:
    available: bool = False
    engine: str = "auto"
    version: str = ""
    reason: str = ""


# 关键词列表图片的缓存。改了渲染参数就把版本加一，让旧图失效
LIST_RENDER_VERSION = 1
LIST_NAME = "meme_list"
LIST_SUFFIXES = (".png", ".jpg", ".gif", ".webp", ".bin")
LIST_REFRESH_DELAY = 3  # 插件加载完几秒后再开始，不拖慢安装和重载


@dataclass(slots=True)
class ListRefresh:
    """一次检查 / 渲染关键词列表的结果。"""

    count: int
    previous: int | None  # 上次渲染时的表情数量；没有记录时为 None
    changed: bool  # 表情包资源和上次渲染时不同（没有记录也算）
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rendered: bool = False
    seconds: float = 0.0
    error: str = ""


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
        self.list_dir = Path(app.data_dir) / "meme_list"
        self._list_lock = asyncio.Lock()
        self._list_tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        if not self.settings.enable:
            self.status.reason = "表情包生成未启用"
            return
        try:  # 加载上千个表情要几十秒，放进线程避免卡住 AstrBot
            package = await asyncio.to_thread(_import_package)
        except Exception as exc:
            self.status.reason = "未安装 meme-generator；请按 README 手动安装匹配版本"
            log.warning("[多模态处理器] %s: %s", self.status.reason, exc)
            return
        if _LenientTags.missing:
            log.info("[多模态处理器] 额外表情用到的 %d 个标签不在 tags.py 中，已自动兜底", len(_LenientTags.missing))
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
        """按触发词查找，也接受 /表情包搜索 返回的英文文件夹名。"""
        lowered = keyword.strip().casefold()
        return self._keyword_map.get(lowered) or self._key_map.get(lowered)

    def match(self, text: str, *, at_start_only: bool = False) -> tuple[str, str] | None:
        """关键词左右都要有边界：空白、消息开头或结尾、@xxx。长词优先。

        返回 (关键词, 去掉关键词和 @xxx 后的其余文字)。at_start_only 用于带唤醒前缀的消息，
        这类消息的第一个词是指令名，关键词在后面时不抢其他插件的指令。
        """
        value = text.strip()
        prefix = self.settings.extra_prefix
        for keyword in self._keywords:
            token = prefix + keyword
            index = value.find(token)
            while index >= 0:
                end = index + len(token)
                if _left_boundary(value, index) and _right_boundary(value, end):
                    if at_start_only and index:
                        break
                    rest = f"{value[:index]} {value[end:]}".split()
                    return keyword, " ".join(t for t in rest if not _at_token(t))
                index = value.find(token, index + 1)
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

    # ------------------------------------------------------------ 关键词列表缓存
    def cached_list(self) -> Path | None:
        """已渲染好的列表图片；没有时返回 None。"""
        for suffix in LIST_SUFFIXES:
            path = self.list_dir / f"{LIST_NAME}{suffix}"
            if path.is_file() and path.stat().st_size:
                return path
        return None

    def _read_list_meta(self) -> dict | None:
        try:
            meta = json.loads((self.list_dir / f"{LIST_NAME}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def _list_fingerprint(self) -> tuple[str, list[str]]:
        """列表图片只取决于各表情的名称、关键词和图片/文字数量（决定分类图标），素材图片不影响。"""
        rows = []
        for meme in self.memes:
            params = self._params(meme)
            rows.append([
                self._key(meme),
                [str(k) for k in (getattr(self._info(meme), "keywords", None) or [])],
                [int(getattr(params, n, 0) or 0) for n in ("min_images", "max_images", "min_texts", "max_texts")],
            ])
        rows.sort(key=lambda row: row[0])
        payload = json.dumps([LIST_RENDER_VERSION, self.status.engine, self.status.version, rows], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest(), [row[0] for row in rows]

    def _save_list(self, data: bytes, fingerprint: str, keys: list[str]) -> None:
        """先写临时文件再替换，发送中的旧图不会读到一半；画质原样保留，不压缩。"""
        self.list_dir.mkdir(parents=True, exist_ok=True)
        target = self.list_dir / f"{LIST_NAME}{self.suffix(data)}"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        for suffix in LIST_SUFFIXES:
            other = self.list_dir / f"{LIST_NAME}{suffix}"
            if other != target:
                other.unlink(missing_ok=True)
        meta = {"fingerprint": fingerprint, "count": len(keys), "keys": keys, "file": target.name, "rendered_at": int(time.time())}
        meta_tmp = self.list_dir / f"{LIST_NAME}.json.tmp"
        meta_tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(meta_tmp, self.list_dir / f"{LIST_NAME}.json")

    def _builtin_memes_dir(self) -> Path | None:
        try:
            return Path(self.package.__file__).parent / "memes"
        except Exception:
            return None

    def _rescan_python(self) -> list[Any]:
        """Python 版只在 AstrBot 启动导入时加载表情，重载插件不会重新导入：
        这里补加载 memes 目录里新增的文件夹，并去掉文件夹已删除的表情。已有表情的代码改动要重启 AstrBot 才生效。"""
        package = self.package
        memes_dir = self._builtin_memes_dir()
        config = getattr(getattr(package, "config", None), "meme", None)
        if memes_dir is not None and memes_dir.is_dir() and getattr(config, "load_builtin_memes", True):
            importlib.invalidate_caches()
            for path in sorted(memes_dir.iterdir()):
                if path.name.startswith(("_", ".")) or not (path.is_dir() or path.suffix == ".py"):
                    continue
                module = f"meme_generator.memes.{path.stem if path.is_file() else path.name}"
                if module not in sys.modules:
                    log.info("[多模态处理器] 发现新表情：%s", path.name)
                    package.load_meme(module)  # 导入失败由 meme-generator 自己记录日志

        def alive(meme: Any) -> bool:
            module = str(getattr(getattr(meme, "function", None), "__module__", "") or "")
            parts = module.split(".")
            if memes_dir is None or len(parts) < 3 or parts[:2] != ["meme_generator", "memes"]:
                return True
            return (memes_dir / parts[2]).is_dir() or (memes_dir / f"{parts[2]}.py").is_file()

        return [meme for meme in package.get_memes() if alive(meme)]

    async def _rescan(self) -> None:
        if self.package is None or self.status.engine != "python" or not hasattr(self.package, "load_meme"):
            return
        try:
            memes = await asyncio.to_thread(self._rescan_python)
        except Exception as exc:
            log.warning("[多模态处理器] 重新扫描表情包失败：%s", exc)
            return
        if memes:
            self.memes = memes
            self._rebuild_index()

    async def refresh_list(self, *, force: bool) -> ListRefresh:
        """检查表情包资源是否有更新，有更新（或还没有图片）时重新渲染列表；force 时总是重新渲染。"""
        async with self._list_lock:
            await self._rescan()
            fingerprint, keys = self._list_fingerprint()
            meta = self._read_list_meta()
            old_keys = set(meta.get("keys") or []) if meta else set()
            result = ListRefresh(
                count=len(keys),
                previous=int(meta.get("count") or 0) if meta else None,
                changed=meta is None or meta.get("fingerprint") != fingerprint,
                added=sorted(set(keys) - old_keys) if meta else [],
                removed=sorted(old_keys - set(keys)),
            )
            if not force and not result.changed and self.cached_list() is not None:
                return result
            started = time.monotonic()
            try:
                data = await self.render_list()
                await asyncio.to_thread(self._save_list, data, fingerprint, keys)
                result.rendered = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result.error = str(exc) or type(exc).__name__
                log.warning("[多模态处理器] 表情包列表渲染失败：%s", result.error)
            result.seconds = time.monotonic() - started
            return result

    def start_list_refresh(self, *, force: bool, delay: float = 0) -> asyncio.Task:
        """在后台检查并渲染列表。返回的任务被取消或等待超时都不影响渲染继续进行（插件停止时除外）。"""

        async def runner() -> ListRefresh:
            if delay:
                await asyncio.sleep(delay)
            result = await self.refresh_list(force=force)
            if result.rendered:
                log.info("[多模态处理器] 表情包列表已重新渲染（%d 个表情，用时 %.1f 秒）", result.count, result.seconds)
            return result

        task = asyncio.create_task(runner())
        self._list_tasks.add(task)
        task.add_done_callback(self._list_task_done)
        return task

    def _list_task_done(self, task: asyncio.Task) -> None:
        self._list_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("[多模态处理器] 表情包列表后台任务出错：%s", task.exception())

    def schedule_list_refresh(self) -> None:
        """安装或重载插件之后（不是加载过程中）检查资源更新，并在需要时重新渲染列表。"""
        if self.status.available and self.settings.enable:
            self.start_list_refresh(force=False, delay=LIST_REFRESH_DELAY)

    async def stop(self) -> None:
        tasks = list(self._list_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

    async def handle_dynamic(self, event: Any, text: str, explicit: bool = True, at_start_only: bool = False) -> bool:
        """返回 True 表示已处理。explicit 为 False 表示普通聊天（没带前缀或 @）。"""
        if not self.status.available or not self.settings.enable:
            return False
        matched = self.match(text, at_start_only=at_start_only)
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
