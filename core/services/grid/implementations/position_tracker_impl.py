"""
Position tracker implementation.

Tracks grid position state, realized/unrealized PnL, trade history, and the
summary statistics shown by the runtime UI.
"""

from typing import Dict, List, Deque, Optional
from decimal import Decimal
from datetime import datetime, timedelta
from collections import deque

from ....logging import get_logger
from ..interfaces.position_tracker import IPositionTracker
from ..models import (
    GridOrder, GridStatistics, GridMetrics,
    GridConfig, GridState
)


class PositionTrackerImpl(IPositionTracker):
    """
    Position tracker implementation.

    Responsibilities:
    1. Track current position and average cost.
    2. Calculate realized and unrealized PnL.
    3. Store trade history.
    4. Produce summary statistics.
    """

    def __init__(self, config: GridConfig, grid_state: GridState):
        """
        Initialize the position tracker.

        Args:
            config: Grid configuration.
            grid_state: Shared grid state.
        """
        self.logger = get_logger(__name__)
        self.config = config
        self.state = grid_state

        # Position state
        self.current_position = Decimal('0')
        self.position_cost = Decimal('0')
        self.average_cost = Decimal('0')

        # PnL state
        self.realized_pnl = Decimal('0')
        self.total_fees = Decimal('0')

        # Filled-trade history
        self.trade_history: Deque[Dict] = deque(maxlen=1000)
        self._filled_order_registry: Dict[str, Dict[str, object]] = {}

        # Statistics
        self.buy_count = 0
        self.sell_count = 0
        self.completed_cycles = 0
        self._completed_profit_cycle_count = 0
        self._completed_cycle_profit_total = Decimal('0')

        # Balances
        self.available_balance = Decimal('0')
        self.frozen_balance = Decimal('0')

        # Runtime timestamps
        self.start_time = datetime.now()
        self.last_trade_time = datetime.now()

        self.logger.info("Position tracker initialized")

    def record_filled_order(self, order: GridOrder):
        """
        Record a filled order into trade history and PnL statistics.

        Design notes:
        - Position size is not updated here. The authoritative position comes
          from `position_monitor` / REST sync.
        - Trade history is tracked locally so the UI can show recent fills and
          completed cycles.
        - Statistics here focus on fill history, realized PnL, and fees.

        This method intentionally does not update:
        - `current_position`
        - `average_cost`
        - `position_cost`

        Those fields are synchronized from exchange-backed position snapshots.

        This method does update:
        - `realized_pnl`
        - `total_fees`

        Args:
            order: Filled order.
        """
        if not order.is_filled():
            self.logger.warning(
                f"Order {order.order_id} is not filled; skipping position record"
            )
            return

        filled_price = order.filled_price or order.price
        filled_amount = order.filled_amount or order.amount

        # Profit is derived from the paired parent fill when available.
        profit = None
        cycle_profit = None
        parent_fill = self._get_parent_fill(order)

        # Realized PnL is recognized when the fill closes a previously opened leg.
        if order.is_buy_order():
            self.buy_count += 1
            if parent_fill and parent_fill['side'] == 'sell':
                profit = (parent_fill['price'] - filled_price) * filled_amount
                cycle_profit = profit
                self.realized_pnl += profit

                self.logger.debug(
                    f"Recorded reverse buy fill: {filled_amount}@{filled_price}, "
                    f"parent_price={parent_fill['price']}, profit={profit}"
                )
            self.logger.debug(
                f"Recorded buy fill: {filled_amount}@{filled_price}"
            )
        else:
            self.sell_count += 1

            # Prefer parent-fill pairing, then fall back to synced average cost.
            if parent_fill and parent_fill['side'] == 'buy':
                profit = (filled_price - parent_fill['price']) * filled_amount
                cycle_profit = profit
                self.realized_pnl += profit

                self.logger.debug(
                    f"Recorded reverse sell fill: {filled_amount}@{filled_price}, "
                    f"parent_price={parent_fill['price']}, profit={profit}"
                )
            elif self.current_position > 0 and self.average_cost > 0:
                # Long-side sell realization.
                sell_cost = self.average_cost * filled_amount
                sell_value = filled_price * filled_amount
                profit = sell_value - sell_cost
                self.realized_pnl += profit

                self.logger.debug(
                    f"Recorded sell fill: {filled_amount}@{filled_price}, "
                    f"avg_cost={self.average_cost}, pnl={profit}"
                )
            elif self.current_position < 0 and self.average_cost > 0:
                # Short-side sell fill extends the short leg and does not
                # realize PnL by itself in this tracker path.
                self.logger.debug(
                    f"Recorded short-side sell fill: {filled_amount}@{filled_price}"
                )

        self.completed_cycles = min(self.buy_count, self.sell_count)

        # Always track exchange fees from the fill notional.
        fee = filled_price * filled_amount * self.config.fee_rate
        self.total_fees += fee

        # Only completed reverse-profit fills contribute to avg cycle profit.
        if cycle_profit is not None:
            self._completed_profit_cycle_count += 1
            self._completed_cycle_profit_total += cycle_profit

        self._store_filled_order(order, filled_price, filled_amount)
        self._record_trade(order, filled_price, filled_amount, profit)

        self.last_trade_time = datetime.now()

        self.logger.info(
            f"Recorded filled order: {order.side.value} {filled_amount}@{filled_price}, "
            f"realized_pnl={self.realized_pnl}, total_fees={self.total_fees} "
            f"(position synced by REST)"
        )

    def _record_trade(self, order: GridOrder, price: Decimal, amount: Decimal, profit: Decimal = None):
        """
        Append one trade record to recent history.

        Args:
            order: Grid order.
            price: Filled price.
            amount: Filled amount.
            profit: Realized profit for this fill, when applicable.
        """
        trade_record = {
            'time': order.filled_at or datetime.now(),
            'order_id': order.order_id,
            'grid_id': order.grid_id,
            'side': order.side.value,
            'price': float(price),
            'amount': float(amount),
            'value': float(price * amount),
            'profit': float(profit) if profit is not None else None,
            'position_after': float(self.current_position),
            'realized_pnl': float(self.realized_pnl)
        }

        self.trade_history.append(trade_record)

    def _get_parent_fill(self, order: GridOrder) -> Optional[Dict[str, object]]:
        """Return the stored parent fill, if this order was created from one."""
        if not order.parent_order_id:
            return None

        parent_fill = self._filled_order_registry.get(order.parent_order_id)
        if not parent_fill:
            self.logger.debug(
                f"Parent fill not found for parent_order_id={order.parent_order_id}"
            )
            return None

        return parent_fill

    def _store_filled_order(self, order: GridOrder, price: Decimal, amount: Decimal):
        """Store a filled order so reverse fills can pair against it later."""
        self._filled_order_registry[order.order_id] = {
            'side': order.side.value,
            'price': price,
            'amount': amount,
            'grid_id': order.grid_id,
        }

    def get_current_position(self) -> Decimal:
        """
        Return the current position size.

        Returns:
            Signed position size. Positive is long, negative is short.
        """
        return self.current_position

    def get_average_cost(self) -> Decimal:
        """
        Return the average position cost.

        Returns:
            Average cost.
        """
        return self.average_cost

    def calculate_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """
        Calculate unrealized PnL.

        Args:
            current_price: Current market price.

        Returns:
            Unrealized PnL.
        """
        if self.current_position == 0:
            return Decimal('0')

        # Unrealized PnL = (current price - average cost) * position.
        unrealized_pnl = (current_price - self.average_cost) * self.current_position

        return unrealized_pnl

    def get_realized_pnl(self) -> Decimal:
        """
        Return realized PnL.

        Returns:
            Realized PnL.
        """
        return self.realized_pnl

    def get_total_pnl(self, current_price: Decimal) -> Decimal:
        """
        Return total PnL.

        Args:
            current_price: Current market price.

        Returns:
            Realized plus unrealized PnL.
        """
        unrealized = self.calculate_unrealized_pnl(current_price)
        return self.realized_pnl + unrealized

    def get_statistics(self) -> GridStatistics:
        """
        Build tracker statistics.

        Returns:
            Grid statistics snapshot.
        """
        current_price = self.state.current_price or self.config.get_first_order_price()

        unrealized_pnl = self.calculate_unrealized_pnl(current_price)
        total_pnl = self.realized_pnl + unrealized_pnl
        net_profit = total_pnl - self.total_fees

        initial_capital = self.config.order_amount * self.config.grid_count * current_price
        profit_rate = (net_profit / initial_capital * 100) if initial_capital > 0 else Decimal('0')

        total_balance = self.available_balance + self.frozen_balance
        capital_utilization = (
            self.frozen_balance / total_balance * 100) if total_balance > 0 else 0.0

        running_time = datetime.now() - self.start_time

        statistics = GridStatistics(
            grid_count=self.config.grid_count,
            grid_interval=self.config.grid_interval,
            price_range=(self.config.lower_price, self.config.upper_price),
            current_price=current_price,
            current_grid_id=self.state.current_grid_id or 1,
            current_position=self.current_position,
            average_cost=self.average_cost,
            pending_buy_orders=self.state.pending_buy_orders,
            pending_sell_orders=self.state.pending_sell_orders,
            total_pending_orders=self.state.pending_buy_orders + self.state.pending_sell_orders,
            filled_buy_count=self.buy_count,
            filled_sell_count=self.sell_count,
            completed_cycles=self.completed_cycles,
            realized_profit=self.realized_pnl,
            unrealized_profit=unrealized_pnl,
            total_profit=total_pnl,
            total_fees=self.total_fees,
            net_profit=net_profit,
            profit_rate=profit_rate,
            grid_utilization=self.state.get_grid_utilization(),
            spot_balance=self.available_balance,  # Reused as the available balance field.
            collateral_balance=Decimal('0'),  # This tracker does not maintain collateral snapshots.
            order_locked_balance=self.frozen_balance,  # Balance locked by open orders.
            total_balance=total_balance,
            capital_utilization=capital_utilization,
            running_time=running_time,
            last_trade_time=self.last_trade_time
        )

        statistics.avg_cycle_profit = (
            self._completed_cycle_profit_total / Decimal(str(self._completed_profit_cycle_count))
            if self._completed_profit_cycle_count > 0
            else Decimal('0')
        )

        return statistics

    def get_metrics(self) -> GridMetrics:
        """
        Build higher-level performance metrics.

        Returns:
            Grid metrics snapshot.
        """
        metrics = GridMetrics()

        current_price = self.state.current_price or self.config.get_first_order_price()

        metrics.total_profit = self.get_total_pnl(current_price)

        initial_capital = self.config.order_amount * self.config.grid_count * current_price
        if initial_capital > 0:
            metrics.profit_rate = (metrics.total_profit / initial_capital) * 100

        metrics.total_trades = self.buy_count + self.sell_count
        metrics.win_trades = self.completed_cycles  # Completed cycles count as successful round trips.
        metrics.loss_trades = 0  # Grid cycles are not classified into separate loss trades here.

        if metrics.total_trades > 0:
            metrics.win_rate = (metrics.win_trades / (metrics.total_trades / 2)) * 100

        running_days = (datetime.now() - self.start_time).days
        if running_days > 0:
            metrics.daily_profit = metrics.total_profit / Decimal(str(running_days))
            metrics.running_days = running_days

        if self._completed_profit_cycle_count > 0:
            metrics.avg_profit_per_trade = self._completed_cycle_profit_total / \
                Decimal(str(self._completed_profit_cycle_count))

        metrics.total_fees = self.total_fees
        if metrics.total_profit != 0:
            metrics.fee_rate = (self.total_fees / abs(metrics.total_profit)) * 100

        metrics.max_position = abs(self.current_position)  # Current peak snapshot only.
        metrics.avg_position = abs(self.current_position)

        return metrics

    def get_trade_history(self, limit: int = 10) -> List[Dict]:
        """
        Return recent trade history.

        Args:
            limit: Maximum number of trade records.

        Returns:
            Trade history records.
        """
        history_list = list(self.trade_history)
        return history_list[-limit:] if len(history_list) > limit else history_list

    def update_balance(self, available: Decimal, frozen: Decimal):
        """
        Update local balance snapshots.

        Args:
            available: Available balance.
            frozen: Locked balance.
        """
        self.available_balance = available
        self.frozen_balance = frozen

    def reset(self):
        """Reset the tracker state."""
        self.current_position = Decimal('0')
        self.position_cost = Decimal('0')
        self.average_cost = Decimal('0')
        self.realized_pnl = Decimal('0')
        self.total_fees = Decimal('0')
        self.trade_history.clear()
        self._filled_order_registry.clear()
        self.buy_count = 0
        self.sell_count = 0
        self.completed_cycles = 0
        self._completed_profit_cycle_count = 0
        self._completed_cycle_profit_total = Decimal('0')
        self.start_time = datetime.now()
        self.last_trade_time = datetime.now()

        self.logger.info("Position tracker reset")

    def sync_initial_position(self, position: Decimal, entry_price: Decimal):
        """
        Sync the initial exchange-backed position into the tracker.

        The position monitor uses a REST snapshot to seed tracker state so the
        runtime starts from the real live position instead of assuming flat.

        High-level flow:
        1. `position_monitor` fetches the live position from REST.
        2. The result is synchronized into the tracker.
        3. Subsequent UI and statistics read from the tracker snapshot.

        Guarantees:
        - Position state comes from exchange data, not guessed fills.
        - Prevents websocket/REST startup drift.
        - Avoids false early exposure reporting.

        Args:
            position: Signed position size.
            entry_price: Average entry price.
        """
        old_position = self.current_position
        self.current_position = position
        self.average_cost = entry_price

        if position != 0:
            self.position_cost = abs(position) * entry_price
        else:
            self.position_cost = Decimal('0')

        # Log at INFO only when the synced position actually changed.
        if old_position != position:
            self.logger.info(
                f"Initial position synced: {old_position} -> {position}, "
                f"avg_cost=${entry_price}, position_cost=${self.position_cost}"
            )
        else:
            self.logger.debug(
                f"Initial position unchanged: size={position}, "
                f"avg_cost=${entry_price}, position_cost=${self.position_cost}"
            )

    def __repr__(self) -> str:
        return (
            f"PositionTracker(position={self.current_position}, "
            f"avg_cost={self.average_cost}, "
            f"realized_pnl={self.realized_pnl})"
        )
