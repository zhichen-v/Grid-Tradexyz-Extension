import unittest
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
from core.services.market_maker.strategy import MarketMakerStrategy


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
            received_monotonic=100.0,
        )

    @staticmethod
    def position(size: str) -> PositionSnapshot:
        return PositionSnapshot(
            symbol="BTC",
            signed_size=Decimal(size),
            entry_price=None,
            unrealized_pnl=None,
            received_monotonic=100.0,
        )

    def quotes(self, size: str = "0", **kwargs):
        kwargs.setdefault("now_monotonic", 100.0)
        return self.strategy.calculate_quotes(
            self.market,
            self.position(size),
            self.metadata,
            self.risk,
            **kwargs,
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

    def test_post_only_boundaries_never_cross_bbo(self) -> None:
        high_bid = self.quotes("-10").bid
        low_ask = self.quotes("10").ask

        self.assertLessEqual(high_bid.price, self.market.best_ask - Decimal("0.1"))
        self.assertGreaterEqual(low_ask.price, self.market.best_bid + Decimal("0.1"))

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


if __name__ == "__main__":
    unittest.main()
