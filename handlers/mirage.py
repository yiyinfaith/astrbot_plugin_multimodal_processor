"""隐藏图指令：幻影坦克、光棱坦克（制作）与光棱显形。

制作类指令优先使用指令里直接附带（或引用）的两张图片；不足两张时进入分步会话，依次接收表图、里图。
会话按「会话来源 + 发送者」隔离：群里不同人同时生成互不干扰，别人的图片不会被收走。
原幻影坦克插件用 session_id 区分会话，在群聊里等同于整个群共用一个会话，这里已修复。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.app import Invocation
from ..core.args import parse_int
from ..core.errors import MediaError, UsageError, UserError
from ..core.formats import IMAGE
from ..core.media import MediaFile, MediaRef, collect_refs
from ..processors.image.ops import ImageResult
from ..processors.mirage.algorithm import generate as mirage_generate
from ..processors.prism import algorithm as prism
from .image import run_image_job

try:
    from astrbot.core.utils.session_waiter import SessionController, SessionFilter, session_waiter
except ImportError:  # pragma: no cover - 测试环境
    SessionController = SessionFilter = session_waiter = None

CANCEL_WORDS = {"取消", "退出", "cancel"}


def session_key(event: Any) -> str:
    return f"{event.unified_msg_origin}#{event.get_sender_id()}"


if SessionFilter is not None:

    class SenderSessionFilter(SessionFilter):
        """以「会话来源 + 发送者」作为会话标识。"""

        def filter(self, event: Any) -> str:
            try:
                return session_key(event)
            except Exception:
                return ""
else:  # pragma: no cover
    SenderSessionFilter = None


async def _fetch_images(inv: Invocation, event: Any, refs: list[MediaRef], workdir: Path, limit: int, max_bytes: int) -> list[MediaFile]:
    images: list[MediaFile] = []
    for ref in refs:
        if len(images) >= limit:
            break
        media = await inv.app.resolver.fetch(event, ref, workdir, max_bytes)
        if media.sniffed.kind != IMAGE:
            raise MediaError("收到的文件不是图片")
        images.append(media)
    return images


@dataclass(slots=True)
class TwoImageJob:
    """一次「表图 + 里图」生成任务的描述。"""

    title: str
    timeout: int
    max_bytes: int
    front_hint: str
    back_hint: str
    done_text: str
    make: Callable[[Path, Path, Path], Path]  # (表图, 里图, 输出) -> 输出


async def _generate_and_send(inv: Invocation, event: Any, job: TwoImageJob, front: Path, back: Path) -> None:
    async with inv.app.tasks.task(inv.user_key, heavy=False, label="hidden") as slot:
        out = slot.workdir / "result.png"
        await inv.run_timed(inv.app.tasks.run_blocking(job.make, front, back, out))
        await inv.app.sender.image(event, out, job.done_text)


async def two_image_flow(inv: Invocation, job: TwoImageJob) -> bool:
    event = inv.event
    key = session_key(event)
    if key in inv.app.mirage_sessions:
        raise UserError("你已有一个进行中的表图/里图会话，请先发送图片，或发送「取消」结束")
    refs = [r for r in collect_refs(event) if r.accepts(IMAGE)]
    timeout = job.timeout

    async with inv.app.tasks.scratch("hidden-session") as workdir:
        given = await _fetch_images(inv, event, refs, workdir, 2, job.max_bytes) if refs else []
        if len(given) >= 2:
            await _generate_and_send(inv, event, job, given[0].path, given[1].path)
            return True
        if session_waiter is None:
            raise UserError("当前 AstrBot 版本不支持分步会话，请随指令同时附带两张图片（第一张为表图）")

        state: dict[str, Path] = {}
        if given:
            state["front"] = given[0].path
            await inv.reply(f"已收到表图，请在 {timeout} 秒内发送里图（{job.back_hint}），发送「取消」可中止")
        else:
            await inv.reply(f"请在 {timeout} 秒内发送表图（{job.front_hint}），发送「取消」可中止")

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, incoming: Any) -> None:
            text = str(getattr(incoming, "message_str", "") or "").strip().lstrip("/")
            if text in CANCEL_WORDS:
                await inv.app.sender.text(incoming, f"已取消{job.title}生成")
                controller.stop()
                return
            incoming_refs = [r for r in collect_refs(incoming) if r.accepts(IMAGE)]
            if not incoming_refs:
                await inv.app.sender.text(incoming, "请发送一张图片，或发送「取消」结束")
                return
            try:
                received = await _fetch_images(inv, incoming, incoming_refs, workdir, 2, job.max_bytes)
            except MediaError as exc:
                await inv.app.sender.text(incoming, f"{exc}，请重新发送")
                return
            for media in received:
                if "front" not in state:
                    state["front"] = media.path
                elif "back" not in state:
                    state["back"] = media.path
            if "back" not in state:
                await inv.app.sender.text(incoming, f"已收到表图，请发送里图（{job.back_hint}）")
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            try:
                await _generate_and_send(inv, incoming, job, state["front"], state["back"])
            except UserError as exc:
                await inv.app.sender.text(incoming, str(exc))
            controller.stop()

        inv.app.mirage_sessions.add(key)
        try:
            await waiter(event, session_filter=SenderSessionFilter())
        except TimeoutError:
            await inv.reply(f"{job.title}等待超时，已取消")
        finally:
            inv.app.mirage_sessions.discard(key)
    return True


async def mirage(inv: Invocation) -> bool:
    mode = "color" if inv.parsed.spec.name.startswith("彩色") else "gray"
    m = inv.settings.mirage
    title = "彩色幻影坦克" if mode == "color" else "幻影坦克"

    def make(front: Path, back: Path, out: Path) -> Path:
        return mirage_generate(
            front, back, out, mode=mode, max_side=m.max_side,
            gray_front_ratio=m.gray_front_ratio, gray_back_ratio=m.gray_back_ratio, a=m.a, b=m.b, w=m.w,
        )

    job = TwoImageJob(
        title, m.timeout, m.max_bytes, "白色背景下显示的图", "黑色背景下显示的图",
        f"{title}生成完毕：白色背景显示表图，点开大图（黑色背景）显示里图", make,
    )
    return await two_image_flow(inv, job)


async def prism_tank(inv: Invocation) -> bool:
    p = inv.settings.prism
    if len(inv.args) > 1 or (inv.args and inv.args[0] not in {"黑白", "灰度", "彩色"}):
        raise UsageError(f"无法识别的参数：{' '.join(inv.args)}")
    colored = p.colored if not inv.args else inv.args[0] == "彩色"

    def make(front: Path, back: Path, out: Path) -> Path:
        return prism.generate(
            front, back, out, colored=colored, max_side=p.max_side,
            inner_limit=p.inner_limit, cover_limit=p.cover_limit, rate=p.rate,
        )

    job = TwoImageJob(
        "光棱坦克", p.timeout, p.max_bytes, "平时看到的图", "被隐藏的图",
        f"光棱坦克生成完毕：平时显示表图。保存原图后发送 /光棱显形，或在修图软件里把暗部色阶 0～{p.inner_limit} 拉伸到 0～255 即可看到里图。"
        "转发时请勾选「原图」，被压缩后可能无法显形。",
        make,
    )
    return await two_image_flow(inv, job)


async def prism_reveal(inv: Invocation) -> bool:
    threshold = parse_int(inv.args[0], "阈值", 1, 200) if inv.args else None
    default = inv.settings.prism.inner_limit

    def process(src: Path, out_base: Path) -> ImageResult:
        path, used, source = prism.reveal(src, out_base.with_suffix(".png"), threshold=threshold, default_threshold=default)
        return ImageResult(path, f"光棱显形完成（阈值 {used}，来源：{source}）。效果不理想时可以手动指定阈值，例如 /光棱显形 48")

    return await run_image_job(inv, process, allow_avatar=False)


HANDLERS = {"mirage": mirage, "prism_tank": prism_tank, "prism_reveal": prism_reveal}
