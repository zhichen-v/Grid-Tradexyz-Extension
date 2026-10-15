"""Exact-order Lighter cancellation using native transaction batches.

The module intentionally never falls back to Lighter's account-wide
``cancel_all_orders`` operation. It signs only the supplied exchange order
indexes, sends them through ``send_tx_batch``, and reconciles outcomes from
active-order snapshots plus market-filtered inactive-order history that follows
``next_cursor`` past the 100-row ``accountInactiveOrders`` page limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Inherit the REST adapter's file handler as well as console diagnostics.
logger = logging.getLogger("core.adapters.exchanges.adapters.lighter_rest.selective_cancel")

PATCH_VERSION = "2026-09-24.1"
HISTORY_PAGE_LIMIT = 100
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


def _wait_seconds(rest: Any, predicted_ms: int) -> float:
    configured_max = max(
        0.3,
        _config(rest, "cancel_execution_wait_max", 15.0, float),
    )
    return min(max(0.3, predicted_ms / 1000 + 0.2), configured_max)


def _is_invalid_nonce_rejection(exc: Exception) -> bool:
    """Recognize the SDK's explicit rejection, never a transport-error substring."""
    try:
        from lighter.exceptions import BadRequestException
    except ImportError:
        return False
    if not isinstance(exc, BadRequestException) or exc.status != 400:
        return False
    data = exc.data
    if data is None:
        try:
            data = json.loads(exc.body or "null")
        except (TypeError, ValueError):
            return False
    if isinstance(data, dict):
        code, message = data.get("code"), data.get("message")
    else:
        code, message = getattr(data, "code", None), getattr(data, "message", None)
    return code == 21104 and message == "invalid nonce"


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
            # send_tx_batch bypasses the SDK's single-transaction nonce recovery.
            # Only a proven nonce rejection permits one freshly signed retry.
            for attempt in range(2):
                state.update(reserved=0, send_started=False)
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
                except asyncio.CancelledError:
                    # Roll back pre-send reservations before releasing the key
                    # lock, not later in wait_for's timeout handler.
                    if not state["send_started"] and state["reserved"]:
                        _rollback(manager, key, state["reserved"])
                        state["reserved"] = 0
                    raise
                except Exception as exc:
                    if state["send_started"] and _is_invalid_nonce_rejection(exc):
                        # This send was rejected. A slow/failed refresh must not
                        # reclassify it as uncertain or roll back refreshed state.
                        state.update(reserved=0, send_started=False)
                        logger.warning(
                            "Lighter selective cancel explicitly rejected invalid nonce; "
                            "refreshing key nonce (attempt %d/2)", attempt + 1,
                        )
                        try:
                            await manager.async_hard_refresh_nonce(key)
                        except Exception as refresh_exc:
                            raise BatchSubmissionError(
                                "selective batch cancellation rejected (invalid nonce); "
                                f"nonce refresh failed: {refresh_exc}",
                                ambiguous=False,
                            ) from refresh_exc
                        if attempt == 0:
                            continue
                        raise BatchSubmissionError(
                            "selective batch cancellation rejected (invalid nonce) "
                            "after one nonce-refresh retry",
                            ambiguous=False,
                        ) from exc
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
        # Pre-send reservations were returned while holding the key lock.
        # Once a send starts, unknown outcomes retain their nonces.
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


async def adapter_cancel_all_disabled(
    adapter: Any,
    symbol: Any = None,
) -> Any:
    raise RuntimeError(
        "Lighter account-wide cancel_all_orders is disabled. "
        "Use selective cancellation with strategy-owned order indexes."
    )


class HistoryReadError(RuntimeError):
    """Raised when paginated terminal-order history cannot be read safely."""


def _history_max_pages(rest: Any) -> int:
    return max(1, min(_config(rest, "cancel_history_max_pages", 5, int), 10))


def _reconcile_attempts(rest: Any) -> int:
    default = max(3, int(getattr(rest, "MUTATION_RECONCILIATION_ATTEMPTS", 2)))
    return max(1, min(_config(rest, "cancel_reconcile_attempts", default, int), 6))


def _reconcile_delay(rest: Any) -> float:
    default = float(getattr(rest, "MUTATION_RECONCILIATION_DELAY", 0.25))
    return max(0.25, _config(rest, "cancel_reconcile_delay", default, float))


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
            found.update(_keys(order) & target_ids)

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
) -> CancelReport:
    report = CancelReport(requested=set(order_ids))
    report.cancelled_by_absence = set()
    unresolved = set(order_ids)
    absence_confirmations: Dict[str, int] = {
        order_id: 0 for order_id in order_ids
    }

    if initial_delay:
        logger.warning(
            "Lighter selective cancel reconciling; waiting %.2fs before paginated verification",
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
            key: order for order in active for key in _keys(order)
        }
        history_map = {
            key: order for order in history for key in _keys(order)
        }

        next_unresolved: Set[str] = set()
        active_ids = set()
        for order_id in unresolved:
            terminal = history_map.get(order_id)
            terminal_status = _status(terminal) if terminal is not None else ""
            if terminal is not None and terminal_status in CANCELLED:
                report.cancelled.add(order_id)
                report.terminal_orders[order_id] = terminal
                continue
            if terminal is not None and terminal_status in FILLED:
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
) -> CancelReport:
    """Cancel only supplied order indexes and verify with cursor pagination."""
    indexes, report = _validated_ids(order_ids)
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

    size = _batch_size(rest)
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
            response = await _send_chunk(rest, market, chunk)
        except BatchSubmissionError as exc:
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
) -> CancelReport:
    normalized = adapter._normalize_symbol(symbol)
    return await adapter._rest.cancel_orders_batch(normalized, order_ids)


def install_lighter_selective_cancel() -> None:
    from .lighter import LighterAdapter
    from .lighter_rest import LighterRest

    if getattr(LighterRest, "_selective_cancel_version", None) == PATCH_VERSION:
        return

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
