"""Exact-order Lighter cancellation using native transaction batches."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Set

logger = logging.getLogger(__name__)
MAX_BATCH = 50
CANCELLED = {"canceled", "cancelled", "rejected", "expired"}
FILLED = {"filled", "closed"}


@dataclass
class CancelReport:
    requested: Set[str] = field(default_factory=set)
    acknowledged: Set[str] = field(default_factory=set)
    cancelled: Set[str] = field(default_factory=set)
    filled: Set[str] = field(default_factory=set)
    still_open: Set[str] = field(default_factory=set)
    uncertain: Set[str] = field(default_factory=set)
    rejected: Dict[str, str] = field(default_factory=dict)
    terminal_orders: Dict[str, Any] = field(default_factory=dict)

    def merge(self, other: "CancelReport") -> None:
        for name in (
            "requested", "acknowledged", "cancelled", "filled",
            "still_open", "uncertain",
        ):
            getattr(self, name).update(getattr(other, name))
        self.rejected.update(other.rejected)
        self.terminal_orders.update(other.terminal_orders)


def _status(order: Any) -> str:
    value = getattr(order, "status", None)
    return str(getattr(value, "value", value) or "").lower()


def _keys(order: Any) -> Set[str]:
    return {
        str(value)
        for value in (
            getattr(order, "id", None),
            getattr(order, "client_id", None),
            getattr(order, "order_id", None),
        )
        if value not in (None, "")
    }


def _validated_ids(order_ids: Iterable[Any]) -> tuple[List[int], CancelReport]:
    report = CancelReport()
    indexes: List[int] = []
    for raw in order_ids:
        text = str(raw).strip()
        try:
            index = int(text)
        except (TypeError, ValueError):
            report.requested.add(text)
            report.rejected[text] = "not a numeric Lighter order_index"
            continue
        order_id = str(index)
        if order_id in report.requested:
            continue
        report.requested.add(order_id)
        if not 1 <= index < (1 << 60):
            report.rejected[order_id] = "order_index outside valid range"
            continue
        indexes.append(index)
    return indexes, report


def _rollback(manager: Any, key: int, count: int) -> None:
    rollback = getattr(manager, "acknowledge_failure", None)
    if callable(rollback):
        for _ in range(count):
            rollback(key)


async def _send_chunk(rest: Any, market: int, indexes: Sequence[int]) -> Any:
    signer = getattr(rest, "signer_client", None)
    manager = getattr(signer, "nonce_manager", None)
    if signer is None or manager is None:
        raise RuntimeError("Lighter signer/nonce manager is unavailable")
    key = int(getattr(rest, "api_key_index", 0))

    async def request() -> Any:
        reserved = 0
        send_started = False
        async with manager.lock(key):
            types: List[int] = []
            infos: List[str] = []
            try:
                for index in indexes:
                    _, nonce = await manager.async_next_nonce(key)
                    reserved += 1
                    tx_type, tx_info, _, error = signer.sign_cancel_order(
                        market_index=market,
                        order_index=index,
                        nonce=nonce,
                        api_key_index=key,
                    )
                    if error or tx_type is None or not tx_info:
                        raise RuntimeError(error or f"cannot sign cancel {index}")
                    types.append(tx_type)
                    infos.append(tx_info)
                send_started = True
                response = await signer.send_tx_batch(
                    tx_types=types,
                    tx_infos=infos,
                )
                if getattr(response, "code", None) != 200:
                    _rollback(manager, key, reserved)
                return response
            except Exception as exc:
                if not send_started or rest._is_rate_limited(exc):
                    _rollback(manager, key, reserved)
                raise

    return await rest._call_api(
        "selective batch cancellation",
        request,
        retry_on_429=False,
    )


async def _history(rest: Any, symbol: str) -> Any:
    try:
        return await rest.get_order_history(symbol, limit=100)
    except TypeError:
        return await rest.get_order_history(symbol)


async def _reconcile(
    rest: Any,
    symbol: str,
    order_ids: Set[str],
    delay: float,
) -> CancelReport:
    report = CancelReport(requested=set(order_ids))
    unresolved = set(order_ids)
    if delay:
        await asyncio.sleep(delay)
    attempts = max(1, int(getattr(rest, "MUTATION_RECONCILIATION_ATTEMPTS", 2)))
    retry_delay = float(getattr(rest, "MUTATION_RECONCILIATION_DELAY", 0.25))
    active_ids: Set[str] = set()

    for attempt in range(attempts):
        active = history = None
        try:
            active = await rest.get_open_orders(symbol)
        except Exception as exc:
            logger.warning("batch cancel active snapshot failed: %s", exc)
        try:
            history = await _history(rest, symbol)
        except Exception as exc:
            logger.warning("batch cancel history snapshot failed: %s", exc)

        active_map = {key: order for order in active or [] for key in _keys(order)}
        history_map = {key: order for order in history or [] for key in _keys(order)}
        next_unresolved: Set[str] = set()
        active_ids = set()
        for order_id in unresolved:
            terminal = history_map.get(order_id)
            if terminal and _status(terminal) in CANCELLED:
                report.cancelled.add(order_id)
                report.terminal_orders[order_id] = terminal
            elif terminal and _status(terminal) in FILLED:
                report.filled.add(order_id)
                report.terminal_orders[order_id] = terminal
            else:
                next_unresolved.add(order_id)
                if order_id in active_map:
                    active_ids.add(order_id)
        unresolved = next_unresolved
        if not unresolved or attempt + 1 >= attempts:
            break
        await asyncio.sleep(retry_delay)

    report.still_open = active_ids & unresolved
    # Active does not prove an acknowledged cancel is no longer queued.
    report.uncertain = unresolved
    return report


def _batch_size(rest: Any) -> int:
    try:
        value = int((getattr(rest, "config", {}) or {}).get("cancel_batch_size", 50))
    except (TypeError, ValueError):
        value = 50
    return max(1, min(value, MAX_BATCH))


def _wait_seconds(rest: Any, response: Any) -> float:
    try:
        predicted = int(getattr(response, "predicted_execution_time_ms", 0) or 0)
    except (TypeError, ValueError):
        predicted = 0
    return min(max(0.3, predicted / 1000 + 0.2), 30.0)


async def cancel_orders_batch(
    rest: Any,
    symbol: str,
    order_ids: Sequence[Any],
) -> CancelReport:
    """Cancel only supplied order indexes; never use account-wide cancel-all."""
    indexes, report = _validated_ids(order_ids)
    if not indexes:
        return report
    market = rest.get_market_index(symbol)
    if market is None:
        for index in indexes:
            report.rejected[str(index)] = f"unknown Lighter market: {symbol}"
        return report

    markers = getattr(rest, "_uncertain_cancellations", None)
    if markers is None:
        markers = rest._uncertain_cancellations = set()
    pending: List[int] = []
    read_only: List[str] = []
    for index in indexes:
        order_id = str(index)
        if (symbol, order_id) in markers:
            read_only.append(order_id)
            report.uncertain.add(order_id)
        else:
            pending.append(index)

    size = _batch_size(rest)
    for start in range(0, len(pending), size):
        chunk = pending[start:start + size]
        ids = {str(index) for index in chunk}
        try:
            response = await _send_chunk(rest, market, chunk)
            rest._require_success_response(response, "selective batch cancellation")
        except Exception as exc:
            if rest._is_rate_limited(exc):
                report.rejected.update({order_id: "HTTP 429" for order_id in ids})
            else:
                report.uncertain.update(ids)
                markers.update((symbol, order_id) for order_id in ids)
            continue

        report.acknowledged.update(ids)
        markers.update((symbol, order_id) for order_id in ids)
        resolved = await _reconcile(rest, symbol, ids, _wait_seconds(rest, response))
        report.merge(resolved)
        for order_id in resolved.cancelled | resolved.filled:
            markers.discard((symbol, order_id))

    # Never resend a transport-uncertain mutation; reconcile it by reads only.
    for start in range(0, len(read_only), size):
        resolved = await _reconcile(rest, symbol, set(read_only[start:start + size]), 0)
        report.merge(resolved)
        report.uncertain.difference_update(resolved.cancelled | resolved.filled)
        for order_id in resolved.cancelled | resolved.filled:
            markers.discard((symbol, order_id))
    return report


async def adapter_cancel_orders(
    adapter: Any,
    order_ids: Sequence[Any],
    symbol: str,
) -> CancelReport:
    return await adapter._rest.cancel_orders_batch(
        adapter._normalize_symbol(symbol),
        order_ids,
    )


def install_lighter_selective_cancel() -> None:
    from .lighter import LighterAdapter
    from .lighter_rest import LighterRest

    if getattr(LighterRest, "_selective_cancel_installed", False):
        return
    LighterRest.cancel_orders_batch = cancel_orders_batch
    LighterAdapter.cancel_orders = adapter_cancel_orders
    LighterRest._selective_cancel_installed = True
