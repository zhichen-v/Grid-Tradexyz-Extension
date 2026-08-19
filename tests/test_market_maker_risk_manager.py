import unittest
from decimal import Decimal

from core.adapters.exchanges.models import OrderSide
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.models import (
    ManagedOrder,
    MarketMetadata,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)
from core.services.market_maker.risk_manager import RiskManager


class RiskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("1"),
            soft_position_ratio=Decimal("0.5"),
            hard_position_ratio=Decimal("0.8"),
        )
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("0"),
        )
        self.manager = RiskManager(self.config)

    @staticmethod
    def position(size: str, received: float = 100.0) -> PositionSnapshot:
        return PositionSnapshot(
            symbol="BTC",
            signed_size=Decimal(size),
            entry_price=None,
            unrealized_pnl=None,
            received_monotonic=received,
        )

    def evaluate(self, size: str, orders=()):
        return self.manager.evaluate(
            self.position(size),
            orders,
            self.metadata,
            now_monotonic=100.0,
        )

    def test_flat_allows_both_sides_and_calculates_worst_case(self) -> None:
        decision = self.evaluate("0")

        self.assertTrue(decision.safe)
        self.assertTrue(decision.allow_buy)
        self.assertTrue(decision.allow_sell)
        self.assertEqual(decision.buy_amount, Decimal("0.2"))
        self.assertEqual(decision.sell_amount, Decimal("0.2"))
        self.assertEqual(decision.worst_long, Decimal("0.2"))
        self.assertEqual(decision.worst_short, Decimal("-0.2"))

    def test_soft_long_scales_only_risk_increasing_buy(self) -> None:
        decision = self.evaluate("0.65")

        self.assertEqual(decision.buy_amount, Decimal("0.1"))
        self.assertEqual(decision.sell_amount, Decimal("0.2"))
        self.assertEqual(decision.runtime_state, RuntimeState.ACTIVE)

    def test_soft_short_scales_only_risk_increasing_sell(self) -> None:
        decision = self.evaluate("-0.65")

        self.assertEqual(decision.buy_amount, Decimal("0.2"))
        self.assertEqual(decision.sell_amount, Decimal("0.1"))

    def test_hard_long_only_allows_reduce_only_sell(self) -> None:
        decision = self.evaluate("0.8")

        self.assertIsNone(decision.buy_amount)
        self.assertEqual(decision.sell_amount, Decimal("0.2"))
        self.assertTrue(decision.sell_reduce_only)
        self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)

    def test_hard_short_only_allows_reduce_only_buy(self) -> None:
        decision = self.evaluate("-0.8")

        self.assertEqual(decision.buy_amount, Decimal("0.2"))
        self.assertTrue(decision.buy_reduce_only)
        self.assertIsNone(decision.sell_amount)
        self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)

    def test_absolute_max_long_and_short_only_reduce(self) -> None:
        long_decision = self.evaluate("1")
        short_decision = self.evaluate("-1")

        self.assertTrue(long_decision.sell_reduce_only)
        self.assertIsNone(long_decision.buy_amount)
        self.assertTrue(short_decision.buy_reduce_only)
        self.assertIsNone(short_decision.sell_amount)
        self.assertIn("absolute", long_decision.reason)

    def test_live_bid_uses_all_extra_capacity_but_keeps_slot_target(self) -> None:
        decision = self.evaluate(
            "0", (self.order(OrderSide.BUY, OrderSlotState.LIVE, "1"),)
        )

        self.assertEqual(decision.buy_capacity, Decimal("0"))
        self.assertEqual(decision.buy_amount, Decimal("0.2"))
        self.assertEqual(decision.worst_long, Decimal("1"))
        self.assertTrue(decision.allow_sell)

    def test_canceling_and_uncertain_orders_still_use_capacity(self) -> None:
        states = (
            OrderSlotState.CANCELING,
            OrderSlotState.UNCERTAIN_SUBMISSION,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        )
        for state in states:
            with self.subTest(state=state):
                decision = self.evaluate(
                    "0", (self.order(OrderSide.BUY, state, "0.9"),)
                )
                self.assertEqual(decision.buy_capacity, Decimal("0.1"))
                self.assertEqual(decision.buy_amount, Decimal("0.2"))
                self.assertEqual(decision.worst_long, Decimal("0.9"))

    def test_uncertainty_flag_keeps_even_terminal_order_in_exposure(self) -> None:
        order = self.order(OrderSide.BUY, OrderSlotState.TERMINAL, "0.9")
        order.cancellation_uncertain = True
        decision = self.evaluate("0", (order,))

        self.assertEqual(decision.buy_capacity, Decimal("0.1"))
        self.assertEqual(decision.buy_amount, Decimal("0.2"))
        self.assertEqual(decision.worst_long, Decimal("0.9"))

    def test_full_capacity_live_slots_keep_same_targets_across_cycles(self) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("0.2"),
        )
        manager = RiskManager(config)
        position = self.position("0")

        initial = manager.evaluate(
            position, (), self.metadata, now_monotonic=100.0
        )
        live = (
            self.order(OrderSide.BUY, OrderSlotState.LIVE, "0.2"),
            self.order(OrderSide.SELL, OrderSlotState.LIVE, "0.2"),
        )
        next_cycle = manager.evaluate(
            position, live, self.metadata, now_monotonic=100.0
        )

        self.assertEqual(initial.buy_amount, Decimal("0.2"))
        self.assertEqual(initial.sell_amount, Decimal("0.2"))
        self.assertEqual(next_cycle.buy_capacity, Decimal("0"))
        self.assertEqual(next_cycle.sell_capacity, Decimal("0"))
        self.assertEqual(next_cycle.buy_amount, initial.buy_amount)
        self.assertEqual(next_cycle.sell_amount, initial.sell_amount)
        self.assertEqual(next_cycle.worst_long, Decimal("0.2"))
        self.assertEqual(next_cycle.worst_short, Decimal("-0.2"))

    def test_reduce_only_live_order_does_not_increase_worst_case(self) -> None:
        order = self.order(OrderSide.BUY, OrderSlotState.LIVE, "1")
        order.reduce_only = True
        decision = self.evaluate("0", (order,))

        self.assertEqual(decision.buy_capacity, Decimal("1"))
        self.assertEqual(decision.buy_amount, Decimal("0.2"))
        self.assertEqual(decision.worst_long, Decimal("0.2"))

    def test_stale_position_disables_both_sides(self) -> None:
        decision = self.manager.evaluate(
            self.position("0", received=89.0),
            (),
            self.metadata,
            now_monotonic=100.0,
        )

        self.assertFalse(decision.safe)
        self.assertFalse(decision.allow_buy)
        self.assertFalse(decision.allow_sell)
        self.assertEqual(decision.runtime_state, RuntimeState.PAUSED_POSITION)
        self.assertIn("stale", decision.reason)

    def test_unknown_position_disables_both_sides(self) -> None:
        decision = self.manager.evaluate(
            None, (), self.metadata, now_monotonic=100.0
        )

        self.assertFalse(decision.safe)
        self.assertEqual(decision.runtime_state, RuntimeState.PAUSED_POSITION)
        self.assertIsNone(decision.buy_amount)
        self.assertIsNone(decision.sell_amount)

    def test_future_position_timestamp_disables_both_sides(self) -> None:
        decision = self.manager.evaluate(
            self.position("0", received=101.0),
            (),
            self.metadata,
            now_monotonic=100.0,
        )

        self.assertFalse(decision.safe)
        self.assertEqual(decision.runtime_state, RuntimeState.PAUSED_POSITION)
        self.assertIn("future", decision.reason)

    def test_unknown_order_state_fails_closed(self) -> None:
        order = self.order(OrderSide.BUY, OrderSlotState.LIVE, "1")
        order.state = "mystery"

        decision = self.evaluate("0", (order,))

        self.assertFalse(decision.safe)
        self.assertEqual(decision.runtime_state, RuntimeState.PAUSED_ORDER_STATE)
        self.assertIsNone(decision.buy_amount)

    def test_rounding_below_minimum_disables_side(self) -> None:
        manager = RiskManager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.15"),
                max_position=Decimal("1"),
            )
        )
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.2"),
            min_quote_amount=Decimal("0"),
        )

        decision = manager.evaluate(
            self.position("0"), (), metadata, now_monotonic=100.0
        )

        self.assertIsNone(decision.buy_amount)
        self.assertIsNone(decision.sell_amount)

    @staticmethod
    def order(
        side: OrderSide,
        state: OrderSlotState,
        remaining: str,
    ) -> ManagedOrder:
        return ManagedOrder(
            side=side,
            state=state,
            order_id="order-1",
            client_id="mm-order-1",
            price=Decimal("100"),
            amount=Decimal(remaining),
            remaining=Decimal(remaining),
            reduce_only=False,
            created_monotonic=90.0,
            updated_monotonic=99.0,
        )


if __name__ == "__main__":
    unittest.main()
