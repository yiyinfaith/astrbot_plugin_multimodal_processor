"""AstrBot logger adapter with a silent fallback for standalone tests."""

from __future__ import annotations

from typing import Any, Callable


class _FallbackLogger:
    """Keep processing modules importable without AstrBot installed.

    The real plugin must use ``astrbot.api.logger``.  A no-op fallback keeps
    the pure processing tests quiet without importing the standard-library
    logging stack as a second logger implementation.
    """

    @staticmethod
    def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    debug: Callable[..., None] = _noop
    info: Callable[..., None] = _noop
    warning: Callable[..., None] = _noop
    error: Callable[..., None] = _noop
    exception: Callable[..., None] = _noop


try:  # AstrBot runtime
    from astrbot.api import logger as log
except ImportError:  # pragma: no cover - standalone unit tests
    log = _FallbackLogger()


__all__ = ["log"]
