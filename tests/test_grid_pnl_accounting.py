import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from core.services.grid.coordinator.grid_coordinator import GridCoordinator
from core.services.grid.implementations.position_tracker_impl import PositionTrackerImpl
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus, GridType


def order(order_id, side, price, parent=None, status=GridOrderStatus.FILLED):
    grid_order = GridOrder(
        order_id=order_id,
        grid_id=1,
        side=side,
        price=Decimal(price),
        amount=Decimal("0.001"),
        status=status,
        created_at=datetime.now(),
        parent_order_id=parent,
    )
    if status == GridOrderStatus.FILLED:
        grid_order.mark_filled(Decimal(price), Decimal("0.001"))
    return grid_order


class TrackerRealizedPnlTests(unittest.TestCase):
    def tracker(self, grid_type):
        config = SimpleNamespace(grid_type=grid_type, fee_rate=Decimal("0"))
        return PositionTrackerImpl(config, SimpleNamespace())

    def test_long_reentry_buy_does_not_double_count_cycle(self):
        tracker = self.tracker(GridType.LONG)
        buy = Decimal("83200")
        tracker.record_filled_order(order("b1", GridOrderSide.BUY, buy))
        tracker.record_filled_order(order("s1", GridOrderSide.SELL, buy + 50, "b1"))
        tracker.record_filled_order(order("b2", GridOrderSide.BUY, buy, "s1"))
        tracker.record_filled_order(order("s2", GridOrderSide.SELL, buy + 50, "b2"))

        # Two round trips of 50 USD x 0.001 BTC, not three.
        self.assertEqual(tracker.get_realized_pnl(), Decimal("0.100"))

    def test_short_grid_realizes_on_buy_back_only(self):
        tracker = self.tracker(GridType.SHORT)
        sell = Decimal("83200")
        tracker.record_filled_order(order("s1", GridOrderSide.SELL, sell))
        tracker.record_filled_order(order("b1", GridOrderSide.BUY, sell - 50, "s1"))
        tracker.record_filled_order(order("s2", GridOrderSide.SELL, sell, "b1"))

        self.assertEqual(tracker.get_realized_pnl(), Decimal("0.050"))


class LotUnrealizedPnlTests(unittest.TestCase):
    def coordinator(self, pending):
        return SimpleNamespace(
            config=SimpleNamespace(grid_interval=Decimal("50"), reverse_order_grid_distance=1),
            engine=SimpleNamespace(get_pending_orders=lambda: pending),
        )

    def unrealized(self, coordinator, price, position, average_cost):
        return GridCoordinator._position_unrealized_profit(
            coordinator, Decimal(price), Decimal(position), Decimal(average_cost)
        )

    def test_held_lots_use_their_own_entries_not_exchange_average(self):
        # Lots bought at 83000 and 82500; the exchange average drifted lower
        # because TP fills of other lots never reduced it.
        pending = [
            order("tp1", GridOrderSide.SELL, "83050", "b1", GridOrderStatus.PENDING),
            order("tp2", GridOrderSide.SELL, "82550", "b2", GridOrderStatus.PENDING),
            order("entry", GridOrderSide.BUY, "81500", None, GridOrderStatus.PENDING),
        ]
        result = self.unrealized(self.coordinator(pending), "81600", "0.002", "81700")

        self.assertEqual(result, Decimal("-2.300"))

    def test_position_without_tp_uses_exchange_average(self):
        pending = [order("tp1", GridOrderSide.SELL, "83050", "b1", GridOrderStatus.PENDING)]
        result = self.unrealized(self.coordinator(pending), "81600", "0.003", "82000")

        # One lot at 83000, two uncovered units at the exchange average.
        self.assertEqual(result, Decimal("-1.400") + Decimal("-0.800"))

    def test_stale_tp_excess_falls_back_to_exchange_average(self):
        pending = [order("tp1", GridOrderSide.SELL, "83050", "b1", GridOrderStatus.PENDING)]
        result = self.unrealized(self.coordinator(pending), "81600", "0", "0")
        self.assertEqual(result, Decimal("0"))
        result = self.unrealized(self.coordinator(pending * 2), "81600", "0.001", "82000")
        self.assertEqual(result, Decimal("-0.400"))


if __name__ == "__main__":
    unittest.main()
