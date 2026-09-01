import unittest
from dataclasses import replace
from decimal import Decimal

from core.adapters.exchanges.models import OrderBookLevel, OrderSide
from core.services.market_maker.market_features import (
    FeatureHealth,
    MarketFeatureStore,
    build_external_book_view,
)
from core.services.market_maker.models import (
    ManagedOrder,
    MarketSnapshot,
    OrderSlotState,
)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def market(
    timestamp: float,
    *,
    bids: tuple[tuple[str, str], ...] = (("99", "3"), ("98", "1")),
    asks: tuple[tuple[str, str], ...] = (("101", "1"), ("102", "1")),
) -> MarketSnapshot:
    bid_levels = tuple(OrderBookLevel(Decimal(p), Decimal(s)) for p, s in bids)
    ask_levels = tuple(OrderBookLevel(Decimal(p), Decimal(s)) for p, s in asks)
    return MarketSnapshot(
        symbol="BTC",
        bids=bid_levels,
        asks=ask_levels,
        best_bid=bid_levels[0].price if bid_levels else Decimal("99"),
        best_ask=ask_levels[0].price if ask_levels else Decimal("101"),
        exchange_timestamp=None,
        received_monotonic=timestamp,
    )


def centered_market(timestamp: float, mid: Decimal) -> MarketSnapshot:
    return market(
        timestamp,
        bids=((str(mid - Decimal("0.5")), "1"),),
        asks=((str(mid + Decimal("0.5")), "1"),),
    )


def own_order(
    side: OrderSide,
    price: str,
    remaining: str,
    *,
    state: OrderSlotState = OrderSlotState.LIVE,
    submission_uncertain: bool = False,
    cancellation_uncertain: bool = False,
) -> ManagedOrder:
    amount = Decimal(remaining)
    return ManagedOrder(
        side=side,
        state=state,
        order_id="own-1",
        client_id="client-1",
        price=Decimal(price),
        amount=amount,
        remaining=amount,
        reduce_only=False,
        created_monotonic=0.0,
        updated_monotonic=0.0,
        submission_uncertain=submission_uncertain,
        cancellation_uncertain=cancellation_uncertain,
    )


def store(clock: FakeClock, **overrides: object) -> MarketFeatureStore:
    values = {
        "price_tick": Decimal("1"),
        "depth_levels": 5,
        "feature_window_seconds": 60,
        "reset_gap_seconds": 10,
        "warmup_seconds": 15,
        "min_samples": 16,
        "stale_after_seconds": 3,
        "clock": clock,
        "max_samples": 256,
    }
    values.update(overrides)
    return MarketFeatureStore(**values)


class ExternalBookViewTests(unittest.TestCase):
    def test_subtracts_only_identifiable_same_side_same_price_size(self) -> None:
        source = market(0)
        original = tuple((level.price, level.size) for level in source.bids)
        orders = (
            own_order(OrderSide.BUY, "99", "1.25"),
            own_order(OrderSide.SELL, "99", "1"),
            own_order(OrderSide.BUY, "98", "0.25"),
            own_order(
                OrderSide.BUY,
                "99",
                "0.5",
                submission_uncertain=True,
            ),
            own_order(
                OrderSide.BUY,
                "99",
                "0.5",
                state=OrderSlotState.UNCERTAIN_CANCELLATION,
            ),
            own_order(
                OrderSide.BUY,
                "99",
                "0.5",
                cancellation_uncertain=True,
            ),
        )

        view = build_external_book_view(source, orders, 5)

        self.assertTrue(view.valid)
        self.assertEqual(view.bids[0].size, Decimal("1.75"))
        self.assertEqual(view.bids[1].size, Decimal("0.75"))
        self.assertEqual(view.asks[0].size, Decimal("1"))
        self.assertEqual(
            tuple((level.price, level.size) for level in source.bids), original
        )

    def test_smaller_equal_and_larger_own_size_never_go_negative(self) -> None:
        cases = (
            ("2", Decimal("1"), Decimal("99")),
            ("3", Decimal("1"), Decimal("98")),
            ("4", Decimal("1"), Decimal("98")),
        )
        for own_size, expected_size, expected_best in cases:
            with self.subTest(own_size=own_size):
                view = build_external_book_view(
                    market(0), (own_order(OrderSide.BUY, "99", own_size),), 5
                )
                self.assertTrue(view.valid)
                self.assertEqual(view.bids[0].size, expected_size)
                self.assertEqual(view.external_best_bid, expected_best)
                self.assertTrue(all(level.size > 0 for level in view.bids))

    def test_depth_is_sorted_aggregated_and_bounded(self) -> None:
        source = market(
            0,
            bids=(("98", "1"), ("99", "1"), ("99", "2"), ("97", "1")),
            asks=(("102", "1"), ("101", "2"), ("101", "1"), ("103", "1")),
        )
        source = replace(source, best_bid=Decimal("99"), best_ask=Decimal("101"))

        view = build_external_book_view(source, (), 2)

        self.assertEqual(
            tuple((level.price, level.size) for level in view.bids),
            ((Decimal("99"), Decimal("3")), (Decimal("98"), Decimal("1"))),
        )
        self.assertEqual(
            tuple((level.price, level.size) for level in view.asks),
            ((Decimal("101"), Decimal("3")), (Decimal("102"), Decimal("1"))),
        )

    def test_empty_crossed_and_malformed_books_are_invalid(self) -> None:
        empty = market(0, bids=())
        crossed = replace(market(0), best_bid=Decimal("101"))
        malformed = market(0)
        malformed.bids[0].size = Decimal("NaN")

        for source in (empty, crossed, malformed):
            with self.subTest(source=source):
                self.assertFalse(build_external_book_view(source, (), 5).valid)

    def test_removing_an_entire_side_is_invalid(self) -> None:
        view = build_external_book_view(
            market(0, bids=(("99", "1"),)),
            (own_order(OrderSide.BUY, "99", "2"),),
            5,
        )
        self.assertFalse(view.valid)


class MarketFeatureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def update(self, feature_store: MarketFeatureStore, timestamp: int, mid: int):
        self.clock.now = float(timestamp)
        return feature_store.update(centered_market(float(timestamp), Decimal(mid)))

    def test_momentum_uses_latest_sample_not_later_than_cutoff(self) -> None:
        feature_store = store(
            self.clock, warmup_seconds=1, min_samples=2, reset_gap_seconds=10
        )
        self.clock.now = 0.0
        feature_store.update(centered_market(0.0, Decimal("100")))
        self.clock.now = 4.9
        feature_store.update(centered_market(4.9, Decimal("104")))
        self.clock.now = 5.1
        result = feature_store.update(centered_market(5.1, Decimal("110")))

        self.assertEqual(result.return_1s_ticks, Decimal("10"))
        self.assertEqual(result.return_5s_ticks, Decimal("10"))

    def test_duplicate_timestamp_replaces_without_appending(self) -> None:
        feature_store = store(self.clock, warmup_seconds=1, min_samples=2)
        self.clock.now = 1.0
        feature_store.update(centered_market(1.0, Decimal("100")))
        replaced = feature_store.update(centered_market(1.0, Decimal("101")))
        result = self.update(feature_store, 2, 103)

        self.assertEqual(replaced.sample_count, 1)
        self.assertEqual(result.sample_count, 2)
        self.assertEqual(result.return_1s_ticks, Decimal("2"))

    def test_time_reversal_and_large_gap_reset_history(self) -> None:
        for timestamps in ((10, 11, 9), (0, 1, 20)):
            with self.subTest(timestamps=timestamps):
                feature_store = store(
                    self.clock,
                    warmup_seconds=1,
                    min_samples=2,
                    reset_gap_seconds=5,
                )
                for timestamp in timestamps:
                    result = self.update(feature_store, timestamp, 100 + timestamp)
                self.assertEqual(result.sample_count, 1)
                self.assertIsNone(result.return_1s_ticks)
                self.assertEqual(result.health, FeatureHealth.WARMING)

    def test_returns_and_rms_are_exact_decimals(self) -> None:
        feature_store = store(self.clock, warmup_seconds=15, min_samples=16)
        for timestamp in range(61):
            result = self.update(feature_store, timestamp, 100 + timestamp)

        self.assertEqual(result.health, FeatureHealth.READY)
        self.assertEqual(result.return_1s_ticks, Decimal("1"))
        self.assertEqual(result.return_5s_ticks, Decimal("5"))
        self.assertEqual(result.return_15s_ticks, Decimal("15"))
        self.assertEqual(result.return_60s_ticks, Decimal("60"))
        self.assertEqual(result.rms_1s_move_15s_ticks, Decimal("1"))
        self.assertEqual(result.rms_1s_move_60s_ticks, Decimal("1"))
        for value in (
            result.mid,
            result.spread_ticks,
            result.return_1s_ticks,
            result.return_5s_ticks,
            result.return_15s_ticks,
            result.return_60s_ticks,
            result.rms_1s_move_15s_ticks,
            result.rms_1s_move_60s_ticks,
            result.microprice,
            result.microprice_shift_ticks,
            result.depth_imbalance,
        ):
            self.assertIsInstance(value, Decimal)
            self.assertTrue(value.is_finite())

    def test_rms_uses_squared_one_second_moves(self) -> None:
        feature_store = store(self.clock, warmup_seconds=1, min_samples=2)
        for timestamp, mid in enumerate((100, 101, 99, 102)):
            result = self.update(feature_store, timestamp, mid)

        expected = (Decimal("14") / Decimal("3")).sqrt()
        self.assertEqual(result.rms_1s_move_15s_ticks, expected)
        self.assertEqual(result.rms_1s_move_60s_ticks, expected)

    def test_microprice_and_top_n_imbalance_are_exact(self) -> None:
        feature_store = store(
            self.clock,
            depth_levels=2,
            warmup_seconds=1,
            min_samples=2,
        )
        self.clock.now = 0.0
        result = feature_store.update(
            market(
                0,
                bids=(("99", "3"), ("98", "1"), ("97", "100")),
                asks=(("101", "1"), ("102", "1"), ("103", "100")),
            )
        )

        self.assertEqual(result.mid, Decimal("100"))
        self.assertEqual(result.microprice, Decimal("100.5"))
        self.assertEqual(result.microprice_shift_ticks, Decimal("0.5"))
        self.assertEqual(result.depth_imbalance, Decimal("1") / Decimal("3"))
        self.assertEqual(result.to_dict()["health"], "warming")
        self.assertIsInstance(result.to_dict()["microprice"], Decimal)

    def test_warming_ready_stale_and_invalid_transitions(self) -> None:
        feature_store = store(
            self.clock,
            warmup_seconds=15,
            min_samples=16,
            stale_after_seconds=2,
        )
        first = self.update(feature_store, 0, 100)
        for timestamp in range(1, 16):
            ready = self.update(feature_store, timestamp, 100 + timestamp)
        self.clock.now = 18.0
        stale = feature_store.snapshot()
        invalid = feature_store.update(
            replace(centered_market(18.0, Decimal("118")), bids=())
        )

        self.assertEqual(first.health, FeatureHealth.WARMING)
        self.assertEqual(ready.health, FeatureHealth.READY)
        self.assertEqual(stale.health, FeatureHealth.STALE)
        self.assertEqual(invalid.health, FeatureHealth.INVALID)

    def test_history_buffer_has_a_hard_capacity(self) -> None:
        feature_store = store(
            self.clock,
            warmup_seconds=1,
            min_samples=2,
            max_samples=5,
        )
        for timestamp in range(20):
            result = self.update(feature_store, timestamp, 100 + timestamp)

        self.assertEqual(feature_store.sample_count, 5)
        self.assertEqual(result.sample_count, 5)


if __name__ == "__main__":
    unittest.main()
