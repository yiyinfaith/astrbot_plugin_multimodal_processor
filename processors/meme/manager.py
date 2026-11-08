"""可选的 meme-generator 适配层。

不复制 meme-generator 的内置或用户扩展素材，也不在插件安装时执行 pip 重装。
运行时按配置适配 Python 0.1.x 和 Rust 0.2.x API；因此 AstrBot 的依赖版本由
宿主环境决定，用户自定义的 site-packages/meme_generator/memes 会被原样使用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging
from pathlib import Path
import re
from typing import Any

from PIL import Image

from ...core.errors import MediaMissing, ProcessingError, UserError
from ...core.formats import IMAGE
from ...core.media import IMAGE as IMAGE_REF, MediaRef, at_targets, collect_refs, qq_avatar_url, component_type
from ...platforms import qq

log = logging.getLogger("astrbot")


@dataclass(slots=True)
class MemeStatus:
    available: bool = False
    engine: str = "python"
    version: str = ""
    reason: str = ""


class MemeManager:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.settings = app.settings.meme
        self.status = MemeStatus(engine=self.settings.engine)
        self.package: Any = None
        self.memes: list[Any] = []
        self._keywords: list[str] = []
        self._keyword_map: dict[str, Any] = {}
        self._resource_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if not self.settings.enable:
            self.status.reason = "表情包生成未启用"
            return
        try:
            import meme_generator  # type: ignore
        except Exception as exc:
            self.status.reason = "未安装 meme-generator；请按 README 手动安装匹配版本"
            log.warning("[多模态处理器] %s: %s", self.status.reason, exc)
            return
        self.package = meme_generator
        self.status.version = self._version(meme_generator)
        is_rust = self._is_rust(meme_generator, self.status.version)
        self.status.engine = "rust" if is_rust else "python"
        if self.settings.engine != self.status.engine:
            self.status.reason = f"配置要求 {self.settings.engine} 版，但当前加载的是 {self.status.version or '未知'}（{self.status.engine}）"
            log.warning("[多模态处理器] %s", self.status.reason)
            return
        try:
            self.memes = list(meme_generator.get_memes())
            self._rebuild_index()
            self.status.available = bool(self.memes)
            if not self.memes:
                self.status.reason = "meme-generator 没有加载到任何表情包"
            if self.settings.check_resources:
                self._resource_task = asyncio.create_task(self._check_resources())
        except Exception as exc:
            self.status.reason = f"读取 meme-generator 表情包失败：{exc}"
            log.exception("[多模态处理器] meme-generator 初始化失败")

    async def _check_resources(self) -> None:
        try:
            if self.status.engine == "python":
                from meme_generator.download import check_resources  # type: ignore
                await check_resources()
            else:
                from meme_generator.resources import check_resources_in_background  # type: ignore
                await asyncio.to_thread(check_resources_in_background)
        except Exception as exc:
            log.warning("[多模态处理器] meme-generator 资源检查失败：%s", exc)

    async def close(self) -> None:
        if self._resource_task and not self._resource_task.done():
            self._resource_task.cancel()
            try:
                await self._resource_task
            except asyncio.CancelledError:
                pass
        self._resource_task = None

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
    def _is_rust(package: Any, version: str) -> bool:
        try:
            major, minor = (int(x) for x in version.split(".")[:2])
            return major > 0 or minor >= 2
        except (ValueError, IndexError):
            return hasattr(package, "Image") and hasattr(package, "MemeSortBy")

    def _info(self, meme: Any) -> Any:
        return getattr(meme, "info", meme)

    def _params(self, meme: Any) -> Any:
        return getattr(meme, "info", meme).params_type if self.status.engine == "python" else meme.info.params

    def _keywords_for(self, meme: Any) -> list[str]:
        info = self._info(meme)
        keywords = getattr(info, "keywords", []) or []
        key = str(getattr(meme, "key", "") or getattr(info, "key", ""))
        return list(dict.fromkeys([key, *(str(x) for x in keywords)]))

    def _rebuild_index(self) -> None:
        self._keywords.clear(); self._keyword_map.clear()
        for meme in self.memes:
            for keyword in self._keywords_for(meme):
                if not keyword: continue
                lowered = keyword.casefold()
                self._keyword_map.setdefault(lowered, meme)
                self._keywords.append(keyword)
        self._keywords = sorted(dict.fromkeys(self._keywords), key=lambda x: (-len(x), x.casefold()))

    @property
    def keywords(self) -> list[str]:
        return list(self._keywords)

    def is_keyword(self, keyword: str) -> bool:
        return keyword.casefold() in self._keyword_map

    def find(self, keyword: str) -> Any | None:
        return self._keyword_map.get(keyword.strip().casefold())

    def match(self, text: str) -> str | None:
        value = text.strip()
        if self.settings.extra_prefix:
            if not value.startswith(self.settings.extra_prefix): return None
            value = value[len(self.settings.extra_prefix):].strip()
        if self.settings.fuzzy_match:
            lowered = value.casefold()
            return next((keyword for keyword in self._keywords if keyword.casefold() in lowered), None)
        tokens = value.split()
        for keyword in self._keywords:
            if keyword in tokens or value == keyword or value.startswith(keyword + " "):
                return keyword
        return None

    def search(self, text: str) -> list[str]:
        q = text.strip().casefold()
        return [x for x in self._keywords if q and q in x.casefold()]

    def disabled_for(self, event: Any) -> list[str]:
        try: group_id = str(event.get_group_id() or "")
        except Exception: group_id = ""
        return self.settings.disabled_for(group_id)

    def _meme_description(self, meme: Any) -> str:
        info = self._info(meme); params = self._params(meme)
        key = str(getattr(meme, "key", "") or getattr(info, "key", ""))
        keywords = getattr(info, "keywords", []) or []
        tags = getattr(info, "tags", []) or []
        lines = [f"名称：{key}", f"别名：{list(keywords)}"]
        min_images, max_images = int(getattr(params, "min_images", 0)), int(getattr(params, "max_images", 0))
        min_texts, max_texts = int(getattr(params, "min_texts", 0)), int(getattr(params, "max_texts", 0))
        if max_images: lines.append(f"所需图片：{min_images}张" if min_images == max_images else f"所需图片：{min_images}~{max_images}张")
        if max_texts: lines.append(f"所需文本：{min_texts}段" if min_texts == max_texts else f"所需文本：{min_texts}~{max_texts}段")
        defaults = getattr(params, "default_texts", []) or []
        if defaults: lines.append(f"默认文本：{defaults}")
        if tags: lines.append(f"标签：{sorted(tags)}")
        return "\n".join(lines)

    async def details(self, keyword: str) -> tuple[str, bytes] | None:
        meme = self.find(keyword)
        if not meme: return None
        preview = await asyncio.to_thread(meme.generate_preview)
        return self._meme_description(meme), self._as_bytes(preview)

    async def render_list(self) -> bytes | None:
        if not self.status.available: return None
        if self.status.engine == "python":
            from meme_generator.utils import MemeProperties, render_meme_list  # type: ignore
            data = await asyncio.to_thread(render_meme_list, [(m, MemeProperties()) for m in self.memes], text_template="{index}.{keywords}", add_category_icon=True)
        else:
            from meme_generator.tools import MemeProperties, MemeSortBy, render_meme_list  # type: ignore
            props = {str(m.key): MemeProperties() for m in self.memes}
            data = await asyncio.to_thread(render_meme_list, meme_properties=props, exclude_memes=[], sort_by=MemeSortBy.KeywordsPinyin, sort_reverse=False, text_template="{index}. {keywords}", add_category_icon=True)
        return self._as_bytes(data)

    @staticmethod
    def _as_bytes(data: Any) -> bytes:
        if isinstance(data, bytes): return data
        if isinstance(data, BytesIO): return data.getvalue()
        if isinstance(data, bytearray): return bytes(data)
        if data is None: raise ProcessingError("meme-generator 没有返回图片")
        error = getattr(data, "error", None) or getattr(data, "feedback", None)
        raise ProcessingError(str(error or data))

    async def generate(self, event: Any, keyword: str, slot: Any) -> bytes:
        meme = self.find(keyword)
        if not meme: raise UserError(f"未找到表情包关键词：{keyword}")
        images, texts, options = await self._collect_params(event, meme, keyword, slot)
        if self.status.engine == "python":
            result = await asyncio.to_thread(meme, images=images, texts=texts, args=options)
        else:
            meme_image = self.package.Image
            wrapped = [meme_image(name=f"image_{i}", data=image) for i, image in enumerate(images)]
            result = await asyncio.to_thread(meme.generate, wrapped, texts, options)
        return self._as_bytes(result)

    async def _collect_params(self, event: Any, meme: Any, keyword: str, slot: Any) -> tuple[list[bytes], list[str], dict[str, Any]]:
        params = self._params(meme)
        max_images = int(getattr(params, "max_images", 0))
        images: list[bytes] = []
        refs = [ref for ref in collect_refs(event) if ref.accepts(IMAGE_REF)]
        if max_images:
            for ref in refs[:max_images]:
                media = await self.app.resolver.fetch(event, ref, slot.workdir, self.app.settings.image.max_bytes)
                if media.sniffed.kind == IMAGE: images.append(media.path.read_bytes())
            if qq.is_onebot(event) and len(images) < max_images:
                target_ids = at_targets(event)
                for uid in [*target_ids, str(event.get_sender_id()), str(event.get_self_id())]:
                    if len(images) >= max_images: break
                    try:
                        media = await self.app.resolver.fetch_url(qq_avatar_url(uid), slot.workdir, self.app.settings.image.max_bytes, f"avatar_{uid}")
                        if media.sniffed.kind == IMAGE: images.append(media.path.read_bytes())
                    except Exception: continue
        texts: list[str] = []
        for component in self._all_components(event):
            if component_type(component) != "plain": continue
            raw = str(getattr(component, "text", "") or "").replace("\r", " ").replace("\n", " ")
            for token in raw.split():
                token = token.strip()
                if token.startswith("/"): token = token[1:]
                if token and token != keyword and token != self.settings.extra_prefix and not token.startswith("--"):
                    texts.append(token)
        default_texts = list(getattr(params, "default_texts", []) or [])
        max_texts = int(getattr(params, "max_texts", 0))
        if len(texts) < max_texts: texts.extend(default_texts[: max_texts - len(texts)])
        texts = texts[:max_texts]
        options: dict[str, Any] = {}
        # 兼容额外参数：--key value 或 --key=value，交给 meme-generator 自身校验类型。
        raw_text = str(getattr(event, "message_str", "") or "")
        for match in re.finditer(r"--([\w-]+)(?:=|\s+)([^\s]+)", raw_text):
            options[match.group(1).replace("-", "_")] = match.group(2)
        if qq.is_onebot(event):
            target = at_targets(event) or [str(event.get_sender_id())]
            try:
                info = await qq.call(event, "get_stranger_info", user_id=int(target[0]))
                options["user_infos"] = [{"name": str((info or {}).get("nickname", "")), "gender": str((info or {}).get("sex", "unknown"))}]
            except Exception:
                pass
        return images[:max_images], texts, options

    @staticmethod
    def _all_components(event: Any) -> list[Any]:
        try:
            components = list(event.get_messages())
        except Exception:
            components = []
        result: list[Any] = []
        for component in components:
            if component_type(component) == "reply": result.extend(getattr(component, "chain", None) or [])
            result.append(component)
        return result

    @staticmethod
    def _compress(data: bytes) -> bytes:
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format == "GIF" or (getattr(image, "n_frames", 1) > 1): return data
                if max(image.size) <= 512: return data
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                out = BytesIO(); fmt = image.format or "PNG"
                if fmt in {"JPEG", "JPG"}: image.convert("RGB").save(out, "JPEG", quality=85)
                else: image.save(out, fmt)
                return out.getvalue()
        except Exception:
            return data

    async def handle_dynamic(self, event: Any, text: str) -> bool:
        if not self.status.available or not self.settings.enable: return False
        keyword = self.match(text)
        if not keyword: return False
        if keyword in self.disabled_for(event): return True
        user_key = f"{getattr(event, 'unified_msg_origin', '?')}#{getattr(event, 'get_sender_id', lambda: '?')()}"
        async with self.app.tasks.task(user_key, heavy=False, label="meme") as slot:
            data = await asyncio.wait_for(self.generate(event, keyword, slot), timeout=self.settings.timeout)
            if self.settings.compress_image: data = self._compress(data)
            suffix = ".gif" if data[:6] in {b"GIF87a", b"GIF89a"} else ".png"
            output = slot.workdir / f"meme_{re.sub(r'[^a-zA-Z0-9_-]', '_', keyword)[:30]}{suffix}"
            output.write_bytes(data)
            await self.app.sender.image(event, output, f"表情包：{keyword}")
        return True
