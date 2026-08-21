"""Strategy-owned cancellation for the grid engine and coordinator."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Sequence, Set

logger = logging.getLogger(__name__)
PATCH_VERSION = "2026-08-21.2"


def _set(report: Any, name: str) -> Set[str]:
    value = getattr(report, name, None)
    return {str(item) for item in value} if value else set()


def _rejected(report: Any) -> Dict[str, str]:
    value = getattr(report, "rejected", None)
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _is_lighter(owner: Any) -> bool:
    engine = getattr(owner, "engine", owner)
    value = getattr(getattr(engine, "config", None), "exchange", None)
    if value is None:
        config = getattr(getattr(engine, "exchange", None), "config", None)
        value = getattr(config, "exchange_id", "")
    return str(value).lower() == "lighter"


def _config(owner: Any, name: str, default: Any, cast: Any) -> Any:
    engine = getattr(owner, "engine", owner)
    exchange = getattr(engine, "exchange", None)
    rest = getattr(exchange, "_rest", None)
    config = getattr(rest, "config", {}) or {}
    try:
        return cast(config.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _owned(engine: Any, order_id: str) -> tuple[Any, Any]:
    key, order = engine._find_cached_order(order_id)
    if order is not None:
        return key, order
    state = getattr(getattr(engine, "coordinator", None), "state", None)
    active = getattr(state, "active_orders", {})
    if isinstance(active, dict):
        for key, candidate in active.items():
            if str(getattr(candidate, "order_id", "")) == order_id:
                return str(key), candidate
    return None, None


async def engine_cancel_orders(engine: Any, order_ids: Sequence[Any]) -> Any:
    """Cancel an exact set after proving every ID belongs to this grid."""
    ids = list(dict.fromkeys(str(item) for item in order_ids))
    ownership = {order_id: _owned(engine, order_id) for order_id in ids}
    unowned = [order_id for order_id, (_, order) in ownership.items() if order is None]
    if unowned:
        from types import SimpleNamespace

        report = SimpleNamespace(
            requested=set(ids),
            acknowledged=set(),
            cancelled=set(),
            filled=set(),
            still_open=set(),
            uncertain=set(),
            rejected={item: "not owned by this grid" for item in unowned},
            terminal_orders={},
        )
        engine._last_selective_cancel_report = report
        return report

    if not _is_lighter(engine):
        from types import SimpleNamespace

        cancelled = {order_id for order_id in ids if await engine.cancel_order(order_id)}
        return SimpleNamespace(
            requested=set(ids),
            acknowledged=set(cancelled),
            cancelled=cancelled,
            filled=set(),
            still_open=set(ids) - cancelled,
            uncertain=set(),
            rejected={},
            terminal_orders={},
        )

    cancel_many = getattr(engine.exchange, "cancel_orders", None)
    if not callable(cancel_many):
        raise RuntimeError("Lighter adapter lacks selective cancellation")

    aliases: Dict[str, Set[str]] = {}
    for order_id, (_, order) in ownership.items():
        aliases[order_id] = set(engine._pending_keys_for_order(order)) | {order_id}
        engine._expected_cancellations.update(aliases[order_id])

    engine.logger.warning(
        f"Using Lighter selective cancellation route: "
        f"version={PATCH_VERSION} owned_orders={len(ids)}"
    )
    report = await cancel_many(ids, engine.config.symbol)
    terminals = getattr(report, "terminal_orders", {}) or {}

    for order_id in sorted(_set(report, "cancelled")):
        key, order = ownership[order_id]
        refs = aliases[order_id] | set(engine._pending_keys_for_order(order))
        if engine._claim_order_finalization(*refs, key, order_id):
            engine._clear_restore_state(order)
            order.mark_cancelled()
            engine._remove_order_from_coordinator_state(order)
        engine._clear_pending_order_refs(*refs, key, order_id)
        engine._consume_expected_cancellation(*refs, key, order_id)

    for order_id in sorted(_set(report, "filled")):
        terminal = terminals.get(order_id)
        if terminal is None:
            report.filled.discard(order_id)
            report.uncertain.add(order_id)
        else:
            await engine._handle_exchange_order_object(terminal)

    for order_id in _set(report, "uncertain") | _set(report, "still_open"):
        engine._uncertain_cancel_order_ids.update(aliases[order_id])
    for order_id in _rejected(report):
        engine._expected_cancellations.difference_update(aliases[order_id])

    engine._last_selective_cancel_report = report
    return report


async def engine_cancel_all_owned(engine: Any) -> int:
    """Cancel only the Lighter orders tracked by this grid engine."""
    if not _is_lighter(engine):
        return await engine._legacy_cancel_all_orders()

    engine.logger.warning(
        f"Lighter shutdown cancellation route active: version={PATCH_VERSION}"
    )

    drain_timeout = max(1.0, _config(engine, "cancel_drain_timeout", 10.0, float))
    try:
        await asyncio.wait_for(engine._placements_drained.wait(), timeout=drain_timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"in-flight placements did not drain within {drain_timeout:.1f}s"
        ) from exc

    _, filled = await engine._resolve_unresolved_submissions_read_only()
    if filled:
        raise RuntimeError(
            "uncertain submissions resolved as filled: " + ", ".join(filled)
        )
    unresolved = engine._unresolved_submission_descriptions()
    if unresolved:
        raise RuntimeError("orders lack exact exchange IDs: " + ", ".join(unresolved))

    pending = list(engine.get_pending_orders())
    ids: List[str] = []
    invalid: List[str] = []
    for order in pending:
        raw = engine._string_or_none(getattr(order, "order_id", None))
        try:
            value = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            value = 0
        if not 1 <= value < (1 << 60):
            invalid.append(str(raw or getattr(order, "grid_id", "missing")))
        elif str(value) not in ids:
            ids.append(str(value))

    if invalid:
        raise RuntimeError("non-final Lighter order IDs: " + ", ".join(invalid))
    if not ids:
        engine.logger.warning("Lighter shutdown found no strategy-owned open orders")
        return 0

    engine.logger.warning(
        f"Lighter shutdown will selectively cancel {len(ids)} "
        "strategy-owned orders"
    )
    report = await engine.cancel_orders(ids)

    if _set(report, "filled"):
        raise RuntimeError(
            "orders filled during cancellation: "
            + ", ".join(sorted(_set(report, "filled")))
        )

    incomplete = (
        _set(report, "uncertain")
        | _set(report, "still_open")
        | set(_rejected(report))
    )
    if incomplete:
        raise RuntimeError(
            "selective cancellation incomplete: " + ", ".join(sorted(incomplete))
        )
    if engine.get_pending_orders():
        raise RuntimeError("local orders remain without terminal cancellation proof")

    cancelled = len(_set(report, "cancelled"))
    engine.logger.warning(
        f"Lighter selective shutdown cancellation completed: "
        f"cancelled={cancelled}"
    )
    return cancelled


def _local_filtered(ops: Any, predicate: Callable[[Any], bool]) -> List[str]:
    orders: List[Any] = []
    seen: Set[int] = set()
    active = getattr(ops.state, "active_orders", {})
    for order in active.values() if isinstance(active, dict) else []:
        if predicate(order) and id(order) not in seen:
            seen.add(id(order))
            orders.append(order)
    for order in ops.engine.get_pending_orders() or []:
        if predicate(order) and id(order) not in seen:
            seen.add(id(order))
            orders.append(order)

    ids = [str(getattr(order, "order_id", "")) for order in orders]
    if any(not order_id for order_id in ids):
        raise RuntimeError("locally owned order lacks exact exchange ID")
    return list(dict.fromkeys(ids))


def _only_429(report: Any) -> bool:
    rejected = _rejected(report)
    return bool(rejected) and all("429" in reason for reason in rejected.values())


async def ops_cancel_all(
    ops: Any,
    max_retries: int = 3,
    retry_delay: float = 1.5,
    first_delay: float = 0.8,
) -> bool:
    if not _is_lighter(ops):
        return await ops._legacy_cancel_all_orders_with_verification(
            max_retries=max_retries,
            retry_delay=retry_delay,
            first_delay=first_delay,
        )

    for attempt in range(max_retries):
        try:
            await ops.engine.cancel_all_orders()
            return True
        except Exception as exc:
            report = getattr(ops.engine, "_last_selective_cancel_report", None)
            ops.logger.error(f"selective Lighter cancellation failed: {exc}")
            if (
                _set(report, "uncertain")
                or _set(report, "still_open")
                or not _only_429(report)
            ):
                return False
            if attempt + 1 >= max_retries:
                return False
            await asyncio.sleep(first_delay if attempt == 0 else retry_delay)
    return False


async def ops_cancel_filtered(
    ops: Any,
    order_filter: Callable[[Any], bool],
    filter_description: str,
    max_attempts: int = 3,
) -> bool:
    if not _is_lighter(ops):
        return await ops._legacy_cancel_orders_by_filter_with_verification(
            order_filter=order_filter,
            filter_description=filter_description,
            max_attempts=max_attempts,
        )

    for attempt in range(max_attempts):
        try:
            ids = _local_filtered(ops, order_filter)
        except RuntimeError as exc:
            ops.logger.error(str(exc))
            return False
        if not ids:
            return True

        report = await ops.engine.cancel_orders(ids)
        if (
            _set(report, "filled")
            or _set(report, "uncertain")
            or _set(report, "still_open")
        ):
            return False
        if _set(report, "cancelled") == set(ids) and not _rejected(report):
            return True
        if attempt + 1 >= max_attempts or not _only_429(report):
            return False
        await asyncio.sleep(0.5)
    return False


async def coordinator_stop_guarded(coordinator: Any) -> None:
    """Keep critical Lighter cleanup alive across a second Ctrl+C, with a cap."""
    if not _is_lighter(coordinator):
        await coordinator._legacy_stop()
        return

    # Preserve the coordinator's original self-cancellation avoidance when an
    # internal stop-loss/emergency task initiates shutdown.  Moving that call
    # into a child task would make _stop_once wait on its own parent.
    current = asyncio.current_task()
    internal_callers = {
        task
        for task in (
            getattr(coordinator, "_stop_loss_monitor_task", None),
            getattr(coordinator, "_emergency_stop_task", None),
        )
        if task is not None
    }
    if current in internal_callers:
        await coordinator._legacy_stop()
        return

    task = getattr(coordinator, "_selective_stop_task", None)
    if task is None or task.done():
        task = asyncio.create_task(
            coordinator._legacy_stop(),
            name="lighter-grid-safe-stop",
        )
        coordinator._selective_stop_task = task

    timeout = max(
        10.0,
        _config(coordinator, "shutdown_cleanup_timeout", 120.0, float),
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            message = f"Lighter shutdown cleanup exceeded {timeout:.1f}s"
            coordinator._unsafe_shutdown_incident = message
            raise RuntimeError(message)

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            return
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()
            coordinator.logger.warning(
                "Shutdown cleanup is still running; additional Ctrl+C was ignored. "
                f"The process will exit after selective cancellation or the "
                f"{timeout:.1f}s timeout."
            )
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            message = f"Lighter shutdown cleanup exceeded {timeout:.1f}s"
            coordinator._unsafe_shutdown_incident = message
            raise RuntimeError(message)


def install_grid_selective_cancel() -> None:
    from .implementations.grid_engine_impl import GridEngineImpl

    if getattr(GridEngineImpl, "_selective_cancel_version", None) == PATCH_VERSION:
        return
    if not hasattr(GridEngineImpl, "_legacy_cancel_all_orders"):
        GridEngineImpl._legacy_cancel_all_orders = GridEngineImpl.cancel_all_orders
    GridEngineImpl.cancel_orders = engine_cancel_orders
    GridEngineImpl.cancel_all_orders = engine_cancel_all_owned
    GridEngineImpl._selective_cancel_installed = True
    GridEngineImpl._selective_cancel_version = PATCH_VERSION
    logger.info("Installed grid selective cancellation version %s", PATCH_VERSION)


def install_coordinator_selective_cancel() -> None:
    from .coordinator.grid_coordinator import GridCoordinator
    from .coordinator.order_operations import OrderOperations

    if getattr(OrderOperations, "_selective_cancel_version", None) != PATCH_VERSION:
        if not hasattr(OrderOperations, "_legacy_cancel_all_orders_with_verification"):
            OrderOperations._legacy_cancel_all_orders_with_verification = (
                OrderOperations.cancel_all_orders_with_verification
            )
        if not hasattr(
            OrderOperations,
            "_legacy_cancel_orders_by_filter_with_verification",
        ):
            OrderOperations._legacy_cancel_orders_by_filter_with_verification = (
                OrderOperations.cancel_orders_by_filter_with_verification
            )
        OrderOperations.cancel_all_orders_with_verification = ops_cancel_all
        OrderOperations.cancel_orders_by_filter_with_verification = ops_cancel_filtered
        OrderOperations._selective_cancel_installed = True
        OrderOperations._selective_cancel_version = PATCH_VERSION

    if getattr(GridCoordinator, "_selective_cancel_version", None) != PATCH_VERSION:
        if not hasattr(GridCoordinator, "_legacy_stop"):
            GridCoordinator._legacy_stop = GridCoordinator.stop
        GridCoordinator.stop = coordinator_stop_guarded
        GridCoordinator._selective_cancel_version = PATCH_VERSION

    logger.info("Installed coordinator selective cancellation version %s", PATCH_VERSION)
