"""Pagination-safe selective cancellation for Lighter.

This patch keeps the exact-order mutation path from ``lighter_selective_cancel``
but fixes shutdown verification for grids larger than the 100-row
``accountInactiveOrders`` page limit. It follows ``next_cursor`` for the
configured market and never calls Lighter's account-wide cancel-all mutation.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import lighter_selective_cancel as base

logger = logging.getLogger(__name__)

PATCH_VERSION = "2026-08-21.3"
HISTORY_PAGE_LIMIT = 100


class HistoryReadError(RuntimeError):
    """Raised when paginated terminal-order history cannot be read safely."""


def _config(rest: Any, name: str, default: Any, cast: Any) -> Any:
    config = getattr(rest, "config", {}) or {}
    try:
        return cast(config.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _read_timeout(rest: Any) -> float:
    return max(1.0, _config(rest, "cancel_read_timeout", 15.0, float))


def _history_max_pages(rest: Any) -> int:
    return max(1, min(_config(rest, "cancel_history_max_pages", 5, int), 10))


def _reconcile_attempts(rest: Any) -> int:
    default = max(3, int(getattr(rest, "MUTATION_RECONCILIATION_ATTEMPTS", 2)))
    return max(1, min(_config(rest, "cancel_reconcile_attempts", default, int), 6))


def _reconcile_delay(rest: Any) -> float:
    default = float(getattr(rest, "MUTATION_RECONCILIATION_DELAY", 0.25))
    return max(0.25, _config(rest, "cancel_reconcile_delay", default, float))


def _wait_seconds(rest: Any, predicted_ms: int) -> float:
    configured_max = max(
        0.3,
        _config(rest, "cancel_execution_wait_max", 15.0, float),
    )
    return min(max(0.3, predicted_ms / 1000 + 0.2), configured_max)


def _create_auth_token(rest: Any) -> str:
    signer = getattr(rest, "signer_client", None)
    if signer is None:
        raise HistoryReadError("Lighter signer is unavailable for history verification")

    result = signer.create_auth_token_with_expiry(
        deadline=3600,
        api_key_index=int(getattr(rest, "api_key_index", 0)),
    )
    if isinstance(result, tuple):
        token, error = result
        if error:
            raise HistoryReadError(f"cannot create Lighter auth token: {error}")
    else:
        token = result
    if not token:
        raise HistoryReadError("cannot create Lighter auth token: empty token")
    return str(token)


def _parse_history_page(rest: Any, response: Any, symbol: str) -> List[Any]:
    if not hasattr(response, "orders"):
        raise HistoryReadError("inactive-orders response has no orders field")

    parsed: List[Any] = []
    for order_info in getattr(response, "orders", None) or []:
        market_index = getattr(order_info, "market_index", None)
        symbol_from_market = symbol
        resolver = getattr(rest, "_get_symbol_from_market_index", None)
        if callable(resolver):
            symbol_from_market = resolver(market_index) or symbol
        parser = getattr(rest, "_parse_order", None)
        if not callable(parser):
            raise HistoryReadError("Lighter order parser is unavailable")
        parsed.append(parser(order_info, symbol_from_market))
    return parsed


async def _history_page(
    rest: Any,
    symbol: str,
    auth_token: str,
    cursor: Optional[str],
) -> Tuple[List[Any], Optional[str]]:
    order_api = getattr(rest, "order_api", None)
    fetch = getattr(order_api, "account_inactive_orders", None)
    if not callable(fetch):
        # Compatibility fallback for an SDK without cursor support. It is only
        # safe for the first page; callers will know pagination was unavailable.
        try:
            orders = await rest.get_order_history(symbol, limit=HISTORY_PAGE_LIMIT)
        except TypeError:
            orders = await rest.get_order_history(symbol)
        return list(orders or []), None

    market_id = rest.get_market_index(symbol)
    if market_id is None:
        raise HistoryReadError(f"unknown Lighter market: {symbol}")

    response = await rest._call_api(
        "inactive orders page query",
        lambda: fetch(
            authorization=auth_token,
            account_index=rest.account_index,
            limit=HISTORY_PAGE_LIMIT,
            market_id=market_id,
            cursor=cursor,
            market_type="perp",
        ),
    )
    rest._require_success_response(response, "inactive orders page query")
    return (
        _parse_history_page(rest, response, symbol),
        getattr(response, "next_cursor", None) or None,
    )


async def _history_for_targets(
    rest: Any,
    symbol: str,
    target_ids: Set[str],
) -> Tuple[bool, List[Any], bool, int]:
    """Read terminal history pages until targets are covered or cursor ends.

    Returns ``(success, orders, cursor_exhausted, page_count)``.
    """
    if not target_ids:
        return True, [], True, 0

    try:
        auth_token = _create_auth_token(rest)
    except Exception as exc:
        logger.warning(
            "Lighter selective cancel cannot prepare history pagination: %s",
            exc,
        )
        return False, [], False, 0

    collected: List[Any] = []
    found: Set[str] = set()
    cursor: Optional[str] = None
    seen_cursors: Set[str] = set()
    max_pages = _history_max_pages(rest)

    for page_number in range(1, max_pages + 1):
        try:
            orders, next_cursor = await asyncio.wait_for(
                _history_page(rest, symbol, auth_token, cursor),
                timeout=_read_timeout(rest),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Lighter selective cancel inactive-orders page %d timed out after %.1fs",
                page_number,
                _read_timeout(rest),
            )
            return False, collected, False, page_number - 1
        except Exception as exc:
            logger.warning(
                "Lighter selective cancel inactive-orders page %d failed: %s",
                page_number,
                exc,
            )
            return False, collected, False, page_number - 1

        collected.extend(orders)
        for order in orders:
            found.update(base._keys(order) & target_ids)

        logger.warning(
            "Lighter selective cancel history page %d: rows=%d matched=%d/%d next_cursor=%s",
            page_number,
            len(orders),
            len(found),
            len(target_ids),
            "yes" if next_cursor else "no",
        )

        if found >= target_ids:
            return True, collected, not bool(next_cursor), page_number
        if not next_cursor:
            return True, collected, True, page_number
        next_cursor = str(next_cursor)
        if next_cursor in seen_cursors:
            logger.error(
                "Lighter selective cancel history cursor repeated on page %d",
                page_number,
            )
            return False, collected, False, page_number
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    logger.warning(
        "Lighter selective cancel history pagination reached the %d-page safety cap",
        max_pages,
    )
    return True, collected, False, max_pages


async def _active_orders(rest: Any, symbol: str) -> Tuple[bool, List[Any]]:
    try:
        orders = await asyncio.wait_for(
            rest.get_open_orders(symbol),
            timeout=_read_timeout(rest),
        )
        return True, list(orders or [])
    except asyncio.TimeoutError:
        logger.warning(
            "Lighter selective cancel active-orders snapshot timed out after %.1fs",
            _read_timeout(rest),
        )
    except Exception as exc:
        logger.warning("Lighter selective cancel active-orders snapshot failed: %s", exc)
    return False, []


async def _reconcile_paginated(
    rest: Any,
    symbol: str,
    order_ids: Set[str],
    initial_delay: float,
) -> base.CancelReport:
    report = base.CancelReport(requested=set(order_ids))
    report.cancelled_by_absence = set()
    unresolved = set(order_ids)
    absence_confirmations: Dict[str, int] = {
        order_id: 0 for order_id in order_ids
    }

    if initial_delay:
        logger.warning(
            "Lighter selective cancel accepted; waiting %.2fs before paginated verification",
            initial_delay,
        )
        await asyncio.sleep(initial_delay)

    attempts = _reconcile_attempts(rest)
    delay = _reconcile_delay(rest)
    active_ids: Set[str] = set()

    for attempt in range(1, attempts + 1):
        logger.warning(
            "Lighter selective cancel verification %d/%d: unresolved=%d",
            attempt,
            attempts,
            len(unresolved),
        )

        active_ok, active = await _active_orders(rest, symbol)
        history_ok, history, history_exhausted, pages = await _history_for_targets(
            rest,
            symbol,
            unresolved,
        )

        active_map = {
            key: order for order in active for key in base._keys(order)
        }
        history_map = {
            key: order for order in history for key in base._keys(order)
        }

        next_unresolved: Set[str] = set()
        active_ids = set()
        for order_id in unresolved:
            terminal = history_map.get(order_id)
            terminal_status = base._status(terminal) if terminal is not None else ""
            if terminal is not None and terminal_status in base.CANCELLED:
                report.cancelled.add(order_id)
                report.terminal_orders[order_id] = terminal
                continue
            if terminal is not None and terminal_status in base.FILLED:
                report.filled.add(order_id)
                report.terminal_orders[order_id] = terminal
                continue

            next_unresolved.add(order_id)
            if active_ok and order_id in active_map:
                active_ids.add(order_id)
                absence_confirmations[order_id] = 0
            elif active_ok:
                absence_confirmations[order_id] += 1

        unresolved = next_unresolved

        # A confirmed batch mutation plus two successful market-filtered active
        # snapshots proving that the exact order indexes are absent is sufficient
        # to finish shutdown even if inactive-order indexing lags. Explicit
        # history still wins and is used to distinguish fills from cancellations.
        absent_twice = {
            order_id
            for order_id in unresolved
            if absence_confirmations.get(order_id, 0) >= 2
        }
        if active_ok and history_ok and absent_twice:
            report.cancelled.update(absent_twice)
            report.cancelled_by_absence.update(absent_twice)
            unresolved.difference_update(absent_twice)
            logger.warning(
                "Lighter selective cancel finalized %d order(s) from two empty active snapshots; history_pages=%d cursor_exhausted=%s",
                len(absent_twice),
                pages,
                history_exhausted,
            )

        logger.warning(
            "Lighter selective cancel verification result: cancelled=%d filled=%d open=%d unresolved=%d history_pages=%d",
            len(report.cancelled),
            len(report.filled),
            len(active_ids & unresolved),
            len(unresolved),
            pages,
        )

        if not unresolved or attempt >= attempts:
            break
        await asyncio.sleep(min(4.0, delay * (2 ** (attempt - 1))))

    report.still_open = active_ids & unresolved
    report.uncertain = unresolved
    return report


async def cancel_orders_batch(
    rest: Any,
    symbol: str,
    order_ids: Sequence[Any],
) -> base.CancelReport:
    """Cancel only supplied order indexes and verify with cursor pagination."""
    indexes, report = base._validated_ids(order_ids)
    report.cancelled_by_absence = set()
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

    size = base._batch_size(rest)
    batch_count = math.ceil(len(pending) / size) if pending else 0
    logger.warning(
        "Lighter selective batch cancellation started: version=%s symbol=%s market_index=%s requested=%d new=%d read_only=%d batch_size=%d batches=%d",
        PATCH_VERSION,
        symbol,
        market,
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
            "Submitting Lighter selective cancel batch %d/%d: market_index=%s orders=%d",
            batch_number,
            batch_count,
            market,
            len(chunk),
        )
        try:
            response = await base._send_chunk(rest, market, chunk)
        except base.BatchSubmissionError as exc:
            if exc.rate_limited:
                report.rejected.update(
                    {order_id: "HTTP 429" for order_id in ids}
                )
                logger.warning(
                    "Lighter selective cancel batch %d/%d was rate limited; mutation was not blindly retried",
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
        resolved = await _reconcile_paginated(
            rest,
            symbol,
            targets,
            _wait_seconds(rest, predicted_wait_ms),
        )
        report.merge_resolution(resolved)
        report.cancelled_by_absence.update(
            getattr(resolved, "cancelled_by_absence", set())
        )
        for order_id in resolved.cancelled | resolved.filled:
            markers.discard((symbol, order_id))

    logger.warning(
        "Lighter selective batch cancellation finished: version=%s requested=%d acknowledged=%d cancelled=%d inferred_not_open=%d filled=%d open=%d uncertain=%d rejected=%d",
        PATCH_VERSION,
        len(report.requested),
        len(report.acknowledged),
        len(report.cancelled),
        len(report.cancelled_by_absence),
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
) -> base.CancelReport:
    normalized = adapter._normalize_symbol(symbol)
    return await adapter._rest.cancel_orders_batch(normalized, order_ids)


def install_lighter_selective_cancel_v3() -> None:
    from .lighter import LighterAdapter
    from .lighter_rest import LighterRest

    if getattr(LighterRest, "_selective_cancel_version", None) == PATCH_VERSION:
        return

    LighterRest.cancel_orders_batch = cancel_orders_batch
    LighterAdapter.cancel_orders = adapter_cancel_orders
    LighterAdapter.cancel_all_orders = base.adapter_cancel_all_disabled
    LighterRest._selective_cancel_installed = True
    LighterRest._selective_cancel_version = PATCH_VERSION
    LighterAdapter._selective_cancel_version = PATCH_VERSION
    logger.info(
        "Installed Lighter selective cancellation version %s",
        PATCH_VERSION,
    )
