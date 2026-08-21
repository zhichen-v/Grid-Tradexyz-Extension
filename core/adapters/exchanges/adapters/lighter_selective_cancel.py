"""Exact-order Lighter cancellation using native transaction batches.

The module intentionally never falls back to Lighter's account-wide
``cancel_all_orders`` operation. It signs only the supplied exchange order
indexes, sends them through ``send_tx_batch``, and reconciles outcomes from
bounded bulk snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Set

logger = logging.getLogger(__name__)

PATCH_VERSION = "2026-08-21.2"
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

    def merge_resolution(self, other: "CancelReport") -> None:
        """Merge a read-only resolution without retaining stale uncertainty."""
        terminal = other.cancelled | other.filled
        self.cancelled.update(other.cancelled)
        self.filled.update(other.filled)
        self.terminal_orders.update(other.terminal_orders)
        self.still_open.difference_update(terminal)
        self.uncertain.difference_update(terminal)
        self.still_open.update(other.still_open)
        self.uncertain.update(other.uncertain)


class BatchSubmissionError(RuntimeError):
    """Classify whether a failed batch may already have reached Lighter."""

    def __init__(
        self,
        message: str,
        *,
        ambiguous: bool,
        rate_limited: bool = False,
    ) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.rate_limited = rate_limited


def _config(rest: Any, name: str, default: Any, cast: Any) -> Any:
    config = getattr(rest, "config", {}) or {}
    try:
        return cast(config.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


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


def _batch_size(rest: Any) -> int:
    value = _config(rest, "cancel_batch_size", MAX_BATCH, int)
    return max(1, min(value, MAX_BATCH))


def _send_timeout(rest: Any) -> float:
    return max(1.0, _config(rest, "cancel_send_timeout", 20.0, float))


def _read_timeout(rest: Any) -> float:
    return max(1.0, _config(rest, "cancel_read_timeout", 15.0, float))


def _reconcile_group_size(rest: Any) -> int:
    # accountInactiveOrders currently returns at most 100 rows. Keeping each
    # target group below that cap prevents one large grid from hiding older
    # terminal records behind newer cancellations.
    value = _config(rest, "cancel_reconcile_group_size", 80, int)
    return max(1, min(value, 80))


def _wait_seconds(rest: Any, predicted_ms: int) -> float:
    configured_max = max(
        0.3,
        _config(rest, "cancel_execution_wait_max", 15.0, float),
    )
    return min(max(0.3, predicted_ms / 1000 + 0.2), configured_max)


async def _send_chunk(rest: Any, market: int, indexes: Sequence[int]) -> Any:
    signer = getattr(rest, "signer_client", None)
    manager = getattr(signer, "nonce_manager", None)
    if signer is None or manager is None:
        raise BatchSubmissionError(
            "Lighter signer/nonce manager is unavailable",
            ambiguous=False,
        )

    key = int(getattr(rest, "api_key_index", 0))
    state = {"reserved": 0, "send_started": False}

    async def request() -> Any:
        async with manager.lock(key):
            tx_types: List[int] = []
            tx_infos: List[str] = []
            try:
                for index in indexes:
                    _, nonce = await manager.async_next_nonce(key)
                    state["reserved"] += 1
                    tx_type, tx_info, _, error = signer.sign_cancel_order(
                        market_index=market,
                        order_index=index,
                        nonce=nonce,
                        api_key_index=key,
                    )
                    if error or tx_type is None or not tx_info:
                        raise BatchSubmissionError(
                            error or f"cannot sign cancel {index}",
                            ambiguous=False,
                        )
                    tx_types.append(tx_type)
                    tx_infos.append(tx_info)

                state["send_started"] = True
                response = await signer.send_tx_batch(
                    tx_types=tx_types,
                    tx_infos=tx_infos,
                )
                code = getattr(response, "code", None)
                if code != 200:
                    _rollback(manager, key, state["reserved"])
                    state["reserved"] = 0
                    raise BatchSubmissionError(
                        f"selective batch cancellation rejected (code={code})",
                        ambiguous=False,
                        rate_limited=str(code) == "429",
                    )
                return response

            except BatchSubmissionError:
                if not state["send_started"] and state["reserved"]:
                    _rollback(manager, key, state["reserved"])
                    state["reserved"] = 0
                raise
            except Exception as exc:
                rate_limited = bool(rest._is_rate_limited(exc))
                if (
                    not state["send_started"] or rate_limited
                ) and state["reserved"]:
                    _rollback(manager, key, state["reserved"])
                    state["reserved"] = 0
                raise BatchSubmissionError(
                    str(exc),
                    ambiguous=state["send_started"] and not rate_limited,
                    rate_limited=rate_limited,
                ) from exc

    try:
        return await asyncio.wait_for(
            rest._call_api(
                "selective batch cancellation",
                request,
                retry_on_429=False,
            ),
            timeout=_send_timeout(rest),
        )
    except asyncio.TimeoutError as exc:
        # Before send_tx_batch starts, reserved optimistic nonces are safe to
        # return. Once the send starts, the outcome is intentionally treated
        # as ambiguous and the nonces are retained.
        if not state["send_started"] and state["reserved"]:
            _rollback(manager, key, state["reserved"])
            state["reserved"] = 0
        raise BatchSubmissionError(
            f"selective batch cancellation timed out after {_send_timeout(rest):.1f}s",
            ambiguous=bool(state["send_started"]),
        ) from exc
    except BatchSubmissionError:
        raise
    except Exception as exc:
        rate_limited = bool(rest._is_rate_limited(exc))
        raise BatchSubmissionError(
            str(exc),
            ambiguous=bool(state["send_started"]) and not rate_limited,
            rate_limited=rate_limited,
        ) from exc


async def _history(rest: Any, symbol: str) -> Any:
    try:
        return await rest.get_order_history(symbol, limit=100)
    except TypeError:
        return await rest.get_order_history(symbol)


async def _read(rest: Any, operation: str, awaitable: Any) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=_read_timeout(rest))
    except asyncio.TimeoutError:
        logger.warning(
            "Lighter selective cancel %s timed out after %.1fs",
            operation,
            _read_timeout(rest),
        )
        return None
    except Exception as exc:
        logger.warning("Lighter selective cancel %s failed: %s", operation, exc)
        return None


async def _reconcile(
    rest: Any,
    symbol: str,
    order_ids: Set[str],
    initial_delay: float,
    *,
    group_label: str,
) -> CancelReport:
    report = CancelReport(requested=set(order_ids))
    unresolved = set(order_ids)

    if initial_delay:
        logger.warning(
            "Lighter selective cancel %s accepted; waiting %.2fs before terminal verification",
            group_label,
            initial_delay,
        )
        await asyncio.sleep(initial_delay)

    configured_attempts = _config(
        rest,
        "cancel_reconcile_attempts",
        max(3, int(getattr(rest, "MUTATION_RECONCILIATION_ATTEMPTS", 2))),
        int,
    )
    attempts = max(1, min(configured_attempts, 6))
    base_delay = max(
        0.25,
        _config(
            rest,
            "cancel_reconcile_delay",
            float(getattr(rest, "MUTATION_RECONCILIATION_DELAY", 0.25)),
            float,
        ),
    )

    active_ids: Set[str] = set()
    for attempt in range(1, attempts + 1):
        logger.warning(
            "Lighter selective cancel %s verification %d/%d: unresolved=%d",
            group_label,
            attempt,
            attempts,
            len(unresolved),
        )

        active = await _read(
            rest,
            "active-orders snapshot",
            rest.get_open_orders(symbol),
        )
        history = await _read(
            rest,
            "inactive-orders snapshot",
            _history(rest, symbol),
        )

        active_map = {
            key: order for order in active or [] for key in _keys(order)
        }
        history_map = {
            key: order for order in history or [] for key in _keys(order)
        }
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
        logger.warning(
            "Lighter selective cancel %s verification result: cancelled=%d filled=%d open=%d unresolved=%d",
            group_label,
            len(report.cancelled),
            len(report.filled),
            len(active_ids),
            len(unresolved),
        )

        if not unresolved or attempt >= attempts:
            break
        await asyncio.sleep(min(4.0, base_delay * (2 ** (attempt - 1))))

    report.still_open = active_ids & unresolved
    report.uncertain = unresolved
    return report


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
    read_only: Set[str] = set()
    for index in indexes:
        order_id = str(index)
        if (symbol, order_id) in markers:
            read_only.add(order_id)
            report.uncertain.add(order_id)
        else:
            pending.append(index)

    size = _batch_size(rest)
    batch_count = math.ceil(len(pending) / size) if pending else 0
    logger.warning(
        "Lighter selective batch cancellation started: version=%s symbol=%s requested=%d new=%d read_only=%d batch_size=%d batches=%d",
        PATCH_VERSION,
        symbol,
        len(report.requested),
        len(pending),
        len(read_only),
        size,
        batch_count,
    )

    predicted_wait_ms = 0
    for batch_number, start in enumerate(
        range(0, len(pending), size),
        start=1,
    ):
        chunk = pending[start:start + size]
        ids = {str(index) for index in chunk}
        logger.warning(
            "Submitting Lighter selective cancel batch %d/%d: orders=%d",
            batch_number,
            batch_count,
            len(chunk),
        )
        try:
            response = await _send_chunk(rest, market, chunk)
        except BatchSubmissionError as exc:
            if exc.rate_limited:
                report.rejected.update(
                    {order_id: "HTTP 429" for order_id in ids}
                )
                logger.warning(
                    "Lighter selective cancel batch %d/%d was rate limited; no blind mutation retry",
                    batch_number,
                    batch_count,
                )
            elif exc.ambiguous:
                report.uncertain.update(ids)
                markers.update((symbol, order_id) for order_id in ids)
                logger.error(
                    "Lighter selective cancel batch %d/%d has an uncertain transport outcome: %s",
                    batch_number,
                    batch_count,
                    exc,
                )
            else:
                report.rejected.update(
                    {order_id: str(exc) for order_id in ids}
                )
                logger.error(
                    "Lighter selective cancel batch %d/%d was rejected before submission: %s",
                    batch_number,
                    batch_count,
                    exc,
                )
            continue

        report.acknowledged.update(ids)
        markers.update((symbol, order_id) for order_id in ids)
        try:
            predicted_wait_ms = max(
                predicted_wait_ms,
                int(getattr(response, "predicted_execution_time_ms", 0) or 0),
            )
        except (TypeError, ValueError):
            pass
        logger.warning(
            "Lighter selective cancel batch %d/%d acknowledged: orders=%d",
            batch_number,
            batch_count,
            len(chunk),
        )

    targets = set(report.acknowledged) | set(report.uncertain) | read_only
    if targets:
        ordered_targets = sorted(targets, key=int)
        group_size = _reconcile_group_size(rest)
        group_count = math.ceil(len(ordered_targets) / group_size)
        initial_delay = _wait_seconds(rest, predicted_wait_ms)

        for group_number, start in enumerate(
            range(0, len(ordered_targets), group_size),
            start=1,
        ):
            group = set(ordered_targets[start:start + group_size])
            resolved = await _reconcile(
                rest,
                symbol,
                group,
                initial_delay if group_number == 1 else 0.0,
                group_label=f"group {group_number}/{group_count}",
            )
            report.merge_resolution(resolved)
            for order_id in resolved.cancelled | resolved.filled:
                markers.discard((symbol, order_id))

    logger.warning(
        "Lighter selective batch cancellation finished: requested=%d acknowledged=%d cancelled=%d filled=%d open=%d uncertain=%d rejected=%d",
        len(report.requested),
        len(report.acknowledged),
        len(report.cancelled),
        len(report.filled),
        len(report.still_open),
        len(report.uncertain),
        len(report.rejected),
    )
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


async def adapter_cancel_all_disabled(
    adapter: Any,
    symbol: Any = None,
) -> Any:
    raise RuntimeError(
        "Lighter account-wide cancel_all_orders is disabled. "
        "Use selective cancellation with strategy-owned order indexes."
    )


def install_lighter_selective_cancel() -> None:
    from .lighter import LighterAdapter
    from .lighter_rest import LighterRest

    if getattr(LighterRest, "_selective_cancel_version", None) == PATCH_VERSION:
        return

    if not hasattr(LighterAdapter, "_legacy_cancel_all_orders"):
        LighterAdapter._legacy_cancel_all_orders = LighterAdapter.cancel_all_orders

    LighterRest.cancel_orders_batch = cancel_orders_batch
    LighterAdapter.cancel_orders = adapter_cancel_orders
    LighterAdapter.cancel_all_orders = adapter_cancel_all_disabled
    LighterRest._selective_cancel_installed = True
    LighterRest._selective_cancel_version = PATCH_VERSION
    LighterAdapter._selective_cancel_version = PATCH_VERSION
    logger.info(
        "Installed Lighter selective cancellation version %s",
        PATCH_VERSION,
    )
