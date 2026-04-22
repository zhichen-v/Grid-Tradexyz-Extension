"""Grid execution engine with clean websocket and REST fallback handling."""

from __future__ import annotations

import asyncio
import time
import traceback
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

        exchange_id = getattr(exchange_adapter.config, "exchange_id", "unknown")
        self.logger.info(f"Grid execution engine initialized for {exchange_id}")

    async def initialize(self, config: GridConfig):
        """Initialize the engine and subscribe to exchange streams."""
        self.config = config
        self._expected_total_orders = config.grid_count
        self._last_ws_message_time = time.time()
        self._last_ws_check_time = 0.0

        if not self.exchange.is_connected():
            await self.exchange.connect()
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
    ) -> Optional[GridOrder]:
        """Place a single limit order and track it locally."""
        try:
            if self._shutting_down or not self._running:
                self.logger.warning(
                    f"Skip order placement while engine is stopping: "
                    f"{order.side.value} {order.amount}@{order.price}"
                )
                return None

            exchange_side = self._convert_order_side(order.side)
            create_kwargs = {
                "symbol": self.config.symbol,
                "side": exchange_side,
                "order_type": OrderType.LIMIT,
                "amount": order.amount,
                "price": order.price,
                "params": None,
            }
            if batch_mode and self._supports_batch_mode():
                create_kwargs["batch_mode"] = True

            exchange_order = await self.exchange.create_order(**create_kwargs)
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
            order.exchange_data = getattr(exchange_order, "raw_data", {}) or {}
            self._register_pending_order(order, order_id, client_id)

            self.logger.info(
                f"Order placed: {order.side.value} {order.amount}@{order.price} "
                f"(Grid {order.grid_id}, OrderID: {order.order_id})"
            )
            return order
        except Exception as exc:
            self.logger.error(f"Order placement failed: {exc}")
            order.mark_failed()
            raise

    async def place_market_order(self, side: GridOrderSide, amount: Decimal) -> None:
        """Place a market order, used mainly for position adjustment flows."""
        try:
            exchange_side = self._convert_order_side(side)
            exchange_order = await self.exchange.create_order(
                symbol=self.config.symbol,
                side=exchange_side,
                order_type=OrderType.MARKET,
                amount=amount,
                price=None,
                params=None,
            )
            order_id = getattr(exchange_order, "id", None) or getattr(
                exchange_order, "order_id", None
            )
            self.logger.info(
                f"Market order placed: {side.value} {amount}, OrderID: {order_id}"
            )
        except Exception as exc:
            self.logger.error(f"Market order placement failed: {exc}")
            raise

    async def place_batch_orders(
        self,
        orders: List[GridOrder],
        max_retries: int = 2,
    ) -> List[GridOrder]:
        """Place orders in batches, with retries for failed items."""
        if not orders:
            return []

        total_orders = len(orders)
        batch_size = 50
        successful_orders: List[GridOrder] = []
        failed_orders: List[Tuple[GridOrder, str]] = []

        self.logger.info(f"Starting batch order placement: total={total_orders}")

        for start in range(0, total_orders, batch_size):
            batch = orders[start:start + batch_size]
            results = await self._execute_batch(batch)

            for order, result in zip(batch, results):
                if isinstance(result, GridOrder):
                    successful_orders.append(result)
                elif result is None:
                    failed_orders.append((order, "order placement returned no result"))
                else:
                    failed_orders.append((order, str(result)))

            if start + batch_size < total_orders:
                await asyncio.sleep(0.5)

        for attempt in range(1, max_retries + 1):
            if not failed_orders:
                break

            self.logger.warning(
                f"Retrying failed orders: attempt={attempt}, count={len(failed_orders)}"
            )
            await asyncio.sleep(1.0)

            retry_orders = [order for order, _ in failed_orders]
            failed_orders = []
            results = await self._execute_batch(retry_orders)

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

        await asyncio.sleep(3)
        await self._sync_order_status_after_batch()
        return successful_orders

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel one order and mark it as expected in the local cache."""
        try:
            cache_key, grid_order = self._find_cached_order(order_id)
            if grid_order:
                for key in self._pending_keys_for_order(grid_order):
                    self._expected_cancellations.add(key)
            else:
                self._expected_cancellations.add(order_id)

            await self.exchange.cancel_order(order_id, self.config.symbol)

            if grid_order:
                grid_order.mark_cancelled()
                self._clear_pending_order_refs(cache_key, order_id)

            self.logger.info(f"Order cancelled successfully: {order_id}")
            return True
        except Exception as exc:
            self.logger.error(f"Order cancel failed for {order_id}: {exc}")
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders for the configured symbol."""
        try:
            if self.config is None:
                return 0

            for key in list(self._pending_orders.keys()):
                self._expected_cancellations.add(key)

            cancelled_orders = await self.exchange.cancel_all_orders(self.config.symbol)
            cancelled_count = len(cancelled_orders) if cancelled_orders else len(
                self.get_pending_orders()
            )

            for grid_order in self.get_pending_orders():
                grid_order.mark_cancelled()

            self._pending_orders.clear()
            self.logger.info(f"All open orders cancelled: count={cancelled_count}")
            return cancelled_count
        except Exception as exc:
            self.logger.error(f"Cancel-all failed: {exc}")
            return 0

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
        self._running = True
        self._start_smart_monitor()
        self._start_order_health_check()
        self.logger.info("Grid execution engine started")

    def begin_shutdown(self):
        """Enter shutdown mode and stop new recovery work."""
        if self._shutting_down:
            return

        self._shutting_down = True
        self._running = False
        self.logger.info("Grid execution engine entered shutdown mode")

    async def stop(self):
        """Stop background monitoring tasks."""
        self._running = False

        tasks = [self._health_check_task, self._polling_task]
        for task in tasks:
            if task and not task.done():
                task.cancel()

        for task in tasks:
            if task and not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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
            for exchange_order in exchange_orders:
                order_id = self._string_or_none(getattr(exchange_order, "id", None))
                client_id = self._string_or_none(
                    getattr(exchange_order, "client_id", None)
                )
                if order_id:
                    exchange_keys.add(order_id)
                if client_id:
                    exchange_keys.add(client_id)

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
                if any(alias in exchange_keys for alias in aliases):
                    continue

                if any(alias in self._expected_cancellations for alias in aliases):
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

                lookup_id = grid_order.order_id or cache_key
                try:
                    exchange_order = await self.exchange.get_order(
                        lookup_id,
                        self.config.symbol,
                    )
                except Exception:
                    continue

                status = (
                    exchange_order.status.value.lower()
                    if getattr(exchange_order, "status", None)
                    else "unknown"
                )
                if status == "filled":
                    filled_price = exchange_order.average or exchange_order.price or grid_order.price
                    filled_amount = exchange_order.filled or grid_order.amount
                    grid_order.mark_filled(filled_price, filled_amount)
                    self._clear_pending_order_refs(*aliases)
                    filled_in_sync.append(grid_order)
                    removed_count += 1
                elif status in {"canceled", "cancelled", "rejected", "expired"}:
                    grid_order.mark_cancelled()
                    self._clear_pending_order_refs(*aliases)
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

                try:
                    grid_side = (
                        GridOrderSide.BUY
                        if side.value.lower() == "buy"
                        else GridOrderSide.SELL
                    )
                    grid_order = GridOrder(
                        order_id=order_id,
                        grid_id=self.config.get_grid_index_by_price(price),
                        side=grid_side,
                        price=price,
                        amount=amount,
                        status=GridOrderStatus.PENDING,
                        created_at=datetime.now(),
                    )
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

        if status in {"FILLED", "CLOSED"}:
            filled_price = update_data.average or update_data.price or grid_order.price
            filled_amount = update_data.filled or grid_order.amount
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
        order_amount = grid_order.amount or Decimal("0")
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
        self._clear_pending_order_refs(*keys)
        if self._consume_expected_cancellation(*keys):
            self.logger.info(
                f"Expected cancellation confirmed: "
                f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
            )
            return

        self.logger.warning(
            f"{log_prefix}: unexpected grid cancellation detected, "
            f"grid_id={grid_order.grid_id}, order_id={grid_order.order_id}"
        )
        await self._restore_cancelled_grid_order(grid_order, grid_order.order_id)

    async def _sync_order_status_after_batch(self):
        """Refresh pending-order state after batch placement."""
        try:
            exchange_orders = await self.exchange.get_open_orders(self.config.symbol)
            await self._sync_orders_from_exchange(exchange_orders)
        except Exception as exc:
            self.logger.warning(f"Post-batch order sync failed: {exc}")

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
        for key in list(candidate_keys):
            if key in self._expected_cancellations:
                self._expected_cancellations.remove(key)
                consumed = True
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
    ) -> None:
        """Re-place an unexpectedly cancelled order to keep the grid intact."""
        if self._shutting_down or not self._running:
            self.logger.info(
                f"Skip order restoration while engine is stopping: source_order_id={order_id}"
            )
            return

        replacement_order = GridOrder(
            order_id="",
            grid_id=grid_order.grid_id,
            side=grid_order.side,
            price=grid_order.price,
            amount=grid_order.amount,
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
        )

        try:
            placed_order = await self.place_order(replacement_order)
            if placed_order:
                self._replace_state_order(order_id, placed_order)
                self.logger.info(
                    "Grid order restored after unexpected cancellation: "
                    f"grid_id={placed_order.grid_id}, side={placed_order.side.value}, "
                    f"amount={placed_order.amount}, price={placed_order.price}, "
                    f"order_id={placed_order.order_id}"
                )
        except Exception as exc:
            self.logger.error(
                "Failed to restore cancelled grid order: "
                f"grid_id={grid_order.grid_id}, source_order_id={order_id}, error={exc}"
            )

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

    async def _execute_batch(self, orders: List[GridOrder]) -> List[Any]:
        """Execute one placement batch, serially when required by the exchange."""
        if self._supports_batch_mode():
            results = []
            for order in orders:
                try:
                    results.append(await self.place_order(order, batch_mode=True))
                except Exception as exc:
                    results.append(exc)
            return results

        tasks = [self.place_order(order) for order in orders]
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

        cumulative_filled = Decimal("0")
        for fill_info in fills_by_id.values():
            cumulative_filled += self._safe_decimal(fill_info.get("amount"), Decimal("0"))
        tracking["cumulative_filled"] = str(cumulative_filled)
        tracking["last_fill_price"] = normalized_price
        return True

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
