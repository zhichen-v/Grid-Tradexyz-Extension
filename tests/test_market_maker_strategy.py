import unittest
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from core.adapters.exchanges.models import OrderBookLevel, OrderSide
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.models import (
    ManagedOrder,
    MarketMetadata,
    MarketSnapshot,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)
from core.services.market_maker.risk_manager import RiskManager
from core.services.market_maker.strategy import (
    MarketMakerStrategy,
    SoftExitEconomics,
)


class MarketMakerStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("1"),
            min_profit_buffer_bps=Decimal("0"),
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
        self.market = self.make_market()
        self.risk = SimpleNamespace(
            buy_amount=Decimal("0.2"),
            sell_amount=Decimal("0.2"),
            buy_reduce_only=False,
            sell_reduce_only=False,
            safe=True,
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
        )
        self.strategy = MarketMakerStrategy(self.config)

    @staticmethod
    def make_market(
        best_bid: Decimal = Decimal("99.9"),
        best_ask: Decimal = Decimal("100.1"),
        received_monotonic: float = 100.0,
    ) -> MarketSnapshot:
        return MarketSnapshot(
            symbol="BTC",
            bids=(
                OrderBookLevel(best_bid, Decimal("1")),
                OrderBookLevel(best_bid - Decimal("0.1"), Decimal("2")),
            ),
            asks=(
                OrderBookLevel(best_ask, Decimal("1")),
                OrderBookLevel(best_ask + Decimal("0.1"), Decimal("2")),
            ),
            best_bid=best_bid,
            best_ask=best_ask,
            exchange_timestamp=None,
            received_monotonic=received_monotonic,
        )

    @staticmethod
    def position(
        size: str,
        entry_price: str | None = "100",
        received_monotonic: float = 100.0,
    ) -> PositionSnapshot:
        signed_size = Decimal(size)
        return PositionSnapshot(
            symbol="BTC",
            signed_size=signed_size,
            entry_price=(
                Decimal(entry_price)
                if signed_size and entry_price is not None
                else None
            ),
            unrealized_pnl=None,
            received_monotonic=received_monotonic,
        )

    def quotes(
        self,
        size: str = "0",
        *,
        entry_price: str | None = "100",
        **kwargs,
    ):
        kwargs.setdefault("now_monotonic", 100.0)
        return self.strategy.calculate_quotes(
            self.market,
            self.position(size, entry_price),
            self.metadata,
            self.risk,
            **kwargs,
        )

    def timed_soft_quote(
        self,
        size: str,
        economics: SoftExitEconomics | None,
    ):
        config = MarketMakerConfig(
            symbol="BTC",
            order_size="0.00020",
            max_position="0.00040",
            max_inventory_skew_ticks=0,
            maker_fee_rate="0.000120",
            min_profit_buffer_bps="0",
            min_completed_net_turnover_bps="0.10",
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps="-0.5",
            soft_exit_surplus_reserve_bps="0.02",
        )
        metadata = replace(
            self.metadata,
            size_decimals=5,
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00001"),
            min_quote_amount=Decimal("0"),
        )
        signed_size = Decimal(size)
        risk = SimpleNamespace(
            buy_amount=abs(signed_size) if signed_size < 0 else None,
            sell_amount=abs(signed_size) if signed_size > 0 else None,
            buy_reduce_only=signed_size < 0,
            sell_reduce_only=signed_size > 0,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="timed reduction",
        )
        market_prices = (
            (Decimal("99900.0"), Decimal("99900.2"))
            if signed_size > 0
            else (Decimal("100099.8"), Decimal("100100.0"))
        )
        strategy = MarketMakerStrategy(config)
        strategy.calculate_quotes(
            self.make_market(*market_prices, received_monotonic=100.0),
            self.position(size, "100000"),
            metadata,
            risk,
            now_monotonic=100.0,
        )
        return strategy.calculate_quotes(
            self.make_market(*market_prices, received_monotonic=220.0),
            self.position(size, "100000"),
            metadata,
            risk,
            now_monotonic=220.0,
            soft_exit_economics=economics,
        )

    def test_flat_position_quotes_symmetrically_around_mid(self) -> None:
        quotes = self.quotes()

        self.assertEqual(quotes.reference_price, Decimal("100.0"))
        self.assertEqual(quotes.reservation_price, Decimal("100.0"))
        self.assertEqual(quotes.bid.price, Decimal("99.9"))
        self.assertEqual(quotes.ask.price, Decimal("100.1"))

    def test_long_inventory_moves_reservation_down(self) -> None:
        quotes = self.quotes("0.5")
        self.assertEqual(quotes.inventory_ratio, Decimal("0.5"))
        self.assertEqual(quotes.reservation_price, Decimal("99.80"))

    def test_short_inventory_moves_reservation_up(self) -> None:
        quotes = self.quotes("-0.5")
        self.assertEqual(quotes.inventory_ratio, Decimal("-0.5"))
        self.assertEqual(quotes.reservation_price, Decimal("100.20"))

    def test_inventory_ratio_clamps_at_one(self) -> None:
        quotes = self.quotes("2")
        self.assertEqual(quotes.inventory_ratio, Decimal("1"))
        self.assertEqual(quotes.reservation_price, Decimal("99.6"))

    def test_fee_floor_can_widen_configured_spread(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                maker_fee_rate=Decimal("0.002"),
                min_profit_buffer_bps=Decimal("0"),
            )
        )
        quotes = strategy.calculate_quotes(
            self.market,
            self.position("0"),
            self.metadata,
            self.risk,
            now_monotonic=100.0,
        )

        self.assertEqual(quotes.half_spread, Decimal("0.2"))

    def test_soft_exit_relaxes_only_timed_reduce_only_inventory(self) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size="0.2",
            max_position="1",
            max_inventory_skew_ticks=0,
            maker_fee_rate="0.000120",
            min_profit_buffer_bps="0",
            min_completed_net_turnover_bps="0.10",
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps="-5.0",
        )
        reduction_risks = (
            (
                "-0.4",
                self.make_market(
                    Decimal("100.4"), Decimal("100.6"), 220.0
                ),
                SimpleNamespace(
                    buy_amount=Decimal("0.2"),
                    sell_amount=None,
                    buy_reduce_only=True,
                    sell_reduce_only=False,
                    safe=True,
                    runtime_state=RuntimeState.RISK_REDUCTION,
                    reason="hard inventory limit reached",
                ),
                OrderSide.BUY,
                Decimal("99.9"),
                Decimal("100.0"),
            ),
            (
                "0.4",
                self.make_market(
                    Decimal("99.4"), Decimal("99.6"), 220.0
                ),
                SimpleNamespace(
                    buy_amount=None,
                    sell_amount=Decimal("0.2"),
                    buy_reduce_only=False,
                    sell_reduce_only=True,
                    safe=True,
                    runtime_state=RuntimeState.RISK_REDUCTION,
                    reason="hard inventory limit reached",
                ),
                OrderSide.SELL,
                Decimal("100.1"),
                Decimal("100.0"),
            ),
        )

        for size, market, risk, side, hard_price, soft_price in reduction_risks:
            with self.subTest(side=side):
                strategy = MarketMakerStrategy(config)
                start_market = self.make_market(
                    market.best_bid, market.best_ask, 100.0
                )
                before_market = self.make_market(
                    market.best_bid, market.best_ask, 219.0
                )
                started = strategy.calculate_quotes(
                    start_market,
                    self.position(size, "100"),
                    self.metadata,
                    risk,
                    now_monotonic=100.0,
                )
                before = strategy.calculate_quotes(
                    before_market,
                    self.position(size, "100"),
                    self.metadata,
                    risk,
                    now_monotonic=219.0,
                )
                active = strategy.calculate_quotes(
                    market,
                    self.position(size, "100"),
                    self.metadata,
                    risk,
                    now_monotonic=220.0,
                    soft_exit_economics=SoftExitEconomics(
                        completed_turnover=Decimal("100"),
                        completed_net=Decimal("1"),
                        open_turnover=Decimal("40"),
                        open_net=Decimal("-0.0048"),
                    ),
                )
                started_order = (
                    started.bid if side is OrderSide.BUY else started.ask
                )
                before_order = before.bid if side is OrderSide.BUY else before.ask
                active_order = active.bid if side is OrderSide.BUY else active.ask

                self.assertEqual(started_order.price, hard_price)
                self.assertEqual(before_order.price, hard_price)
                self.assertEqual(active_order.price, soft_price)
                self.assertTrue(active_order.reduce_only)
                self.assertEqual(active.reason, "soft_exit_active")
                turnover = Decimal("100") + soft_price
                gross = (
                    soft_price - Decimal("100")
                    if side is OrderSide.SELL
                    else Decimal("100") - soft_price
                )
                net_bps = (
                    gross - config.maker_fee_rate * turnover
                ) / turnover * Decimal("10000")
                self.assertGreaterEqual(
                    net_bps, config.soft_exit_net_turnover_bps
                )

    def test_soft_exit_without_economics_uses_hard_boundary(self) -> None:
        for size, side, expected in (
            ("0.00020", OrderSide.SELL, Decimal("100026.1")),
            ("-0.00020", OrderSide.BUY, Decimal("99974.0")),
        ):
            with self.subTest(side=side):
                quotes = self.timed_soft_quote(size, None)
                order = quotes.ask if side is OrderSide.SELL else quotes.bid

                self.assertEqual(order.price, expected)
                self.assertEqual(quotes.reason, "soft_exit_hard_fallback")

    def test_soft_exit_without_completed_surplus_uses_hard_boundary(
        self,
    ) -> None:
        economics = SoftExitEconomics(
            completed_turnover=Decimal("100"),
            completed_net=Decimal("0.001"),
            open_turnover=Decimal("20"),
            open_net=Decimal("-0.0024"),
        )
        for size, side, expected in (
            ("0.00020", OrderSide.SELL, Decimal("100026.1")),
            ("-0.00020", OrderSide.BUY, Decimal("99974.0")),
        ):
            with self.subTest(side=side):
                quotes = self.timed_soft_quote(size, economics)
                order = quotes.ask if side is OrderSide.SELL else quotes.bid

                self.assertEqual(order.price, expected)
                self.assertEqual(quotes.reason, "soft_exit_no_surplus")

    def test_soft_exit_partial_surplus_uses_exact_budget_boundary(self) -> None:
        economics = SoftExitEconomics(
            completed_turnover=Decimal("100"),
            completed_net=Decimal("0.003"),
            open_turnover=Decimal("20"),
            open_net=Decimal("-0.0024"),
        )
        for size, side, expected in (
            ("0.00020", OrderSide.SELL, Decimal("100017.1")),
            ("-0.00020", OrderSide.BUY, Decimal("99983.0")),
        ):
            with self.subTest(side=side):
                quotes = self.timed_soft_quote(size, economics)
                order = quotes.ask if side is OrderSide.SELL else quotes.bid

                self.assertEqual(order.price, expected)
                self.assertEqual(quotes.reason, "soft_exit_active")

    def test_soft_exit_ample_surplus_clamps_to_static_floor(self) -> None:
        economics = SoftExitEconomics(
            completed_turnover=Decimal("100"),
            completed_net=Decimal("1"),
            open_turnover=Decimal("20"),
            open_net=Decimal("-0.0024"),
        )
        for size, side, expected in (
            ("0.00020", OrderSide.SELL, Decimal("100014.1")),
            ("-0.00020", OrderSide.BUY, Decimal("99986.0")),
        ):
            with self.subTest(side=side):
                quotes = self.timed_soft_quote(size, economics)
                order = quotes.ask if side is OrderSide.SELL else quotes.bid

                self.assertEqual(order.price, expected)
                self.assertEqual(quotes.reason, "soft_exit_active")

    def test_soft_exit_open_loss_and_partial_position_tighten_budget(self) -> None:
        base = SoftExitEconomics(
            completed_turnover=Decimal("100"),
            completed_net=Decimal("0.003"),
            open_turnover=Decimal("20"),
            open_net=Decimal("-0.0024"),
        )
        larger_open_loss = replace(base, open_net=Decimal("-0.0044"))

        for size, side in (
            ("0.00020", OrderSide.SELL),
            ("-0.00020", OrderSide.BUY),
        ):
            with self.subTest(side=side, case="open_loss"):
                normal = self.timed_soft_quote(size, base)
                stricter = self.timed_soft_quote(size, larger_open_loss)
                normal_price = (
                    normal.ask.price
                    if side is OrderSide.SELL
                    else normal.bid.price
                )
                stricter_price = (
                    stricter.ask.price
                    if side is OrderSide.SELL
                    else stricter.bid.price
                )
                if side is OrderSide.SELL:
                    self.assertGreater(stricter_price, normal_price)
                else:
                    self.assertLess(stricter_price, normal_price)

        full_long = self.timed_soft_quote("0.00020", base)
        partial_long = self.timed_soft_quote("0.00010", base)
        full_short = self.timed_soft_quote("-0.00020", base)
        partial_short = self.timed_soft_quote("-0.00010", base)
        self.assertGreater(partial_long.ask.price, full_long.ask.price)
        self.assertLess(partial_short.bid.price, full_short.bid.price)

    def test_soft_exit_remains_reduce_only_after_partial_fill(self) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size="0.00020",
            max_position="0.00040",
            soft_position_ratio="0.5",
            hard_position_ratio="1",
            max_inventory_skew_ticks=4,
            maker_fee_rate="0.000120",
            min_profit_buffer_bps="0.5",
            min_completed_net_turnover_bps="0.10",
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps="-5.0",
        )
        metadata = replace(
            self.metadata,
            size_decimals=5,
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("10"),
        )
        manager = RiskManager(config)
        strategy = MarketMakerStrategy(config)

        def position(size: str, now: float) -> PositionSnapshot:
            return PositionSnapshot(
                symbol="BTC",
                signed_size=Decimal(size),
                entry_price=Decimal("78900"),
                unrealized_pnl=None,
                received_monotonic=now,
            )

        start_position = position("0.00040", 100.0)
        start_risk = manager.evaluate(
            start_position, (), metadata, now_monotonic=100.0
        )
        strategy.calculate_quotes(
            self.make_market(
                Decimal("78899.9"), Decimal("78900.1"), 100.0
            ),
            start_position,
            metadata,
            start_risk,
            now_monotonic=100.0,
        )
        cap_position = position("0.00040", 220.0)
        cap_risk = manager.evaluate(
            cap_position, (), metadata, now_monotonic=220.0
        )
        strategy.calculate_quotes(
            self.make_market(
                Decimal("78899.9"), Decimal("78900.1"), 220.0
            ),
            cap_position,
            metadata,
            cap_risk,
            now_monotonic=220.0,
        )

        partial_position = position("0.00020", 221.0)
        partial_risk = manager.evaluate(
            partial_position, (), metadata, now_monotonic=221.0
        )
        partial_quotes = strategy.calculate_quotes(
            self.make_market(
                Decimal("78899.9"), Decimal("78900.1"), 221.0
            ),
            partial_position,
            metadata,
            partial_risk,
            now_monotonic=221.0,
            soft_exit_economics=SoftExitEconomics(
                completed_turnover=Decimal("100"),
                completed_net=Decimal("1"),
                open_turnover=Decimal("31.56"),
                open_net=Decimal("-0.0038"),
            ),
        )

        self.assertEqual(
            partial_risk.runtime_state, RuntimeState.RISK_REDUCTION
        )
        self.assertIsNone(partial_quotes.bid)
        self.assertIsNotNone(partial_quotes.ask)
        self.assertTrue(partial_quotes.ask.reduce_only)
        self.assertEqual(partial_quotes.reason, "soft_exit_active")

    def test_soft_exit_does_not_relax_non_reduce_only_quote(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size="0.2",
                max_position="1",
                max_inventory_skew_ticks=0,
                maker_fee_rate="0.000120",
                min_profit_buffer_bps="0",
                min_completed_net_turnover_bps="0.10",
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps="-5.0",
            )
        )
        risk = SimpleNamespace(
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=False,
            sell_reduce_only=False,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="not reduce-only",
        )
        strategy.calculate_quotes(
            self.make_market(Decimal("100.4"), Decimal("100.6"), 100.0),
            self.position("-0.4", "100"),
            self.metadata,
            risk,
            now_monotonic=100.0,
        )
        quotes = strategy.calculate_quotes(
            self.make_market(Decimal("100.4"), Decimal("100.6"), 220.0),
            self.position("-0.4", "100"),
            self.metadata,
            risk,
            now_monotonic=220.0,
        )

        self.assertEqual(quotes.bid.price, Decimal("99.9"))
        self.assertFalse(quotes.reason.startswith("soft_exit_active:"))

    def test_soft_exit_timer_resets_on_flat_sign_flip_and_clock_rollback(
        self,
    ) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size="0.2",
                max_position="1",
                maker_fee_rate="0.000120",
                min_completed_net_turnover_bps="0.10",
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps="-5.0",
            )
        )
        risk = SimpleNamespace(
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            sell_reduce_only=False,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="reduce short",
        )

        self.assertEqual(
            strategy._inventory_age_seconds(Decimal("-0.4"), 100.0), 0.0
        )
        self.assertEqual(
            strategy._inventory_age_seconds(Decimal("-0.2"), 150.0), 50.0
        )
        self.assertEqual(
            strategy._inventory_age_seconds(Decimal("0"), 160.0), 0.0
        )
        self.assertEqual(
            strategy._inventory_age_seconds(Decimal("-0.4"), 200.0), 0.0
        )
        self.assertEqual(
            strategy._inventory_age_seconds(Decimal("0.4"), 250.0), 0.0
        )
        self.assertEqual(
            strategy._inventory_age_seconds(Decimal("0.2"), 240.0), 0.0
        )
        self.assertFalse(
            strategy._soft_exit_active(Decimal("-0.4"), risk, 119.9)
        )
        self.assertTrue(
            strategy._soft_exit_active(Decimal("-0.4"), risk, 120.0)
        )

    def test_soft_exit_active_without_entry_price_fails_closed(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size="0.2",
                max_position="1",
                maker_fee_rate="0.000120",
                min_completed_net_turnover_bps="0.10",
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps="-5.0",
            )
        )
        risk = SimpleNamespace(
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            sell_reduce_only=False,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="reduce short",
        )
        strategy.calculate_quotes(
            self.make_market(received_monotonic=100.0),
            self.position("-0.4", "100"),
            self.metadata,
            risk,
            now_monotonic=100.0,
        )
        quotes = strategy.calculate_quotes(
            self.make_market(received_monotonic=220.0),
            self.position("-0.4", None),
            self.metadata,
            risk,
            now_monotonic=220.0,
        )

        self.assertEqual(quotes.runtime_state, RuntimeState.PAUSED_POSITION)
        self.assertIn("entry price", quotes.reason)

    def test_reduce_only_below_base_minimum_fails_closed(self) -> None:
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=2,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            min_base_amount=Decimal("0.10"),
            min_quote_amount=Decimal("0"),
        )
        risk = SimpleNamespace(
            buy_amount=None,
            sell_amount=Decimal("0.05"),
            buy_reduce_only=False,
            sell_reduce_only=True,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="dust reduction",
        )

        quotes = self.strategy.calculate_quotes(
            self.market,
            self.position("0.05"),
            metadata,
            risk,
            now_monotonic=100.0,
        )

        self.assertIsNone(quotes.bid)
        self.assertIsNone(quotes.ask)

    def test_reduce_only_minimum_amount_for_smaller_residual_is_generated(
        self,
    ) -> None:
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=5,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("0"),
        )
        risk = SimpleNamespace(
            buy_amount=None,
            sell_amount=Decimal("0.00020"),
            buy_reduce_only=False,
            sell_reduce_only=True,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="residual reduction",
        )

        quotes = self.strategy.calculate_quotes(
            self.market,
            self.position("0.00011"),
            metadata,
            risk,
            now_monotonic=100.0,
        )

        self.assertIsNone(quotes.bid)
        self.assertEqual(quotes.ask.amount, Decimal("0.00020"))
        self.assertTrue(quotes.ask.reduce_only)

    def test_non_reduce_only_dust_still_obeys_base_minimum(self) -> None:
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=2,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            min_base_amount=Decimal("0.10"),
            min_quote_amount=Decimal("0"),
        )
        risk = SimpleNamespace(
            buy_amount=None,
            sell_amount=Decimal("0.05"),
            buy_reduce_only=False,
            sell_reduce_only=False,
            safe=True,
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
        )

        quotes = self.strategy.calculate_quotes(
            self.market,
            self.position("0"),
            metadata,
            risk,
            now_monotonic=100.0,
        )

        self.assertIsNone(quotes.ask)

    def test_reduce_only_still_obeys_quote_minimum(self) -> None:
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("25"),
        )
        reduce_risk = SimpleNamespace(
            buy_amount=None,
            sell_amount=Decimal("0.2"),
            buy_reduce_only=False,
            sell_reduce_only=True,
            safe=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
            reason="dust reduction",
        )
        normal_risk = SimpleNamespace(
            buy_amount=None,
            sell_amount=Decimal("0.2"),
            buy_reduce_only=False,
            sell_reduce_only=False,
            safe=True,
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
        )

        reduce_quotes = self.strategy.calculate_quotes(
            self.market,
            self.position("0.2"),
            metadata,
            reduce_risk,
            now_monotonic=100.0,
        )
        normal_quotes = self.strategy.calculate_quotes(
            self.market,
            self.position("0"),
            metadata,
            normal_risk,
            now_monotonic=100.0,
        )

        self.assertIsNone(reduce_quotes.ask)
        self.assertIsNone(normal_quotes.ask)

    def test_reduce_only_dust_still_requires_positive_finite_aligned_amount(
        self,
    ) -> None:
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=2,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            min_base_amount=Decimal("0.10"),
            min_quote_amount=Decimal("0"),
        )

        for amount in (
            Decimal("0"),
            Decimal("-0.01"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("0.055"),
        ):
            with self.subTest(amount=amount):
                risk = SimpleNamespace(
                    buy_amount=None,
                    sell_amount=amount,
                    buy_reduce_only=False,
                    sell_reduce_only=True,
                    safe=True,
                    runtime_state=RuntimeState.RISK_REDUCTION,
                    reason="dust reduction",
                )
                quotes = self.strategy.calculate_quotes(
                    self.market,
                    self.position("0.05"),
                    metadata,
                    risk,
                    now_monotonic=100.0,
                )

                self.assertIsNone(quotes.ask)

    def test_inventory_exit_prices_cover_fee_and_completed_net_floor(self) -> None:
        fee_rate = Decimal("0.0012")
        minimum_net_bps = Decimal("0.10")
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                maker_fee_rate=fee_rate,
                min_profit_buffer_bps=Decimal("0"),
                min_completed_net_turnover_bps=minimum_net_bps,
            )
        )

        long_quotes = strategy.calculate_quotes(
            self.market,
            self.position("0.2", "100"),
            self.metadata,
            self.risk,
            now_monotonic=100.0,
        )
        short_quotes = strategy.calculate_quotes(
            self.market,
            self.position("-0.2", "100"),
            self.metadata,
            self.risk,
            now_monotonic=100.0,
        )

        self.assertEqual(long_quotes.ask.price, Decimal("100.3"))
        self.assertEqual(short_quotes.bid.price, Decimal("99.7"))
        for entry, exit_price, gross in (
            (
                Decimal("100"),
                long_quotes.ask.price,
                long_quotes.ask.price - Decimal("100"),
            ),
            (
                Decimal("100"),
                short_quotes.bid.price,
                Decimal("100") - short_quotes.bid.price,
            ),
        ):
            turnover = entry + exit_price
            net_turnover_bps = (
                (gross - fee_rate * turnover) / turnover * Decimal("10000")
            )
            self.assertGreaterEqual(net_turnover_bps, minimum_net_bps)
        for exit_price, gross in (
            (Decimal("100.2"), Decimal("0.2")),
            (Decimal("99.8"), Decimal("0.2")),
        ):
            turnover = Decimal("100") + exit_price
            net_turnover_bps = (
                (gross - fee_rate * turnover) / turnover * Decimal("10000")
            )
            self.assertLess(net_turnover_bps, minimum_net_bps)

    def test_unsafe_live_exit_quote_is_removed_before_replacement(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                maker_fee_rate=Decimal("0.0012"),
                min_profit_buffer_bps=Decimal("0"),
                min_completed_net_turnover_bps=Decimal("0.10"),
                min_order_lifetime_ms=30_000,
                reprice_threshold_ticks=250,
            )
        )

        long_quotes = strategy.calculate_quotes(
            self.market,
            self.position("0.2", "100"),
            self.metadata,
            self.risk,
            (self.managed_order(OrderSide.SELL, "100.2"),),
            now_monotonic=100.0,
        )
        short_quotes = strategy.calculate_quotes(
            self.market,
            self.position("-0.2", "100"),
            self.metadata,
            self.risk,
            (self.managed_order(OrderSide.BUY, "99.8"),),
            now_monotonic=100.0,
        )
        safe_long_quotes = strategy.calculate_quotes(
            self.market,
            self.position("0.2", "100"),
            self.metadata,
            self.risk,
            (self.managed_order(OrderSide.SELL, "100.3"),),
            now_monotonic=100.0,
        )
        safe_short_quotes = strategy.calculate_quotes(
            self.market,
            self.position("-0.2", "100"),
            self.metadata,
            self.risk,
            (self.managed_order(OrderSide.BUY, "99.7"),),
            now_monotonic=100.0,
        )

        self.assertIsNone(long_quotes.ask)
        self.assertIsNotNone(long_quotes.bid)
        self.assertIn("fee-aware", long_quotes.reason)
        self.assertIsNone(short_quotes.bid)
        self.assertIsNotNone(short_quotes.ask)
        self.assertIn("fee-aware", short_quotes.reason)
        self.assertIsNotNone(safe_long_quotes.ask)
        self.assertIsNotNone(safe_short_quotes.bid)

    def test_disabled_exit_floor_does_not_require_entry_price(self) -> None:
        quotes = self.quotes("0.2", entry_price=None)

        self.assertEqual(quotes.runtime_state, RuntimeState.ACTIVE)
        self.assertIsNotNone(quotes.bid)
        self.assertIsNotNone(quotes.ask)

    def test_e1_regression_rejects_three_unsafe_historical_exits(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                base_half_spread_ticks=250,
                reprice_threshold_ticks=250,
                min_order_lifetime_ms=30_000,
                maker_fee_rate=Decimal("0.000120"),
                min_profit_buffer_bps=Decimal("0.5"),
                min_completed_net_turnover_bps=Decimal("0.10"),
            )
        )
        episodes = (
            ("0.2", "79265.6", OrderSide.SELL, "79292.4", False),
            ("-0.2", "79336.4", OrderSide.BUY, "79317.7", True),
            ("0.2", "79311.1", OrderSide.SELL, "79330.6", True),
            ("0.2", "79299.0", OrderSide.SELL, "79311.6", True),
        )

        for size, entry, side, exit_price, should_cancel in episodes:
            with self.subTest(entry=entry, exit=exit_price):
                price = Decimal(exit_price)
                market = self.make_market(price - Decimal("0.1"), price)
                if side is OrderSide.BUY:
                    market = self.make_market(price, price + Decimal("0.1"))
                quotes = strategy.calculate_quotes(
                    market,
                    self.position(size, entry),
                    self.metadata,
                    self.risk,
                    (self.managed_order(side, exit_price),),
                    now_monotonic=100.0,
                )
                exit_quote = (
                    quotes.ask if side is OrderSide.SELL else quotes.bid
                )
                self.assertEqual(exit_quote is None, should_cancel)

    def test_nonflat_position_without_valid_entry_price_fails_closed(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                maker_fee_rate=Decimal("0.0012"),
                min_profit_buffer_bps=Decimal("0"),
                min_completed_net_turnover_bps=Decimal("0.10"),
            )
        )
        for entry_price in (None, "0", "-1", "NaN", "Infinity"):
            with self.subTest(entry_price=entry_price):
                quotes = strategy.calculate_quotes(
                    self.market,
                    self.position("0.2", entry_price),
                    self.metadata,
                    self.risk,
                    now_monotonic=100.0,
                )
                self.assertEqual(quotes.runtime_state, RuntimeState.PAUSED_POSITION)
                self.assertIsNone(quotes.bid)
                self.assertIsNone(quotes.ask)
                self.assertIn("entry price", quotes.reason)

    def test_post_only_boundaries_never_cross_bbo(self) -> None:
        high_bid = self.quotes("-10").bid
        low_ask = self.quotes("10").ask

        self.assertLessEqual(high_bid.price, self.market.best_ask - Decimal("0.1"))
        self.assertGreaterEqual(low_ask.price, self.market.best_bid + Decimal("0.1"))

    def test_raw_spread_guard_allows_boundary_and_pauses_above_it(self) -> None:
        strategy = MarketMakerStrategy(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                max_raw_spread_bps=Decimal("100"),
                min_profit_buffer_bps=Decimal("0"),
            )
        )

        for bid, ask, expected_state in (
            ("99.6", "100.4", RuntimeState.ACTIVE),
            ("99.5", "100.5", RuntimeState.ACTIVE),
            ("99.4", "100.6", RuntimeState.PAUSED_MARKET),
        ):
            with self.subTest(bid=bid, ask=ask):
                quotes = strategy.calculate_quotes(
                    self.make_market(Decimal(bid), Decimal(ask)),
                    self.position("0"),
                    self.metadata,
                    self.risk,
                    now_monotonic=100.0,
                )

                self.assertEqual(quotes.runtime_state, expected_state)
                if expected_state is RuntimeState.PAUSED_MARKET:
                    self.assertIsNone(quotes.bid)
                    self.assertIsNone(quotes.ask)
                    self.assertIn("spread", quotes.reason)
                else:
                    self.assertIsNotNone(quotes.bid)
                    self.assertIsNotNone(quotes.ask)

    def test_decimal_tick_rounding_is_outward(self) -> None:
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=2,
            size_decimals=1,
            price_tick=Decimal("0.05"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("0"),
        )
        market = self.make_market(Decimal("99.91"), Decimal("100.07"))
        quotes = self.strategy.calculate_quotes(
            market,
            self.position("0"),
            metadata,
            self.risk,
            now_monotonic=100.0,
        )

        self.assertEqual(quotes.bid.price, Decimal("99.90"))
        self.assertEqual(quotes.ask.price, Decimal("100.05"))

    def test_empty_crossed_and_non_finite_books_fail_closed(self) -> None:
        invalid_markets = (
            MarketSnapshot(
                symbol="BTC",
                bids=(),
                asks=self.market.asks,
                best_bid=Decimal("99.9"),
                best_ask=Decimal("100.1"),
                exchange_timestamp=None,
                received_monotonic=100.0,
            ),
            self.make_market(Decimal("100.1"), Decimal("100.0")),
            self.make_market(Decimal("NaN"), Decimal("100.1")),
            self.make_market(Decimal("99.9"), Decimal("Infinity")),
        )

        for market in invalid_markets:
            with self.subTest(market=market):
                quotes = self.strategy.calculate_quotes(
                    market,
                    self.position("0"),
                    self.metadata,
                    self.risk,
                    now_monotonic=100.0,
                )
                self.assertIsNone(quotes.bid)
                self.assertIsNone(quotes.ask)
                self.assertEqual(quotes.runtime_state, RuntimeState.PAUSED_DATA)

    def test_own_quote_keeps_top_when_other_liquidity_remains(self) -> None:
        own_bid = self.managed_bid(Decimal("0.4"))
        quotes = self.quotes(live_orders=(own_bid,))
        self.assertEqual(quotes.reference_price, Decimal("100.0"))

    def test_own_quote_uses_next_level_when_it_is_the_top_liquidity(self) -> None:
        own_bid = self.managed_bid(Decimal("1"))
        quotes = self.quotes(live_orders=(own_bid,))
        self.assertEqual(quotes.reference_price, Decimal("99.95"))

    def test_stale_book_fails_closed_when_clock_is_supplied(self) -> None:
        quotes = self.quotes(now_monotonic=104.0)
        self.assertEqual(quotes.runtime_state, RuntimeState.PAUSED_DATA)
        self.assertIsNone(quotes.bid)
        self.assertIn("stale", quotes.reason)

    def test_missing_or_future_clock_fails_closed(self) -> None:
        missing = self.strategy.calculate_quotes(
            self.market,
            self.position("0"),
            self.metadata,
            self.risk,
        )
        future = self.quotes(now_monotonic=99.0)

        self.assertEqual(missing.runtime_state, RuntimeState.PAUSED_DATA)
        self.assertIsNone(missing.bid)
        self.assertIn("required", missing.reason)
        self.assertEqual(future.runtime_state, RuntimeState.PAUSED_DATA)
        self.assertIsNone(future.ask)
        self.assertIn("future", future.reason)

    @staticmethod
    def managed_bid(remaining: Decimal) -> ManagedOrder:
        return ManagedOrder(
            side=OrderSide.BUY,
            state=OrderSlotState.LIVE,
            order_id="bid-1",
            client_id="mm-bid-1",
            price=Decimal("99.9"),
            amount=Decimal("1"),
            remaining=remaining,
            reduce_only=False,
            created_monotonic=90.0,
            updated_monotonic=99.0,
        )

    @staticmethod
    def managed_order(side: OrderSide, price: str) -> ManagedOrder:
        return ManagedOrder(
            side=side,
            state=OrderSlotState.LIVE,
            order_id=f"{side.value}-1",
            client_id=f"mm-{side.value}-1",
            price=Decimal(price),
            amount=Decimal("0.2"),
            remaining=Decimal("0.2"),
            reduce_only=False,
            created_monotonic=99.9,
            updated_monotonic=99.9,
        )


if __name__ == "__main__":
    unittest.main()
