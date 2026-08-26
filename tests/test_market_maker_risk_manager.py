import unittest
from dataclasses import replace
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

    def test_single_side_mode_limits_targets_and_worst_exposure(self) -> None:
        for quote_mode, expected_buy, expected_sell, worst_long, worst_short in (
            ("bid_only", Decimal("0.2"), None, Decimal("0.2"), Decimal("0")),
            ("ask_only", None, Decimal("0.2"), Decimal("0"), Decimal("-0.2")),
        ):
            with self.subTest(quote_mode=quote_mode):
                manager = RiskManager(replace(self.config, quote_mode=quote_mode))
                decision = manager.evaluate(
                    self.position("0"), (), self.metadata, now_monotonic=100.0
                )

                self.assertEqual(decision.buy_amount, expected_buy)
                self.assertEqual(decision.sell_amount, expected_sell)
                self.assertEqual(decision.worst_long, worst_long)
                self.assertEqual(decision.worst_short, worst_short)

    def test_single_side_mode_does_not_fallback_in_hard_zone(self) -> None:
        cases = (("bid_only", "0.8"), ("ask_only", "-0.8"))
        for quote_mode, size in cases:
            with self.subTest(quote_mode=quote_mode):
                manager = RiskManager(replace(self.config, quote_mode=quote_mode))
                decision = manager.evaluate(
                    self.position(size), (), self.metadata, now_monotonic=100.0
                )

                self.assertIsNone(decision.buy_amount)
                self.assertIsNone(decision.sell_amount)
                self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)

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
        self.assertEqual(decision.sell_amount, Decimal("0.8"))
        self.assertTrue(decision.sell_reduce_only)
        self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)

    def test_hard_short_only_allows_reduce_only_buy(self) -> None:
        decision = self.evaluate("-0.8")

        self.assertEqual(decision.buy_amount, Decimal("0.8"))
        self.assertTrue(decision.buy_reduce_only)
        self.assertIsNone(decision.sell_amount)
        self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)

    def test_sub_order_position_uses_executable_reduce_only_exit(self) -> None:
        manager = RiskManager(
            replace(
                self.config,
                order_size=Decimal("0.00020"),
                max_position=Decimal("0.00040"),
            )
        )
        metadata = replace(
            self.metadata,
            size_decimals=5,
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("10"),
        )
        cases = (
            ("0.00009", None, Decimal("0.00020"), False, True),
            ("-0.00009", Decimal("0.00020"), None, True, False),
        )
        for size, buy, sell, buy_reduce_only, sell_reduce_only in cases:
            with self.subTest(size=size):
                decision = manager.evaluate(
                    self.position(size), (), metadata, now_monotonic=100.0
                )

                self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)
                self.assertEqual(decision.buy_amount, buy)
                self.assertEqual(decision.sell_amount, sell)
                self.assertEqual(decision.buy_reduce_only, buy_reduce_only)
                self.assertEqual(decision.sell_reduce_only, sell_reduce_only)
                self.assertEqual(decision.worst_long, Decimal(size))
                self.assertEqual(decision.worst_short, Decimal(size))

    def test_soft_exit_latch_survives_partial_fill_until_flat(self) -> None:
        config = replace(
            self.config,
            order_size=Decimal("0.00020"),
            max_position=Decimal("0.00040"),
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-5.0"),
            min_completed_net_turnover_bps=Decimal("0.1"),
        )
        metadata = replace(
            self.metadata,
            size_decimals=5,
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("10"),
        )
        for sign in (Decimal("1"), Decimal("-1")):
            with self.subTest(sign=sign):
                manager = RiskManager(config)

                def evaluate(size: str, now: float):
                    return manager.evaluate(
                        self.position(size, received=now),
                        (),
                        metadata,
                        now_monotonic=now,
                    )

                cap = "0.00040" if sign > 0 else "-0.00040"
                half = "0.00020" if sign > 0 else "-0.00020"
                residual = "0.00009" if sign > 0 else "-0.00009"
                evaluate(cap, 100.0)
                before_timeout = evaluate(cap, 219.0)
                self.assertNotEqual(
                    before_timeout.reason, "soft exit latched until flat"
                )
                latched = evaluate(cap, 220.0)
                partial = evaluate(half, 221.0)
                dust = evaluate(residual, 222.0)

                expected_amounts = (
                    (latched, Decimal("0.00040")),
                    (partial, Decimal("0.00020")),
                    (dust, Decimal("0.00020")),
                )
                for decision, expected_amount in expected_amounts:
                    self.assertEqual(
                        decision.runtime_state, RuntimeState.RISK_REDUCTION
                    )
                    self.assertEqual(
                        decision.reason, "soft exit latched until flat"
                    )
                    if sign > 0:
                        self.assertIsNone(decision.buy_amount)
                        self.assertEqual(
                            decision.sell_amount, expected_amount
                        )
                        self.assertTrue(decision.sell_reduce_only)
                    else:
                        self.assertEqual(
                            decision.buy_amount, expected_amount
                        )
                        self.assertIsNone(decision.sell_amount)
                        self.assertTrue(decision.buy_reduce_only)

                flat = evaluate("0", 223.0)
                new_half = evaluate(half, 224.0)
                self.assertEqual(flat.runtime_state, RuntimeState.ACTIVE)
                self.assertEqual(new_half.runtime_state, RuntimeState.ACTIVE)
                self.assertIsNotNone(new_half.buy_amount)
                self.assertIsNotNone(new_half.sell_amount)

    def test_timed_half_inventory_latches_before_hard_or_residual_zone(
        self,
    ) -> None:
        config = replace(
            self.config,
            order_size=Decimal("0.1"),
            max_position=Decimal("0.4"),
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-5.0"),
            min_completed_net_turnover_bps=Decimal("0.1"),
        )
        for sign in (Decimal("1"), Decimal("-1")):
            with self.subTest(sign=sign):
                manager = RiskManager(config)

                def evaluate(size: Decimal, now: float):
                    return manager.evaluate(
                        self.position(str(size), received=now),
                        (),
                        self.metadata,
                        now_monotonic=now,
                    )

                half = sign * Decimal("0.2")
                partial = sign * Decimal("0.1")
                started = evaluate(half, 100.0)
                before_timeout = evaluate(half, 219.9)
                latched = evaluate(half, 220.0)
                after_partial_fill = evaluate(partial, 221.0)

                for decision in (started, before_timeout):
                    self.assertEqual(decision.runtime_state, RuntimeState.ACTIVE)
                    self.assertEqual(decision.buy_amount, Decimal("0.1"))
                    self.assertEqual(decision.sell_amount, Decimal("0.1"))
                    self.assertFalse(decision.buy_reduce_only)
                    self.assertFalse(decision.sell_reduce_only)

                for decision, expected_amount in (
                    (latched, Decimal("0.2")),
                    (after_partial_fill, Decimal("0.1")),
                ):
                    self.assertEqual(
                        decision.runtime_state, RuntimeState.RISK_REDUCTION
                    )
                    if sign > 0:
                        self.assertIsNone(decision.buy_amount)
                        self.assertEqual(decision.sell_amount, expected_amount)
                        self.assertTrue(decision.sell_reduce_only)
                    else:
                        self.assertEqual(decision.buy_amount, expected_amount)
                        self.assertTrue(decision.buy_reduce_only)
                        self.assertIsNone(decision.sell_amount)

                trusted_flat = evaluate(Decimal("0"), 222.0)
                new_inventory = evaluate(partial, 223.0)
                for decision in (trusted_flat, new_inventory):
                    self.assertEqual(decision.runtime_state, RuntimeState.ACTIVE)
                    self.assertEqual(decision.buy_amount, Decimal("0.1"))
                    self.assertEqual(decision.sell_amount, Decimal("0.1"))
                    self.assertFalse(decision.buy_reduce_only)
                    self.assertFalse(decision.sell_reduce_only)

    def test_stale_flat_does_not_clear_soft_exit_latch(self) -> None:
        manager = RiskManager(
            replace(
                self.config,
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps=Decimal("-5.0"),
                min_completed_net_turnover_bps=Decimal("0.1"),
            )
        )
        manager.evaluate(
            self.position("1", received=100.0),
            (),
            self.metadata,
            now_monotonic=100.0,
        )
        manager.evaluate(
            self.position("1", received=220.0),
            (),
            self.metadata,
            now_monotonic=220.0,
        )

        stale_flat = manager.evaluate(
            self.position("0", received=100.0),
            (),
            self.metadata,
            now_monotonic=221.0,
        )
        partial = manager.evaluate(
            self.position("0.2", received=222.0),
            (),
            self.metadata,
            now_monotonic=222.0,
        )

        self.assertEqual(stale_flat.runtime_state, RuntimeState.PAUSED_POSITION)
        self.assertEqual(partial.runtime_state, RuntimeState.RISK_REDUCTION)
        self.assertIsNone(partial.buy_amount)
        self.assertEqual(partial.sell_amount, Decimal("0.2"))
        self.assertTrue(partial.sell_reduce_only)

    def test_soft_exit_latch_survives_sign_flip_and_clock_rollback(self) -> None:
        manager = RiskManager(
            replace(
                self.config,
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps=Decimal("-5.0"),
                min_completed_net_turnover_bps=Decimal("0.1"),
            )
        )
        manager.evaluate(
            self.position("1", received=100.0),
            (),
            self.metadata,
            now_monotonic=100.0,
        )
        manager.evaluate(
            self.position("1", received=220.0),
            (),
            self.metadata,
            now_monotonic=220.0,
        )

        flipped = manager.evaluate(
            self.position("-0.2", received=221.0),
            (),
            self.metadata,
            now_monotonic=221.0,
        )
        rolled_back = manager.evaluate(
            self.position("-0.2", received=50.0),
            (),
            self.metadata,
            now_monotonic=50.0,
        )

        for decision in (flipped, rolled_back):
            self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)
            self.assertEqual(decision.buy_amount, Decimal("0.2"))
            self.assertTrue(decision.buy_reduce_only)
            self.assertIsNone(decision.sell_amount)

    def test_sub_order_exit_respects_conflicting_single_side_mode(self) -> None:
        cases = (("bid_only", "0.1"), ("ask_only", "-0.1"))
        for quote_mode, size in cases:
            with self.subTest(quote_mode=quote_mode, size=size):
                manager = RiskManager(replace(self.config, quote_mode=quote_mode))
                decision = manager.evaluate(
                    self.position(size), (), self.metadata, now_monotonic=100.0
                )

                self.assertEqual(decision.runtime_state, RuntimeState.RISK_REDUCTION)
                self.assertIsNone(decision.buy_amount)
                self.assertIsNone(decision.sell_amount)
                self.assertFalse(decision.buy_reduce_only)
                self.assertFalse(decision.sell_reduce_only)

    def test_absolute_max_long_and_short_only_reduce(self) -> None:
        long_decision = self.evaluate("1")
        short_decision = self.evaluate("-1")

        self.assertTrue(long_decision.sell_reduce_only)
        self.assertEqual(long_decision.sell_amount, Decimal("1"))
        self.assertIsNone(long_decision.buy_amount)
        self.assertTrue(short_decision.buy_reduce_only)
        self.assertEqual(short_decision.buy_amount, Decimal("1"))
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
