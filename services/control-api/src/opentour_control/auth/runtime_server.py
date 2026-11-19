"""Serve one control auth runtime and release it before Uvicorn replays signals."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import uvicorn

from .runtime import ControlAuthRuntime

GRACEFUL_SHUTDOWN_SECONDS = 20


class _RuntimeServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, close: Callable[[], None]) -> None:
        super().__init__(config)
        self._close_runtime = close

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        with super().capture_signals():
            try:
                yield
            finally:
                # Uvicorn restores the original handlers and re-raises captured
                # signals as it leaves this context. SIGTERM can exit immediately.
                self._close_runtime()


def serve_auth_runtime(
    runtime: ControlAuthRuntime, configure: Callable[[], uvicorn.Config]
) -> None:
    """Own runtime cleanup through configuration, startup, serving, and shutdown."""
    closed = False

    def close_once() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if not runtime.close():
            raise SystemExit("Control auth runtime shutdown is incomplete")

    try:
        config = configure()
        config.timeout_graceful_shutdown = GRACEFUL_SHUTDOWN_SECONDS
        _RuntimeServer(config, close_once).run()
    finally:
        close_once()
