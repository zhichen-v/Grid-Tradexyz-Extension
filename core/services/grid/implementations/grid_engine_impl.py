"""Grid execution engine with clean websocket and REST fallback handling."""

from __future__ import annotations

import asyncio
import time
import traceback
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ....adapters.exchanges import (
    ExchangeInterface,
    OrderData as ExchangeOrderData,
    OrderSide as ExchangeOrderSide,
    OrderType,
)
from ....logging import get_logger
from ..interfaces.grid_engine import IGridEngine
from ..models import GridConfig, GridOrder, GridOrderSide, GridOrderStatus


class GridEngineImpl(IGridEngine):
    """Place, track, and reconcile grid orders against an exchange adapter."""

    def __init__(self, exchange_adapter: ExchangeInterface):
        self.logger = get_logger(__name__)
        self.exchange = exchange_adapter
        self.config: Optional[GridConfig] = None
        self.coordinator = None

        self._order_callbacks: List[Callable] = []
        self._pending_orders: Dict[str, GridOrder] = {}
        self._expected_cancellations: set[str] = set()
        self._uncertain_cancel_order_ids: set[str] = set()

        self._current_price: Optional[Decimal] = None
        self._last_ticker_price: Optional[Decimal] = None
        self._last_price_update_time: float = 0.0
        self._price_ws_enabled = False

        self._expected_total_orders: int = 0
        self._health_checker = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._last_health_check_time: float = 0.0
        self._last_health_repair_count: int = 0
        self._last_health_repair_time: float = 0.0

        self._running = False
        self._shutting_down = False
        self._shutdown_fill_incident: Optional[str] = None
        self._placements_paused = False
        self._placement_lock = asyncio.Lock()
        self._placement_epoch = 0
        self._inflight_placements = 0
        self._placements_drained = asyncio.Event()
        self._placements_drained.set()
        self._polling_task: Optional[asyncio.Task] = None
        self._ws_monitoring_enabled = False
        self._last_ws_check_time: float = 0.0
        self._ws_check_interval: int = 30
        self._last_ws_message_time: float = 0.0
        self._ws_timeout_threshold: int = 600

        self._last_position_warning_time: float = 0.0
        self._position_warning_interval: float = 60.0
        self._exchange_sync_grace_period: float = 5.0
        self._missing_order_resolution_timeout: float = 8.0
        self._health_repairs_suspended_reason: Optional[str] = None
        self._recently_finalized_order_ids: Dict[str, float] = {}
        self._finalized_order_cache_seconds: float = 300.0
        self._restore_tasks: Dict[str, asyncio.Task] = {}
        self._restore_state: Dict[str, Dict[str, float]] = {}
        self._restore_max_attempts: int = 3
        self._restore_base_delay: float = 2.0
        self._restore_attempt_window: float = 60.0
        self._restore_circuit_seconds: float = 300.0
        self._reserved_market_open_amount = Decimal("0")
        self._reserved_market_position: Optional[Decimal] = None
        self._uncertain_market_submissions: Dict[str, Dict[str, Any]] = {}
        self._resolved_submission_client_ids: set[str] = set()
        self._unmanaged_partial_carries: Dict[str, Dict[str, Any]] = {}

        exchange_id = getattr(exchange_adapter.config, "exchange_id", "unknown")
        self.logger.info(f"Grid execution engine initialized for {exchange_id}")

    async def initialize(self, config: GridConfig):
        """Initialize the engine and subscribe to exchange streams."""
        self.config = config
        self._expected_total_orders = config.grid_count
        self._last_ws_message_time = time.time()
        self._last_ws_check_time = 0.0

        if not self.exchange.is_connected():
            connected = await self.exchange.connect()
            if not connected or not self.exchange.is_connected():
                raise ConnectionError(
                    f"Failed to connect to exchange: {config.exchange}"
                )
            self.logger.info(f"Connected to exchange: {config.exchange}")

        try:
            await self.exchange.subscribe_user_data(self._on_order_update)
            self._ws_monitoring_enabled = True
            self._last_ws_message_time = time.time()
            self.logger.info("Order update monitor subscribed with WebSocket")
            self.logger.info("Using WebSocket mode for real-time order monitoring")
        except Exception as exc:
            self._ws_monitoring_enabled = False
            self.logger.warning(
                f"WebSocket order subscription failed, starting in REST polling mode: {exc}"
            )

        self._start_smart_monitor()
        await self._start_price_monitor()

        from .order_health_checker import OrderHealthChecker

        reserve_manager = None
        if self.coordinator and hasattr(self.coordinator, "reserve_manager"):
            reserve_manager = self.coordinator.reserve_manager

        self._health_checker = OrderHealthChecker(config, self, reserve_manager)
        self.logger.info(
            f"Grid execution engine ready for {config.exchange}/{config.symbol}"
        )

    async def place_order(
        self,
        order: GridOrder,
        batch_mode: bool = False,
        allow_while_paused: bool = False,
        defer_uncertain: bool = False,
    ) -> Optional[GridOrder]:
        """Serialize the exposure check through pending-order registration."""
        if self._requires_exposure_lock(order.side):
            async with self._get_placement_lock():
                return await self._place_order_unlocked(
                    order,
                    batch_mode=batch_mode,
                    allow_while_paused=allow_while_paused,
                    defer_uncertain=defer_uncertain,
                )
        return await self._place_order_unlocked(
            order,
            batch_mode=batch_mode,
            allow_while_paused=allow_while_paused,
            defer_uncertain=defer_uncertain,
        )

    async def _place_order_unlocked(
        self,
        order: GridOrder,
        batch_mode: bool = False,
        allow_while_paused: bool = False,
        defer_uncertain: bool = False,
    ) -> Optional[GridOrder]:
        """Place a single limit order and track it locally."""
        if (
            self._shutting_down
            or not self._running
            or self._placement_is_paused(allow_while_paused)
        ):
            self.logger.warning(
                f"Skip order placement while engine is stopping: "
                f"{order.side.value} {order.amount}@{order.price}"
            )
            return None

        position_gate = getattr(
            getattr(self, "coordinator", None),
            "can_place_order_within_max_position",
            None,
        )
        if position_gate and not position_gate(
            order.side,
            order.amount,
            additional_open_amount=getattr(
                self,
                "_reserved_market_open_amount",
                Decimal("0"),
            ),
        ):
            self.logger.warning(
                "Skip order placement because max_position would be exceeded: "
                f"{order.side.value} {order.amount}@{order.price}"
            )
            return None

        self._begin_inflight_placement()
        try:
            exchange_side = self._convert_order_side(order.side)
            exchange_name = str(getattr(self.config, "exchange", "")).lower()
            is_lighter = exchange_name == "lighter"
            is_reverse_order = bool(order.parent_order_id)
            is_closing_order = is_lighter and self._is_reverse_side(order.side)
            order_params = None
            if is_lighter:
                order_params = {
                    "time_in_force": (
                        "GTT"
                        if is_reverse_order or is_closing_order
                        else "POST_ONLY"
                    )
                }
                if order_params["time_in_force"] == "GTT":
                    order_params["skip_order_index_query"] = True
                if is_closing_order:
                    order_params["reduce_only"] = True
            create_kwargs = {
                "symbol": self.config.symbol,
                "side": exchange_side,
                "order_type": OrderType.LIMIT,
                "amount": order.amount,
                "price": order.price,
                "params": order_params,
            }
            if batch_mode and self._supports_batch_mode():
                create_kwargs["batch_mode"] = True

            exchange_order = await self.exchange.create_order(**create_kwargs)
            if exchange_order is None:
                raise RuntimeError("Exchange rejected the order without an error response")
            order_id = self._string_or_none(
                getattr(exchange_order, "id", None)
                or getattr(exchange_order, "order_id", None)
            )
            client_id = self._string_or_none(getattr(exchange_order, "client_id", None))

            if not order_id or order_id == "pending":
                temp_id = self._build_temp_order_id(order)
                order_id = temp_id
                self.logger.warning(
                    f"Exchange returned no final order id, using temporary id: {temp_id}"
                )

            order.order_id = order_id
            order.status = GridOrderStatus.PENDING
            order.exchange_data = self._merge_order_exchange_data(
                order,
                getattr(exchange_order, "raw_data", {}) or {},
            )
            self._register_pending_order(order, order_id, client_id)

            exchange_status = self._exchange_order_status(exchange_order)
            if exchange_status in {"canceled", "cancelled", "rejected", "expired"}:
                self._clear_pending_order_refs(order_id, client_id)
                reason = (
                    "Exchange returned a terminal placement failure: "
                    f"grid_id={order.grid_id}, order_id={order_id}, status={exchange_status}"
                )
                self._fail_closed_submission(reason, order)
                raise RuntimeError(reason)

            if self._is_submission_uncertain(order):
                if self._is_submission_acknowledged(order):
                    order.exchange_data["submission_acknowledged_at"] = time.time()
                    if defer_uncertain:
                        order.exchange_data["health_repair_deferred"] = True
                elif defer_uncertain:
                    order.exchange_data["health_repair_deferred"] = True
                    self.logger.warning(
                        "Health repair submission remains uncertain; retaining it "
                        "for the next authoritative snapshot: "
                        f"grid_id={order.grid_id}, side={order.side.value}, "
                        f"price={order.price}, client_id={order.order_id}"
                    )
                elif not await self._resolve_uncertain_grid_submission_with_grace(
                    order
                ):
                    self._quarantine_uncertain_grid_submission(order)
            elif exchange_status == "filled":
                self._schedule_deferred_fill_finalization(
                    order,
                    exchange_order,
                    order_id,
                    client_id,
                )

            self.logger.info(
                f"Order placed: {order.side.value} {order.amount}@{order.price} "
                f"(Grid {order.grid_id}, OrderID: {order.order_id})"
            )
            return order
        except Exception as exc:
            self.logger.error(f"Order placement failed: {exc}")
            order.mark_failed()
            raise
        finally:
            self._end_inflight_placement()

    async def place_market_order(
        self,
        side: GridOrderSide,
        amount: Decimal,
        reduce_only: bool = False,
        reference_price: Optional[Decimal] = None,
        allow_while_paused: bool = False,
    ) -> None:
        """Serialize market orders with limit-order exposure checks."""
        if not reduce_only and self._requires_exposure_lock(side):
            async with self._get_placement_lock():
                await self._place_market_order_unlocked(
                    side,
                    amount,
                    reduce_only=reduce_only,
                    reference_price=reference_price,
                    allow_while_paused=allow_while_paused,
                )
                return
        await self._place_market_order_unlocked(
            side,
            amount,
            reduce_only=reduce_only,
            reference_price=reference_price,
            allow_while_paused=allow_while_paused,
        )

    async def _place_market_order_unlocked(
        self,
        side: GridOrderSide,
        amount: Decimal,
        reduce_only: bool = False,
        reference_price: Optional[Decimal] = None,
        allow_while_paused: bool = False,
    ) -> None:
        """Place a market order, used mainly for position adjustment flows."""
        if (
            self._shutting_down
            or not self._running
            or self._placement_is_paused(allow_while_paused)
        ):
            raise RuntimeError("Cannot place a market order while engine is stopping")

        position_gate = getattr(
            getattr(self, "coordinator", None),
            "can_place_order_within_max_position",
            None,
        )
        if (
            not reduce_only
            and position_gate
            and not position_gate(
                side,
                amount,
                additional_open_amount=getattr(
                    self,
                    "_reserved_market_open_amount",
                    Decimal("0"),
                ),
            )
        ):
            raise RuntimeError("Market order would exceed max_position")

        self._begin_inflight_placement()
        try:
            exchange_side = self._convert_order_side(side)
            exchange_order = await self.exchange.create_order(
                symbol=self.config.symbol,
                side=exchange_side,
                order_type=OrderType.MARKET,
                amount=amount,
                price=reference_price,
                params={"reduce_only": True} if reduce_only else None,
            )
            if exchange_order is None:
                raise RuntimeError("Exchange returned no market order")
            if not reduce_only and self._is_opening_side(side):
                self._reserve_market_open_amount(amount)
            order_id = getattr(exchange_order, "id", None) or getattr(
                exchange_order, "order_id", None
            )
            if self._exchange_submission_is_uncertain(exchange_order):
                self._record_uncertain_market_submission(
                    exchange_order,
                    side,
                    amount,
                    reduce_only,
                )
                reason = (
                    "Market order submission outcome is uncertain; position/client-id "
                    f"verification is required: side={side.value}, amount={amount}, "
                    f"reduce_only={reduce_only}, client_id={order_id}"
                )
                self._fail_closed_submission(reason)
                raise RuntimeError(reason)
            self.logger.info(
                f"Market order placed: {side.value} {amount}, OrderID: {order_id}"
            )
        except Exception as exc:
            self.logger.error(f"Market order placement failed: {exc}")
            raise
        finally:
            self._end_inflight_placement()

    async def place_batch_orders(
        self,
        orders: List[GridOrder],
        max_retries: int = 2,
        allow_while_paused: bool = False,
        defer_uncertain: bool = False,
    ) -> List[GridOrder]:
        """Place orders in batches, with retries for failed items."""
        if not orders:
            return []
        if self._placements_paused and not allow_while_paused:
            self.logger.warning("Skip batch placement while reset gate is active")
            return []

        total_orders = len(orders)
        batch_size = 50
        successful_orders: List[GridOrder] = []
        failed_orders: List[Tuple[GridOrder, str]] = []

        self.logger.info(f"Starting batch order placement: total={total_orders}")

        for start in range(0, total_orders, batch_size):
            batch = orders[start:start + batch_size]
            results = await self._execute_batch(
                batch,
                allow_while_paused,
                defer_uncertain,
            )

            for order, result in zip(batch, results):
                if isinstance(result, GridOrder):
                    successful_orders.append(result)
                elif result is None:
                    failed_orders.append((order, "order placement returned no result"))
                else:
                    failed_orders.append((order, str(result)))

            if self._placements_paused and not allow_while_paused:
                self.logger.warning(
                    "Abort remaining batch placements because reset started"
                )
                return successful_orders

            if start + batch_size < total_orders:
                await asyncio.sleep(0.5)

        for attempt in range(1, max_retries + 1):
            if not failed_orders:
                break
            if self._placements_paused and not allow_while_paused:
                self.logger.warning(
                    "Abort batch retries because reset gate is active"
                )
                return successful_orders

            self.logger.warning(
                f"Retrying failed orders: attempt={attempt}, count={len(failed_orders)}"
            )
            await asyncio.sleep(1.0)

            retry_orders = [order for order, _ in failed_orders]
            failed_orders = []
            results = await self._execute_batch(
                retry_orders,
                allow_while_paused,
                defer_uncertain,
            )

            for order, result in zip(retry_orders, results):
                if isinstance(result, GridOrder):
                    successful_orders.append(result)
                elif result is None:
                    failed_orders.append((order, "order placement returned no result"))
                else:
                    failed_orders.append((order, str(result)))

        success_rate = (len(successful_orders) / total_orders) * 100
        if failed_orders:
            self.logger.warning(
                f"Batch placement finished with partial success: "
                f"success={len(successful_orders)}/{total_orders} ({success_rate:.1f}%), "
                f"failed={len(failed_orders)}"
            )
            for order, error in failed_orders:
                self.logger.error(
                    f"Final order failure: Grid {order.grid_id}, "
                    f"{order.side.value} {order.amount}@{order.price}, error={error}"
                )
        else:
            self.logger.info(
                f"Batch placement complete: "
                f"success={len(successful_orders)}/{total_orders} ({success_rate:.1f}%)"
            )

        await self._sync_order_status_after_batch()
        return successful_orders

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel one order and mark it as expected in the local cache."""
        expected_keys: set[str] = set()
        try:
            cache_key, grid_order = self._find_cached_order(order_id)
            if grid_order:
                expected_keys.update(self._pending_keys_for_order(grid_order))
            else:
                expected_keys.add(order_id)
            self._expected_cancellations.update(expected_keys)

            cancel_result = await self.exchange.cancel_order(order_id, self.config.symbol)
            if not cancel_result:
                if self._cancel_outcome_is_uncertain(order_id):
                    self._uncertain_cancel_order_ids.update(expected_keys)
                    self.logger.critical(
                        "Order cancellation outcome remains uncertain; retaining local "
                        f"state until an exact terminal update: order_id={order_id}"
                    )
                else:
                    self._expected_cancellations.difference_update(expected_keys)
                    self.logger.error(
                        f"Order cancellation was rejected: order_id={order_id}"
                    )
                return False

            if self._is_nonterminal_cancel_ack(cancel_result):
                self._uncertain_cancel_order_ids.update(expected_keys)
                self.logger.warning(
                    "Cancellation was acknowledged but is not terminal; retaining local "
                    f"state until exact history or WebSocket proof: order_id={order_id}"
                )
                return False

            if grid_order:
                aliases = self._pending_keys_for_order(grid_order)
                if self._claim_order_finalization(
                    *aliases,
                    cache_key,
                    order_id,
                    grid_order.order_id,
                ):
                    self._clear_restore_state(grid_order)
                    grid_order.mark_cancelled()
                    self._remove_order_from_coordinator_state(grid_order)
                self._clear_pending_order_refs(*aliases, cache_key, order_id)
                self._consume_expected_cancellation(*aliases, cache_key, order_id)
            else:
                self._note_finalized_order(order_id)
                self._expected_cancellations.discard(order_id)

            self.logger.info(f"Order cancelled successfully: {order_id}")
            return True
        except Exception as exc:
            if self._cancel_outcome_is_uncertain(order_id):
                self._uncertain_cancel_order_ids.update(expected_keys)
                self.logger.critical(
                    "Order cancellation response was lost; retaining local state until "
                    f"an exact terminal update: order_id={order_id}, error={exc}"
                )
                return False
            self._expected_cancellations.difference_update(expected_keys)
            self.logger.error(f"Order cancel failed for {order_id}: {exc}")
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders for the configured symbol."""
        if self.config is None:
            return 0

        # Shutdown first blocks new placements, then waits for any request that
        # already crossed that gate to finish before taking the cancel snapshot.
        await self._placements_drained.wait()

        initial_pending_count = len(self.get_pending_orders())
        expected_keys: set[str] = set()
        try:
            _, resolved_filled = await self._resolve_unresolved_submissions_read_only()
            if resolved_filled:
                self._shutdown_fill_incident = (
                    "Previously uncertain submissions resolved as filled: "
                    + ", ".join(resolved_filled)
                )

            pending_orders = self.get_pending_orders()
            expected_keys = set(self._pending_orders.keys())
            self._expected_cancellations.update(expected_keys)

            cancelled_orders = await self.exchange.cancel_all_orders(self.config.symbol)
            remaining_orders = await self.exchange.get_open_orders(self.config.symbol)
            if remaining_orders:
                raise RuntimeError(
                    f"{len(remaining_orders)} orders remain open after cancel-all"
                )
            resolved_active, resolved_filled = ([], [])
            if self._unresolved_submission_descriptions():
                resolved_active, resolved_filled = (
                    await self._resolve_unresolved_submissions_read_only()
                )
            if resolved_active:
                raise RuntimeError(
                    "Previously uncertain submissions resolved as active after the "
                    f"cancel snapshot: {', '.join(resolved_active)}"
                )
            if resolved_filled:
                self._shutdown_fill_incident = (
                    "Previously uncertain submissions resolved as filled: "
                    + ", ".join(resolved_filled)
                )
                raise RuntimeError(
                    self._shutdown_fill_incident
                )
            unresolved_cancels, filled_cancels = (
                await self._resolve_uncertain_cancellations_read_only()
            )
            if filled_cancels:
                self._shutdown_fill_incident = (
                    "Previously uncertain cancellations resolved as filled: "
                    + ", ".join(filled_cancels)
                )
                raise RuntimeError(
                    self._shutdown_fill_incident
                )
            if unresolved_cancels:
                raise RuntimeError(
                    "Cancel-all cannot be verified while cancellations remain uncertain: "
                    + ", ".join(unresolved_cancels)
                )
            unresolved_submissions = self._unresolved_submission_descriptions()
            if unresolved_submissions:
                raise RuntimeError(
                    "Cancel-all cannot be verified while submissions remain uncertain: "
                    + ", ".join(unresolved_submissions)
                )
            unresolved_pending, filled_pending = (
                await self._reconcile_shutdown_pending_orders(
                    pending_orders,
                    cancelled_orders,
                )
            )
            if filled_pending:
                self._shutdown_fill_incident = (
                    "Orders filled during shutdown instead of being cancelled: "
                    + ", ".join(filled_pending)
                )
                raise RuntimeError(
                    self._shutdown_fill_incident
                )
            if unresolved_pending:
                raise RuntimeError(
                    "Cancel-all lacks exact terminal proof for local orders: "
                    + ", ".join(unresolved_pending)
                )
            if getattr(self, "_shutdown_fill_incident", None):
                raise RuntimeError(self._shutdown_fill_incident)
        except Exception as exc:
            self._expected_cancellations.difference_update(expected_keys)
            self.logger.error(f"Cancel-all failed: {exc}")
            raise

        cancelled_count = (
            len(cancelled_orders) if cancelled_orders else initial_pending_count
        )
        self._pending_orders.clear()
        self._expected_cancellations.difference_update(expected_keys)
        self.logger.info(f"All open orders cancelled: count={cancelled_count}")
        return cancelled_count

    async def get_order_status(self, order_id: str) -> Optional[GridOrder]:
        """Fetch and apply the latest exchange status for a local order."""
        try:
            exchange_order = await self.exchange.get_order(order_id, self.config.symbol)
            cache_key, grid_order = self._find_cached_order(
                order_id,
                getattr(exchange_order, "client_id", None),
            )
            if not grid_order:
                return None

            status = (
                exchange_order.status.value.lower()
                if getattr(exchange_order, "status", None)
                else ""
            )
            if status == "filled":
                filled_price = exchange_order.average or exchange_order.price or grid_order.price
                filled_amount = exchange_order.filled or grid_order.amount
                grid_order.mark_filled(filled_price, filled_amount)
            elif status in {"canceled", "cancelled", "rejected", "expired"}:
                grid_order.mark_cancelled()
                self._clear_pending_order_refs(cache_key, order_id)

            return grid_order
        except Exception as exc:
            self.logger.error(f"Get-order-status failed for {order_id}: {exc}")
            return None

    async def get_current_price(self) -> Decimal:
        """Return the freshest available price, preferring websocket cache."""
        try:
            if self._current_price is not None:
                if time.time() - self._last_price_update_time < 5:
                    return self._current_price

            ticker = await self.exchange.get_ticker(self.config.symbol)
            price = self._extract_price_from_ticker(ticker)
            self._current_price = price
            self._last_ticker_price = price
            self._last_price_update_time = time.time()
            return price
        except Exception as exc:
            self.logger.error(f"Current price fetch failed: {exc}")
            if self._current_price is not None:
                return self._current_price
            raise

    def get_pending_orders(self) -> List[GridOrder]:
        """Return unique local pending orders, deduplicated by object identity."""
        seen_ids = set()
        unique_orders: List[GridOrder] = []

        for order in self._pending_orders.values():
            object_id = id(order)
            if object_id in seen_ids:
                continue
            seen_ids.add(object_id)
            unique_orders.append(order)

        return unique_orders

    def subscribe_order_updates(self, callback: Callable):
        """Register a callback for filled grid orders."""
        self._order_callbacks.append(callback)

    def get_monitoring_mode(self) -> str:
        """Return the active order-monitoring mode."""
        return "WebSocket" if self._ws_monitoring_enabled else "REST polling"

    async def get_real_time_position(self, symbol: str) -> Dict[str, Decimal]:
        """Return cached websocket position data when available."""
        try:
            position_cache = getattr(self.exchange, "_position_cache", None)
            if isinstance(position_cache, dict) and symbol in position_cache:
                cached_position = position_cache[symbol]
                return {
                    "size": self._safe_decimal(cached_position.get("size")),
                    "entry_price": self._safe_decimal(cached_position.get("entry_price")),
                    "unrealized_pnl": self._safe_decimal(
                        cached_position.get("unrealized_pnl")
                    ),
                    "has_cache": True,
                }

            current_time = time.time()
            if current_time - self._last_position_warning_time >= self._position_warning_interval:
                self.logger.debug(
                    f"No websocket position cache available for {symbol}, using empty position"
                )
                self._last_position_warning_time = current_time
        except Exception as exc:
            self.logger.error(f"Real-time position fetch failed: {exc}")

        return {
            "size": Decimal("0"),
            "entry_price": Decimal("0"),
            "unrealized_pnl": Decimal("0"),
            "has_cache": False,
        }

    async def start(self):
        """Start background monitoring tasks."""
        self._shutting_down = False
        self._shutdown_fill_incident = None
        self._placements_paused = False
        self._unmanaged_partial_carries = {}
        self._running = True
        self._start_smart_monitor()
        self._start_order_health_check()
        self.logger.info("Grid execution engine started")

    def begin_shutdown(self):
        """Enter shutdown mode and stop new recovery work."""
        if self._shutting_down:
            return

        self._shutting_down = True
        self.pause_placements()
        self._running = False
        self.logger.info("Grid execution engine entered shutdown mode")

    def pause_placements(self) -> None:
        """Block new placement requests while allowing in-flight ones to finish."""
        if not self._placements_paused:
            self._placement_epoch += 1
        self._placements_paused = True

    def resume_placements(self) -> None:
        """Re-open the reset placement gate for normal strategy work."""
        if not self._shutting_down:
            self._placements_paused = False

    def _placement_is_paused(self, allow_while_paused: bool) -> bool:
        """Return whether normal strategy placement is currently paused."""
        if allow_while_paused:
            return False
        if self._placements_paused:
            return True

        coordinator = getattr(self, "coordinator", None)
        if coordinator is None:
            return False
        if getattr(coordinator, "_paused", False):
            return True
        public_state = getattr(coordinator, "is_paused", False)
        if isinstance(public_state, bool):
            return public_state
        if callable(public_state):
            try:
                return bool(public_state())
            except Exception:
                return False
        return False

    def _get_placement_lock(self) -> asyncio.Lock:
        """Return the shared placement lock, including lightweight test engines."""
        lock = getattr(self, "_placement_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._placement_lock = lock
        return lock

    def _requires_exposure_lock(self, side: GridOrderSide) -> bool:
        """Serialize only capped opening exposure; preserve normal batch concurrency."""
        coordinator = getattr(self, "coordinator", None)
        config = getattr(coordinator, "config", None) or getattr(self, "config", None)
        return (
            getattr(config, "max_position", None) is not None
            and self._is_opening_side(side)
        )

    def _is_opening_side(self, side: GridOrderSide) -> bool:
        """Return whether this side increases configured strategy exposure."""
        base_side = self._base_side_for_grid_type()
        return base_side is not None and side == base_side

    def _reserve_market_open_amount(self, amount: Decimal) -> None:
        """Hold submitted market exposure until a later REST position snapshot confirms it."""
        reserved_amount = getattr(self, "_reserved_market_open_amount", Decimal("0"))
        if reserved_amount <= 0:
            tracker = getattr(getattr(self, "coordinator", None), "tracker", None)
            current = (
                Decimal(str(tracker.get_current_position()))
                if tracker and hasattr(tracker, "get_current_position")
                else Decimal("0")
            )
            sign = Decimal("1") if self._base_side_for_grid_type() == GridOrderSide.BUY else Decimal("-1")
            self._reserved_market_position = sign * current
        self._reserved_market_open_amount = reserved_amount + Decimal(str(amount))

    def reconcile_market_open_reservations(self, current_position: Decimal) -> None:
        """Release market exposure reservations reflected by a REST position snapshot."""
        current_position = Decimal(str(current_position))
        if (
            getattr(self, "_reserved_market_open_amount", Decimal("0")) > 0
            and getattr(self, "_reserved_market_position", None) is not None
        ):
            sign = Decimal("1") if self._base_side_for_grid_type() == GridOrderSide.BUY else Decimal("-1")
            directional_position = sign * current_position
            reflected = max(directional_position - self._reserved_market_position, Decimal("0"))
            self._reserved_market_open_amount = max(
                self._reserved_market_open_amount - reflected,
                Decimal("0"),
            )
            self._reserved_market_position = (
                directional_position if self._reserved_market_open_amount > 0 else None
            )
        self._reconcile_uncertain_market_positions(current_position)

    async def wait_for_inflight_placements(self) -> None:
        """Wait until every request that crossed the placement gate has returned."""
        await self._placements_drained.wait()

    @property
    def placement_epoch(self) -> int:
        """Generation used to invalidate repair work captured before a reset."""
        return self._placement_epoch

    def _begin_inflight_placement(self) -> None:
        self._inflight_placements += 1
        self._placements_drained.clear()

    def _end_inflight_placement(self) -> None:
        self._inflight_placements -= 1
        if self._inflight_placements == 0:
            self._placements_drained.set()

    async def stop(self):
        """Stop background monitoring tasks."""
        self._running = False

        tasks = [
            self._health_check_task,
            self._polling_task,
            *self._restore_tasks.values(),
        ]
        for task in tasks:
            if task and not task.done():
                task.cancel()

        for task in tasks:
            if task and not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._restore_tasks.clear()

        self.logger.info("Grid execution engine stopped")

    def is_running(self) -> bool:
        """Return whether the engine is actively running."""
        return self._running

    def suspend_health_repairs(self, reason: str) -> None:
        """Temporarily block health-check repair actions while a local batch is in flight."""
        normalized_reason = (reason or "unspecified").strip()
        if self._health_repairs_suspended_reason == normalized_reason:
            return

        self._health_repairs_suspended_reason = normalized_reason
        self.logger.info(
            f"Health-check repairs suspended: reason={normalized_reason}"
        )

    def resume_health_repairs(self, reason: Optional[str] = None) -> None:
        """Re-enable health-check repair actions after a protected batch finishes."""
        active_reason = self._health_repairs_suspended_reason
        if not active_reason:
            return

        self._health_repairs_suspended_reason = None
        requested_reason = (reason or "").strip()
        if requested_reason and requested_reason != active_reason:
            self.logger.info(
                "Health-check repairs resumed: "
                f"cleared_reason={active_reason}, requested_by={requested_reason}"
            )
            return

        self.logger.info(
            f"Health-check repairs resumed: reason={active_reason}"
        )

    def get_health_repair_suspend_reason(self) -> Optional[str]:
        """Return the current health-repair suspension reason when one is active."""
        return self._health_repairs_suspended_reason

    def __repr__(self) -> str:
        return f"GridEngine(exchange={self.exchange}, running={self._running})"

    async def _start_price_monitor(self):
        """Subscribe to ticker updates for fresher price reads."""
        try:
            await self.exchange.subscribe_ticker(self.config.symbol, self._on_price_update)
            self._price_ws_enabled = True
            self.logger.info("Price monitor subscribed with WebSocket")
        except Exception as exc:
            self._price_ws_enabled = False
            self.logger.warning(
                f"Price WebSocket subscription failed, falling back to REST price reads: {exc}"
            )

    def _on_price_update(self, ticker_data) -> None:
        """Update the cached market price from a websocket ticker event."""
        try:
            price = self._extract_price_from_ticker(ticker_data)
            self._current_price = price
            self._last_ticker_price = price
            self._last_price_update_time = time.time()
        except Exception as exc:
            self.logger.debug(f"Price update ignored: {exc}")

    def get_price_monitor_mode(self) -> str:
        """Return the active price-monitoring mode."""
        if self._price_ws_enabled and self._current_price is not None:
            if time.time() - self._last_price_update_time < 10:
                return "WebSocket"
        return "REST"

    def _start_smart_monitor(self):
        """Start websocket monitoring and REST fallback loop."""
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._smart_monitor_loop())
            mode = "WebSocket" if self._ws_monitoring_enabled else "REST polling"
            self.logger.info(f"Smart order monitor started in {mode} mode")

    def _get_websocket_client(self):
        """Return the underlying websocket client when exposed by the adapter."""
        return getattr(self.exchange, "_websocket", None) or getattr(
            self.exchange, "websocket", None
        )

    def _is_websocket_connected(self) -> bool:
        """Check the real websocket client state, not only adapter heartbeat state."""
        ws_client = self._get_websocket_client()
        if ws_client is not None:
            get_status = getattr(ws_client, "get_connection_status", None)
            if callable(get_status):
                try:
                    status = get_status()
                    if isinstance(status, dict) and "healthy" in status:
                        return bool(status["healthy"])
                except Exception:
                    pass

            is_connected = getattr(ws_client, "is_connected", None)
            if callable(is_connected):
                try:
                    return bool(is_connected())
                except Exception:
                    pass

            for attr in ("_ws_connected", "_xyz_ws_connected", "connected"):
                if hasattr(ws_client, attr):
                    return bool(getattr(ws_client, attr))

            connection = getattr(ws_client, "_ws_connection", None) or getattr(
                ws_client, "_xyz_ws", None
            )
            if connection is not None:
                return not bool(getattr(connection, "closed", False))

        is_connected = getattr(self.exchange, "is_connected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:
                return False
        return False

    def _get_websocket_heartbeat_timestamp(self) -> float:
        """Read the freshest heartbeat timestamp available from the adapter stack."""
        ws_client = self._get_websocket_client()
        candidates = []

        if ws_client is not None:
            candidates.append(getattr(ws_client, "_last_heartbeat", None))
            get_status = getattr(ws_client, "get_connection_status", None)
            if callable(get_status):
                try:
                    status = get_status() or {}
                    candidates.append(status.get("last_heartbeat"))
                except Exception:
                    pass

        candidates.append(getattr(self.exchange, "_last_heartbeat", None))

        timestamps = [self._to_timestamp(value) for value in candidates]
        timestamps = [value for value in timestamps if value > 0]
        return max(timestamps) if timestamps else 0.0

    async def _smart_monitor_loop(self):
        """Monitor websocket health and fall back to REST only when needed."""
        self.logger.info("Smart order monitor loop started")

        while True:
            try:
                if self._ws_monitoring_enabled:
                    await asyncio.sleep(30)

                    current_time = time.time()
                    time_since_last_message = current_time - self._last_ws_message_time
                    if not self._is_websocket_connected():
                        self.logger.error(
                            "WebSocket connection lost, switching to REST polling mode"
                        )
                        self.logger.info(
                            "Last websocket message time: "
                            f"{self._format_timestamp(self._last_ws_message_time)}"
                        )
                        self.logger.info(
                            f"Current pending order count: {len(self.get_pending_orders())}"
                        )
                        self._ws_monitoring_enabled = False
                        self._last_ws_check_time = current_time
                        continue

                    exchange_name = str(self.config.exchange).lower()
                    if exchange_name == "lighter":
                        self.logger.info(
                            "WebSocket health status: connected, "
                            f"last_message_age={time_since_last_message:.0f}s"
                        )
                        continue

                    last_heartbeat = self._get_websocket_heartbeat_timestamp()
                    heartbeat_age = (
                        current_time - last_heartbeat if last_heartbeat > 0 else 0.0
                    )
                    if (
                        last_heartbeat > 0
                        and heartbeat_age > self._ws_timeout_threshold
                        and time_since_last_message > self._ws_timeout_threshold
                    ):
                        self.logger.error(
                            "WebSocket heartbeat timed out, switching to REST polling mode: "
                            f"heartbeat_age={heartbeat_age:.0f}s"
                        )
                        self.logger.info(
                            f"Last heartbeat time: {self._format_timestamp(last_heartbeat)}"
                        )
                        self.logger.info(
                            "Last websocket message time: "
                            f"{self._format_timestamp(self._last_ws_message_time)}"
                        )
                        self.logger.info(
                            f"Current pending order count: {len(self.get_pending_orders())}"
                        )
                        self._ws_monitoring_enabled = False
                        self._last_ws_check_time = current_time
                        continue

                    self.logger.info(
                        "WebSocket health status: connected, "
                        f"heartbeat_age={heartbeat_age:.0f}s, "
                        f"last_message_age={time_since_last_message:.0f}s"
                    )
                    continue

                await asyncio.sleep(3)
                if self._pending_orders:
                    await self._check_pending_orders()

                current_time = time.time()
                if current_time - self._last_ws_check_time >= self._ws_check_interval:
                    self._last_ws_check_time = current_time
                    await self._try_restore_websocket()
            except asyncio.CancelledError:
                self.logger.info("Smart order monitor loop stopped")
                break
            except Exception as exc:
                self.logger.error(f"Smart order monitor loop failed: {exc}")
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(5)

    async def _try_restore_websocket(self):
        """Attempt to restore websocket monitoring after REST fallback."""
        if self._ws_monitoring_enabled:
            return

        try:
            self.logger.info("Attempting to restore websocket monitoring")
            await self.exchange.subscribe_user_data(self._on_order_update)
            self._ws_monitoring_enabled = True
            self._last_ws_message_time = time.time()
            self.logger.info("WebSocket monitoring restored successfully")
            self.logger.info("Using WebSocket mode for real-time order monitoring")
        except Exception as exc:
            self.logger.warning(
                f"WebSocket restore failed, staying on REST polling: {type(exc).__name__}: {exc}"
            )

    def _start_order_health_check(self):
        """Start periodic order health checks."""
        if self._health_check_task is None or self._health_check_task.done():
            self._last_health_check_time = time.time()
            self._health_check_task = asyncio.create_task(self._order_health_check_loop())
            self.logger.info(
                f"Order health check started: interval={self.config.order_health_check_interval}s"
            )

    async def _order_health_check_loop(self):
        """Run health checks on the configured interval."""
        self.logger.info("Order health-check loop started")
        interval = max(1, int(self.config.order_health_check_interval))
        await asyncio.sleep(interval)

        while self._running:
            try:
                current_time = time.time()
                elapsed = current_time - self._last_health_check_time

                if elapsed >= interval:
                    self.logger.info(
                        f"Triggering health check: since_last={elapsed:.0f}s, interval={interval}s"
                    )
                    if self._health_checker:
                        success = await self._health_checker.perform_health_check()
                        if success:
                            self.logger.info("Health check complete")
                        else:
                            self.logger.warning(
                                "Health check finished with repair errors"
                            )
                    else:
                        self.logger.error("Health checker is not initialized")

                    self._last_health_check_time = time.time()

                sleep_for = max(1.0, interval - (time.time() - self._last_health_check_time))
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                self.logger.info("Order health-check loop stopped")
                break
            except Exception as exc:
                self.logger.error(f"Order health-check loop failed: {exc}")
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(interval)

    def _notify_health_check_complete(self, filled_count: int):
        """Log the post-health-check order summary."""
        try:
            pending_orders = self.get_pending_orders()
            buy_count = sum(
                1 for order in pending_orders if order.side == GridOrderSide.BUY
            )
            sell_count = sum(
                1 for order in pending_orders if order.side == GridOrderSide.SELL
            )
            self.logger.info(
                "Health check order summary: "
                f"total={len(pending_orders)}, buy={buy_count}, sell={sell_count}"
            )

            if filled_count > 0:
                self._last_health_repair_count = filled_count
                self._last_health_repair_time = time.time()
                self.logger.info(
                    f"Health check repaired missing orders: count={filled_count}"
                )
        except Exception as exc:
            self.logger.error(f"Failed to finalize health check summary: {exc}")

    async def _sync_orders_from_exchange(self, exchange_orders: Sequence[ExchangeOrderData]):
        """Reconcile local pending orders against the exchange open-order snapshot."""
        try:
            if self._shutting_down:
                return

            exchange_keys = set()
            exchange_orders_by_key: Dict[str, ExchangeOrderData] = {}
            for exchange_order in exchange_orders:
                order_id = self._string_or_none(getattr(exchange_order, "id", None))
                client_id = self._string_or_none(
                    getattr(exchange_order, "client_id", None)
                )
                if order_id:
                    exchange_keys.add(order_id)
                    exchange_orders_by_key[order_id] = exchange_order
                if client_id:
                    exchange_keys.add(client_id)
                    exchange_orders_by_key[client_id] = exchange_order

            history_by_key: Dict[str, ExchangeOrderData] = {}
            history_is_complete = True
            needs_history = bool(
                getattr(self, "_uncertain_market_submissions", {})
            )
            seen_pending_objects = set()
            for pending_order in self._pending_orders.values():
                object_id = id(pending_order)
                if object_id in seen_pending_objects:
                    continue
                seen_pending_objects.add(object_id)
                aliases = self._pending_keys_for_order(pending_order)
                if any(alias in exchange_keys for alias in aliases):
                    continue
                if any(
                    alias in self._expected_cancellations for alias in aliases
                ) and not self._is_uncertain_cancel(*aliases):
                    continue
                order_age = (
                    max(0.0, time.time() - pending_order.created_at.timestamp())
                    if pending_order.created_at
                    else self._exchange_sync_grace_period
                )
                if order_age >= self._exchange_sync_grace_period:
                    needs_history = True
                    break

            if needs_history:
                get_order_history = getattr(self.exchange, "get_order_history", None)
                if callable(get_order_history):
                    history_orders = await get_order_history(
                        self.config.symbol,
                        limit=100,
                    )
                    history_orders = list(history_orders or [])
                    history_is_complete = len(history_orders) < 100
                    for history_order in history_orders:
                        for alias in (
                            getattr(history_order, "id", None),
                            getattr(history_order, "client_id", None),
                        ):
                            normalized = self._string_or_none(alias)
                            if normalized:
                                history_by_key[normalized] = history_order
                else:
                    history_is_complete = False

            self._reconcile_uncertain_market_submissions(
                exchange_orders_by_key,
                history_by_key,
            )

            removed_count = 0
            added_count = 0
            filled_in_sync: List[GridOrder] = []
            processed_objects = set()

            for cache_key, grid_order in list(self._pending_orders.items()):
                object_id = id(grid_order)
                if object_id in processed_objects:
                    continue
                processed_objects.add(object_id)

                aliases = self._pending_keys_for_order(grid_order)
                matched_order = next(
                    (
                        exchange_orders_by_key[alias]
                        for alias in aliases
                        if alias in exchange_orders_by_key
                    ),
                    None,
                )
                if matched_order is not None:
                    if self._is_submission_uncertain(grid_order):
                        self._adopt_reconciled_grid_submission(
                            grid_order,
                            matched_order,
                        )
                    self._record_exchange_order_progress(
                        grid_order,
                        matched_order,
                    )
                    continue

                if self._is_submission_uncertain(grid_order):
                    if grid_order.exchange_data.get("health_repair_deferred"):
                        continue
                    acknowledged_at = grid_order.exchange_data.get(
                        "submission_acknowledged_at"
                    )
                    if (
                        self._is_submission_acknowledged(grid_order)
                        and isinstance(acknowledged_at, (int, float))
                        and time.time() - acknowledged_at
                        < self._missing_order_resolution_timeout
                    ):
                        continue
                    self._quarantine_uncertain_grid_submission(grid_order)

                if any(alias in self._expected_cancellations for alias in aliases):
                    if not self._is_uncertain_cancel(*aliases):
                        if not self._claim_order_finalization(
                            *aliases,
                            grid_order.order_id,
                        ):
                            self._clear_pending_order_refs(*aliases)
                            self._consume_expected_cancellation(*aliases)
                            removed_count += 1
                            continue
                        self._clear_restore_state(grid_order)
                        grid_order.mark_cancelled()
                        self._clear_pending_order_refs(*aliases)
                        self._consume_expected_cancellation(*aliases)
                        removed_count += 1
                        continue

                order_age = (
                    max(0.0, time.time() - grid_order.created_at.timestamp())
                    if grid_order.created_at
                    else self._exchange_sync_grace_period
                )
                if order_age < self._exchange_sync_grace_period:
                    continue

                exchange_order = next(
                    (
                        history_by_key[alias]
                        for alias in aliases
                        if alias in history_by_key
                    ),
                    None,
                )
                if exchange_order is None:
                    if not history_is_complete:
                        self.logger.debug(
                            "Order absent from a limited history snapshot; deferring: "
                            f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
                        )
                        continue
                    if self._is_submission_uncertain(grid_order):
                        self.logger.critical(
                            "Uncertain submission remains absent from read snapshots; "
                            "retaining the original client id without restore: "
                            f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
                        )
                    continue

                status = (
                    exchange_order.status.value.lower()
                    if getattr(exchange_order, "status", None)
                    else "unknown"
                )
                if status in {"open", "pending", "new"} and self._is_submission_uncertain(
                    grid_order
                ):
                    self._adopt_reconciled_grid_submission(
                        grid_order,
                        exchange_order,
                    )
                self._record_exchange_order_progress(grid_order, exchange_order)
                if self._is_user_fill_snapshot(exchange_order):
                    handled, finalized = await self._handle_tradexyz_user_fill_snapshot(
                        grid_order,
                        exchange_order,
                        "REST polling",
                        *aliases,
                    )
                    if handled:
                        if finalized:
                            removed_count += 1
                        continue

                if status == "filled":
                    self._consume_expected_cancellation(*aliases)
                    filled_price = exchange_order.average or exchange_order.price or grid_order.price
                    filled_amount = self._get_finalized_fill_amount(
                        grid_order,
                        exchange_order.filled or grid_order.amount,
                    )
                    if not self._claim_order_finalization(
                        *aliases,
                        getattr(exchange_order, "id", None),
                        getattr(exchange_order, "client_id", None),
                        grid_order.order_id,
                    ):
                        self._clear_pending_order_refs(*aliases)
                        continue
                    self._clear_uncertain_cancellation_markers(
                        {
                            normalized
                            for key in (
                                *aliases,
                                getattr(exchange_order, "id", None),
                                getattr(exchange_order, "client_id", None),
                                grid_order.order_id,
                            )
                            if (normalized := self._string_or_none(key))
                        }
                    )
                    self._clear_restore_state(grid_order)
                    grid_order.mark_filled(filled_price, filled_amount)
                    self._clear_pending_order_refs(*aliases)
                    filled_in_sync.append(grid_order)
                    removed_count += 1
                elif status in {"canceled", "cancelled", "rejected", "expired"}:
                    await self._finalize_cancellation(
                        grid_order,
                        "REST terminal cancellation received",
                        *aliases,
                        getattr(exchange_order, "id", None),
                        getattr(exchange_order, "client_id", None),
                        grid_order.order_id,
                    )
                    removed_count += 1

            for grid_order in filled_in_sync:
                await self._run_order_callbacks(grid_order)

            local_keys = set(self._pending_orders.keys())
            for exchange_order in exchange_orders:
                order_id = self._string_or_none(getattr(exchange_order, "id", None))
                client_id = self._string_or_none(
                    getattr(exchange_order, "client_id", None)
                )
                if (order_id and order_id in local_keys) or (client_id and client_id in local_keys):
                    continue

                price = getattr(exchange_order, "price", None)
                amount = getattr(exchange_order, "amount", None)
                side = getattr(exchange_order, "side", None)
                if not order_id or price is None or amount is None or side is None:
                    continue
                if self._was_recently_finalized_order(order_id) or (
                    client_id and self._was_recently_finalized_order(client_id)
                ):
                    continue

                try:
                    grid_order = self._build_grid_order_from_exchange_order(exchange_order)
                    if grid_order is None:
                        continue
                    self._register_pending_order(grid_order, order_id, client_id)
                    added_count += 1
                except Exception as exc:
                    self.logger.warning(
                        f"Failed to import exchange order into local cache: {exc}"
                    )

            total_local = len(self.get_pending_orders())
            total_exchange = len(exchange_orders)
            self.logger.info(
                "Order sync complete: "
                f"exchange={total_exchange}, local={total_local}, "
                f"added={added_count}, removed={removed_count}"
            )
        except Exception as exc:
            self.logger.error(f"Order sync failed: {exc}")
            self.logger.error(traceback.format_exc())

    async def _check_pending_orders(self):
        """Use REST polling to keep order state fresh when websocket monitoring is off."""
        try:
            open_orders = await self.exchange.get_open_orders(self.config.symbol)
            await self._sync_orders_from_exchange(open_orders)
        except Exception as exc:
            self.logger.error(f"REST polling order check failed: {exc}")

    async def _on_order_update(self, update_data: Any):
        """Process websocket order updates from several adapter payload styles."""
        try:
            if self._shutting_down or not self._running:
                return

            self._last_ws_message_time = time.time()

            if isinstance(update_data, ExchangeOrderData):
                await self._handle_exchange_order_object(update_data)
                return

            if isinstance(update_data, list):
                handled = False
                for item in update_data:
                    if isinstance(item, dict):
                        handled = await self._handle_generic_order_dict(item) or handled
                if handled:
                    return

            if not isinstance(update_data, dict):
                self.logger.warning(
                    f"Unsupported websocket order update payload: {type(update_data)}"
                )
                return

            update_type = update_data.get("type")
            if update_type in {"user_fill", "order_update"}:
                payload = update_data.get("data", update_data)
                items = self._extract_nested_update_items(payload)
                handled = False
                for item in items:
                    handled = await self._handle_vendor_update_dict(
                        item,
                        source="TradeXYZ WebSocket",
                        fill_on_user_fill=(update_type == "user_fill"),
                    ) or handled
                if handled:
                    return

            data = update_data.get("data", update_data)
            if isinstance(data, dict):
                await self._handle_binance_style_update(data)
        except Exception as exc:
            self.logger.error(f"Failed to process websocket order update: {exc}")
            self.logger.error(traceback.format_exc())

    async def _handle_exchange_order_object(self, update_data: ExchangeOrderData):
        """Handle websocket callbacks that already provide a typed OrderData object."""
        order_id = self._string_or_none(update_data.id)
        client_id = self._string_or_none(update_data.client_id)
        status = update_data.status.value.upper() if update_data.status else ""
        cache_key, grid_order = self._find_cached_order(client_id, order_id)

        if not grid_order:
            return

        if self._is_submission_uncertain(grid_order):
            self._adopt_reconciled_grid_submission(grid_order, update_data)

        self._record_exchange_order_progress(grid_order, update_data)

        if status in {"FILLED", "CLOSED"}:
            filled_price = update_data.average or update_data.price or grid_order.price
            filled_amount = self._get_finalized_fill_amount(
                grid_order,
                update_data.filled or grid_order.amount,
            )
            await self._finalize_fill(
                grid_order,
                filled_price,
                filled_amount,
                "WebSocket fill received",
                cache_key,
                client_id,
                order_id,
            )
            return

        if status in {"CANCELLED", "CANCELED"}:
            await self._finalize_cancellation(
                grid_order,
                "WebSocket cancellation received",
                cache_key,
                client_id,
                order_id,
            )

    async def _handle_generic_order_dict(self, item: Dict[str, Any]) -> bool:
        """Handle plain dict order updates with generic id/status fields."""
        order_id = self._string_or_none(item.get("id") or item.get("oid"))
        status = str(item.get("status") or item.get("state") or "").lower()
        cache_key, grid_order = self._find_cached_order(order_id)
        if not grid_order:
            return False

        if status in {"filled", "closed"}:
            filled_price = self._safe_decimal(item.get("price") or item.get("px"), grid_order.price)
            filled_amount = self._safe_decimal(
                item.get("filled") or item.get("sz"),
                grid_order.amount,
            )
            await self._finalize_fill(
                grid_order,
                filled_price,
                filled_amount,
                "WebSocket fill received",
                cache_key,
                order_id,
            )
            return True

        if status in {"canceled", "cancelled", "rejected", "expired"}:
            await self._finalize_cancellation(
                grid_order,
                "WebSocket cancellation received",
                cache_key,
                order_id,
            )
            return True

        return False

    async def _handle_vendor_update_dict(
        self,
        item: Dict[str, Any],
        source: str,
        fill_on_user_fill: bool,
    ) -> bool:
        """Handle TradeXYZ-style vendor order and fill payloads."""
        order_id = self._string_or_none(
            item.get("oid")
            or item.get("orderId")
            or item.get("order_id")
            or item.get("id")
        )
        cache_key, grid_order = self._find_cached_order(order_id)
        if not grid_order:
            return False

        status = str(item.get("status") or item.get("state") or item.get("X") or "").lower()
        if fill_on_user_fill:
            return await self._handle_tradexyz_user_fill(
                grid_order,
                item,
                source,
                cache_key,
                order_id,
            )

        if status in {"filled", "closed"}:
            filled_price = self._safe_decimal(
                item.get("px")
                or item.get("avgPx")
                or item.get("price")
                or item.get("p"),
                grid_order.price,
            )
            filled_amount = self._safe_decimal(
                item.get("sz")
                or item.get("filledSz")
                or item.get("filled")
                or item.get("z"),
                grid_order.amount,
            )
            await self._finalize_fill(
                grid_order,
                filled_price,
                filled_amount,
                f"{source} fill received",
                cache_key,
                order_id,
            )
            return True

        if status in {"canceled", "cancelled", "rejected", "expired"}:
            await self._finalize_cancellation(
                grid_order,
                f"{source} cancellation received",
                cache_key,
                order_id,
            )
            return True

        return False

    async def _handle_tradexyz_user_fill(
        self,
        grid_order: GridOrder,
        item: Dict[str, Any],
        source: str,
        *keys: Optional[str],
    ) -> bool:
        """Accumulate TradeXYZ user fills and finalize only once the full grid order is filled."""
        filled_price = self._safe_decimal(
            item.get("px")
            or item.get("avgPx")
            or item.get("price")
            or item.get("p"),
            grid_order.price,
        )
        fill_amount = self._safe_decimal(
            item.get("sz")
            or item.get("filledSz")
            or item.get("filled")
            or item.get("z"),
            Decimal("0"),
        )
        if fill_amount <= 0:
            return False

        tracking = self._get_tradexyz_fill_tracking(grid_order)
        event_key = self._build_tradexyz_fill_event_key(
            item,
            grid_order,
            filled_price,
            fill_amount,
        )
        self.logger.info(
            f"{source} user fill candidate: "
            f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}, "
            f"price={filled_price}, amount={fill_amount}, event_key={event_key}, "
            f"payload={self._summarize_tradexyz_fill_item(item)}"
        )
        if not self._record_tradexyz_fill_entry(
            tracking,
            event_key,
            fill_amount,
            filled_price,
        ):
            self.logger.info(
                f"Skipping duplicate TradeXYZ user fill: "
                f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}, "
                f"event={event_key}, cumulative={tracking.get('cumulative_filled')}, "
                f"payload={self._summarize_tradexyz_fill_item(item)}"
            )
            return True

        cumulative_filled = self._safe_decimal(
            tracking.get("cumulative_filled"),
            Decimal("0"),
        )
        order_amount = self._ensure_tradexyz_target_amount(grid_order)
        tolerance = self._get_order_fill_tolerance()

        if order_amount > 0 and cumulative_filled > order_amount:
            overflow = cumulative_filled - order_amount
            if overflow > tolerance:
                self.logger.warning(
                    f"TradeXYZ user fill overflow detected: "
                    f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}, "
                    f"cumulative={cumulative_filled}, order_amount={order_amount}"
                )
            cumulative_filled = order_amount

        tracking["cumulative_filled"] = str(cumulative_filled)
        tracking["remaining_amount"] = str(max(order_amount - cumulative_filled, Decimal("0")))
        tracking["last_fill_price"] = str(filled_price)
        self.logger.info(
            f"{source} user fill ledger updated: "
            f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}, "
            f"incremental={fill_amount}, cumulative={cumulative_filled}, "
            f"remaining={tracking['remaining_amount']}, event_key={event_key}"
        )

        if order_amount > 0 and (order_amount - cumulative_filled) > tolerance:
            self.logger.info(
                f"{source} partial fill recorded: "
                f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
                f"incremental={fill_amount}, cumulative={cumulative_filled}, "
                f"remaining={order_amount - cumulative_filled}, order_id={grid_order.order_id}"
            )
            return True

        final_amount = order_amount if order_amount > 0 else cumulative_filled
        await self._finalize_fill(
            grid_order,
            filled_price,
            final_amount,
            f"{source} final fill received",
            *keys,
        )
        return True

    async def _handle_tradexyz_user_fill_snapshot(
        self,
        grid_order: GridOrder,
        exchange_order: ExchangeOrderData,
        source: str,
        *keys: Optional[str],
    ) -> Tuple[bool, bool]:
        """Apply a TradeXYZ REST userFills snapshot without finalizing partial fills."""
        filled_price = exchange_order.average or exchange_order.price or grid_order.price
        snapshot_fill = self._safe_decimal(
            getattr(exchange_order, "filled", None),
            Decimal("0"),
        )
        raw_data = getattr(exchange_order, "raw_data", {}) or {}
        fills = []
        if isinstance(raw_data, dict) and isinstance(raw_data.get("fills"), list):
            fills = [fill for fill in raw_data["fills"] if isinstance(fill, dict)]

        if snapshot_fill <= 0 and not fills:
            return False, False

        tracking = self._get_tradexyz_fill_tracking(grid_order)
        target_amount = self._ensure_tradexyz_target_amount(grid_order)
        recorded_count = 0

        for fill in fills:
            fill_price = self._safe_decimal(
                fill.get("px") or fill.get("avgPx") or fill.get("price"),
                filled_price,
            )
            fill_amount = self._safe_decimal(
                fill.get("sz") or fill.get("filledSz") or fill.get("filled"),
                Decimal("0"),
            )
            if fill_amount <= 0:
                continue
            event_key = self._build_tradexyz_fill_event_key(
                fill,
                grid_order,
                fill_price,
                fill_amount,
            )
            if self._record_tradexyz_fill_entry(
                tracking,
                event_key,
                fill_amount,
                fill_price,
            ):
                recorded_count += 1

        cumulative_filled = self._safe_decimal(
            tracking.get("cumulative_filled"),
            Decimal("0"),
        )
        carry_amount = self._safe_decimal(
            tracking.get("carry_filled_amount"),
            Decimal("0"),
        )
        if target_amount > 0:
            snapshot_fill = min(snapshot_fill, max(target_amount - carry_amount, Decimal("0")))
        snapshot_cumulative = carry_amount + snapshot_fill
        if snapshot_cumulative > cumulative_filled:
            tracking["snapshot_cumulative_filled"] = str(snapshot_fill)
            tracking["cumulative_filled"] = str(snapshot_cumulative)
            tracking["last_fill_price"] = str(filled_price)
            cumulative_filled = snapshot_cumulative

        tolerance = self._get_order_fill_tolerance()
        if target_amount > 0 and cumulative_filled > target_amount:
            overflow = cumulative_filled - target_amount
            if overflow > tolerance:
                self.logger.warning(
                    f"TradeXYZ REST fill snapshot overflow detected: "
                    f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}, "
                    f"cumulative={cumulative_filled}, target_amount={target_amount}"
                )
            cumulative_filled = target_amount
            tracking["cumulative_filled"] = str(cumulative_filled)

        remaining = max(target_amount - cumulative_filled, Decimal("0"))
        tracking["remaining_amount"] = str(remaining)
        self.logger.info(
            f"{source} TradeXYZ fill snapshot applied: "
            f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
            f"cumulative={cumulative_filled}, remaining={remaining}, "
            f"target_amount={target_amount}, recorded_fills={recorded_count}, "
            f"order_id={grid_order.order_id}"
        )

        if target_amount > 0 and remaining > tolerance:
            return True, False

        final_amount = target_amount if target_amount > 0 else cumulative_filled
        await self._finalize_fill(
            grid_order,
            filled_price,
            final_amount,
            f"{source} TradeXYZ final fill snapshot",
            *keys,
        )
        return True, True

    async def _handle_binance_style_update(self, data: Dict[str, Any]):
        """Handle updates that use Binance-style fields such as X, i, p, and z."""
        order_id = self._string_or_none(data.get("i") or data.get("id"))
        status = str(data.get("X") or "").upper()
        event_type = str(data.get("e") or "").lower()
        cache_key, grid_order = self._find_cached_order(order_id)

        if not grid_order:
            return

        if status == "FILLED" or event_type == "orderfilled":
            filled_price = self._safe_decimal(data.get("p"), grid_order.price)
            filled_amount = self._safe_decimal(data.get("z"), grid_order.amount)
            await self._finalize_fill(
                grid_order,
                filled_price,
                filled_amount,
                "WebSocket fill received",
                cache_key,
                order_id,
            )
            return

        if status == "CANCELLED" or event_type == "ordercancelled":
            await self._finalize_cancellation(
                grid_order,
                "WebSocket cancellation received",
                cache_key,
                order_id,
            )

    async def _finalize_fill(
        self,
        grid_order: GridOrder,
        filled_price: Decimal,
        filled_amount: Decimal,
        log_prefix: str,
        *keys: Optional[str],
    ):
        """Mark an order as filled, remove it from cache, and trigger callbacks."""
        if not self._claim_order_finalization(*keys, grid_order.order_id):
            self.logger.info(
                "Skip duplicate terminal fill: "
                f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
            )
            return
        self._clear_uncertain_cancellation_markers(
            {
                normalized
                for key in (*keys, grid_order.order_id)
                if (normalized := self._string_or_none(key))
            }
        )
        self._consume_expected_cancellation(*keys, grid_order.order_id)
        self._clear_restore_state(grid_order)
        grid_order.mark_filled(filled_price, filled_amount)
        self._clear_pending_order_refs(*keys)
        self.logger.info(
            f"{log_prefix}: "
            f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
            f"amount={filled_amount}, price={filled_price}, order_id={grid_order.order_id}"
        )
        await self._run_order_callbacks(grid_order)

    async def _finalize_cancellation(
        self,
        grid_order: GridOrder,
        log_prefix: str,
        *keys: Optional[str],
    ):
        """Remove a cancelled order and restore it when the cancel was unexpected."""
        submission_was_uncertain = self._is_submission_uncertain(grid_order)
        if not self._claim_order_finalization(*keys, grid_order.order_id):
            self.logger.info(
                "Skip duplicate terminal cancellation: "
                f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
            )
            return
        self._clear_uncertain_cancellation_markers(
            {
                normalized
                for key in (*keys, grid_order.order_id)
                if (normalized := self._string_or_none(key))
            }
        )
        grid_order.mark_cancelled()
        self._remove_order_from_coordinator_state(grid_order)
        self._clear_pending_order_refs(*keys)
        if self._consume_expected_cancellation(*keys):
            self._clear_restore_state(grid_order)
            self.logger.info(
                f"Expected cancellation confirmed: "
                f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
            )
            return

        if submission_was_uncertain:
            self._clear_restore_state(grid_order)
            self._fail_closed_submission(
                "Uncertain order submission reached a terminal failure without being "
                f"restored: grid_id={grid_order.grid_id}, order_id={grid_order.order_id}",
                grid_order,
            )
            return

        self.logger.warning(
            f"{log_prefix}: unexpected grid cancellation detected, "
            f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
        )
        await self._restore_cancelled_grid_order(grid_order, grid_order.order_id)

    async def _sync_order_status_after_batch(self):
        """Resolve acknowledged batch submissions with bounded bulk snapshots."""
        unresolved = []
        last_error = None
        for delay in (0.3, 0.5, 1.0, 2.0, 4.0):
            await asyncio.sleep(delay)
            try:
                exchange_orders = await self.exchange.get_open_orders(
                    self.config.symbol
                )
                await self._sync_orders_from_exchange(exchange_orders)
                last_error = None
            except Exception as exc:
                last_error = exc

            unresolved = [
                order
                for order in self.get_pending_orders()
                if self._is_submission_uncertain(order)
                and self._is_submission_acknowledged(order)
                and not order.exchange_data.get("health_repair_deferred")
            ]
            if not unresolved:
                return

        if last_error is not None:
            self.logger.warning(f"Post-batch order sync failed: {last_error}")
        for order in unresolved:
            self._quarantine_uncertain_grid_submission(order)

    def _find_cached_order(self, *candidates: Optional[str]) -> Tuple[Optional[str], Optional[GridOrder]]:
        """Find a cached order by any known alias key."""
        for candidate in candidates:
            key = self._string_or_none(candidate)
            if key and key in self._pending_orders:
                return key, self._pending_orders[key]

        candidate_set = {
            self._string_or_none(candidate)
            for candidate in candidates
            if self._string_or_none(candidate)
        }
        if not candidate_set:
            return None, None

        for key, order in self._pending_orders.items():
            aliases = set(self._pending_keys_for_order(order))
            if aliases & candidate_set:
                return key, order
        return None, None

    def _register_pending_order(self, grid_order: GridOrder, *keys: Optional[str]) -> None:
        """Register one GridOrder object under all known exchange alias keys."""
        for key in keys:
            normalized = self._string_or_none(key)
            if normalized:
                self._pending_orders[normalized] = grid_order

    def _build_grid_order_from_exchange_order(
        self,
        exchange_order: ExchangeOrderData,
    ) -> Optional[GridOrder]:
        """Convert an exchange open order into a locally tracked grid order."""
        order_id = self._string_or_none(getattr(exchange_order, "id", None))
        price = getattr(exchange_order, "price", None)
        amount = getattr(exchange_order, "amount", None)
        side = getattr(exchange_order, "side", None)
        if not order_id or price is None or amount is None or side is None:
            return None

        grid_side = (
            GridOrderSide.BUY
            if side.value.lower() == "buy"
            else GridOrderSide.SELL
        )
        grid_id = self._infer_grid_id_for_exchange_order(Decimal(str(price)), grid_side)
        if grid_id is None:
            return None

        parent_order_id = None
        if self._is_reverse_side(grid_side):
            parent_order_id = f"mirrored:{order_id}"

        return GridOrder(
            order_id=order_id,
            grid_id=grid_id,
            side=grid_side,
            price=Decimal(str(price)),
            amount=Decimal(str(amount)),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
            parent_order_id=parent_order_id,
            exchange_data={"mirrored_exchange_order": True},
        )

    def _infer_grid_id_for_exchange_order(
        self,
        price: Decimal,
        side: GridOrderSide,
    ) -> Optional[int]:
        """Infer the logical source grid for base and reverse exchange orders."""
        if self.config is None:
            return None

        if not self._is_reverse_side(side):
            return self.config.get_grid_index_by_price(price)

        source_price = self._source_price_for_reverse_order(price, side)
        if source_price is None:
            return self.config.get_grid_index_by_price(price)
        return self.config.get_grid_index_by_price(source_price)

    def _source_price_for_reverse_order(
        self,
        price: Decimal,
        side: GridOrderSide,
    ) -> Optional[Decimal]:
        """Return the source grid price for an opposite-side reverse order."""
        base_side = self._base_side_for_grid_type()
        if base_side is None:
            return None

        distance = getattr(self.config, "reverse_order_grid_distance", 1) or 1
        offset = self.config.grid_interval * Decimal(str(distance))
        if base_side == GridOrderSide.BUY and side == GridOrderSide.SELL:
            return price - offset
        if base_side == GridOrderSide.SELL and side == GridOrderSide.BUY:
            return price + offset
        return None

    def _base_side_for_grid_type(self) -> Optional[GridOrderSide]:
        """Return the opening side for the configured grid type."""
        if self.config is None:
            return None

        grid_type = getattr(self.config, "grid_type", None)
        value = getattr(grid_type, "value", str(grid_type)).lower()
        if value in {"long", "follow_long", "martingale_long"}:
            return GridOrderSide.BUY
        if value in {"short", "follow_short", "martingale_short"}:
            return GridOrderSide.SELL
        return None

    def _is_reverse_side(self, side: GridOrderSide) -> bool:
        """Return whether an order side is opposite to the configured base side."""
        base_side = self._base_side_for_grid_type()
        return base_side is not None and side != base_side

    def _note_finalized_order(self, *keys: Optional[str]) -> None:
        """Remember recently finalized order ids so stale snapshots are not re-imported."""
        self._prune_recently_finalized_orders()
        now = time.time()
        for key in keys:
            normalized = self._string_or_none(key)
            if normalized:
                self._recently_finalized_order_ids[normalized] = now

    def _claim_order_finalization(self, *keys: Optional[str]) -> bool:
        """Atomically claim one terminal order transition across REST and WebSocket paths."""
        self._prune_recently_finalized_orders()
        normalized_keys = {
            normalized
            for key in keys
            if (normalized := self._string_or_none(key))
        }
        if any(key in self._recently_finalized_order_ids for key in normalized_keys):
            return False

        now = time.time()
        for key in normalized_keys:
            self._recently_finalized_order_ids[key] = now
        return True

    def _was_recently_finalized_order(self, key: Optional[str]) -> bool:
        """Return whether an order id was finalized recently enough to ignore stale open snapshots."""
        normalized = self._string_or_none(key)
        if not normalized:
            return False
        self._prune_recently_finalized_orders()
        return normalized in self._recently_finalized_order_ids

    def _prune_recently_finalized_orders(self) -> None:
        """Drop old finalized-order ids from the stale-snapshot guard."""
        recently_finalized = getattr(self, "_recently_finalized_order_ids", None)
        if recently_finalized is None:
            recently_finalized = {}
            self._recently_finalized_order_ids = recently_finalized
        if not recently_finalized:
            return
        cutoff = time.time() - getattr(self, "_finalized_order_cache_seconds", 300.0)
        for key, seen_at in list(recently_finalized.items()):
            if seen_at < cutoff:
                del recently_finalized[key]

    def _pending_keys_for_order(self, grid_order: GridOrder) -> List[str]:
        """Return all cache keys that point to the provided GridOrder object."""
        object_id = id(grid_order)
        return [
            key for key, cached_order in self._pending_orders.items()
            if id(cached_order) == object_id
        ]

    def _clear_pending_order_refs(self, *keys: Optional[str]) -> int:
        """Remove all cache aliases that point to the same order object."""
        _, grid_order = self._find_cached_order(*keys)
        explicit_keys = {
            self._string_or_none(key)
            for key in keys
            if self._string_or_none(key)
        }
        if grid_order is not None:
            explicit_keys.update(self._pending_keys_for_order(grid_order))

        removed = 0
        for key in list(explicit_keys):
            if key in self._pending_orders:
                del self._pending_orders[key]
                removed += 1
        return removed

    def _consume_expected_cancellation(self, *keys: Optional[str]) -> bool:
        """Consume expected-cancellation markers for all aliases of one order."""
        _, grid_order = self._find_cached_order(*keys)
        candidate_keys = {
            self._string_or_none(key)
            for key in keys
            if self._string_or_none(key)
        }
        if grid_order is not None:
            candidate_keys.update(self._pending_keys_for_order(grid_order))

        consumed = False
        uncertain_ids = getattr(self, "_uncertain_cancel_order_ids", None)
        for key in list(candidate_keys):
            if key in self._expected_cancellations:
                self._expected_cancellations.remove(key)
                consumed = True
            if uncertain_ids is not None:
                uncertain_ids.discard(key)
        return consumed

    async def _run_order_callbacks(self, grid_order: GridOrder) -> None:
        """Run registered order callbacks and await coroutine results when needed."""
        for callback in self._order_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(grid_order)
                else:
                    result = callback(grid_order)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                self.logger.error(f"Order callback failed: {exc}")

    async def _restore_cancelled_grid_order(
        self,
        grid_order: GridOrder,
        order_id: str,
    ) -> bool:
        """Schedule a bounded restore for an unexpectedly cancelled grid order."""
        if self._shutting_down or not self._running:
            self.logger.info(
                f"Skip order restoration while engine is stopping: source_order_id={order_id}"
            )
            return False

        remaining_amount = self._get_remaining_order_amount(grid_order)
        if remaining_amount <= self._get_order_fill_tolerance():
            self.logger.info(
                "Skip order restoration because the logical order is already filled: "
                f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
                f"price={grid_order.price}, source_order_id={order_id}"
            )
            return False

        if self._quarantine_subminimum_continuation_if_needed(
            grid_order,
            remaining_amount,
        ):
            return True

        restore_key = self._restore_key(grid_order)
        now = time.monotonic()
        state = self._restore_state.setdefault(
            restore_key,
            {"attempts": 0.0, "last_attempt": 0.0, "circuit_until": 0.0},
        )
        if now >= state["circuit_until"] and (
            now - state["last_attempt"] > self._restore_attempt_window
        ):
            state.update(attempts=0.0, circuit_until=0.0)

        if now < state["circuit_until"]:
            self.logger.error(
                "Order restoration circuit is open: "
                f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
                f"price={grid_order.price}, retry_after="
                f"{state['circuit_until'] - now:.1f}s"
            )
            return False

        active_task = self._restore_tasks.get(restore_key)
        if active_task and not active_task.done():
            self.logger.warning(
                "Order restoration already scheduled: "
                f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
                f"price={grid_order.price}"
            )
            return True

        task = asyncio.create_task(
            self._run_cancelled_order_restore(
                grid_order,
                order_id,
                remaining_amount,
                restore_key,
            )
        )
        self._restore_tasks[restore_key] = task
        return True

    async def _run_cancelled_order_restore(
        self,
        grid_order: GridOrder,
        order_id: str,
        remaining_amount: Decimal,
        restore_key: str,
    ) -> None:
        """Restore one logical order with backoff and a per-level circuit breaker."""
        state = self._restore_state[restore_key]
        try:
            while state["attempts"] < self._restore_max_attempts:
                if self._shutting_down or not self._running:
                    return

                if any(
                    pending.grid_id == grid_order.grid_id
                    and pending.side == grid_order.side
                    and pending.price == grid_order.price
                    for pending in self.get_pending_orders()
                ):
                    self.logger.info(
                        "Skip order restoration because an equivalent order already exists: "
                        f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
                        f"price={grid_order.price}"
                    )
                    return

                attempts = int(state["attempts"])
                if attempts:
                    await asyncio.sleep(
                        self._restore_base_delay * (2 ** (attempts - 1))
                    )
                    if self._shutting_down or not self._running:
                        return

                state["attempts"] = float(attempts + 1)
                state["last_attempt"] = time.monotonic()
                replacement_order = GridOrder(
                    order_id="",
                    grid_id=grid_order.grid_id,
                    side=grid_order.side,
                    price=grid_order.price,
                    amount=remaining_amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now(),
                    parent_order_id=grid_order.parent_order_id,
                    exchange_data=self._build_continuation_exchange_data(
                        grid_order,
                        remaining_amount,
                        order_id,
                    ),
                )

                try:
                    placed_order = await self.place_order(replacement_order)
                except Exception as exc:
                    self.logger.error(
                        "Failed to restore cancelled grid order: "
                        f"grid_id={grid_order.grid_id}, source_order_id={order_id}, "
                        f"attempt={attempts + 1}/{self._restore_max_attempts}, "
                        f"error={exc}"
                    )
                    continue

                if placed_order:
                    self._replace_state_order(order_id, placed_order)
                    self.logger.info(
                        "Grid order restored after unexpected cancellation: "
                        f"grid_id={placed_order.grid_id}, side={placed_order.side.value}, "
                        f"amount={placed_order.amount}, price={placed_order.price}, "
                        f"order_id={placed_order.order_id}, "
                        f"attempt={attempts + 1}/{self._restore_max_attempts}"
                    )
                    return

            state["circuit_until"] = time.monotonic() + self._restore_circuit_seconds
            self.logger.error(
                "Order restoration circuit opened after repeated failures: "
                f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
                f"price={grid_order.price}, attempts={self._restore_max_attempts}, "
                f"cooldown={self._restore_circuit_seconds:.0f}s"
            )
            if self._get_cumulative_fill_amount(grid_order) > 0:
                self._quarantine_unmanaged_partial(
                    grid_order,
                    remaining_amount,
                    "bounded continuation restore exhausted",
                )
            else:
                self._clear_matching_grid_lock(grid_order)
        finally:
            current_task = asyncio.current_task()
            if self._restore_tasks.get(restore_key) is current_task:
                self._restore_tasks.pop(restore_key, None)

    def _restore_key(self, grid_order: GridOrder) -> str:
        """Return the stable identity shared by replacement orders for one level."""
        return f"{grid_order.grid_id}:{grid_order.side.value}:{grid_order.price}"

    def _clear_restore_state(self, grid_order: GridOrder) -> None:
        """Clear retry history once the logical order reaches a terminal success."""
        restore_key = self._restore_key(grid_order)
        restore_tasks = getattr(self, "_restore_tasks", None)
        if restore_tasks is None:
            restore_tasks = {}
            self._restore_tasks = restore_tasks
        task = restore_tasks.pop(restore_key, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        restore_state = getattr(self, "_restore_state", None)
        if restore_state is not None:
            restore_state.pop(restore_key, None)
        unmanaged_carries = getattr(self, "_unmanaged_partial_carries", None)
        if unmanaged_carries is not None:
            unmanaged_carries.pop(restore_key, None)

    def _remove_order_from_coordinator_state(self, grid_order: GridOrder) -> None:
        """Remove the terminal source order without leaving pending state counts."""
        coordinator = getattr(self, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        active_orders = getattr(state, "active_orders", None) if state else None
        if not isinstance(active_orders, dict):
            return

        remove_keys = [
            key
            for key, state_order in active_orders.items()
            if state_order is grid_order or str(key) == str(grid_order.order_id)
        ]
        for key in remove_keys:
            stale_order = active_orders.pop(key)
            if stale_order.side == GridOrderSide.BUY and getattr(state, "pending_buy_orders", 0) > 0:
                state.pending_buy_orders -= 1
            elif stale_order.side == GridOrderSide.SELL and getattr(state, "pending_sell_orders", 0) > 0:
                state.pending_sell_orders -= 1

    def _quarantine_subminimum_continuation_if_needed(
        self,
        grid_order: GridOrder,
        remaining_amount: Decimal,
    ) -> bool:
        """Fail-stop an opening continuation that cannot satisfy exchange minimums."""
        if (
            not self._is_opening_side(grid_order.side)
            or self._get_cumulative_fill_amount(grid_order) <= 0
        ):
            return False
        checker = getattr(self, "_health_checker", None)
        minimum_violation = getattr(checker, "_market_minimum_violation", None)
        if not callable(minimum_violation):
            return False
        violation = minimum_violation(remaining_amount, grid_order.price)
        if not violation:
            return False
        self._quarantine_unmanaged_partial(
            grid_order,
            remaining_amount,
            f"continuation below exchange minimum ({violation})",
        )
        return True

    def _quarantine_unmanaged_partial(
        self,
        grid_order: GridOrder,
        remaining_amount: Decimal,
        reason: str,
    ) -> None:
        """Retain partial exposure accounting and block this grid until restart."""
        carry_amount = self._get_cumulative_fill_amount(grid_order)
        restore_key = self._restore_key(grid_order)
        unmanaged_carries = getattr(self, "_unmanaged_partial_carries", None)
        if unmanaged_carries is None:
            unmanaged_carries = {}
            self._unmanaged_partial_carries = unmanaged_carries
        unmanaged_carries[restore_key] = {
            "grid_id": grid_order.grid_id,
            "side": grid_order.side,
            "price": grid_order.price,
            "carry_amount": carry_amount,
            "remaining_amount": Decimal(str(remaining_amount)),
            "reason": reason,
        }

        coordinator = getattr(self, "coordinator", None)
        locks = getattr(coordinator, "_grid_level_locks", None) if coordinator else None
        if isinstance(locks, dict):
            locks[grid_order.grid_id] = {
                "tp_side": grid_order.side.value,
                "tp_price": grid_order.price,
                "tp_order_id": None,
                "reason": "unmanaged_partial",
            }

        fatal_reason = (
            "Partial fill continuation cannot be placed safely: "
            f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
            f"carry={carry_amount}, remaining={remaining_amount}, "
            f"price={grid_order.price}, reason={reason}"
        )
        self.logger.critical(fatal_reason)
        request_fatal_stop = getattr(coordinator, "_request_fatal_stop", None)
        if callable(request_fatal_stop):
            request_fatal_stop(fatal_reason)

    def _clear_matching_grid_lock(self, grid_order: GridOrder) -> None:
        """Release only the coordinator lock owned by this exhausted restore."""
        coordinator = getattr(self, "coordinator", None)
        locks = getattr(coordinator, "_grid_level_locks", None) if coordinator else None
        if not locks:
            return
        lock = locks.get(grid_order.grid_id)
        if not isinstance(lock, dict):
            return
        if str(lock.get("tp_side", "")).lower() != grid_order.side.value.lower():
            return
        lock_price = lock.get("tp_price")
        if lock_price is None or Decimal(str(lock_price)) != Decimal(str(grid_order.price)):
            return
        del locks[grid_order.grid_id]

    def _replace_state_order(self, old_order_id: str, new_order: GridOrder) -> None:
        """Replace one coordinator state order entry after a restore."""
        coordinator = getattr(self, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if not state or not hasattr(state, "active_orders"):
            return

        stale_order = state.active_orders.pop(old_order_id, None)
        if stale_order is not None:
            if stale_order.side == GridOrderSide.BUY and getattr(state, "pending_buy_orders", 0) > 0:
                state.pending_buy_orders -= 1
            elif stale_order.side == GridOrderSide.SELL and getattr(state, "pending_sell_orders", 0) > 0:
                state.pending_sell_orders -= 1

        if hasattr(state, "add_order"):
            state.add_order(new_order)

    async def _execute_batch(
        self,
        orders: List[GridOrder],
        allow_while_paused: bool = False,
        defer_uncertain: bool = False,
    ) -> List[Any]:
        """Execute one placement batch, serially when required by the exchange."""
        if self._supports_batch_mode():
            results = []
            for order in orders:
                try:
                    results.append(
                        await self.place_order(
                            order,
                            batch_mode=True,
                            allow_while_paused=allow_while_paused,
                            defer_uncertain=defer_uncertain,
                        )
                    )
                except Exception as exc:
                    results.append(exc)
            return results

        tasks = [
            self.place_order(
                order,
                allow_while_paused=allow_while_paused,
                defer_uncertain=defer_uncertain,
            )
            for order in orders
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def _supports_batch_mode(self) -> bool:
        """Return whether the current exchange needs serialized batch placement."""
        exchange_name = str(self.config.exchange).lower() if self.config else ""
        return exchange_name in {"lighter", "tradexyz"}

    def _convert_order_side(self, grid_side: GridOrderSide) -> ExchangeOrderSide:
        """Convert a grid side to the exchange-side enum."""
        if grid_side == GridOrderSide.BUY:
            return ExchangeOrderSide.BUY
        return ExchangeOrderSide.SELL

    def _extract_price_from_ticker(self, ticker_data) -> Decimal:
        """Extract a usable price from either a ticker object or a raw dict."""
        if hasattr(ticker_data, "last"):
            if ticker_data.last is not None:
                return Decimal(str(ticker_data.last))
            if ticker_data.bid is not None and ticker_data.ask is not None:
                return (Decimal(str(ticker_data.bid)) + Decimal(str(ticker_data.ask))) / Decimal("2")
            if ticker_data.bid is not None:
                return Decimal(str(ticker_data.bid))
            if ticker_data.ask is not None:
                return Decimal(str(ticker_data.ask))

        if isinstance(ticker_data, dict):
            last = ticker_data.get("last") or ticker_data.get("price") or ticker_data.get("p")
            bid = ticker_data.get("bid") or ticker_data.get("b")
            ask = ticker_data.get("ask") or ticker_data.get("a")
            if last is not None:
                return Decimal(str(last))
            if bid is not None and ask is not None:
                return (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")
            if bid is not None:
                return Decimal(str(bid))
            if ask is not None:
                return Decimal(str(ask))

        raise ValueError("Ticker data does not contain a usable price")

    def _extract_nested_update_items(self, payload: Any) -> List[Dict[str, Any]]:
        """Extract a flat list of order-like dict items from vendor websocket payloads."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            nested = (
                payload.get("data")
                or payload.get("fills")
                or payload.get("orders")
                or payload.get("orderUpdates")
            )
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            return [payload]

        return []

    def _build_temp_order_id(self, order: GridOrder) -> str:
        """Build a stable temporary id when the exchange has not returned one yet."""
        amount_token = int((order.amount or Decimal("0")) * Decimal("1000000"))
        price_token = int(order.price or 0)
        return f"grid_{order.grid_id}_{price_token}_{amount_token}"

    def _merge_order_exchange_data(
        self,
        order: GridOrder,
        raw_exchange_data: Any,
    ) -> Dict[str, Any]:
        """Preserve local fill tracking while attaching the latest exchange payload."""
        existing = order.exchange_data if isinstance(order.exchange_data, dict) else {}
        merged = deepcopy(existing)

        if isinstance(raw_exchange_data, dict):
            for key, value in raw_exchange_data.items():
                merged.setdefault(key, value)
            if raw_exchange_data:
                merged["exchange_order_raw"] = raw_exchange_data
        elif raw_exchange_data:
            merged["exchange_order_raw"] = raw_exchange_data

        return merged

    def _get_tradexyz_fill_tracking(self, grid_order: GridOrder) -> Dict[str, Any]:
        """Return mutable TradeXYZ user-fill tracking data for one pending order."""
        if not isinstance(grid_order.exchange_data, dict):
            grid_order.exchange_data = {}

        tracking = grid_order.exchange_data.get("tradexyz_fill_tracking")
        if not isinstance(tracking, dict):
            tracking = {}
            grid_order.exchange_data["tradexyz_fill_tracking"] = tracking
        if not isinstance(tracking.get("seen_fill_ids"), list):
            tracking["seen_fill_ids"] = []
        if not isinstance(tracking.get("fills_by_id"), dict):
            tracking["fills_by_id"] = {}
        return tracking

    def _record_exchange_order_progress(
        self,
        grid_order: GridOrder,
        exchange_order: ExchangeOrderData,
    ) -> None:
        """Record a cumulative exchange snapshot without finalizing a partial fill."""
        reported_filled = self._safe_decimal(
            getattr(exchange_order, "filled", None),
            Decimal("0"),
        )
        reported_remaining = self._safe_decimal(
            getattr(exchange_order, "remaining", None),
            Decimal("0"),
        )
        if reported_filled <= 0 and reported_remaining <= 0:
            return

        tracking = self._get_tradexyz_fill_tracking(grid_order)
        target_amount = self._ensure_tradexyz_target_amount(grid_order)
        carry_amount = self._safe_decimal(
            tracking.get("carry_filled_amount"),
            Decimal("0"),
        )
        current_order_target = max(target_amount - carry_amount, Decimal("0"))
        if current_order_target > 0:
            reported_filled = min(reported_filled, current_order_target)

        cumulative_filled = carry_amount + reported_filled
        previous_cumulative = self._safe_decimal(
            tracking.get("cumulative_filled"),
            Decimal("0"),
        )
        if cumulative_filled > previous_cumulative:
            tracking["snapshot_cumulative_filled"] = str(reported_filled)
            tracking["cumulative_filled"] = str(cumulative_filled)

        effective_cumulative = max(previous_cumulative, cumulative_filled)
        tracking["remaining_amount"] = str(
            max(target_amount - effective_cumulative, Decimal("0"))
        )
        tracking["exchange_remaining_amount"] = str(reported_remaining)
        status = getattr(exchange_order, "status", None)
        tracking["last_exchange_status"] = getattr(status, "value", str(status or ""))

    def _record_tradexyz_fill_entry(
        self,
        tracking: Dict[str, Any],
        event_key: str,
        fill_amount: Decimal,
        filled_price: Decimal,
    ) -> bool:
        """Record one unique TradeXYZ fill into the shared ledger."""
        fills_by_id = tracking.setdefault("fills_by_id", {})
        seen_fill_ids = tracking.setdefault("seen_fill_ids", [])
        if event_key in fills_by_id:
            return False

        normalized_amount = format(fill_amount.normalize(), "f")
        normalized_price = format(filled_price.normalize(), "f")
        fills_by_id[event_key] = {
            "amount": normalized_amount,
            "price": normalized_price,
        }
        seen_fill_ids.append(event_key)
        if len(seen_fill_ids) > 200:
            stale_keys = seen_fill_ids[:-200]
            del seen_fill_ids[:-200]
            for stale_key in stale_keys:
                if stale_key not in seen_fill_ids:
                    fills_by_id.pop(stale_key, None)

        snapshot_floor = self._safe_decimal(
            tracking.get("snapshot_cumulative_filled"),
            Decimal("0"),
        )
        carry_amount = self._safe_decimal(
            tracking.get("carry_filled_amount"),
            Decimal("0"),
        )
        ledger_total = Decimal("0")
        for fill_info in fills_by_id.values():
            ledger_total += self._safe_decimal(fill_info.get("amount"), Decimal("0"))
        current_order_filled = (
            ledger_total
            if ledger_total >= snapshot_floor
            else snapshot_floor + ledger_total
        )
        cumulative_filled = carry_amount + current_order_filled
        tracking["cumulative_filled"] = str(cumulative_filled)
        tracking["last_fill_price"] = normalized_price
        return True

    def _ensure_tradexyz_target_amount(self, grid_order: GridOrder) -> Decimal:
        """Return the logical full amount for a possibly restored partial order."""
        tracking = self._get_tradexyz_fill_tracking(grid_order)
        target_amount = self._safe_decimal(
            tracking.get("target_amount"),
            Decimal("0"),
        )
        order_amount = self._safe_decimal(
            getattr(grid_order, "amount", None),
            Decimal("0"),
        )
        if target_amount <= 0:
            target_amount = order_amount
            if target_amount > 0:
                tracking["target_amount"] = str(target_amount)
        return target_amount

    def _get_cumulative_fill_amount(self, grid_order: GridOrder) -> Decimal:
        """Return the cumulative logical fill amount currently recorded locally."""
        tracking = self._get_tradexyz_fill_tracking(grid_order)
        return self._safe_decimal(
            tracking.get("cumulative_filled"),
            Decimal("0"),
        )

    def _get_remaining_order_amount(self, grid_order: GridOrder) -> Decimal:
        """Return the exchange amount still needed to complete the logical order."""
        target_amount = self._ensure_tradexyz_target_amount(grid_order)
        cumulative_filled = self._get_cumulative_fill_amount(grid_order)
        if target_amount <= 0:
            return self._safe_decimal(getattr(grid_order, "amount", None), Decimal("0"))
        return max(target_amount - min(cumulative_filled, target_amount), Decimal("0"))

    def _get_finalized_fill_amount(
        self,
        grid_order: GridOrder,
        reported_fill_amount: Decimal,
    ) -> Decimal:
        """Return the logical amount to use when a restored partial order completes."""
        target_amount = self._ensure_tradexyz_target_amount(grid_order)
        cumulative_filled = self._get_cumulative_fill_amount(grid_order)
        if target_amount > 0 and cumulative_filled > 0:
            return target_amount
        return reported_fill_amount

    def _build_continuation_exchange_data(
        self,
        grid_order: GridOrder,
        remaining_amount: Decimal,
        source_order_id: str,
    ) -> Dict[str, Any]:
        """Carry partial-fill ledger data onto a replacement exchange order."""
        data = deepcopy(grid_order.exchange_data) if isinstance(grid_order.exchange_data, dict) else {}
        tracking = data.get("tradexyz_fill_tracking")
        if not isinstance(tracking, dict):
            tracking = {}
            data["tradexyz_fill_tracking"] = tracking

        target_amount = self._ensure_tradexyz_target_amount(grid_order)
        cumulative_filled = self._get_cumulative_fill_amount(grid_order)
        prior_fills = tracking.get("fills_by_id")
        if prior_fills and "prior_fills_by_id" not in tracking:
            tracking["prior_fills_by_id"] = deepcopy(prior_fills)
        tracking["fills_by_id"] = {}
        tracking["seen_fill_ids"] = []
        tracking["carry_filled_amount"] = str(cumulative_filled)
        tracking["snapshot_cumulative_filled"] = "0"
        tracking["target_amount"] = str(target_amount)
        tracking["cumulative_filled"] = str(cumulative_filled)
        tracking["remaining_amount"] = str(remaining_amount)
        tracking["continuation_source_order_id"] = str(source_order_id or "")
        return data

    def _is_user_fill_snapshot(self, exchange_order: ExchangeOrderData) -> bool:
        """Return whether get_order() only has a TradeXYZ user-fill snapshot."""
        params = getattr(exchange_order, "params", {}) or {}
        return bool(params.get("user_fill_snapshot_only"))

    @staticmethod
    def _exchange_order_status(exchange_order: ExchangeOrderData) -> str:
        """Return one normalized exchange-order status value."""
        status = getattr(exchange_order, "status", None)
        return str(getattr(status, "value", status) or "unknown").lower()

    @staticmethod
    def _is_nonterminal_cancel_ack(cancel_result: Any) -> bool:
        """Return whether a cancel response is only a transaction acknowledgement."""
        for payload in (
            getattr(cancel_result, "params", None),
            getattr(cancel_result, "raw_data", None),
        ):
            if isinstance(payload, dict) and payload.get("cancel_terminal") is False:
                return True
        return False

    @staticmethod
    def _exchange_submission_is_uncertain(exchange_order: ExchangeOrderData) -> bool:
        """Return whether an exchange result is a transport-uncertain placeholder."""
        params = getattr(exchange_order, "params", {}) or {}
        raw_data = getattr(exchange_order, "raw_data", {}) or {}
        return bool(
            (isinstance(params, dict) and params.get("submission_uncertain"))
            or (
                isinstance(raw_data, dict)
                and raw_data.get("submission_uncertain")
            )
        )

    def _fail_closed_submission(
        self,
        reason: str,
        grid_order: Optional[GridOrder] = None,
    ) -> None:
        """Synchronously close the placement gate and request one fatal stop."""
        self.pause_placements()
        coordinator = getattr(self, "coordinator", None)
        locks = getattr(coordinator, "_grid_level_locks", None) if coordinator else None
        if grid_order is not None and isinstance(locks, dict):
            locks.setdefault(
                grid_order.grid_id,
                {
                    "tp_side": grid_order.side.value,
                    "tp_price": grid_order.price,
                    "tp_order_id": None,
                    "reason": "submission_uncertain",
                },
            )

        self.logger.critical(reason)
        request_fatal_stop = getattr(coordinator, "_request_fatal_stop", None)
        if callable(request_fatal_stop):
            request_fatal_stop(reason)

    def _quarantine_uncertain_grid_submission(self, grid_order: GridOrder) -> None:
        """Keep one ambiguous client id read-only and prevent replacement orders."""
        exchange_data = getattr(grid_order, "exchange_data", None)
        if not isinstance(exchange_data, dict):
            exchange_data = {}
            grid_order.exchange_data = exchange_data
        if exchange_data.get("submission_quarantined"):
            return
        exchange_data["submission_quarantined"] = True
        self._fail_closed_submission(
            "Order submission outcome is uncertain; retaining the original client id "
            "without restore: "
            f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
            f"amount={grid_order.amount}, price={grid_order.price}, "
            f"client_id={grid_order.order_id}",
            grid_order,
        )

    async def _resolve_uncertain_grid_submission_with_grace(
        self,
        grid_order: GridOrder,
    ) -> bool:
        """Pause new mutations while an ambiguous submission gets exact read proof."""
        resolver = getattr(self.exchange, "resolve_unresolved_submissions", None)
        if not callable(resolver):
            return False

        owns_pause = not self._placements_paused
        self.pause_placements()
        resolved = False
        for delay in (0.5, 1.0, 2.0):
            await asyncio.sleep(delay)
            try:
                await self._resolve_unresolved_submissions_read_only()
            except Exception as exc:
                self.logger.warning(
                    "Ambiguous submission grace lookup failed: "
                    f"grid_id={grid_order.grid_id}, error={exc}"
                )
            if (
                not self._is_submission_uncertain(grid_order)
                or grid_order.status != GridOrderStatus.PENDING
            ):
                resolved = True
                break

        if owns_pause and resolved and not self._shutting_down:
            self.resume_placements()
        return resolved

    def _adopt_reconciled_grid_submission(
        self,
        grid_order: GridOrder,
        exchange_order: ExchangeOrderData,
    ) -> None:
        """Adopt an exact client-id match without issuing another mutation."""
        order_id = self._string_or_none(getattr(exchange_order, "id", None))
        client_id = self._string_or_none(getattr(exchange_order, "client_id", None))
        if order_id:
            grid_order.order_id = order_id
        self._register_pending_order(grid_order, order_id, client_id)
        exchange_data = getattr(grid_order, "exchange_data", None)
        if not isinstance(exchange_data, dict):
            exchange_data = {}
            grid_order.exchange_data = exchange_data
        exchange_data["submission_uncertain"] = False
        exchange_data["submission_reconciled"] = True
        exchange_data.pop("health_repair_deferred", None)
        resolved = getattr(self, "_resolved_submission_client_ids", None)
        if resolved is None:
            resolved = set()
            self._resolved_submission_client_ids = resolved
        for key in (order_id, client_id):
            if key:
                resolved.add(key)
        self.logger.warning(
            "Adopted previously uncertain order submission by exact client id: "
            f"grid_id={grid_order.grid_id}, order_id={order_id}, client_id={client_id}"
        )

    def _schedule_deferred_fill_finalization(
        self,
        grid_order: GridOrder,
        exchange_order: ExchangeOrderData,
        order_id: str,
        client_id: Optional[str],
    ) -> None:
        """Finalize an immediate fill after the placement caller records state."""
        self._record_exchange_order_progress(grid_order, exchange_order)
        filled_price = (
            getattr(exchange_order, "average", None)
            or getattr(exchange_order, "price", None)
            or grid_order.price
        )
        reported_filled_amount = (
            getattr(exchange_order, "filled", None)
            or grid_order.amount
        )
        filled_amount = self._get_finalized_fill_amount(
            grid_order,
            Decimal(str(reported_filled_amount)),
        )

        def schedule() -> None:
            asyncio.create_task(
                self._finalize_fill(
                    grid_order,
                    Decimal(str(filled_price)),
                    filled_amount,
                    "Immediate exchange fill received",
                    order_id,
                    client_id,
                )
            )

        asyncio.get_running_loop().call_soon(schedule)

    def _record_uncertain_market_submission(
        self,
        exchange_order: ExchangeOrderData,
        side: GridOrderSide,
        amount: Decimal,
        reduce_only: bool,
    ) -> None:
        """Retain one market mutation intent until reads confirm its outcome."""
        client_id = self._string_or_none(
            getattr(exchange_order, "client_id", None)
            or getattr(exchange_order, "id", None)
        )
        if not client_id:
            return
        tracker = getattr(getattr(self, "coordinator", None), "tracker", None)
        position_before = None
        if tracker and hasattr(tracker, "get_current_position"):
            sign = (
                Decimal("1")
                if self._base_side_for_grid_type() == GridOrderSide.BUY
                else Decimal("-1")
            )
            position_before = sign * Decimal(str(tracker.get_current_position()))
        records = getattr(self, "_uncertain_market_submissions", None)
        if records is None:
            records = {}
            self._uncertain_market_submissions = records
        records[client_id] = {
            "client_id": client_id,
            "side": side,
            "amount": Decimal(str(amount)),
            "reduce_only": bool(reduce_only),
            "position_before": position_before,
        }
        getattr(self, "_resolved_submission_client_ids", set()).discard(client_id)

    def _reconcile_uncertain_market_submissions(
        self,
        open_orders_by_key: Dict[str, ExchangeOrderData],
        history_by_key: Dict[str, ExchangeOrderData],
    ) -> None:
        """Resolve market mutation intents only from an exact client-id match."""
        records = getattr(self, "_uncertain_market_submissions", None)
        if not records:
            return
        resolved_ids = getattr(self, "_resolved_submission_client_ids", None)
        if resolved_ids is None:
            resolved_ids = set()
            self._resolved_submission_client_ids = resolved_ids
        for client_id in list(records):
            exchange_order = open_orders_by_key.get(client_id) or history_by_key.get(client_id)
            if exchange_order is None:
                continue
            records.pop(client_id, None)
            resolved_ids.add(client_id)
            self.logger.warning(
                "Resolved uncertain market submission by exact client id: "
                f"client_id={client_id}, status={self._exchange_order_status(exchange_order)}"
            )

    def _reconcile_uncertain_market_positions(self, current_position: Decimal) -> None:
        """Resolve a market intent only after REST position movement proves it."""
        records = getattr(self, "_uncertain_market_submissions", None)
        if not records:
            return
        sign = (
            Decimal("1")
            if self._base_side_for_grid_type() == GridOrderSide.BUY
            else Decimal("-1")
        )
        directional_position = sign * Decimal(str(current_position))
        precision = int(getattr(self.config, "quantity_precision", 8) or 8)
        tolerance = Decimal("1").scaleb(-precision)
        resolved_ids = getattr(self, "_resolved_submission_client_ids", None)
        if resolved_ids is None:
            resolved_ids = set()
            self._resolved_submission_client_ids = resolved_ids

        for client_id, record in list(records.items()):
            position_before = record.get("position_before")
            if position_before is None:
                continue
            amount = Decimal(str(record["amount"]))
            if record.get("reduce_only"):
                target = max(Decimal(str(position_before)) - amount, Decimal("0"))
                confirmed = directional_position <= target + tolerance
            else:
                target = Decimal(str(position_before)) + amount
                confirmed = directional_position >= target - tolerance
            if confirmed:
                records.pop(client_id, None)
                resolved_ids.add(client_id)
                self.logger.warning(
                    "Resolved uncertain market submission from REST position movement: "
                    f"client_id={client_id}, position={current_position}"
                )

    async def _resolve_unresolved_submissions_read_only(
        self,
    ) -> Tuple[List[str], List[str]]:
        """Resolve adapter intents by reads before declaring cancel-all safe."""
        resolver = getattr(self.exchange, "resolve_unresolved_submissions", None)
        if not callable(resolver):
            return [], []
        resolved_orders = list(await resolver() or [])
        if not resolved_orders:
            return [], []

        resolved_by_key = {
            normalized: exchange_order
            for exchange_order in resolved_orders
            for key in (
                getattr(exchange_order, "id", None),
                getattr(exchange_order, "client_id", None),
            )
            if (normalized := self._string_or_none(key))
        }
        self._reconcile_uncertain_market_submissions({}, resolved_by_key)

        active: List[str] = []
        filled: List[str] = []
        resolved_ids = getattr(self, "_resolved_submission_client_ids", None)
        if resolved_ids is None:
            resolved_ids = set()
            self._resolved_submission_client_ids = resolved_ids

        for exchange_order in resolved_orders:
            order_id = self._string_or_none(getattr(exchange_order, "id", None))
            client_id = self._string_or_none(getattr(exchange_order, "client_id", None))
            label = order_id or client_id or "unknown"
            status = self._exchange_order_status(exchange_order)
            _, grid_order = self._find_cached_order(order_id, client_id)

            if status in {"filled"}:
                if grid_order is not None:
                    await self._finalize_fill(
                        grid_order,
                        getattr(exchange_order, "average", None)
                        or getattr(exchange_order, "price", None)
                        or grid_order.price,
                        getattr(exchange_order, "filled", None) or grid_order.amount,
                        "Read-only submission reconciliation found a fill",
                        order_id,
                        client_id,
                    )
                resolved_ids.update(key for key in (order_id, client_id) if key)
                reason = (
                    "Previously uncertain submission resolved as filled during "
                    f"shutdown verification: order_id={label}"
                )
                self._fail_closed_submission(reason, grid_order)
                filled.append(label)
                continue

            if status in {"canceled", "cancelled", "rejected", "expired"}:
                if grid_order is not None:
                    await self._finalize_cancellation(
                        grid_order,
                        "Read-only submission reconciliation found a terminal failure",
                        order_id,
                        client_id,
                    )
                resolved_ids.update(key for key in (order_id, client_id) if key)
                continue

            if grid_order is not None and self._is_submission_uncertain(grid_order):
                self._adopt_reconciled_grid_submission(grid_order, exchange_order)
            active.append(label)

        return active, filled

    async def _reconcile_shutdown_pending_orders(
        self,
        pending_orders: Sequence[GridOrder],
        cancelled_orders: Any,
    ) -> Tuple[List[str], List[str]]:
        """Require exact cancel-response or terminal-history proof per local order."""
        cancel_proof_by_key: Dict[str, Any] = {}
        if isinstance(cancelled_orders, (list, tuple, set)):
            for exchange_order in cancelled_orders:
                if self._is_nonterminal_cancel_ack(exchange_order):
                    continue
                for key in (
                    getattr(exchange_order, "id", None),
                    getattr(exchange_order, "client_id", None),
                    getattr(exchange_order, "order_id", None),
                ):
                    normalized = self._string_or_none(key)
                    if normalized:
                        cancel_proof_by_key[normalized] = exchange_order

        pending_keys = []
        needs_history = False
        for grid_order in pending_orders:
            aliases = set(self._pending_keys_for_order(grid_order))
            if not aliases:
                continue
            order_id = self._string_or_none(grid_order.order_id)
            if order_id:
                aliases.add(order_id)
            pending_keys.append((grid_order, aliases))
            if not any(alias in cancel_proof_by_key for alias in aliases):
                needs_history = True

        history_by_key: Dict[str, Any] = {}
        if needs_history:
            get_history = getattr(self.exchange, "get_order_history", None)
            if callable(get_history):
                try:
                    history = await get_history(self.config.symbol, limit=100)
                except TypeError:
                    history = await get_history(self.config.symbol)
                except Exception as exc:
                    self.logger.error(
                        f"Shutdown terminal-history verification failed: {exc}"
                    )
                    history = []
                for exchange_order in history or []:
                    for key in (
                        getattr(exchange_order, "id", None),
                        getattr(exchange_order, "client_id", None),
                    ):
                        normalized = self._string_or_none(key)
                        if normalized:
                            history_by_key[normalized] = exchange_order

        unresolved: List[str] = []
        filled: List[str] = []
        cancel_statuses = {"canceled", "cancelled", "rejected", "expired"}

        for grid_order, aliases in pending_keys:
            proof = next(
                (cancel_proof_by_key[key] for key in aliases if key in cancel_proof_by_key),
                None,
            )
            explicit_cancel = proof is not None
            if proof is None:
                proof = next(
                    (history_by_key[key] for key in aliases if key in history_by_key),
                    None,
                )

            label = self._string_or_none(grid_order.order_id) or str(grid_order.grid_id)
            if proof is None:
                unresolved.append(label)
                continue

            status = self._exchange_order_status(proof)
            if status in {"filled", "closed"}:
                self._record_exchange_order_progress(grid_order, proof)
                await self._finalize_fill(
                    grid_order,
                    getattr(proof, "average", None)
                    or getattr(proof, "price", None)
                    or grid_order.price,
                    self._get_finalized_fill_amount(
                        grid_order,
                        getattr(proof, "filled", None) or grid_order.amount,
                    ),
                    "Shutdown terminal-history reconciliation found a fill",
                    *aliases,
                    getattr(proof, "id", None),
                    getattr(proof, "client_id", None),
                )
                self._clear_uncertain_cancellation_markers(aliases)
                reason = (
                    "Order filled during shutdown cancellation verification: "
                    f"order_id={label}"
                )
                self._fail_closed_submission(reason, grid_order)
                filled.append(label)
                continue

            if explicit_cancel or status in cancel_statuses:
                self._clear_uncertain_cancellation_markers(aliases)
                await self._finalize_cancellation(
                    grid_order,
                    "Shutdown cancellation confirmed",
                    *aliases,
                    getattr(proof, "id", None),
                    getattr(proof, "client_id", None),
                )
                continue

            unresolved.append(label)

        return unresolved, filled

    async def _resolve_uncertain_cancellations_read_only(
        self,
    ) -> Tuple[List[str], List[str]]:
        """Require exact terminal history before clearing response-loss cancels."""
        uncertain_ids = set(getattr(self, "_uncertain_cancel_order_ids", set()))
        marker_sources = (
            self.exchange,
            getattr(self.exchange, "_rest", None),
            getattr(self.exchange, "_base", None),
        )
        for source in marker_sources:
            markers = getattr(source, "_uncertain_cancellations", ()) if source else ()
            for marker in markers:
                marker_id = marker[1] if isinstance(marker, tuple) and len(marker) > 1 else marker
                normalized = self._string_or_none(marker_id)
                if normalized:
                    uncertain_ids.add(normalized)

        if not uncertain_ids:
            return [], []

        history = await self.exchange.get_order_history(self.config.symbol, limit=100)
        history_by_key = {
            normalized: order
            for order in history or []
            for key in (
                getattr(order, "id", None),
                getattr(order, "client_id", None),
            )
            if (normalized := self._string_or_none(key))
        }
        unresolved: List[str] = []
        filled: List[str] = []
        processed_groups: set[frozenset[str]] = set()

        for uncertain_id in sorted(uncertain_ids):
            _, grid_order = self._find_cached_order(uncertain_id)
            group = {uncertain_id}
            if grid_order is not None:
                group.update(self._pending_keys_for_order(grid_order))
                normalized_order_id = self._string_or_none(grid_order.order_id)
                if normalized_order_id:
                    group.add(normalized_order_id)
            frozen_group = frozenset(group)
            if frozen_group in processed_groups:
                continue
            processed_groups.add(frozen_group)

            exchange_order = next(
                (history_by_key[key] for key in group if key in history_by_key),
                None,
            )
            if exchange_order is None:
                unresolved.append(uncertain_id)
                continue

            status = self._exchange_order_status(exchange_order)
            if status not in {"filled", "canceled", "cancelled", "rejected", "expired"}:
                unresolved.append(uncertain_id)
                continue

            self._clear_uncertain_cancellation_markers(group)
            if status != "filled":
                if grid_order is not None:
                    await self._finalize_cancellation(
                        grid_order,
                        "Read-only cancellation reconciliation found a terminal cancellation",
                        getattr(exchange_order, "id", None),
                        getattr(exchange_order, "client_id", None),
                        *group,
                    )
                continue

            label = (
                self._string_or_none(getattr(exchange_order, "id", None))
                or uncertain_id
            )
            if grid_order is not None:
                self._record_exchange_order_progress(grid_order, exchange_order)
                await self._finalize_fill(
                    grid_order,
                    getattr(exchange_order, "average", None)
                    or getattr(exchange_order, "price", None)
                    or grid_order.price,
                    self._get_finalized_fill_amount(
                        grid_order,
                        getattr(exchange_order, "filled", None) or grid_order.amount,
                    ),
                    "Read-only cancellation reconciliation found a fill",
                    getattr(exchange_order, "id", None),
                    getattr(exchange_order, "client_id", None),
                    *group,
                )
            reason = (
                "Previously uncertain cancellation resolved as filled during "
                f"shutdown verification: order_id={label}"
            )
            self._fail_closed_submission(reason, grid_order)
            filled.append(label)

        return unresolved, filled

    def _clear_uncertain_cancellation_markers(self, keys: set[str]) -> None:
        """Clear local and adapter response-loss markers after exact terminal proof."""
        getattr(self, "_uncertain_cancel_order_ids", set()).difference_update(keys)
        for source in (
            self.exchange,
            getattr(self.exchange, "_rest", None),
            getattr(self.exchange, "_base", None),
        ):
            markers = getattr(source, "_uncertain_cancellations", None) if source else None
            if not isinstance(markers, set):
                continue
            for marker in list(markers):
                marker_id = marker[1] if isinstance(marker, tuple) and len(marker) > 1 else marker
                if self._string_or_none(marker_id) in keys:
                    markers.discard(marker)

    def _unresolved_submission_descriptions(self) -> List[str]:
        """Return local and adapter mutation intents that still lack exact proof."""
        descriptions = [
            f"limit:{order.order_id}"
            for order in self.get_pending_orders()
            if self._is_submission_uncertain(order)
        ]
        descriptions.extend(
            f"market:{client_id}"
            for client_id in getattr(self, "_uncertain_market_submissions", {})
        )

        get_unresolved = getattr(self.exchange, "get_unresolved_submissions", None)
        resolved_ids = getattr(self, "_resolved_submission_client_ids", set())
        if callable(get_unresolved):
            for item in get_unresolved() or []:
                client_id = self._string_or_none(item.get("client_order_id"))
                if client_id and client_id not in resolved_ids:
                    descriptions.append(f"adapter:{client_id}")
        return list(dict.fromkeys(descriptions))

    def _cancel_outcome_is_uncertain(self, order_id: str) -> bool:
        """Inspect the Lighter adapter marker after a response-loss exception."""
        target = str(order_id)
        for source in (
            self.exchange,
            getattr(self.exchange, "_rest", None),
            getattr(self.exchange, "_base", None),
        ):
            uncertain = getattr(source, "_uncertain_cancellations", ()) if source else ()
            for marker in uncertain:
                marker_id = marker[1] if isinstance(marker, tuple) and len(marker) > 1 else marker
                if str(marker_id) == target:
                    return True
        return False

    def _is_uncertain_cancel(self, *keys: Optional[str]) -> bool:
        """Return whether cancellation proof is still pending for any alias."""
        uncertain_ids = getattr(self, "_uncertain_cancel_order_ids", set())
        return any(
            normalized in uncertain_ids
            for key in keys
            if (normalized := self._string_or_none(key))
        )

    @staticmethod
    def _is_submission_uncertain(grid_order: GridOrder) -> bool:
        """Return whether placement produced a tracked transport-uncertain phantom."""
        exchange_data = getattr(grid_order, "exchange_data", {}) or {}
        return bool(
            isinstance(exchange_data, dict)
            and exchange_data.get("submission_uncertain")
        )

    @staticmethod
    def _is_submission_acknowledged(grid_order: GridOrder) -> bool:
        """Return whether Lighter accepted the mutation but has not exposed its ID."""
        exchange_data = getattr(grid_order, "exchange_data", {}) or {}
        return bool(
            isinstance(exchange_data, dict)
            and exchange_data.get("submission_acknowledged")
        )

    def _build_tradexyz_fill_event_key(
        self,
        item: Dict[str, Any],
        grid_order: GridOrder,
        filled_price: Decimal,
        fill_amount: Decimal,
    ) -> str:
        """Build a stable key so duplicate TradeXYZ user-fill events are ignored."""
        explicit_id = (
            item.get("tid")
            or item.get("fillId")
            or item.get("fill_id")
            or item.get("tradeId")
            or item.get("hash")
            or item.get("txHash")
        )
        if explicit_id is not None:
            return str(explicit_id)

        event_time = item.get("time") or item.get("timestamp") or ""
        side = item.get("side") or grid_order.side.value
        start_position = item.get("startPosition") or item.get("start_pos") or ""
        direction = item.get("dir") or item.get("direction") or ""
        fee = item.get("fee") or item.get("commission") or ""
        normalized_price = format(filled_price.normalize(), "f")
        normalized_amount = format(fill_amount.normalize(), "f")
        return (
            f"{grid_order.order_id}:{event_time}:{side}:{start_position}:{direction}:{fee}:"
            f"{normalized_price}:{normalized_amount}"
        )

    def _summarize_tradexyz_fill_item(self, item: Dict[str, Any]) -> str:
        """Return a compact diagnostic summary for one TradeXYZ fill payload."""
        interesting_keys = (
            "oid",
            "tid",
            "fillId",
            "fill_id",
            "tradeId",
            "hash",
            "txHash",
            "time",
            "timestamp",
            "side",
            "dir",
            "direction",
            "startPosition",
            "start_pos",
            "fee",
            "commission",
            "px",
            "avgPx",
            "price",
            "sz",
            "filledSz",
            "filled",
        )
        parts = [
            f"{key}={item.get(key)}"
            for key in interesting_keys
            if key in item and item.get(key) not in (None, "")
        ]
        return ", ".join(parts) if parts else "no-interesting-fields"

    def _string_or_none(self, value: Any) -> Optional[str]:
        """Normalize a value into a non-empty string key."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _safe_decimal(self, value: Any, default: Decimal = Decimal("0")) -> Decimal:
        """Convert a value into Decimal with a fallback default."""
        if value is None:
            return default
        try:
            return Decimal(str(value))
        except Exception:
            return default

    def _get_order_fill_tolerance(self) -> Decimal:
        """Return the fill-total tolerance derived from configured quantity precision."""
        precision = getattr(self.config, "quantity_precision", None)
        if precision is None:
            return Decimal("0.00000001")

        try:
            quantizer = Decimal("0.1") ** int(precision)
        except Exception:
            return Decimal("0.00000001")
        return quantizer / Decimal("2")

    def _to_timestamp(self, value: Any) -> float:
        """Convert datetime-like heartbeat values into unix timestamps."""
        if value is None:
            return 0.0
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return float(value)
        except Exception:
            return 0.0

    def _format_timestamp(self, value: Any) -> str:
        """Format a timestamp for logging."""
        timestamp = self._to_timestamp(value)
        if timestamp <= 0:
            return "unknown"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
