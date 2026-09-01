from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Iterable

from ...adapters.exchanges.models import (
    OrderBookData,
    OrderData,
    OrderSide,
    OrderStatus,
    PositionData,
)
from .account_monitor import AccountAuditError, MarketMakerAccountMonitor
from .config import MarketMakerConfig, ceil_to_step, floor_to_step
from .controllers import (
    FixedEntryQuoteController,
    QuoteControllerContext,
    QuoteControllerDecision,
    SideQuoteAdjustment,
    ToxicityAwareEntryQuoteController,
)
from .inventory_unwind import InventoryEpisodeExecutor
from .market_features import MarketFeatureStore, build_external_book_view
from .metrics import MarketMakerMetrics
from .models import (
    MarketMetadata,
    MarketSnapshot,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)
from .order_manager import MarketMakerOrderManager
from .quote_arbiter import (
    QuoteArbiterContext,
    apply_entry_controller,
    controller_decision_error,
)
from .risk_manager import RiskManager
from .strategy import MarketMakerStrategy, SoftExitEconomics


logger = logging.getLogger(__name__)
_PAUSED_STATES = {
    RuntimeState.PAUSED_DATA,
    RuntimeState.PAUSED_POSITION,
    RuntimeState.PAUSED_EXCHANGE,
    RuntimeState.PAUSED_ORDER_STATE,
    RuntimeState.PAUSED_ERROR,
}
_QUOTING_STATES = {RuntimeState.ACTIVE, RuntimeState.RISK_REDUCTION}


@dataclass(frozen=True)
class _ActiveUnwindTruthToken:
    episode_id: int
    signed_position: Decimal
    position_received_monotonic: float
    audit_generation: int
    audit_monotonic: float
    minimum_book_received_monotonic: float
    manager_mutation_generation: int
    manager_prepare_generation: int


def _valid_book_level(level: Any) -> bool:
    price = getattr(level, "price", None)
    size = getattr(level, "size", None)
    return (
        isinstance(price, Decimal)
        and price.is_finite()
        and price > 0
        and isinstance(size, Decimal)
        and size.is_finite()
        and size > 0
    )


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
        account_monitor: Any | None = None,
        inventory_executor: Any | None = None,
        quote_controller: Any | None = None,
        market_feature_store: Any | None = None,
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
        self.metrics.round_trip_fee_bps = (
            Decimal("2") * config.maker_fee_rate * Decimal("10000")
        )
        self.metrics.min_profit_buffer_bps = config.min_profit_buffer_bps
        self.account_monitor = account_monitor
        self.inventory_executor = inventory_executor or InventoryEpisodeExecutor(
            config
        )
        self.quote_controller = quote_controller or (
            FixedEntryQuoteController()
            if config.quote_controller_mode == "fixed"
            else ToxicityAwareEntryQuoteController.from_config(config)
        )
        self.market_feature_store = market_feature_store
        self._controller_telemetry_decision_id = 0
        self._account_monitor_initialized = False
        self._processed_fill_generation = 0
        self._audited_fill_generation = 0
        self._authenticated = False
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
        self._eligible_quote_seconds = 0.0
        self._eligible_quote_started: float | None = None
        self._active_unwind_truth_token: _ActiveUnwindTruthToken | None = None
        self._entry_admission_blocked = False
        self._entry_admission_metrics: dict[str, Any] = {}

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
            self._account_monitor_initialized = False
            self.inventory_executor.reset_session()
            self._active_unwind_truth_token = None
            self._processed_fill_generation = 0
            self._audited_fill_generation = -1
            self._authenticated = False
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
                self._authenticated = True
                if (
                    not self.config.dry_run
                    and self.config.account_audit_interval_seconds
                    and self.account_monitor is None
                ):
                    self.account_monitor = MarketMakerAccountMonitor(
                        self.adapter,
                        self.config,
                        monotonic=self._monotonic,
                        sleep=self._sleep,
                    )
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
                        sleep=self._sleep,
                    )
                await self.order_manager.initialize()
                positions = await self.adapter.get_positions([self.config.symbol])
                self._position = self._position_from_rest(positions)
                await self._initialize_account_monitor()
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

        cleanup_error = await self._cleanup_orders_and_final_audit()
        error = self._combine_cleanup_errors(error, cleanup_error)

        try:
            if self._subscribed:
                await asyncio.wait_for(self.adapter.unsubscribe(), timeout=5.0)
                self._subscribed = False
        except BaseException as exc:
            error = self._combine_cleanup_errors(error, exc)
        try:
            await asyncio.wait_for(self.adapter.disconnect(), timeout=5.0)
        except BaseException as exc:
            error = self._combine_cleanup_errors(error, exc)
        finally:
            self._authenticated = False

        self._stopping = False
        failure = self._combine_cleanup_errors(self._fatal_exception, error)
        if failure is not None:
            self._fatal_exception = failure
            self._transition(RuntimeState.PAUSED_ERROR, str(failure))
        else:
            self._transition(RuntimeState.STOPPED)
        self._stopped_event.set()
        if error is not None:
            assert failure is not None
            raise failure

    async def _shutdown_order_manager_locked(self) -> None:
        async with self._cycle_lock:
            await self.order_manager.shutdown()

    async def _cleanup_orders_and_final_audit(self) -> BaseException | None:
        shutdown_error: BaseException | None = None
        if self.order_manager is not None:
            cleanup_timeout = max(
                5.0,
                float(self.config.stale_position_seconds),
            )
            try:
                await asyncio.wait_for(
                    self._shutdown_order_manager_locked(),
                    timeout=cleanup_timeout,
                )
            except TimeoutError as exc:
                shutdown_error = RuntimeError(
                    "order manager shutdown did not finish within "
                    f"{cleanup_timeout:g} seconds"
                )
                shutdown_error.__cause__ = exc
            except BaseException as exc:
                shutdown_error = exc

        initialization_error: BaseException | None = None
        if (
            not self.config.dry_run
            and self._authenticated
            and self.config.account_audit_interval_seconds
            and self.account_monitor is not None
            and not self._account_monitor_initialized
        ):
            try:
                await self._initialize_account_monitor()
            except BaseException as exc:
                initialization_error = exc

        audit_error: BaseException | None = None
        status_error: BaseException | None = None
        if (
            not self.config.dry_run
            and self._authenticated
            and callable(getattr(self.account_monitor, "audit", None))
        ):
            try:
                await self.audit_account_once()
            except BaseException as exc:
                audit_error = exc
            try:
                await self.emit_status_once(
                    event="market_maker_final_account_audit"
                )
            except BaseException as exc:
                status_error = exc
        return self._combine_cleanup_errors(
            shutdown_error,
            initialization_error,
            audit_error,
            status_error,
        )

    @staticmethod
    def _combine_cleanup_errors(
        *errors: BaseException | None,
    ) -> BaseException | None:
        failures = tuple(error for error in errors if error is not None)
        if not failures:
            return None
        if len(failures) == 1:
            return failures[0]
        combined = RuntimeError(
            "; ".join(str(error) or type(error).__name__ for error in failures)
        )
        combined.__cause__ = failures[0]
        return combined

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
            if getattr(book, "symbol", None) == self.config.symbol:
                self._market = None
                if not self._stopping:
                    self._quote_event.set()
            return
        mid = (self._market.best_bid + self._market.best_ask) / Decimal("2")
        if self.metrics.fill_markouts:
            try:
                self.metrics.update_fill_markouts(
                    now=self._market.received_monotonic,
                    mid=mid,
                    external_mid=self._external_markout_mid(),
                )
            except Exception:
                self.metrics.increment("markout_telemetry_errors")
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
        self._active_unwind_truth_token = None
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
        self._active_unwind_truth_token = None
        self._pending_orders.append(order)
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
                and self._active_unwind_truth_token is None
                and self._last_cycle_monotonic is not None
                and now - self._last_cycle_monotonic < minimum_interval
            ):
                return
            self._last_cycle_monotonic = now
            while self._pending_orders:
                order = self._pending_orders.popleft()
                fill_observed = await self.order_manager.handle_order_update(
                    order
                )
                if fill_observed is True:
                    self._processed_fill_generation += 1
                    self._position_refresh_required = True
                    self._record_maker_fill_markout(
                        order, now, source="websocket_order_update"
                    )
                    self.metrics.increment(
                        "full_fills"
                        if order.status is OrderStatus.FILLED
                        else "partial_fills"
                    )
            self._sync_open_maker_fill_progress(
                self.order_manager.snapshot()
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
            account_state = self.metrics.account_audit.get("state")
            economic_state = self.metrics.account_audit.get("economic_state")
            if account_state == "hard_stop" or economic_state == "no_go":
                reason = str(
                    self.metrics.account_audit.get("economic_reason")
                    or self.metrics.account_audit.get("reason")
                    or "account economic circuit breaker is no-go"
                )
                await self._fail_closed(RuntimeState.PAUSED_ERROR, reason)
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
            economic_stop_pending = (
                self.metrics.account_audit.get("economic_state")
                == "economic_stop_pending_flat"
            )
            allow_new_episode = (
                not self.config.ping_pong_enabled
                or self.config.dry_run
                or self._authenticated_flat_checkpoint()
            ) and not economic_stop_pending and (
                self._entry_admission_allows_new_episode(now)
            )
            risk = self.risk_manager.evaluate(
                self._position,
                orders,
                self.metadata,
                now_monotonic=now,
                allow_new_episode=allow_new_episode,
                force_inventory_exit=economic_stop_pending,
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

            base_desired = self.strategy.calculate_quotes(
                self._market,
                self._position,
                self.metadata,
                risk,
                orders,
                now_monotonic=now,
                soft_exit_economics=self._trusted_soft_exit_economics(now),
            )
            desired, controller_cycle = self._apply_entry_quote_controller(
                base=base_desired,
                risk=risk,
                orders=orders,
                now=now,
                economic_stop_pending=economic_stop_pending,
                entry_admission_allowed=allow_new_episode,
            )
            self.metrics.reservation_price = desired.reservation_price
            self.metrics.target_bid = desired.bid.price if desired.bid else None
            self.metrics.target_ask = desired.ask.price if desired.ask else None
            self.metrics.quote_reason = desired.reason
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
                self.metrics.quote_edge_after_fees_bps = (
                    self.metrics.quote_spread_bps
                    - self.metrics.round_trip_fee_bps
                )
            else:
                self.metrics.quote_spread_ticks = None
                self.metrics.quote_spread_bps = None
                self.metrics.quote_edge_after_fees_bps = None
            if desired.runtime_state is RuntimeState.PAUSED_ERROR:
                await self._fail_closed(RuntimeState.PAUSED_ERROR, desired.reason)
                raise RuntimeError(f"strategy hard stop: {desired.reason}")
            stranded_distance = desired.half_spread + self.metadata.price_tick
            stranded_soft_exit = (
                getattr(risk, "soft_exit_latched", False) is True
                and (
                    (
                        desired.bid is not None
                        and desired.bid.reduce_only
                        and desired.reference_price - desired.bid.price
                        > stranded_distance
                    )
                    or (
                        desired.ask is not None
                        and desired.ask.reduce_only
                        and desired.ask.price - desired.reference_price
                        > stranded_distance
                    )
                )
            )
            active_lane = False
            if self.config.active_unwind_enabled:
                unwind = self.inventory_executor.evaluate(
                    position=self._position,
                    market=self._market,
                    metadata=self.metadata,
                    account_snapshot=self._trusted_inventory_unwind_snapshot(now),
                    now_monotonic=now,
                    soft_exit_latched=(
                        getattr(risk, "soft_exit_latched", False) is True
                    ),
                    stranded_soft_exit=stranded_soft_exit,
                    authenticated_flat=self._authenticated_flat_checkpoint(),
                    active_unwind_pending=bool(
                        getattr(
                            self.order_manager, "active_unwind_pending", False
                        )
                    ),
                    normal_passive_price=self._normal_passive_unwind_price(
                        desired
                    ),
                )
                self.metrics.inventory_unwind = unwind.snapshot()
                if unwind.blocked:
                    if unwind.budget_blocked:
                        self.metrics.increment("episode_cap_blocked")
                    self.metrics.increment("active_unwind_blocks")
                    self.metrics.quote_reason = unwind.reason
                    await self._fail_closed(RuntimeState.PAUSED_ERROR, unwind.reason)
                    raise RuntimeError(
                        f"inventory unwind hard stop: {unwind.reason}"
                    )
                if unwind.active_order is not None:
                    active_lane = True
                    prepared_generation = self._consume_active_unwind_truth(
                        unwind.episode_id, now
                    )
                    manager_prepared_generation = getattr(
                        self.order_manager,
                        "active_unwind_prepared_generation",
                        None,
                    )
                    if (
                        prepared_generation is None
                        and type(manager_prepared_generation) is int
                    ):
                        if not await self._refresh_active_unwind_truth():
                            return
                        if not self._arm_active_unwind_truth(unwind.episode_id):
                            reason = (
                                "active unwind fresh-truth token could not be re-armed"
                            )
                            await self._fail_closed(
                                RuntimeState.PAUSED_ORDER_STATE, reason
                            )
                            return
                        self._quote_event.set()
                        return
                    result = await self.order_manager.execute_active_unwind(
                        unwind.active_order,
                        prepared_generation=prepared_generation,
                    )
                else:
                    if unwind.passive_order is not None:
                        if unwind.passive_order.side is OrderSide.BUY:
                            desired = replace(
                                desired,
                                bid=unwind.passive_order,
                                ask=None,
                                reason=unwind.reason,
                            )
                        else:
                            desired = replace(
                                desired,
                                bid=None,
                                ask=unwind.passive_order,
                                reason=unwind.reason,
                            )
                    elif unwind.suppress_passive:
                        desired = replace(
                            desired,
                            bid=None,
                            ask=None,
                            reason=unwind.reason,
                        )
                    result = await self.order_manager.reconcile(desired, risk)
            elif stranded_soft_exit:
                reason = (
                    "soft exit is stranded outside the normal passive quote "
                    "band by the economic gate"
                )
                self.metrics.quote_reason = reason
                await self._fail_closed(RuntimeState.PAUSED_ERROR, reason)
                raise RuntimeError(f"strategy hard stop: {reason}")
            else:
                result = await self.order_manager.reconcile(desired, risk)
            if active_lane and unwind.active_order is not None:
                self.metrics.target_bid = (
                    unwind.active_order.price
                    if unwind.active_order.side is OrderSide.BUY
                    else None
                )
                self.metrics.target_ask = (
                    unwind.active_order.price
                    if unwind.active_order.side is OrderSide.SELL
                    else None
                )
            else:
                self.metrics.target_bid = desired.bid.price if desired.bid else None
                self.metrics.target_ask = desired.ask.price if desired.ask else None
            actions = tuple(getattr(result, "actions", ()))
            self._record_quote_contexts(actions, controller_cycle, now)
            self._record_reconcile_actions(actions)
            if active_lane:
                for action in actions:
                    if (
                        getattr(action, "operation", "") == "active_unwind"
                        and "not sent"
                        not in str(getattr(action, "reason", "")).lower()
                    ):
                        self.inventory_executor.record_active_attempt()
            current_orders = self.order_manager.snapshot()
            self._sync_open_maker_fill_progress(current_orders)
            self._update_live_metrics(current_orders)
            if getattr(result, "fill_observed", False) is True:
                self._processed_fill_generation += 1
                for observed_order in getattr(
                    result, "observed_fill_orders", ()
                ):
                    self._record_maker_fill_markout(
                        observed_order,
                        now,
                        source="reconciliation",
                    )
            if active_lane and (
                result.errors
                or getattr(self.order_manager, "has_uncertain_state", False)
            ):
                self.metrics.increment("reconciliation_failure")
                reason = "; ".join(result.errors) or "active unwind state is uncertain"
                await self._record_error(reason, source="reconcile")
                await self._fail_closed(RuntimeState.PAUSED_ORDER_STATE, reason)
                raise RuntimeError(f"active unwind hard stop: {reason}")
            if getattr(result, "position_refresh_required", False) is True:
                prepared_active_unwind = active_lane and any(
                    getattr(action, "operation", "")
                    == "prepare_active_unwind"
                    and getattr(action, "success", None) is True
                    for action in actions
                )
                self._position = None
                self._position_refresh_required = True
                if active_lane:
                    if not await self._refresh_active_unwind_truth():
                        return
                    if prepared_active_unwind:
                        if not self._arm_active_unwind_truth(unwind.episode_id):
                            reason = (
                                "active unwind fresh-truth token could not be armed"
                            )
                            await self._fail_closed(
                                RuntimeState.PAUSED_ORDER_STATE, reason
                            )
                            return
                        self._quote_event.set()
                elif not await self._refresh_position_after_order_update():
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
                risk.runtime_state if active_lane else result.runtime_state,
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
            async with self._cycle_lock:
                position_refresh_required = (
                    await self.order_manager.sync_open_orders()
                )
                self._sync_open_maker_fill_progress(
                    self.order_manager.snapshot()
                )
                if position_refresh_required is True:
                    self._processed_fill_generation += 1
                    self._position = None
                    sync_result = getattr(
                        self.order_manager, "last_sync_result", None
                    )
                    for observed_order in getattr(
                        sync_result, "observed_fill_orders", ()
                    ):
                        self._record_maker_fill_markout(
                            observed_order,
                            self._monotonic(),
                            source="rest_open_order_sync",
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

    async def _initialize_account_monitor(self) -> None:
        if (
            not self.config.account_audit_interval_seconds
            or self._account_monitor_initialized
        ):
            return
        if self.account_monitor is None:
            self.account_monitor = MarketMakerAccountMonitor(
                self.adapter,
                self.config,
                monotonic=self._monotonic,
                sleep=self._sleep,
            )
        try:
            await asyncio.wait_for(
                self.account_monitor.initialize(),
                timeout=self.config.account_audit_timeout_seconds,
            )
        except TimeoutError as exc:
            self.account_monitor.mark_hard_stop(
                "account audit initialization timed out"
            )
            raise AccountAuditError(
                "account audit initialization timed out"
            ) from exc
        except AccountAuditError:
            raise
        except Exception as exc:
            reason = (
                "account audit initialization failed: "
                f"{type(exc).__name__}: {exc}"
            )
            self.account_monitor.mark_hard_stop(reason)
            raise AccountAuditError(reason) from exc
        self._account_monitor_initialized = True
        self._audited_fill_generation = self._processed_fill_generation
        self._update_account_audit_metrics()

    async def emit_status_once(
        self, *, event: str | None = None
    ) -> dict[str, Any]:
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
        self.metrics.eligible_quote_seconds = self._eligible_seconds(now)
        self._update_live_metrics(orders)
        self._update_account_audit_metrics()
        snapshot = self.metrics.snapshot(now)
        if event is not None:
            snapshot["event"] = event
        if self._status_callback is None:
            logger.info("%s %s", event or "market_maker_status", snapshot)
        else:
            result = self._status_callback(snapshot)
            if inspect.isawaitable(result):
                await result
        return snapshot

    async def audit_account_once(self) -> None:
        if self.account_monitor is None:
            return
        try:
            await asyncio.wait_for(
                self._audit_account_locked(),
                timeout=self.config.account_audit_timeout_seconds,
            )
        except TimeoutError as exc:
            reason = "account audit timed out"
            marker = getattr(self.account_monitor, "mark_hard_stop", None)
            if callable(marker):
                marker(reason)
            self.request_stop()
            raise AccountAuditError(reason) from exc
        except AccountAuditError:
            self.request_stop()
            raise
        except Exception as exc:
            reason = f"account audit failed: {type(exc).__name__}: {exc}"
            marker = getattr(self.account_monitor, "mark_hard_stop", None)
            if callable(marker):
                marker(reason)
            self.request_stop()
            raise AccountAuditError(reason) from exc
        finally:
            self._update_account_audit_metrics()

    async def _audit_account_locked(self) -> None:
        async with self._cycle_lock:
            audit_succeeded = False
            try:
                await self._audit_account_current_state()
                audit_succeeded = True
            except BaseException:
                self.request_stop()
                raise
            finally:
                # Publish the economic latch before a cycle waiting on this
                # lock can quote from the previous audit state.
                self._update_account_audit_metrics()
                trusted_no_go_flat = (
                    self.metrics.account_audit.get("state") == "hard_stop"
                    and self.metrics.account_audit.get("economic_state") == "no_go"
                )
                trusted_stopped_flat = (
                    self.metrics.account_audit.get("state") == "hard_stop"
                    and self.metrics.account_audit.get(
                        "last_audit_authenticated"
                    )
                    is True
                )
                if (
                    (
                        audit_succeeded
                        or trusted_no_go_flat
                        or trusted_stopped_flat
                    )
                    and self.metrics.account_audit.get("ledger_position")
                    == Decimal("0")
                    and self.metrics.account_audit.get("audited_position")
                    == Decimal("0")
                ):
                    self._audited_fill_generation = self._processed_fill_generation
                    recorder = getattr(
                        self.inventory_executor,
                        "record_authenticated_flat",
                        None,
                    )
                    if callable(recorder):
                        recorder()
                    snapshotter = getattr(
                        self.inventory_executor,
                        "execution_snapshot",
                        None,
                    )
                    if callable(snapshotter):
                        execution = snapshotter()
                        if isinstance(execution, dict):
                            self.metrics.inventory_unwind = dict(execution)

    async def _audit_account_current_state(self) -> None:
        managed_ids = self._managed_order_id_snapshot()
        active_ids = frozenset(
            getattr(self.order_manager, "active_unwind_order_ids", ())
        )
        raw_terminal_ids = getattr(
            self.order_manager, "terminal_order_ids", ()
        )
        terminal_ids = (
            frozenset(raw_terminal_ids)
            if isinstance(raw_terminal_ids, (set, frozenset, list, tuple))
            else frozenset()
        )
        audit_options: dict[str, Any] = {}
        if active_ids:
            audit_options["active_unwind_order_ids"] = active_ids
        if terminal_ids:
            audit_options["terminal_order_ids"] = terminal_ids
        await self.account_monitor.audit(managed_ids, **audit_options)
        self._audited_fill_generation = self._processed_fill_generation

    async def _refresh_active_unwind_truth(self) -> bool:
        """Refresh all truth used by the next active-unwind decision."""
        self._active_unwind_truth_token = None
        try:
            await self._refresh_market_once()
            positions = await self.adapter.get_positions([self.config.symbol])
            position = self._position_from_rest(positions)
            if position is None:
                raise RuntimeError("position REST response is unavailable")
            self._position = position
            self._position_refresh_required = False
            if self.account_monitor is None:
                raise AccountAuditError(
                    "active unwind requires an authenticated account monitor"
                )
            await asyncio.wait_for(
                self._audit_account_current_state(),
                timeout=self.config.account_audit_timeout_seconds,
            )
            self._update_account_audit_metrics()
        except Exception as exc:
            reason = f"active unwind truth refresh failed: {type(exc).__name__}: {exc}"
            marker = getattr(self.account_monitor, "mark_hard_stop", None)
            if callable(marker):
                marker(reason)
            await self._fail_closed(RuntimeState.PAUSED_ERROR, reason)
            return False
        self.metrics.increment("position_refresh_after_order_update")
        return True

    def _arm_active_unwind_truth(self, episode_id: int | None) -> bool:
        now = self._monotonic()
        manager = self.order_manager
        monitor = self.account_monitor
        market = self._market
        position = self._position
        snapshot = self._trusted_inventory_unwind_snapshot(now)
        prepared_generation = getattr(
            manager, "active_unwind_prepared_generation", None
        )
        mutation_generation = getattr(manager, "mutation_generation", None)
        audit_monotonic = getattr(monitor, "last_audit_monotonic", None)
        if (
            type(episode_id) is not int
            or episode_id <= 0
            or market is None
            or position is None
            or snapshot is None
            or type(prepared_generation) is not int
            or type(mutation_generation) is not int
            or isinstance(audit_monotonic, bool)
            or not isinstance(audit_monotonic, (int, float))
            or not math.isfinite(audit_monotonic)
            or snapshot.get("audited_position") != position.signed_size
        ):
            return False
        self._active_unwind_truth_token = _ActiveUnwindTruthToken(
            episode_id=episode_id,
            signed_position=position.signed_size,
            position_received_monotonic=position.received_monotonic,
            audit_generation=self._audited_fill_generation,
            audit_monotonic=float(audit_monotonic),
            minimum_book_received_monotonic=market.received_monotonic,
            manager_mutation_generation=mutation_generation,
            manager_prepare_generation=prepared_generation,
        )
        return True

    def _consume_active_unwind_truth(
        self, episode_id: int | None, now: float
    ) -> int | None:
        token = self._active_unwind_truth_token
        self._active_unwind_truth_token = None
        if token is None:
            return None
        manager = self.order_manager
        monitor = self.account_monitor
        market = self._market
        position = self._position
        snapshot = self._trusted_inventory_unwind_snapshot(now)
        if (
            episode_id != token.episode_id
            or market is None
            or position is None
            or snapshot is None
            or position.signed_size != token.signed_position
            or position.received_monotonic
            != token.position_received_monotonic
            or self._audited_fill_generation != token.audit_generation
            or getattr(monitor, "last_audit_monotonic", None)
            != token.audit_monotonic
            or market.received_monotonic
            < token.minimum_book_received_monotonic
            or getattr(manager, "mutation_generation", None)
            != token.manager_mutation_generation
            or getattr(manager, "active_unwind_prepared_generation", None)
            != token.manager_prepare_generation
            or snapshot.get("audited_position") != token.signed_position
        ):
            return None
        return token.manager_prepare_generation

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
        if getattr(result, "fill_observed", False) is True:
            self._processed_fill_generation += 1
            now = self._monotonic()
            for observed_order in getattr(result, "observed_fill_orders", ()):
                self._record_maker_fill_markout(
                    observed_order,
                    now,
                    source="reconciliation",
                )
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
            self._sync_open_maker_fill_progress(
                self.order_manager.snapshot()
            )
            if position_refresh_required is True:
                self._processed_fill_generation += 1
                self._position = None
                sync_result = getattr(
                    self.order_manager, "last_sync_result", None
                )
                for observed_order in getattr(
                    sync_result, "observed_fill_orders", ()
                ):
                    self._record_maker_fill_markout(
                        observed_order,
                        self._monotonic(),
                        source="rest_open_order_sync",
                    )
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
        now = self._monotonic()
        was_quoting = self._state in _QUOTING_STATES
        will_quote = state in _QUOTING_STATES
        if was_quoting and not will_quote:
            if self._eligible_quote_started is not None:
                self._eligible_quote_seconds += max(
                    0.0, now - self._eligible_quote_started
                )
            self._eligible_quote_started = None
        elif will_quote and not was_quoting:
            self._eligible_quote_started = now
        self._state = state
        self.metrics.transition(state, reason)

    def _eligible_seconds(self, now: float) -> float:
        current = self._eligible_quote_seconds
        if self._eligible_quote_started is not None:
            current += max(0.0, now - self._eligible_quote_started)
        return current

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
            elif operation == "active_unwind":
                self.metrics.increment("active_unwind_attempts")
                reason = str(getattr(action, "reason", "")).lower()
                if getattr(action, "success", None) is True:
                    self.metrics.increment("active_unwind_success")
                    if "partial fill" in reason:
                        self.metrics.increment("active_unwind_partial_fill")
                    elif "filled" not in reason:
                        self.metrics.increment("active_unwind_no_fill")
                elif getattr(action, "success", None) is None:
                    self.metrics.increment("active_unwind_ambiguous")
            elif operation == "would_active_unwind":
                self.metrics.increment("would_active_unwind")

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
        manager = self.order_manager
        self.metrics.counters["unresolved_cancellations"] = int(
            getattr(manager, "unresolved_cancellation_count", 0)
            if manager is not None
            else 0
        )
        self.metrics.counters["resolved_ambiguous_cancellations"] = int(
            getattr(manager, "resolved_ambiguous_cancellations", 0)
            if manager is not None
            else 0
        )

    def _managed_order_id_snapshot(self) -> set[str]:
        return {
            str(order_id)
            for order_id in getattr(self.order_manager, "known_order_ids", ())
            if str(order_id)
        }

    def _sync_open_maker_fill_progress(self, orders: Iterable[Any]) -> None:
        active_ids = set(
            getattr(self.order_manager, "active_unwind_order_ids", ())
        )
        open_maker_ids = {
            order_id
            for order in orders
            if (order_id := str(getattr(order, "order_id", "") or ""))
            and order_id not in active_ids
        }
        self.metrics.sync_open_maker_order_ids(open_maker_ids)

    def _record_maker_fill_markout(
        self, order: OrderData, now: float, *, source: str
    ) -> None:
        order_id = str(order.id).strip() if order.id is not None else ""
        if not order_id or self._market is None or order_id in set(
            getattr(self.order_manager, "active_unwind_order_ids", ())
        ):
            return
        mid = (self._market.best_bid + self._market.best_ask) / Decimal("2")
        try:
            recorded = self.metrics.record_maker_fill_markout(
                order_id=order_id,
                side=order.side.value,
                cumulative_filled=order.filled,
                cumulative_cost=order.cost,
                average_price=order.average or order.price,
                now=now,
                mid=mid,
                external_mid=self._external_markout_mid(),
                source=source,
                terminal=order.status
                in {
                    OrderStatus.FILLED,
                    OrderStatus.CANCELED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                },
            )
        except Exception:
            self.metrics.increment("markout_telemetry_errors")
            return
        if recorded:
            try:
                self.metrics.record_controller_fill_snapshot(order_id, now)
            except Exception:
                self.metrics.increment("markout_telemetry_errors")

    def _external_markout_mid(self) -> Decimal | None:
        manager = self.order_manager
        if self._market is None or manager is None:
            return None
        try:
            for state_check in (
                "has_uncertain_state",
                "has_unknown_order_state",
            ):
                state = getattr(manager, state_check, None)
                state = state() if callable(state) else state
                if state is not False:
                    return None
            snapshot = getattr(manager, "snapshot", None)
            if not callable(snapshot):
                return None
            orders = tuple(snapshot())
            if any(
                order.state
                in {
                    OrderSlotState.SUBMITTING,
                    OrderSlotState.UNCERTAIN_SUBMISSION,
                    OrderSlotState.UNCERTAIN_CANCELLATION,
                }
                or order.submission_uncertain
                or order.cancellation_uncertain
                for order in orders
            ):
                return None
            view = build_external_book_view(
                self._market,
                orders,
                self.config.toxicity_book_depth_levels,
            )
        except Exception:
            return None
        if (
            not view.valid
            or view.external_best_bid is None
            or view.external_best_ask is None
        ):
            return None
        return (view.external_best_bid + view.external_best_ask) / Decimal("2")

    def _authenticated_flat_checkpoint(self) -> bool:
        return bool(
            self._position is not None
            and self._position.signed_size == 0
            and self._account_monitor_initialized
            and self._audited_fill_generation == self._processed_fill_generation
            and self.metrics.account_audit.get("ledger_position") == Decimal("0")
        )

    def _trusted_inventory_unwind_snapshot(
        self, now: float
    ) -> dict[str, Any] | None:
        if (
            not self._account_monitor_initialized
            or self._audited_fill_generation != self._processed_fill_generation
            or self.account_monitor is None
            or self._position is None
        ):
            return None
        try:
            snapshot = self.account_monitor.snapshot(now)
            if not isinstance(snapshot, dict) or snapshot.get("state") != "healthy":
                return None
            age = snapshot.get("age_seconds")
            if (
                isinstance(age, bool)
                or not isinstance(age, (int, float, Decimal))
                or not math.isfinite(age)
                or age < 0
                or age
                > self.config.account_audit_interval_seconds
                + self.config.account_audit_timeout_seconds
            ):
                return None
            ledger_position = snapshot.get("ledger_position")
            if (
                not isinstance(ledger_position, Decimal)
                or not ledger_position.is_finite()
                or ledger_position != self._position.signed_size
            ):
                return None
            return snapshot
        except Exception:
            return None

    def _entry_admission_allows_new_episode(self, now: float) -> bool:
        if (
            not self.config.active_unwind_enabled
            or self._position is None
            or self._position.signed_size != 0
        ):
            self._entry_admission_blocked = False
            return True

        reserve = self._reserved_worst_case_exit_cost()
        if self.config.dry_run:
            self._entry_admission_blocked = False
            self._entry_admission_metrics = {
                "entry_admission": "allowed",
                "entry_admission_reason": "dry run has no exchange mutations",
                "reserved_worst_case_exit_cost": reserve,
                "remaining_drawdown_for_entry": None,
            }
            self.metrics.account_audit.update(self._entry_admission_metrics)
            return True

        snapshot = self._trusted_inventory_unwind_snapshot(now)
        remaining = (
            snapshot.get("remaining_session_loss_for_unwind")
            if snapshot is not None
            else None
        )
        current_drawdown = (
            snapshot.get("current_drawdown") if snapshot is not None else None
        )
        remaining_drawdown = (
            max(Decimal("0"), self.config.max_session_drawdown - current_drawdown)
            if isinstance(current_drawdown, Decimal)
            and current_drawdown.is_finite()
            else None
        )
        budgets_trusted = bool(
            isinstance(remaining, Decimal)
            and remaining.is_finite()
            and isinstance(remaining_drawdown, Decimal)
        )
        allowed = bool(
            budgets_trusted
            and remaining >= reserve
            and remaining_drawdown > reserve
        )
        if allowed:
            reason = None
        elif not budgets_trusted:
            reason = "entry admission requires fresh authenticated economics"
        elif remaining < reserve:
            reason = "remaining session unwind budget cannot fund a full-lot stop"
        else:
            reason = "remaining drawdown cannot fund a full-lot stop"
        self._entry_admission_metrics = {
            "entry_admission": "allowed" if allowed else "blocked",
            "entry_admission_reason": reason,
            "reserved_worst_case_exit_cost": reserve,
            "remaining_drawdown_for_entry": remaining_drawdown,
        }
        self.metrics.account_audit.update(self._entry_admission_metrics)
        cap_blocked = budgets_trusted and not allowed
        if cap_blocked and not self._entry_admission_blocked:
            self.metrics.increment("episode_cap_blocked")
        self._entry_admission_blocked = cap_blocked
        return allowed

    def _reserved_worst_case_exit_cost(self) -> Decimal:
        return self.config.max_episode_loss_for_unwind

    def _normal_passive_unwind_price(self, desired: Any) -> Decimal | None:
        if (
            self._position is None
            or self._position.signed_size == 0
            or self._market is None
        ):
            return None
        if self._position.signed_size > 0:
            return max(
                ceil_to_step(
                    desired.reservation_price + desired.half_spread,
                    self.metadata.price_tick,
                ),
                self._market.best_bid + self.metadata.price_tick,
            )
        return min(
            floor_to_step(
                desired.reservation_price - desired.half_spread,
                self.metadata.price_tick,
            ),
            self._market.best_ask - self.metadata.price_tick,
        )

    def _ensure_market_feature_store(self) -> Any | None:
        if self.config.quote_controller_mode == "fixed":
            return None
        if self.market_feature_store is not None:
            return self.market_feature_store
        if self.metadata is None:
            return None
        self.market_feature_store = MarketFeatureStore(
            price_tick=self.metadata.price_tick,
            depth_levels=self.config.toxicity_book_depth_levels,
            feature_window_seconds=(
                self.config.toxicity_feature_window_seconds
            ),
            reset_gap_seconds=self.config.toxicity_feature_reset_gap_seconds,
            warmup_seconds=self.config.toxicity_warmup_seconds,
            min_samples=self.config.toxicity_min_samples,
            stale_after_seconds=self.config.stale_book_seconds,
            clock=self._monotonic,
        )
        return self.market_feature_store

    @staticmethod
    def _feature_snapshot_mapping(features: Any | None) -> dict[str, Any]:
        if features is None:
            return {}
        serializer = getattr(features, "to_dict", None)
        if not callable(serializer):
            return {}
        values = serializer()
        return dict(values) if isinstance(values, dict) else {}

    def _controller_failure_decision(
        self, features: Any | None, reason: str, decision_id: int
    ) -> QuoteControllerDecision:
        return QuoteControllerDecision(
            mode=self.config.quote_controller_mode,
            controller=self.config.quote_controller_type,
            ready=False,
            reason=reason,
            decision_id=decision_id,
            bid=SideQuoteAdjustment(reason=reason),
            ask=SideQuoteAdjustment(reason=reason),
            features=features,
        )

    def _apply_entry_quote_controller(
        self,
        *,
        base: Any,
        risk: Any,
        orders: Iterable[Any],
        now: float,
        economic_stop_pending: bool,
        entry_admission_allowed: bool,
    ) -> tuple[Any, dict[str, Any]]:
        assert self._market is not None
        assert self._position is not None
        assert self.metadata is not None

        mode = self.config.quote_controller_mode
        self._controller_telemetry_decision_id += 1
        decision_id = self._controller_telemetry_decision_id
        features = None
        feature_snapshot: dict[str, Any] = {}
        controller_error: str | None = None
        if mode != "fixed":
            try:
                store = self._ensure_market_feature_store()
                if store is None:
                    controller_error = "feature_store_unavailable"
                else:
                    features = store.update(
                        self._market,
                        orders,
                        now_monotonic=now,
                    )
                    feature_snapshot = self._feature_snapshot_mapping(features)
                    if not feature_snapshot:
                        controller_error = "feature_snapshot_unavailable"
            except Exception as exc:
                features = None
                feature_snapshot = {}
                controller_error = (
                    f"feature_store_exception:{type(exc).__name__}"
                )

        entry_markout_feedback: dict[str, Any] = {}
        if (
            controller_error is None
            and mode == "shadow"
            and self.config.toxicity_use_markout_feedback
        ):
            try:
                entry_markout_feedback = (
                    self.metrics.authenticated_entry_markout_feedback(
                        now_monotonic=now,
                        half_life_seconds=(
                            self.config.toxicity_markout_half_life_seconds
                        ),
                    )
                )
            except Exception as exc:
                controller_error = (
                    f"markout_feedback_exception:{type(exc).__name__}"
                )

        context = QuoteControllerContext(
            now_monotonic=now,
            features=features,
            market=self._market,
            metadata=self.metadata,
            position=self._position,
            risk=risk,
            live_orders=tuple(orders),
            base_quotes=base,
            entry_markout_feedback=entry_markout_feedback,
            economic_stop_pending=economic_stop_pending,
            entry_admission_allowed=entry_admission_allowed,
            inventory_unwind_active=(
                self.config.active_unwind_enabled
                and self._position.signed_size != 0
            ),
            active_unwind_pending=bool(
                getattr(self.order_manager, "active_unwind_pending", False)
            ),
            active_unwind_ready=self._active_unwind_truth_token is not None,
        )
        if controller_error is not None:
            decision = self._controller_failure_decision(
                features, controller_error, decision_id
            )
        else:
            try:
                decision = self.quote_controller.evaluate(context)
                if isinstance(decision, QuoteControllerDecision):
                    decision = replace(decision, decision_id=decision_id)
            except Exception as exc:
                controller_error = f"controller_exception:{type(exc).__name__}"
                decision = self._controller_failure_decision(
                    features, "controller_exception", decision_id
                )
        validation_error = controller_decision_error(
            decision, expected_mode=mode
        )
        if validation_error is not None:
            controller_error = controller_error or validation_error
            decision = self._controller_failure_decision(
                features, validation_error, decision_id
            )
        if getattr(decision, "reason", None) in {
            "features_invalid",
            "invalid_time",
        }:
            controller_error = controller_error or str(decision.reason)

        arbiter_context = QuoteArbiterContext(
            ordinary_flat_entry=(
                self._position.signed_size == 0
                and risk.runtime_state is RuntimeState.ACTIVE
                and risk.buy_reduce_only is False
                and risk.sell_reduce_only is False
            ),
            economic_stop_pending=economic_stop_pending,
            entry_admission_allowed=entry_admission_allowed,
            risk_reduction_active=(
                risk.runtime_state is not RuntimeState.ACTIVE
                or getattr(risk, "soft_exit_latched", False) is True
            ),
            inventory_unwind_active=context.inventory_unwind_active,
            active_unwind_pending=context.active_unwind_pending,
            active_unwind_ready=context.active_unwind_ready,
        )
        entry_applicable = (
            arbiter_context.ordinary_flat_entry
            and not arbiter_context.economic_stop_pending
            and arbiter_context.entry_admission_allowed
            and not arbiter_context.risk_reduction_active
            and not arbiter_context.inventory_unwind_active
            and not arbiter_context.active_unwind_pending
            and not arbiter_context.active_unwind_ready
        )
        if mode == "fixed":
            shadow = base
            applied = base
        elif mode == "shadow":
            shadow = apply_entry_controller(
                base,
                replace(decision, mode="active"),
                self._position,
                risk,
                self.metadata,
                context=arbiter_context,
            )
            applied = base
        else:
            applied = apply_entry_controller(
                base,
                decision,
                self._position,
                risk,
                self.metadata,
                context=arbiter_context,
            )
            shadow = applied

        self.metrics.record_controller_decision(
            decision,
            now=now,
            base_bid=base.bid.price if base.bid is not None else None,
            base_ask=base.ask.price if base.ask is not None else None,
            shadow_bid=shadow.bid.price if shadow.bid is not None else None,
            shadow_ask=shadow.ask.price if shadow.ask is not None else None,
            applied_bid=applied.bid.price if applied.bid is not None else None,
            applied_ask=applied.ask.price if applied.ask is not None else None,
            feature_snapshot=feature_snapshot,
            entry_applicable=entry_applicable,
            error=controller_error,
        )
        return applied, {
            "base": base,
            "shadow": shadow,
            "applied": applied,
            "decision": decision,
            "features": feature_snapshot,
        }

    def _record_quote_contexts(
        self,
        actions: Iterable[Any],
        controller_cycle: dict[str, Any],
        now: float,
    ) -> None:
        base = controller_cycle["base"]
        shadow = controller_cycle["shadow"]
        applied = controller_cycle["applied"]
        decision = controller_cycle["decision"]
        for action in actions:
            order_id = str(getattr(action, "order_id", "") or "").strip()
            if (
                getattr(action, "operation", None) != "place"
                or getattr(action, "success", None) is not True
                or getattr(action, "reduce_only", False) is not False
                or not order_id
                or getattr(action, "side", None)
                not in {OrderSide.BUY, OrderSide.SELL}
            ):
                continue
            side = action.side
            side_name = "bid" if side is OrderSide.BUY else "ask"
            adjustment = getattr(decision, side_name)
            base_order = getattr(base, side_name)
            shadow_order = getattr(shadow, side_name)
            applied_order = getattr(applied, side_name)
            self.metrics.record_quote_context(
                order_id,
                {
                    "order_id": order_id,
                    "side": side.value,
                    "decision_id": getattr(decision, "decision_id", None),
                    "controller_mode": self.config.quote_controller_mode,
                    "base_price": (
                        base_order.price if base_order is not None else None
                    ),
                    "shadow_price": (
                        shadow_order.price if shadow_order is not None else None
                    ),
                    "applied_price": (
                        applied_order.price if applied_order is not None else None
                    ),
                    "toxicity_score_ticks": getattr(
                        adjustment, "toxicity_score_ticks", None
                    ),
                    "extra_spread_ticks": getattr(
                        adjustment, "extra_spread_ticks", None
                    ),
                    "feature_snapshot": dict(controller_cycle["features"]),
                    "reduce_only": False,
                    "created_monotonic": now,
                },
            )

    def _update_account_audit_metrics(self) -> None:
        if self.account_monitor is None:
            return
        now = self._monotonic()
        snapshot = self.account_monitor.snapshot(now)
        eligible_seconds = self._eligible_seconds(now)
        turnover = snapshot.get("maker_turnover")
        fills = snapshot.get("unique_maker_fills")
        if eligible_seconds > 0:
            eligible_hours = Decimal(str(eligible_seconds)) / Decimal("3600")
            snapshot["maker_turnover_per_eligible_hour"] = (
                turnover / eligible_hours
                if isinstance(turnover, Decimal)
                else None
            )
            snapshot["maker_fills_per_eligible_hour"] = (
                Decimal(fills) / eligible_hours if type(fills) is int else None
            )
        else:
            snapshot["maker_turnover_per_eligible_hour"] = None
            snapshot["maker_fills_per_eligible_hour"] = None
        snapshot.update(self._entry_admission_metrics)
        self.metrics.apply_authenticated_fill_attributions(
            snapshot.get("authenticated_fill_attributions", ())
        )
        self.metrics.account_audit = snapshot

    def _trusted_soft_exit_economics(
        self, now: float
    ) -> SoftExitEconomics | None:
        if (
            self.config.dry_run
            or self.config.soft_exit_after_seconds <= 0
            or not self._account_monitor_initialized
            or self._audited_fill_generation != self._processed_fill_generation
            or self.account_monitor is None
            or self._position is None
        ):
            return None

        try:
            position = self._position.signed_size
        except Exception:
            return None
        if (
            not isinstance(position, Decimal)
            or not position.is_finite()
            or position == 0
        ):
            return None

        try:
            snapshot = self.account_monitor.snapshot(now)
            if not isinstance(snapshot, dict) or snapshot.get("state") != "healthy":
                return None

            age = snapshot.get("age_seconds")
            if (
                isinstance(age, bool)
                or not isinstance(age, (int, float, Decimal))
                or not math.isfinite(age)
                or age < 0
                or age
                > self.config.account_audit_interval_seconds
                + self.config.account_audit_timeout_seconds
            ):
                return None

            ledger_position = snapshot.get("ledger_position")
            completed_turnover = snapshot.get("completed_turnover")
            completed_net = snapshot.get("completed_net_ex_funding")
            open_turnover = snapshot.get("open_episode_turnover")
            open_net = snapshot.get("open_episode_net_ex_funding")
            values = (
                ledger_position,
                completed_turnover,
                completed_net,
                open_turnover,
                open_net,
            )
            if any(
                not isinstance(value, Decimal) or not value.is_finite()
                for value in values
            ):
                return None
            if (
                ledger_position != position
                or completed_turnover < 0
                or (
                    completed_turnover == 0
                    and self.config.max_session_loss_for_maker_exit <= 0
                )
                or open_turnover <= 0
            ):
                return None

            if self.config.max_session_loss_for_maker_exit > 0:
                last_flat_equity_change = snapshot.get(
                    "last_flat_equity_change"
                )
                completed_fills = snapshot.get("completed_fills")
                last_flat_completed_fills = snapshot.get(
                    "last_flat_completed_fills"
                )
                if (
                    not isinstance(last_flat_equity_change, Decimal)
                    or not last_flat_equity_change.is_finite()
                    or type(completed_fills) is not int
                    or type(last_flat_completed_fills) is not int
                    or completed_fills < 0
                    or last_flat_completed_fills != completed_fills
                ):
                    return None
                return SoftExitEconomics(
                    completed_turnover=completed_turnover,
                    completed_net=min(
                        completed_net, last_flat_equity_change
                    ),
                    open_turnover=open_turnover,
                    open_net=open_net,
                )

            minimum_rate = (
                self.config.min_completed_net_turnover_bps / Decimal("10000")
            )
            surplus = completed_net - minimum_rate * completed_turnover
            reserve = min(
                surplus,
                self.config.soft_exit_surplus_reserve_bps
                / Decimal("10000")
                * completed_turnover,
            )
            if surplus <= 0 or surplus - reserve <= 0:
                return None

            return SoftExitEconomics(
                completed_turnover=completed_turnover,
                completed_net=completed_net,
                open_turnover=open_turnover,
                open_net=open_net,
            )
        except Exception:
            return None

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
        loops = [
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
        ]
        if (
            self.account_monitor is not None
            and self.config.account_audit_interval_seconds
        ):
            loops.append(
                (
                    "market-maker-account-audit",
                    self._periodic_loop(
                        self.config.account_audit_interval_seconds,
                        self.audit_account_once,
                    ),
                )
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
        error = await self._cleanup_orders_and_final_audit()
        if self._subscribed:
            try:
                await asyncio.wait_for(self.adapter.unsubscribe(), timeout=5.0)
            except BaseException as exc:
                error = self._combine_cleanup_errors(error, exc)
            self._subscribed = False
        try:
            await asyncio.wait_for(self.adapter.disconnect(), timeout=5.0)
        except BaseException as exc:
            error = self._combine_cleanup_errors(error, exc)
        finally:
            self._authenticated = False
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
            exchange_timestamp = book.exchange_timestamp
        elif isinstance(book, OrderBookData):
            exchange_timestamp = book.exchange_timestamp or book.timestamp
        else:
            raise ValueError("order book update is invalid")
        if book.symbol != self.config.symbol:
            raise ValueError("order book symbol does not match config")
        bids = tuple(level for level in book.bids if _valid_book_level(level))
        asks = tuple(level for level in book.asks if _valid_book_level(level))
        if not bids or not asks:
            raise ValueError("order book is empty or has no valid positive levels")
        best_bid = max(level.price for level in bids)
        best_ask = min(level.price for level in asks)
        if best_bid >= best_ask:
            raise ValueError("order book is crossed")
        return MarketSnapshot(
            symbol=book.symbol,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            exchange_timestamp=exchange_timestamp,
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
