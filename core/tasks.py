"""任务调度与临时目录管理。

- 每个任务独占一个目录，结束（成功、失败、取消、超时）后立即删除。
- 重型任务（FFmpeg）和轻型任务（Pillow/NumPy）分别限制并发；排队数和单用户并发也有上限。
- 后台定期删除过期目录，作为进程崩溃等极端情况下的保底清理。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable

from .errors import Busy

log = logging.getLogger("astrbot")

TASK_PREFIX = "task-"


@dataclass(slots=True)
class TaskSlot:
    workdir: Path
    queued: int  # 进入时前面排队的任务数


class TaskManager:
    def __init__(
        self,
        root: Path,
        *,
        heavy_concurrency: int = 1,
        light_concurrency: int = 2,
        queue_limit: int = 5,
        per_user_limit: int = 2,
        expire_seconds: int = 3600,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._heavy = asyncio.Semaphore(max(1, heavy_concurrency))
        self._light = asyncio.Semaphore(max(1, light_concurrency))
        self._heavy_limit = max(1, heavy_concurrency)
        self.queue_limit = max(0, queue_limit)
        self.per_user_limit = max(1, per_user_limit)
        self.expire_seconds = max(60, expire_seconds)
        self._waiting_heavy = 0
        self._running_heavy = 0
        self._user_tasks: dict[str, int] = defaultdict(int)
        self._active: set[Path] = set()
        self._sweeper: asyncio.Task | None = None

    # ------------------------------------------------------------------ 目录
    def new_dir(self, label: str = "task") -> Path:
        path = self.root / f"{TASK_PREFIX}{label}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=False)
        self._active.add(path)
        return path

    def release_dir(self, path: Path) -> None:
        self._active.discard(path)
        shutil.rmtree(path, ignore_errors=True)

    @asynccontextmanager
    async def scratch(self, label: str = "session") -> AsyncIterator[Path]:
        """不占并发名额的临时目录（例如幻影坦克等待用户发图时）。"""
        path = self.new_dir(label)
        try:
            yield path
        finally:
            self.release_dir(path)

    # ------------------------------------------------------------------ 任务
    @property
    def heavy_busy(self) -> bool:
        return self._running_heavy >= self._heavy_limit

    @asynccontextmanager
    async def task(
        self,
        user_key: str,
        *,
        heavy: bool,
        label: str = "task",
        on_wait: Callable[[int], Awaitable[None]] | None = None,
    ) -> AsyncIterator[TaskSlot]:
        """on_wait(前面排队数) 只在需要排队时调用一次，用于提示用户。"""
        if self._user_tasks.get(user_key, 0) >= self.per_user_limit:
            raise Busy(f"你已有 {self._user_tasks[user_key]} 个任务在处理中，请等它们完成后再试")
        if heavy and self.heavy_busy and self._waiting_heavy >= self.queue_limit:
            raise Busy("当前排队的任务太多，请稍后再试")
        self._user_tasks[user_key] += 1
        queued = self._waiting_heavy if heavy and self.heavy_busy else 0
        semaphore = self._heavy if heavy else self._light
        workdir: Path | None = None
        try:
            if heavy and self.heavy_busy and on_wait is not None:
                try:
                    await on_wait(queued)
                except Exception:  # 提示发送失败不影响任务本身
                    pass
            if heavy:
                self._waiting_heavy += 1
                try:
                    await semaphore.acquire()
                finally:
                    self._waiting_heavy -= 1
                self._running_heavy += 1
            else:
                await semaphore.acquire()
            try:
                workdir = self.new_dir(label)
                yield TaskSlot(workdir, queued)
            finally:
                if workdir is not None:
                    self.release_dir(workdir)
                if heavy:
                    self._running_heavy -= 1
                semaphore.release()
        finally:
            self._user_tasks[user_key] -= 1
            if self._user_tasks[user_key] <= 0:
                self._user_tasks.pop(user_key, None)

    @staticmethod
    async def run_blocking(func: Callable, *args, **kwargs):
        """在线程池中执行 CPU 密集的同步函数，不阻塞事件循环。"""
        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    async def with_timeout(awaitable: Awaitable, seconds: float):
        return await asyncio.wait_for(awaitable, timeout=seconds)

    # ------------------------------------------------------------------ 清理
    def sweep(self, max_age: float | None = None) -> int:
        """删除超过 max_age 秒、且当前没有任务在用的目录。"""
        limit = self.expire_seconds if max_age is None else max_age
        now = time.time()
        removed = 0
        for path in self.root.glob(f"{TASK_PREFIX}*"):
            if path in self._active:
                continue
            try:
                if now - path.stat().st_mtime >= limit:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def start(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        interval = min(600, max(60, self.expire_seconds // 4))
        while True:
            await asyncio.sleep(interval)
            try:
                removed = self.sweep()
                if removed:
                    log.info(f"[多模态处理器] 清理了 {removed} 个过期临时目录")
            except Exception as exc:  # pragma: no cover - 保底任务不能退出
                log.warning(f"[多模态处理器] 清理临时目录失败：{exc}")

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):
                pass
            self._sweeper = None
        # 停用插件时，没有任务在用的目录全部删除
        self.sweep(max_age=0)
