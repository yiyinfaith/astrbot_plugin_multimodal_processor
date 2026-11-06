"""媒体获取的安全检查（SSRF、路径、大小、真实类型）与任务目录清理、并发控制。"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image

from conftest import comp, run

from mmp.core import download as dl
from mmp.core.errors import Busy, MediaError, MediaTooLarge
from mmp.core.formats import sniff_bytes, sniff_file
from mmp.core.media import MediaRef, MediaResolver, at_targets, collect_refs
from mmp.core.tasks import TaskManager


# ---------------------------------------------------------------- 格式识别
@pytest.mark.parametrize(
    "head, kind, ext",
    [
        (b"GIF89a....", "image", ".gif"),
        (b"\x89PNG\r\n\x1a\n....", "image", ".png"),
        (b"\xff\xd8\xff\xe0", "image", ".jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image", ".webp"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00", "av", ".mp4"),
        (b"\x00\x00\x00\x18ftypM4A \x00\x00", "audio", ".m4a"),
        (b"\x1aE\xdf\xa3\x00webm", "av", ".webm"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio", ".wav"),
        (b"OggS\x00\x02" + b"\x00" * 22 + b"OpusHead", "av", ".opus"),
        (b"ID3\x03", "audio", ".mp3"),
        (b"\xff\xf1\x50\x80", "audio", ".aac"),
        (b"\x02#!SILK_V3", "silk", ".silk"),
        (b"fLaC\x00", "audio", ".flac"),
        (b"<html>", "unknown", ".bin"),
    ],
)
def test_sniff(head, kind, ext):
    result = sniff_bytes(head)
    assert (result.kind, result.ext) == (kind, ext)


# ---------------------------------------------------------------- SSRF
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png", "http://localhost/a.png", "http://10.0.0.5/x", "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
        "http://100.64.0.1/", "http://0.0.0.0/", "file:///etc/passwd", "ftp://example.com/a",
        "http://user:pass@example.com/a", "gopher://example.com/",
    ],
)
def test_validate_url_blocks_private_and_odd_schemes(url):
    with pytest.raises(MediaError):
        dl.validate_url(url)


def test_validate_url_allows_public():
    dl.validate_url("https://multimedia.nt.qq.com.cn/download?appid=1")
    dl.validate_url("http://8.8.8.8/a.png")


def test_resolver_blocks_domains_pointing_to_private_ips():
    async def go():
        resolver = dl._PublicOnlyResolver()
        try:
            with pytest.raises(MediaError):
                await resolver.resolve("localhost", 80)
        finally:
            await resolver.close()

    run(go())


def _serve(handler_map):
    async def start():
        app = web.Application()
        for path, handler in handler_map.items():
            app.router.add_get(path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, port

    return start


def test_download_streams_with_size_limit_and_blocks_redirect_to_private(tmp_path):
    big = b"x" * 300_000

    async def big_handler(request):
        resp = web.StreamResponse()  # 不带 Content-Length，必须靠流式计数中断
        await resp.prepare(request)
        for i in range(0, len(big), 50_000):
            await resp.write(big[i:i + 50_000])
        return resp

    async def ok_handler(request):
        return web.Response(body=b"GIF89a" + b"0" * 100)

    async def redirect_handler(request):
        raise web.HTTPFound("http://127.0.0.1:1/secret")

    async def go():
        runner, port = await _serve({"/big": big_handler, "/ok": ok_handler, "/redir": redirect_handler})()
        try:
            base = f"http://127.0.0.1:{port}"
            # 默认拒绝内网
            with pytest.raises(MediaError):
                await dl.download(base + "/ok", tmp_path / "a", 10_000)
            # 显式允许内网后可以下载
            size = await dl.download(base + "/ok", tmp_path / "b", 10_000, allow_private=True)
            assert size == 106
            with pytest.raises(MediaTooLarge):
                await dl.download(base + "/big", tmp_path / "c", 100_000, allow_private=True)
            assert not (tmp_path / "c").exists(), "超限后应删除半成品"
        finally:
            await runner.cleanup()

    run(go())


# ---------------------------------------------------------------- 本地路径与组件
def test_local_paths_only_inside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.png"
    Image.new("RGB", (4, 4)).save(outside)
    inside = allowed / "in.png"
    Image.new("RGB", (4, 4)).save(inside)
    work = tmp_path / "work"
    work.mkdir()
    resolver = MediaResolver([allowed])

    async def go():
        ok = await resolver.fetch(None, MediaRef(comp("Image", file=str(inside), url="", path=""), "image", "message"), work, 10_000)
        assert ok.sniffed.ext == ".png" and ok.path.parent == work
        with pytest.raises(MediaError):
            await resolver.fetch(None, MediaRef(comp("Image", file=str(outside), url="", path=""), "image", "message"), work, 10_000)
        traversal = str(allowed / ".." / "outside.png")
        with pytest.raises(MediaError):
            await resolver.fetch(None, MediaRef(comp("Image", file=traversal, url="", path=""), "image", "message"), work, 10_000)
        with pytest.raises(MediaError):
            await resolver.fetch(None, MediaRef(comp("Image", file=(allowed / "in.png").as_uri().replace("allowed/in.png", "outside.png"), url="", path=""), "image", "message"), work, 10_000)
        with pytest.raises(MediaTooLarge):
            await resolver.fetch(None, MediaRef(comp("Image", file=str(inside), url="", path=""), "image", "message"), work, 10)
        payload = "base64://" + base64.b64encode(inside.read_bytes()).decode()
        b64 = await resolver.fetch(None, MediaRef(comp("Image", file=payload, url="", path=""), "image", "message"), work, 10_000)
        assert sniff_file(b64.path).ext == ".png"

    run(go())


def test_collect_refs_order_and_at_targets():
    reply = comp("Reply", id="1", chain=[comp("Image", file="r.png", url="", path="")])
    event = type("E", (), {})()
    event.get_messages = lambda: [
        reply, comp("At", qq="12345"), comp("At", qq="99999"), comp("At", qq="all"),
        comp("Plain", text="反色"), comp("Video", file="v.mp4", url="", path=""), comp("File", name="a.mp3", url="", file_=""),
    ]
    event.get_self_id = lambda: "99999"
    refs = collect_refs(event)
    assert [(r.origin, r.hint) for r in refs] == [("reply", "image"), ("message", "video"), ("message", "audio")]
    assert at_targets(event) == ["12345"]
    assert refs[1].accepts("audio") and not refs[0].accepts("video")


# ---------------------------------------------------------------- 任务管理
def test_task_dirs_cleaned_on_success_error_cancel_and_timeout(tmp_path):
    manager = TaskManager(tmp_path / "tasks")
    seen: list[Path] = []

    async def ok():
        async with manager.task("u", heavy=False) as slot:
            (slot.workdir / "x").write_text("1")
            seen.append(slot.workdir)

    async def boom():
        async with manager.task("u", heavy=True) as slot:
            seen.append(slot.workdir)
            raise RuntimeError("boom")

    async def slow():
        async with manager.task("u", heavy=True) as slot:
            seen.append(slot.workdir)
            await asyncio.sleep(10)

    async def go():
        await ok()
        with pytest.raises(RuntimeError):
            await boom()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow(), 0.2)
        task = asyncio.create_task(slow())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert len(seen) == 4
    assert all(not p.exists() for p in seen)
    assert list((tmp_path / "tasks").iterdir()) == []
    assert manager._user_tasks == {} and manager._running_heavy == 0 and manager._waiting_heavy == 0


def test_heavy_tasks_are_serialized_and_queue_limited(tmp_path):
    manager = TaskManager(tmp_path / "tasks", heavy_concurrency=1, queue_limit=1, per_user_limit=5)
    running = 0
    peak = 0
    waits: list[int] = []

    async def job(user):
        nonlocal running, peak

        async def on_wait(ahead):
            waits.append(ahead)

        async with manager.task(user, heavy=True, on_wait=on_wait):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.1)
            running -= 1

    async def go():
        first = asyncio.create_task(job("a"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(job("b"))
        await asyncio.sleep(0.01)
        with pytest.raises(Busy):  # 正在运行 1 个、排队 1 个，排队已满
            await job("c")
        await asyncio.gather(first, second)

    run(go())
    assert peak == 1 and waits == [0]


def test_per_user_limit(tmp_path):
    manager = TaskManager(tmp_path / "tasks", per_user_limit=1, light_concurrency=4)

    async def go():
        async with manager.task("same", heavy=False):
            with pytest.raises(Busy):
                async with manager.task("same", heavy=False):
                    pass
            async with manager.task("other", heavy=False):
                pass

    run(go())


def test_sweep_removes_only_stale_unused_dirs(tmp_path):
    manager = TaskManager(tmp_path / "tasks", expire_seconds=60)
    stale = manager.root / "task-old"
    stale.mkdir()
    old = time.time() - 3600
    import os

    os.utime(stale, (old, old))
    active = manager.new_dir("busy")
    os.utime(active, (old, old))
    fresh = manager.root / "task-fresh"
    fresh.mkdir()
    assert manager.sweep() == 1
    assert not stale.exists() and active.exists() and fresh.exists()
    manager.release_dir(active)
    assert not active.exists()
