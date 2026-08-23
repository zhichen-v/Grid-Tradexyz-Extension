from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Iterable

from ...adapters.exchanges.models import (
    OrderBookData,
    OrderData,
    OrderSide,
    OrderStatus,
    PositionData,
)
from .config import MarketMakerConfig
from .metrics import MarketMakerMetrics
from .models import MarketMetadata, MarketSnapshot, PositionSnapshot, RuntimeState
from .order_manager import MarketMakerOrderManager
from .risk_manager import RiskManager
from .strategy import MarketMakerStrategy


logger = logging.getLogger(__name__)
_PAUSED_STATES = {
    RuntimeState.PAUSED_DATA,
    RuntimeState.PAUSED_POSITION,
    RuntimeState.PAUSED_EXCHANGE,
    RuntimeState.PAUSED_ORDER_STATE,
    RuntimeState.PAUSED_ERROR,
}


class MarketMakerCoordinator:
    """Own the MM lifecycle and serialize quote reconciliation cycles."""

    def __init__(
        self,
        adapter: Any,
        config: MarketMakerConfig,
        *,
        metadata: MarketMetadata | None = None,
        order_manager: Any | None = None,
        strategy: Any | None = None,
        risk_manager: Any | None = None,
        metrics: MarketMakerMetrics | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        status_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.metadata = metadata
        self.order_manager = order_manager
        self.strategy = strategy or MarketMakerStrategy(config)
        self.risk_manager = risk_manager or RiskManager(config)
        self._monotonic = monotonic
        self._sleep = sleep
        self._status_callback = status_callback

        now = monotonic()
        self.metrics = metrics or MarketMakerMetrics(now)
        self._state = RuntimeState.STARTING
        self._market: MarketSnapshot | None = None
        self._position: PositionSnapshot | None = None
        self._exchange_healthy = False
        self._quote_event = asyncio.Event()
        self._ws_book_event = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._pending_orders: deque[OrderData] = deque()
        self._position_refresh_required = False
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_task: asyncio.Task[None] | None = None
        self._last_cycle_monotonic: float | None = None
        self._error_paused_until: float | None = None
        self._error_streaks: dict[str, int] = {
            "reconcile": 0,
            "position": 0,
            "orders": 0,
            "health": 0,
            "cancel": 0,
        }
        self._stopped_event = asyncio.Event()
        self._running = False
        self._stopping = False
        self._stop_requested = False
        self._subscribed = False
        self._fatal_exception: BaseException | None = None

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def running(self) -> bool:
        return self._running

    @property
    def market_snapshot(self) -> MarketSnapshot | None:
        return self._market

    @property
    def position_snapshot(self) -> PositionSnapshot | None:
        return self._position

    @property
    def quote_event(self) -> asyncio.Event:
        return self._quote_event

    @property
    def fatal_exception(self) -> BaseException | None:
        return self._fatal_exception

    async def wait(self) -> None:
        """Wait for runtime termination and surface background failures."""
        await self._stopped_event.wait()
        if self._fatal_exception is not None:
            raise RuntimeError("market maker runtime failed") from self._fatal_exception

    def request_stop(self) -> None:
        """Synchronously block new cycles while asynchronous cleanup begins."""
        self._stop_requested = True
        self._running = False
        self._quote_event.set()
        self._ws_book_event.set()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            if self._stop_requested:
                self._transition(RuntimeState.STOPPED)
                self._stopped_event.set()
                return
            self._stopping = False
            self._fatal_exception = None
            self._error_paused_until = None
            self._stopped_event.clear()
            self._ws_book_event.clear()
            for source in self._error_streaks:
                self._error_streaks[source] = 0
            self._transition(RuntimeState.STARTING)
            try:
                if not await self.adapter.connect():
                    raise RuntimeError("exchange connection failed")
                if await self._abort_start_if_requested():
                    return
                if not await self.adapter.authenticate():
                    raise RuntimeError("exchange authentication failed")
                if await self._abort_start_if_requested():
                    return

                info = await self.adapter.get_exchange_info()
                if self.metadata is None:
                    self.metadata = self._metadata_from_exchange_info(info)
                self.config.validate_order_size(self.metadata)

                health = await self.adapter.health_check()
                if not self._health_is_healthy(health):
                    raise RuntimeError("exchange health check failed")
                self._exchange_healthy = True
                if await self._abort_start_if_requested():
                    return

                book = await self.adapter.get_orderbook(self.config.symbol)
                if book is not None:
                    self._market = self._normalize_market(book)

                if self._market is not None:
                    target = (self._market.best_bid + self._market.best_ask) / 2
                    self.config.validate_order_size(self.metadata, target)

                if self.order_manager is None:
                    self.order_manager = MarketMakerOrderManager(
                        self.adapter,
                        self.config,
                        self.metadata,
                        monotonic=self._monotonic,
                    )
                await self.order_manager.initialize()
                positions = await self.adapter.get_positions([self.config.symbol])
                self._position = self._position_from_rest(positions)
                if await self._abort_start_if_requested():
                    return

                self._running = True
                await self.adapter.subscribe_orderbook(
                    self.config.symbol, self.on_orderbook
                )
                self._subscribed = True
                if await self._abort_start_if_requested():
                    return
                await self.adapter.subscribe_user_data(self.on_order_update)
                if await self._abort_start_if_requested():
                    return
                await self.adapter.subscribe_positions(self.on_position)
                if await self._abort_start_if_requested():
                    return

                try:
                    await asyncio.wait_for(
                        self._ws_book_event.wait(),
                        timeout=float(self.config.stale_book_seconds),
                    )
                except TimeoutError:
                    raise RuntimeError(
                        "no fresh websocket order book received during startup"
                    ) from None
                if await self._abort_start_if_requested():
                    return
                await self._wait_for_subscription_health()
                if await self._abort_start_if_requested():
                    return
                target = (
                    self._market.best_bid + self._market.best_ask
                ) / Decimal("2")
                self.config.validate_order_size(self.metadata, target)

                self._transition(RuntimeState.SYNCING)
                self._quote_event.clear()
                await self.run_one_cycle(force=True)
                if await self._abort_start_if_requested():
                    return
                self._start_tasks()
            except BaseException as exc:
                self._running = False
                cleanup_error = (
                    None
                    if self._stopped_event.is_set()
                    else await self._startup_cleanup()
                )
                reason = str(exc)
                if cleanup_error is not None:
                    self._fatal_exception = cleanup_error
                    reason = f"{reason}; startup cleanup failed: {cleanup_error}"
                self._transition(RuntimeState.PAUSED_ERROR, reason)
                if cleanup_error is not None:
                    if isinstance(cleanup_error, asyncio.CancelledError):
                        raise RuntimeError("market maker startup cleanup was cancelled") from cleanup_error
                    raise cleanup_error from exc
                raise

    async def stop(self) -> None:
        self.request_stop()
        async with self._lifecycle_lock:
            if self._stopped_event.is_set() and self._state in {
                RuntimeState.STOPPED,
                RuntimeState.PAUSED_ERROR,
            }:
                return
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(
                    self._perform_stop(), name="market-maker-stop"
                )
            stop_task = self._stop_task

        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError as cleanup_cancelled:
                raise RuntimeError("market maker cleanup was cancelled") from cleanup_cancelled
            except BaseException:
                raise
            raise

    async def _perform_stop(self) -> None:
        self._stopping = True
        self._running = False
        self._transition(RuntimeState.STOPPING)
        error: BaseException | None = None

        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except TimeoutError as exc:
                error = RuntimeError("worker tasks did not stop within 5 seconds")
                error.__cause__ = exc
        self._tasks.clear()

        try:
            if self.order_manager is not None:
                async with self._cycle_lock:
                    await asyncio.wait_for(
                        self.order_manager.shutdown(),
                        timeout=max(
                            5.0,
                            float(self.config.stale_position_seconds),
                        ),
                    )
        except BaseException as exc:
            error = error or exc

        try:
            if self._subscribed:
                await asyncio.wait_for(self.adapter.unsubscribe(), timeout=5.0)
                self._subscribed = False
        except BaseException as exc:
            error = error or exc
        try:
            await asyncio.wait_for(self.adapter.disconnect(), timeout=5.0)
        except BaseException as exc:
            error = error or exc

        self._stopping = False
        failure = error or self._fatal_exception
        if failure is not None:
            self._fatal_exception = failure
            self._transition(RuntimeState.PAUSED_ERROR, str(failure))
        else:
            self._transition(RuntimeState.STOPPED)
        self._stopped_event.set()
        if error is not None:
            raise error

    async def on_orderbook(self, book: OrderBookData | MarketSnapshot) -> None:
        """Cache one typed book update; trading mutations happen in the loop."""
        if self._stop_requested or self._stopping or self._state in {
            RuntimeState.STOPPING,
            RuntimeState.STOPPED,
        }:
            return
        try:
            self._market = self._normalize_market(book)
        except (AttributeError, TypeError, ValueError):
            self.metrics.increment("invalid_book_updates")
            return
        self._ws_book_event.set()
        if not self._stopping:
            self._quote_event.set()

    async def on_position(self, position: PositionData | PositionSnapshot) -> None:
        """Cache one typed position update; trading mutations happen in the loop."""
        if self._stop_requested or self._stopping or self._state in {
            RuntimeState.STOPPING,
            RuntimeState.STOPPED,
        }:
            return
        try:
            self._position = self._normalize_position(position)
        except (AttributeError, TypeError, ValueError):
            self.metrics.increment("invalid_position_updates")
            return
        if not self._stopping:
            self._quote_event.set()

    async def on_order_update(self, order: OrderData) -> None:
        """Queue a typed order update for the next serialized cycle."""
        if self._stop_requested or self._stopping or self._state in {
            RuntimeState.STOPPING,
            RuntimeState.STOPPED,
        }:
            return
        if not isinstance(order, OrderData):
            self.metrics.increment("invalid_order_updates")
            return
        if order.symbol != self.config.symbol:
            self.metrics.increment("ignored_other_symbol_orders")
            return
        self._pending_orders.append(order)
        try:
            filled = Decimal(str(order.filled))
            if (filled.is_finite() and filled > 0) or order.status is OrderStatus.FILLED:
                self._position_refresh_required = True
                self.metrics.increment(
                    "full_fills"
                    if order.status is OrderStatus.FILLED
                    else "partial_fills"
                )
        except (InvalidOperation, TypeError, ValueError):
            self._position_refresh_required = True
        if not self._stopping:
            self._quote_event.set()

    async def process_quote_event(self) -> bool:
        """Process one coalesced event; useful to deterministic callers/tests."""
        if not self._quote_event.is_set():
            return False
        self._quote_event.clear()
        await self.run_one_cycle()
        return True

    async def run_one_cycle(self, *, force: bool = False) -> None:
        if not self._running or self._stopping or self.order_manager is None:
            return
        async with self._cycle_lock:
            if not self._running or self._stopping:
                return
            now = self._monotonic()
            minimum_interval = self.config.refresh_interval_ms / 1000
            has_order_updates = bool(self._pending_orders)
            if (
                not force
                and not has_order_updates
                and self._last_cycle_monotonic is not None
                and now - self._last_cycle_monotonic < minimum_interval
            ):
                return
            self._last_cycle_monotonic = now
            while self._pending_orders:
                await self.order_manager.handle_order_update(
                    self._pending_orders.popleft()
                )
            if not await self._refresh_after_post_only_cancellations():
                return

            if self._position_refresh_required:
                if not await self._refresh_position_after_order_update():
                    return

            if self._state is RuntimeState.PAUSED_ERROR:
                if (
                    self._error_paused_until is not None
                    and now < self._error_paused_until
                ):
                    return
                if not await self._recover_from_error_pause():
                    return
                now = self._monotonic()

            pause_reason = getattr(self.order_manager, "pause_reason", None)
            if pause_reason or getattr(
                self.order_manager, "has_uncertain_state", False
            ):
                if has_order_updates and pause_reason and "unknown" in pause_reason:
                    self.metrics.increment("unknown_orders")
                await self._fail_closed(
                    RuntimeState.PAUSED_ORDER_STATE,
                    pause_reason or "order state is uncertain",
                )
                return
            if not self._exchange_healthy:
                await self._fail_closed(
                    RuntimeState.PAUSED_EXCHANGE, "exchange is unhealthy"
                )
                return
            if self._state in {
                RuntimeState.PAUSED_DATA,
                RuntimeState.PAUSED_POSITION,
            }:
                if not await self._recover_from_error_pause(
                    reset_errors=False
                ):
                    return
                now = self._monotonic()
            if self._market is None:
                await self._fail_closed(
                    RuntimeState.PAUSED_DATA, "order book snapshot is unavailable"
                )
                return
            book_age = now - self._market.received_monotonic
            if book_age < 0 or book_age > self.config.stale_book_seconds:
                await self._fail_closed(
                    RuntimeState.PAUSED_DATA, "order book snapshot is stale"
                )
                return
            if self._position is None:
                await self._fail_closed(
                    RuntimeState.PAUSED_POSITION,
                    "position snapshot is unavailable",
                )
                return
            position_age = now - self._position.received_monotonic
            if position_age < 0 or position_age > self.config.stale_position_seconds:
                await self._fail_closed(
                    RuntimeState.PAUSED_POSITION,
                    "position snapshot is stale",
                )
                return

            if self._state in _PAUSED_STATES:
                self._transition(RuntimeState.SYNCING)

            orders = self.order_manager.snapshot()
            risk = self.risk_manager.evaluate(
                self._position,
                orders,
                self.metadata,
                now_monotonic=now,
            )
            self._update_market_metrics(now, orders)
            self.metrics.signed_position = self._position.signed_size
            self.metrics.inventory_ratio = risk.inventory_ratio
            self.metrics.worst_long = risk.worst_long
            self.metrics.worst_short = risk.worst_short
            self.metrics.max_position_utilization = max(
                abs(risk.worst_long), abs(risk.worst_short)
            ) / self.config.max_position
            if self._position.signed_size > 0:
                increasing_amount = risk.buy_amount
            elif self._position.signed_size < 0:
                increasing_amount = risk.sell_amount
            else:
                increasing_amount = self.config.order_size
            self.metrics.risk_increasing_side_multiplier = (
                increasing_amount / self.config.order_size
                if increasing_amount is not None
                else Decimal("0")
            )
            if not risk.safe:
                await self._fail_closed(risk.runtime_state, risk.reason)
                return

            desired = self.strategy.calculate_quotes(
                self._market,
                self._position,
                self.metadata,
                risk,
                orders,
                now_monotonic=now,
            )
            self.metrics.reservation_price = desired.reservation_price
            self.metrics.target_bid = desired.bid.price if desired.bid else None
            self.metrics.target_ask = desired.ask.price if desired.ask else None
            self.metrics.skew_ticks = (
                desired.reservation_price - desired.reference_price
            ) / self.metadata.price_tick
            self.metrics.reduce_only_mode = any(
                order is not None and order.reduce_only
                for order in (desired.bid, desired.ask)
            )
            if desired.bid is not None and desired.ask is not None:
                quote_spread = desired.ask.price - desired.bid.price
                self.metrics.quote_spread_ticks = (
                    quote_spread / self.metadata.price_tick
                )
                self.metrics.quote_spread_bps = (
                    quote_spread / desired.reference_price * Decimal("10000")
                )
            else:
                self.metrics.quote_spread_ticks = None
                self.metrics.quote_spread_bps = None
            result = await self.order_manager.reconcile(desired, risk)
            self._record_reconcile_actions(getattr(result, "actions", ()))
            self._update_live_metrics(self.order_manager.snapshot())
            if getattr(result, "position_refresh_required", False) is True:
                self._position = None
                self._position_refresh_required = True
                if not await self._refresh_position_after_order_update():
                    return
            if not await self._refresh_after_post_only_cancellations():
                return
            if result.errors:
                self.metrics.increment("reconciliation_failure")
                error_reason = "; ".join(result.errors)
                count = await self._record_error(
                    error_reason, source="reconcile"
                )
                normalized_error = (
                    error_reason.lower().replace("_", "").replace(" ", "")
                )
                if "429" in normalized_error or "ratelimit" in normalized_error:
                    self._error_paused_until = (
                        self._monotonic()
                        + self.config.error_cooldown_seconds
                    )
                    await self._fail_closed(
                        RuntimeState.PAUSED_ERROR,
                        "mutation rate limited; error cooldown is active",
                    )
                    return
                if count >= self.config.max_consecutive_errors:
                    await self._fail_closed(
                        RuntimeState.PAUSED_ERROR,
                        "maximum consecutive reconcile errors reached",
                    )
                    return
            else:
                self.metrics.increment("reconciliation_success")
                self.metrics.record_success(now)
                self._reset_error("reconcile")
            self._transition(
                result.runtime_state,
                getattr(self.order_manager, "pause_reason", None),
            )

    async def poll_position_once(self) -> bool:
        try:
            positions = await self.adapter.get_positions([self.config.symbol])
            candidate = self._position_from_rest(positions)
            if candidate is None:
                raise RuntimeError("position REST response is unavailable")
            if (
                self._position is not None
                and candidate.signed_size != self._position.signed_size
            ):
                await self._fail_closed(
                    RuntimeState.PAUSED_POSITION,
                    "position changed without a confirmed order fill; "
                    "full REST resync is required",
                )
                return False
            self._position = candidate
        except Exception as exc:
            count = await self._record_error(
                f"position poll failed: {exc}", source="position"
            )
            if count >= self.config.max_consecutive_errors:
                await self._fail_closed(
                    RuntimeState.PAUSED_ERROR, "repeated position poll errors"
                )
            return False
        self._reset_error("position")
        self._quote_event.set()
        return True

    async def sync_open_orders_once(self) -> bool:
        try:
            position_refresh_required = (
                await self.order_manager.sync_open_orders()
            )
        except Exception as exc:
            count = await self._record_error(
                f"open-order sync failed: {exc}", source="orders"
            )
            self._position = None
            await self._fail_closed(
                (
                    RuntimeState.PAUSED_ERROR
                    if count >= self.config.max_consecutive_errors
                    else RuntimeState.PAUSED_ORDER_STATE
                ),
                "open-order state is unavailable",
            )
            return False
        self._reset_error("orders")
        if getattr(self.order_manager, "has_unknown_order_state", False):
            self.metrics.increment("unknown_orders")
        if position_refresh_required is True:
            self._position = None
            if not await self.poll_position_once():
                if self._state is not RuntimeState.PAUSED_ERROR:
                    await self._fail_closed(
                        RuntimeState.PAUSED_POSITION,
                        "position refresh failed after terminal fill",
                    )
                return False
        if not await self._refresh_after_post_only_cancellations():
            return False
        pause_reason = getattr(self.order_manager, "pause_reason", None)
        if pause_reason or getattr(
            self.order_manager, "has_uncertain_state", False
        ):
            await self._fail_closed(
                RuntimeState.PAUSED_ORDER_STATE,
                pause_reason or "order state is uncertain",
            )
            return False
        if self._state is RuntimeState.PAUSED_ORDER_STATE:
            self._transition(RuntimeState.SYNCING)
        self._quote_event.set()
        return True

    async def poll_health_once(self) -> bool:
        try:
            health = await self.adapter.health_check()
        except Exception as exc:
            count = await self._record_error(
                f"health check failed: {exc}", source="health"
            )
            health = None
        else:
            count = 0
        healthy = self._health_is_healthy(health)
        if isinstance(health, dict):
            websocket = health.get("websocket")
            if isinstance(websocket, dict):
                reconnects = websocket.get(
                    "reconnect_count", websocket.get("reconnect_attempts")
                )
                if type(reconnects) is int and reconnects >= 0:
                    self.metrics.ws_reconnect_count = reconnects
        if not healthy:
            self._exchange_healthy = False
            await self._fail_closed(
                (
                    RuntimeState.PAUSED_ERROR
                    if count >= self.config.max_consecutive_errors
                    else RuntimeState.PAUSED_EXCHANGE
                ),
                "exchange is unhealthy",
            )
            return False

        recovering = not self._exchange_healthy or self._state is RuntimeState.PAUSED_EXCHANGE
        self._reset_error("health")
        if recovering:
            self._transition(RuntimeState.SYNCING)
            try:
                await self._refresh_market_once()
            except Exception as exc:
                await self._fail_closed(
                    RuntimeState.PAUSED_EXCHANGE,
                    f"health recovery market sync failed: {exc}",
                )
                return False
            if not await self.poll_position_once():
                await self._fail_closed(
                    (
                        RuntimeState.PAUSED_ERROR
                        if self._state is RuntimeState.PAUSED_ERROR
                        else RuntimeState.PAUSED_POSITION
                    ),
                    "health recovery position sync failed",
                )
                return False
            if not await self.sync_open_orders_once():
                await self._fail_closed(
                    (
                        RuntimeState.PAUSED_ERROR
                        if self._state is RuntimeState.PAUSED_ERROR
                        else RuntimeState.PAUSED_ORDER_STATE
                    ),
                    "health recovery order sync failed",
                )
                return False
        self._exchange_healthy = True
        self._quote_event.set()
        return True

    async def emit_status_once(self) -> dict[str, Any]:
        now = self._monotonic()
        orders = (
            self.order_manager.snapshot()
            if self.order_manager is not None
            else ()
        )
        if self.metadata is not None:
            self._update_market_metrics(now, orders)
        elif self._position is not None:
            self.metrics.position_age_seconds = max(
                0.0, now - self._position.received_monotonic
            )
        if self._position is not None:
            self.metrics.signed_position = self._position.signed_size
        self._update_live_metrics(orders)
        snapshot = self.metrics.snapshot(now)
        if self._status_callback is None:
            logger.info("market_maker_status %s", snapshot)
        else:
            result = self._status_callback(snapshot)
            if inspect.isawaitable(result):
                await result
        return snapshot

    async def _fail_closed(self, state: RuntimeState, reason: str) -> None:
        self._transition(state, reason)
        try:
            result = await self.order_manager.cancel_managed_orders(reason)
        except Exception as exc:
            await self._record_error(
                f"fail-closed cancel failed: {exc}", source="cancel"
            )
            return
        if result is None:
            return
        self._record_reconcile_actions(getattr(result, "actions", ()))
        if getattr(result, "position_refresh_required", False) is True:
            self._position = None
            self._position_refresh_required = True
        errors = tuple(getattr(result, "errors", ()))
        if errors:
            await self._record_error("; ".join(errors), source="cancel")
        else:
            self._reset_error("cancel")

    async def _record_error(self, reason: str, *, source: str) -> int:
        self.metrics.record_error()
        normalized_reason = reason.lower().replace("_", "").replace(" ", "")
        rate_limited = (
            "429" in normalized_reason or "ratelimit" in normalized_reason
        )
        if rate_limited:
            self.metrics.increment("http_429")
        count = self._error_streaks.get(source, 0) + 1
        self._error_streaks[source] = count
        self.metrics.consecutive_errors = max(self._error_streaks.values())
        if count >= self.config.max_consecutive_errors or (
            source == "cancel" and rate_limited
        ):
            self._error_paused_until = (
                self._monotonic() + self.config.error_cooldown_seconds
            )
            self._transition(RuntimeState.PAUSED_ERROR, reason)
        return count

    def _reset_error(self, source: str) -> None:
        self._error_streaks[source] = 0
        self.metrics.consecutive_errors = max(self._error_streaks.values())

    async def _refresh_position_after_order_update(self) -> bool:
        try:
            positions = await self.adapter.get_positions([self.config.symbol])
            position = self._position_from_rest(positions)
            if position is None:
                raise RuntimeError("position REST response is unavailable")
        except Exception as exc:
            await self._fail_closed(
                RuntimeState.PAUSED_POSITION,
                f"position refresh after order update failed: {exc}",
            )
            return False
        self._position = position
        self._position_refresh_required = False
        self.metrics.increment("position_refresh_after_order_update")
        return True

    async def _recover_from_error_pause(
        self, *, reset_errors: bool = True
    ) -> bool:
        """Require cooldown plus fresh health/data/order truth before quoting."""
        try:
            health = await self.adapter.health_check()
            if not self._health_is_healthy(health):
                raise RuntimeError("exchange remains unhealthy")
            await self._refresh_market_once()
            positions = await self.adapter.get_positions([self.config.symbol])
            position = self._position_from_rest(positions)
            if position is None:
                raise RuntimeError("position REST response is unavailable")
            self._position = position
            position_refresh_required = (
                await self.order_manager.sync_open_orders()
            )
            if position_refresh_required is True:
                self._position = None
                positions = await self.adapter.get_positions([self.config.symbol])
                position = self._position_from_rest(positions)
                if position is None:
                    raise RuntimeError(
                        "position REST response is unavailable after order sync"
                    )
                self._position = position
            pause_reason = getattr(self.order_manager, "pause_reason", None)
            if pause_reason or getattr(
                self.order_manager, "has_uncertain_state", False
            ):
                raise RuntimeError(
                    pause_reason or "order state is uncertain"
                )
        except Exception as exc:
            self._error_paused_until = (
                self._monotonic() + self.config.error_cooldown_seconds
            )
            await self._fail_closed(
                RuntimeState.PAUSED_ERROR,
                f"error recovery checks failed: {exc}",
            )
            return False
        self._exchange_healthy = True
        recovered_sources = (
            self._error_streaks
            if reset_errors
            else ("health", "position", "orders")
        )
        for source in recovered_sources:
            self._error_streaks[source] = 0
        self.metrics.consecutive_errors = max(self._error_streaks.values())
        if reset_errors:
            self._error_paused_until = None
        self._transition(RuntimeState.SYNCING)
        return True

    def _transition(self, state: RuntimeState, reason: str | None = None) -> None:
        self._state = state
        self.metrics.transition(state, reason)

    def _update_market_metrics(self, now: float, orders: Iterable[Any]) -> None:
        if self._market is not None:
            self.metrics.book_age_seconds = max(
                0.0, now - self._market.received_monotonic
            )
            self.metrics.best_bid = self._market.best_bid
            self.metrics.best_ask = self._market.best_ask
            self.metrics.mid = (
                self._market.best_bid + self._market.best_ask
            ) / Decimal("2")
            spread = self._market.best_ask - self._market.best_bid
            self.metrics.raw_spread_ticks = spread / self.metadata.price_tick
            self.metrics.raw_spread_bps = (
                spread / self.metrics.mid * Decimal("10000")
                if self.metrics.mid > 0
                else None
            )
            self.metrics.reference_includes_own_quote = any(
                (
                    order.side is OrderSide.BUY
                    and order.price == self._market.best_bid
                )
                or (
                    order.side is OrderSide.SELL
                    and order.price == self._market.best_ask
                )
                for order in orders
            )
        if self._position is not None:
            self.metrics.position_age_seconds = max(
                0.0, now - self._position.received_monotonic
            )

    def _record_reconcile_actions(self, actions: Iterable[Any]) -> None:
        for action in actions:
            operation = getattr(action, "operation", "")
            if operation == "place":
                self.metrics.increment("create_attempts")
                if getattr(action, "success", None) is True:
                    self.metrics.increment("create_success")
                elif getattr(action, "success", None) is None:
                    self.metrics.increment("ambiguous_submissions")
            elif operation == "cancel":
                self.metrics.increment("cancel_attempts")
                if getattr(action, "success", None) is True:
                    self.metrics.increment("cancel_success")
                elif getattr(action, "success", None) is None:
                    self.metrics.increment("ambiguous_cancellations")
            elif operation == "would_place":
                self.metrics.increment("would_place")
            elif operation == "would_cancel":
                self.metrics.increment("would_cancel")
            elif operation == "blocked":
                self.metrics.increment("mutation_limiter_blocks")

    def _update_live_metrics(self, orders: Iterable[Any]) -> None:
        live = tuple(orders)
        buys = tuple(order for order in live if order.side is OrderSide.BUY)
        sells = tuple(order for order in live if order.side is OrderSide.SELL)
        self.metrics.live_buy_remaining = sum(
            (order.remaining for order in buys), Decimal("0")
        )
        self.metrics.live_sell_remaining = sum(
            (order.remaining for order in sells), Decimal("0")
        )
        self.metrics.live_bid = buys[0].price if buys else None
        self.metrics.live_ask = sells[0].price if sells else None

    async def _refresh_market_once(self) -> None:
        book = await self.adapter.get_orderbook(self.config.symbol)
        if book is None:
            raise RuntimeError("order book REST response is unavailable")
        book = self._normalize_market(book)
        await self.on_orderbook(book)

    async def _refresh_after_post_only_cancellations(self) -> bool:
        consumer = getattr(
            self.order_manager, "consume_post_only_cancellations", None
        )
        if not callable(consumer):
            return True

        event_count, generation = consumer()
        refresh_required = getattr(
            self.order_manager, "post_only_book_refresh_required", False
        ) is True
        if event_count:
            self.metrics.increment("post_only_cancellations", event_count)
        if not refresh_required:
            return True

        try:
            await self._refresh_market_once()
        except Exception:
            await self._fail_closed(
                RuntimeState.PAUSED_DATA,
                "fresh order book is unavailable after post-only cancellation",
            )
            return False

        acknowledge = getattr(
            self.order_manager,
            "acknowledge_post_only_book_refresh",
            None,
        )
        if callable(acknowledge):
            acknowledge(generation)
        return True

    async def _quote_loop(self) -> None:
        timeout = self.config.refresh_interval_ms / 1000
        while self._running:
            try:
                await asyncio.wait_for(self._quote_event.wait(), timeout=timeout)
            except TimeoutError:
                pass
            self._quote_event.clear()
            await self.run_one_cycle()

    async def _periodic_loop(
        self, interval: float, operation: Callable[[], Awaitable[Any]]
    ) -> None:
        while self._running:
            await self._sleep(interval)
            if self._running:
                await operation()

    def _start_tasks(self) -> None:
        loops = (
            ("market-maker-quotes", self._quote_loop()),
            (
                "market-maker-position",
                self._periodic_loop(
                    self.config.position_poll_interval_seconds,
                    self.poll_position_once,
                ),
            ),
            (
                "market-maker-orders",
                self._periodic_loop(
                    self.config.order_sync_interval_seconds,
                    self.sync_open_orders_once,
                ),
            ),
            (
                "market-maker-health",
                self._periodic_loop(
                    self.config.health_check_interval_seconds,
                    self.poll_health_once,
                ),
            ),
            (
                "market-maker-status",
                self._periodic_loop(
                    self.config.log_status_interval_seconds,
                    self.emit_status_once,
                ),
            ),
        )
        for name, coroutine in loops:
            task = asyncio.create_task(coroutine, name=name)
            task.add_done_callback(self._task_done)
            self._tasks.append(task)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            return
        self._fatal_exception = error
        self.request_stop()
        asyncio.create_task(self._stop_after_task_failure(task.get_name(), error))

    async def _stop_after_task_failure(
        self, task_name: str, error: BaseException
    ) -> None:
        try:
            await self.stop()
        except BaseException:
            logger.exception("critical cleanup failed after %s", task_name)

    async def _startup_cleanup(self) -> BaseException | None:
        error: BaseException | None = None
        if self.order_manager is not None:
            try:
                await asyncio.wait_for(
                    self.order_manager.shutdown(),
                    timeout=max(
                        5.0,
                        float(self.config.stale_position_seconds),
                    ),
                )
            except BaseException as exc:
                error = exc
        if self._subscribed:
            try:
                await asyncio.wait_for(self.adapter.unsubscribe(), timeout=5.0)
            except BaseException as exc:
                error = error or exc
            self._subscribed = False
        try:
            await asyncio.wait_for(self.adapter.disconnect(), timeout=5.0)
        except BaseException as exc:
            error = error or exc
        self._stopped_event.set()
        return error

    async def _abort_start_if_requested(self) -> bool:
        if not self._stop_requested:
            return False
        self._running = False
        self._transition(RuntimeState.STOPPING)
        cleanup_error = await self._startup_cleanup()
        if cleanup_error is not None:
            raise RuntimeError(
                f"startup stop cleanup failed: {cleanup_error}"
            ) from cleanup_error
        self._transition(RuntimeState.STOPPED)
        return True

    async def _wait_for_subscription_health(self) -> None:
        async def wait_until_healthy() -> None:
            while not self._stop_requested:
                try:
                    health = await self.adapter.health_check()
                except Exception:
                    health = None
                if self._health_is_healthy(health):
                    self._exchange_healthy = True
                    return
                await self._sleep(2.0)

        try:
            await asyncio.wait_for(
                wait_until_healthy(),
                timeout=max(5.0, float(self.config.stale_book_seconds)),
            )
        except TimeoutError:
            raise RuntimeError(
                "private websocket subscriptions did not become healthy"
            ) from None

    def _metadata_from_exchange_info(self, info: Any) -> MarketMetadata:
        markets = getattr(info, "markets", None)
        market = markets.get(self.config.symbol) if isinstance(markets, dict) else None
        if market is None:
            symbols = getattr(info, "symbols", ()) or ()
            market = next(
                (
                    item
                    for item in symbols
                    if isinstance(item, dict)
                    and item.get("symbol") == self.config.symbol
                ),
                None,
            )
        if not isinstance(market, dict):
            raise ValueError("configured market metadata is unavailable")

        price_decimals = market.get(
            "price_decimals", market.get("supported_price_decimals")
        )
        size_decimals = market.get(
            "size_decimals", market.get("supported_size_decimals")
        )
        if (
            type(price_decimals) is not int
            or price_decimals < 0
            or type(size_decimals) is not int
            or size_decimals < 0
        ):
            raise ValueError("market precision metadata is invalid")
        min_base = self._metadata_decimal(market.get("min_base_amount"), "min_base_amount")
        min_quote = self._metadata_decimal(
            market.get("min_quote_amount"), "min_quote_amount"
        )
        return MarketMetadata(
            symbol=self.config.symbol,
            price_decimals=price_decimals,
            size_decimals=size_decimals,
            price_tick=Decimal(1).scaleb(-price_decimals),
            quantity_step=Decimal(1).scaleb(-size_decimals),
            min_base_amount=min_base,
            min_quote_amount=min_quote,
        )

    @staticmethod
    def _metadata_decimal(value: Any, name: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} metadata is invalid") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"{name} metadata is invalid")
        return parsed

    def _normalize_market(
        self, book: OrderBookData | MarketSnapshot
    ) -> MarketSnapshot:
        if isinstance(book, MarketSnapshot):
            if book.symbol != self.config.symbol:
                raise ValueError("order book symbol does not match config")
            return replace(book, received_monotonic=self._monotonic())
        if not isinstance(book, OrderBookData) or book.symbol != self.config.symbol:
            raise ValueError("order book update is invalid")
        bids = tuple(book.bids)
        asks = tuple(book.asks)
        if not bids or not asks:
            raise ValueError("order book is empty")
        return MarketSnapshot(
            symbol=book.symbol,
            bids=bids,
            asks=asks,
            best_bid=Decimal(str(bids[0].price)),
            best_ask=Decimal(str(asks[0].price)),
            exchange_timestamp=book.exchange_timestamp or book.timestamp,
            received_monotonic=self._monotonic(),
        )

    def _normalize_position(
        self, position: PositionData | PositionSnapshot
    ) -> PositionSnapshot:
        if isinstance(position, PositionSnapshot):
            if position.symbol != self.config.symbol:
                raise ValueError("position symbol does not match config")
            return replace(position, received_monotonic=self._monotonic())
        if not isinstance(position, PositionData) or position.symbol != self.config.symbol:
            raise ValueError("position update is invalid")
        side = getattr(position.side, "value", "").lower()
        size = Decimal(str(position.size))
        if side == "short":
            size = -abs(size)
        elif side == "long":
            size = abs(size)
        elif size != 0:
            raise ValueError("position side is ambiguous")
        return PositionSnapshot(
            symbol=position.symbol,
            signed_size=size,
            entry_price=(
                Decimal(str(position.entry_price))
                if position.entry_price is not None
                else None
            ),
            unrealized_pnl=(
                Decimal(str(position.unrealized_pnl))
                if position.unrealized_pnl is not None
                else None
            ),
            received_monotonic=self._monotonic(),
        )

    def _position_from_rest(
        self, positions: Iterable[PositionData] | None
    ) -> PositionSnapshot | None:
        if positions is None:
            return None
        for position in positions:
            if getattr(position, "symbol", None) == self.config.symbol:
                return self._normalize_position(position)
        return PositionSnapshot(
            symbol=self.config.symbol,
            signed_size=Decimal("0"),
            entry_price=None,
            unrealized_pnl=None,
            received_monotonic=self._monotonic(),
        )

    @staticmethod
    def _health_is_healthy(health: Any) -> bool:
        if isinstance(health, bool):
            return health
        return isinstance(health, dict) and health.get("healthy") is True
