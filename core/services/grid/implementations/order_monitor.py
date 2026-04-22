"""
Order monitor with REST API polling fallback.

When exchange websocket order updates are unavailable or unreliable, this
component polls the REST API to detect order fills.
"""

import asyncio
from typing import Dict, Set, Callable, List, Optional
from decimal import Decimal
from datetime import datetime

from ....logging import get_logger
from ....adapters.exchanges import ExchangeInterface, OrderStatus
from ..models import GridOrder, GridOrderStatus


class OrderMonitor:
    """
    Poll-based order monitor.

    Responsibilities:
    1. Poll the exchange for live order status.
    2. Detect filled orders.
    3. Trigger fill callbacks.
    """

    def __init__(
        self,
        exchange: ExchangeInterface,
        symbol: str,
        poll_interval: float = 2.0,
    ):
        """
        Initialize the order monitor.

        Args:
            exchange: Exchange adapter instance.
            symbol: Trading symbol.
            poll_interval: Polling interval in seconds.
        """
        self.logger = get_logger(__name__)
        self.exchange = exchange
        self.symbol = symbol
        self.poll_interval = poll_interval

        # order_id -> GridOrder
        self._monitored_orders: Dict[str, GridOrder] = {}

        # Fill callbacks
        self._fill_callbacks: List[Callable] = []

        # Runtime state
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Statistics
        self._total_checks = 0
        self._total_fills = 0
        self._last_check_time: Optional[datetime] = None

        self.logger.info(
            f"Order monitor initialized: symbol={symbol}, "
            f"poll_interval={poll_interval}s"
        )

    def add_order(self, order: GridOrder):
        """
        Add one order to the monitor.

        Args:
            order: Grid order to track.
        """
        if not order.order_id or order.order_id == "pending":
            self.logger.warning(f"Order ID is invalid; skip monitoring: {order.order_id}")
            return

        self._monitored_orders[order.order_id] = order
        self.logger.debug(
            f"Added order to monitor: {order.order_id} "
            f"(Grid {order.grid_id}, {order.side.value} {order.amount}@{order.price})"
        )

    def remove_order(self, order_id: str):
        """
        Remove one order from the monitor.

        Args:
            order_id: Order ID.
        """
        if order_id in self._monitored_orders:
            del self._monitored_orders[order_id]
            self.logger.debug(f"Removed order from monitor: {order_id}")

    def add_fill_callback(self, callback: Callable):
        """
        Register one fill callback.

        Args:
            callback: Callback that receives the filled GridOrder.
        """
        self._fill_callbacks.append(callback)
        self.logger.debug(f"Added fill callback: {callback}")

    async def start(self):
        """Start monitoring."""
        if self._running:
            self.logger.warning("Order monitor is already running")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        self.logger.info("Order monitor started")

    async def stop(self):
        """Stop monitoring."""
        if not self._running:
            return

        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        self.logger.info("Order monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop."""
        self.logger.info(
            f"Order monitor loop started; polling every {self.poll_interval}s"
        )

        while self._running:
            try:
                await self._check_orders()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Order monitor loop failed: {e}")
                await asyncio.sleep(self.poll_interval)

        self.logger.info("Order monitor loop exited")

    async def _check_orders(self):
        """Poll the exchange and detect newly filled orders."""
        if not self._monitored_orders:
            return

        self._total_checks += 1
        self._last_check_time = datetime.now()

        try:
            open_orders = await self.exchange.get_open_orders(self.symbol)
            open_order_ids = {order.id for order in open_orders if order.id}

            # Orders missing from the open-order list may have been filled.
            filled_order_ids: Set[str] = set()

            for order_id, grid_order in list(self._monitored_orders.items()):
                if order_id not in open_order_ids:
                    filled_order_ids.add(order_id)

            if filled_order_ids:
                await self._process_filled_orders(filled_order_ids)

            if self._total_checks % 30 == 0:
                self.logger.debug(
                    f"Order monitor stats: "
                    f"checks={self._total_checks}, "
                    f"fills={self._total_fills}, "
                    f"active={len(self._monitored_orders)} orders"
                )

        except Exception as e:
            self.logger.error(f"Failed to poll order status: {e}")

    async def _process_filled_orders(self, filled_order_ids: Set[str]):
        """
        Process orders that are no longer open on the exchange.

        Args:
            filled_order_ids: Filled order IDs.
        """
        for order_id in filled_order_ids:
            if order_id not in self._monitored_orders:
                continue

            grid_order = self._monitored_orders[order_id]

            try:
                exchange_order = await self.exchange.get_order(order_id, self.symbol)

                if exchange_order.status == OrderStatus.FILLED:
                    filled_price = exchange_order.average or exchange_order.price or grid_order.price
                    filled_amount = exchange_order.filled or grid_order.amount

                    grid_order.mark_filled(filled_price, filled_amount)

                    self.logger.info(
                        f"Order filled: {grid_order.side.value} "
                        f"{filled_amount}@{filled_price} "
                        f"(Grid {grid_order.grid_id}, Order {order_id})"
                    )

                    del self._monitored_orders[order_id]
                    self._total_fills += 1

                    await self._trigger_fill_callbacks(grid_order)

                elif exchange_order.status == OrderStatus.CANCELED:
                    self.logger.warning(
                        f"Order was canceled: {order_id} "
                        f"(Grid {grid_order.grid_id})"
                    )
                    del self._monitored_orders[order_id]

                else:
                    self.logger.debug(
                        f"Order status is still {exchange_order.status.value}: {order_id}"
                    )

            except Exception as e:
                self.logger.error(
                    f"Failed to process potential fill for order {order_id}: {e}"
                )

    async def _trigger_fill_callbacks(self, filled_order: GridOrder):
        """
        Trigger registered fill callbacks.

        Args:
            filled_order: Filled grid order.
        """
        for callback in self._fill_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(filled_order)
                else:
                    callback(filled_order)
            except Exception as e:
                self.logger.error(f"Fill callback failed: {e}")

    def get_statistics(self) -> Dict:
        """
        Return monitor statistics.

        Returns:
            Statistics dictionary.
        """
        return {
            "total_checks": self._total_checks,
            "total_fills": self._total_fills,
            "monitored_orders": len(self._monitored_orders),
            "last_check_time": self._last_check_time,
            "poll_interval": self.poll_interval,
            "is_running": self._running,
        }

    def __repr__(self) -> str:
        return (
            f"OrderMonitor("
            f"symbol={self.symbol}, "
            f"monitored={len(self._monitored_orders)}, "
            f"checks={self._total_checks}, "
            f"fills={self._total_fills})"
        )
