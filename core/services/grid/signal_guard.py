"""Cleanup-safe SIGINT handling for the grid asyncio entrypoint."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from typing import Any

logger = logging.getLogger(__name__)


class ProcessSigintGuard:
    """Prevent asyncio.run's second Ctrl+C from aborting exchange cleanup."""

    def __init__(self, loop: Any, main_task: Any, previous_handler: Any) -> None:
        self.loop = loop
        self.main_task = main_task
        self.previous_handler = previous_handler
        self.requested = False
        self.installed = False

    def install(self) -> bool:
        if threading.current_thread() is not threading.main_thread():
            return False
        try:
            signal.signal(signal.SIGINT, self.handle)
        except (AttributeError, OSError, RuntimeError, ValueError):
            return False
        self.installed = True
        self.main_task.add_done_callback(lambda _task: self.restore())
        return True

    def restore(self) -> None:
        if not self.installed:
            return
        self.installed = False
        try:
            signal.signal(signal.SIGINT, self.previous_handler)
        except (AttributeError, OSError, RuntimeError, ValueError):
            pass

    def handle(self, signum: int, frame: Any) -> None:
        if self.loop.is_closed() or self.main_task.done():
            self.restore()
            previous = self.previous_handler
            if callable(previous):
                previous(signum, frame)
            elif previous != signal.SIG_IGN:
                raise KeyboardInterrupt
            return

        if not self.requested:
            self.requested = True
            self.loop.call_soon_threadsafe(self.main_task.cancel)
            return

        self.loop.call_soon_threadsafe(
            logger.warning,
            "Shutdown cleanup is already running; additional Ctrl+C was ignored.",
        )


_guard: ProcessSigintGuard | None = None


def install_cleanup_safe_sigint_guard() -> None:
    """Install once while the grid entrypoint's main asyncio task is active."""
    global _guard
    if _guard is not None and _guard.installed:
        return
    if threading.current_thread() is not threading.main_thread():
        return

    try:
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
    except RuntimeError:
        return
    if main_task is None:
        return

    guard = ProcessSigintGuard(
        loop,
        main_task,
        signal.getsignal(signal.SIGINT),
    )
    if guard.install():
        _guard = guard
        logger.info("Installed cleanup-safe SIGINT guard for the grid runtime")
