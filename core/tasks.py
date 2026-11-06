from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import tempfile
import time
import uuid


class TaskManager:
    def __init__(self, root: str | Path | None = None, concurrency: int = 1, timeout: int = 300):
        self.root = Path(root or Path(tempfile.gettempdir()) / "astrbot_multimodal_processor")
        self.root.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(max(1, int(concurrency)))
        self.timeout = max(5, int(timeout))

    @asynccontextmanager
    async def task(self, heavy: bool = False):
        work = self.root / f"task-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True, exist_ok=False)
        try:
            if heavy:
                async with self.sem:
                    yield work
            else:
                yield work
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def cleanup_stale(self, age_seconds: int = 3600) -> int:
        now = time.time(); count = 0
        for path in self.root.glob("task-*"):
            try:
                if now - path.stat().st_mtime > age_seconds:
                    shutil.rmtree(path, ignore_errors=True); count += 1
            except OSError:
                continue
        return count
