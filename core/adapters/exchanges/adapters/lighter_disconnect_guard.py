"""Bounded Lighter adapter shutdown so the CLI always returns to the shell."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)
PATCH_VERSION = "2026-08-21.3"


def _timeout(adapter: Any, name: str, default: float) -> float:
    rest = getattr(adapter, "_rest", None)
    config = getattr(rest, "config", {}) or {}
    try:
        return max(1.0, float(config.get(name, default)))
    except (TypeError, ValueError):
        return default


async def _bounded_call(
    factory: Callable[[], Awaitable[Any]],
    *,
    name: str,
    timeout: float,
) -> str | None:
    try:
        await asyncio.wait_for(factory(), timeout=timeout)
        return None
    except asyncio.TimeoutError:
        return f"{name} timed out after {timeout:.1f}s"
    except Exception as exc:
        return f"{name} failed: {exc}"


async def guarded_lighter_disconnect(adapter: Any) -> None:
    """Run normal disconnect with a cap, then force-close child clients."""
    timeout = _timeout(adapter, "disconnect_timeout", 20.0)
    original = adapter._unguarded_disconnect
    task = asyncio.create_task(original(), name="lighter-adapter-disconnect")

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return
    except asyncio.TimeoutError:
        logger.error(
            "Lighter adapter disconnect exceeded %.1fs; forcing child-client cleanup",
            timeout,
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception:
        # The normal path failed. Continue into bounded child cleanup before
        # re-raising a summarized error to the entrypoint.
        await asyncio.gather(task, return_exceptions=True)

    child_timeout = _timeout(adapter, "disconnect_child_timeout", 5.0)
    errors = []

    websocket = getattr(adapter, "_websocket", None)
    websocket_disconnect = getattr(websocket, "disconnect", None)
    if callable(websocket_disconnect):
        error = await _bounded_call(
            websocket_disconnect,
            name="Lighter websocket disconnect",
            timeout=child_timeout,
        )
        if error:
            errors.append(error)

    rest = getattr(adapter, "_rest", None)
    rest_close = getattr(rest, "close", None)
    if callable(rest_close):
        error = await _bounded_call(
            rest_close,
            name="Lighter REST close",
            timeout=child_timeout,
        )
        if error:
            errors.append(error)

    adapter._connected = False
    adapter._authenticated = False

    if errors:
        raise RuntimeError("; ".join(errors))


def install_lighter_disconnect_guard() -> None:
    from .lighter import LighterAdapter

    if getattr(LighterAdapter, "_disconnect_guard_version", None) == PATCH_VERSION:
        return
    if not hasattr(LighterAdapter, "_unguarded_disconnect"):
        LighterAdapter._unguarded_disconnect = LighterAdapter.disconnect
    LighterAdapter.disconnect = guarded_lighter_disconnect
    LighterAdapter._disconnect_guard_version = PATCH_VERSION
    logger.info("Installed Lighter disconnect guard version %s", PATCH_VERSION)
