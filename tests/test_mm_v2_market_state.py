"""Public bounded-depth market data and scalar EWMA contracts; offline only."""

from decimal import Decimal as D, localcontext
import unittest

from core.services.market_maker_v2.market_state import MarketState, MAX_VOLATILITY_BUFFER_BPS


def levels(*pairs):
    return tuple((D(price), D(size)) for price, size in pairs)


class MarketStateTests(unittest.TestCase):
    def setUp(self):
        self.market = MarketState("BTC", tick_size=D("0.1"), size_step=D("0.001"),
                                  min_order_size=D("0.002"))

    def update(self, **changes):
        data = dict(bids=levels(("99", "3")), asks=levels(("101", "1")),
                    own_bids=(), own_asks=(), observed_monotonic=0.0, trusted=True)
        data.update(changes)
        return self.market.update(**data)

    def test_external_best_sizes_set_microprice_and_metadata(self):
        snapshot = self.update()
        self.assertEqual(snapshot.microprice, D("100.5"))
        self.assertEqual(snapshot.external_bid, D("99"))
        self.assertEqual(snapshot.external_ask, D("101"))
        self.assertEqual(snapshot.ewma_move_bps, D("0"))
        self.assertEqual(snapshot.tick_size, D("0.1"))
        self.assertIs(snapshot, self.market.snapshot())
        self.assertEqual(MAX_VOLATILITY_BUFFER_BPS, D("5"))

    def test_own_aggregated_sizes_are_removed_before_best_and_microprice(self):
        snapshot = self.update(
            bids=levels(("99", "3"), ("98", "4")),
            asks=levels(("101", "1"), ("102", "4")),
            own_bids=levels(("99", "1"), ("99", "2"), ("98", "1")),
            own_asks=levels(("101", "1"), ("102", "3")),
        )
        self.assertEqual((snapshot.external_bid, snapshot.external_ask), (D("98"), D("102")))
        self.assertEqual(snapshot.microprice, D("101"))

    def test_unsorted_book_zero_levels_and_own_outside_depth(self):
        snapshot = self.update(
            bids=levels(("97", "1"), ("105", "0"), ("98", "2")),
            asks=levels(("104", "1"), ("95", "0"), ("102", "2")),
            own_bids=levels(("96", "1")), own_asks=levels(("105", "1")),
        )
        self.assertEqual((snapshot.external_bid, snapshot.external_ask), (D("98"), D("102")))
        self.assertEqual(snapshot.microprice, D("100"))

    def test_malformed_or_incoherent_book_clears_cached_snapshot(self):
        cases = (
            dict(bids=[]), dict(bids=((D("99"),),)),
            dict(bids=((99, D("1")),)), dict(bids=((D("99"), D("NaN")),)),
            dict(bids=levels(("99", "-1"))), dict(bids=levels(("99.01", "1"))),
            dict(bids=levels(("99", "1"), ("99", "2"))),
            dict(bids=()), dict(bids=levels(("101", "1"))),
            dict(bids=levels(("102", "1"))),
            dict(own_bids=levels(("99", "4"))),
            dict(own_bids=levels(("99", "3"))),
            dict(own_bids=levels(("100", "1"))),
            dict(bids=levels(("99", "1"), ("97", "1")),
                 own_bids=levels(("98", "1"))),
            dict(own_asks=levels(("100", "1"))),
            dict(trusted=False), dict(trusted="yes"),
            dict(observed_monotonic=float("nan")), dict(observed_monotonic=-1),
            dict(observed_monotonic=True), dict(observed_monotonic=0.0),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.setUp()
                self.update()
                with self.assertRaises(ValueError):
                    self.update(**(dict(observed_monotonic=1.0) | changes))
                with self.assertRaises(ValueError):
                    self.market.snapshot()

    def test_only_sampled_external_mid_moves_feed_one_ewma(self):
        self.update()
        unchanged = self.update(observed_monotonic=0.5,
                                bids=levels(("109", "3")), asks=levels(("111", "1")))
        self.assertEqual(unchanged.ewma_move_bps, D("0"))
        sampled = self.update(observed_monotonic=5.0,
                              bids=levels(("109", "1")), asks=levels(("111", "3")))
        self.assertEqual(sampled.ewma_move_bps, D("500"))
        decayed = self.update(observed_monotonic=10.0,
                              bids=levels(("109", "100")), asks=levels(("111", "1")))
        self.assertEqual(decayed.ewma_move_bps, D("250"))
        self.assertNotEqual(sampled.microprice, decayed.microprice)

    def test_exact_one_second_cadence_uses_decimal_half_life(self):
        self.update(observed_monotonic=0.1)
        snapshot = self.update(observed_monotonic=1.1,
                               bids=levels(("100", "3")), asks=levels(("102", "1")))
        expected = (1 - D("0.5") ** (D("1") / D("5"))) * D("100")
        self.assertEqual(snapshot.ewma_move_bps, expected)

    def test_bad_update_does_not_advance_accepted_clock_or_ewma(self):
        self.update()
        with self.assertRaises(ValueError):
            self.update(observed_monotonic=10.0, own_bids=levels(("99", "4")))
        recovered = self.update(observed_monotonic=5.0,
                                bids=levels(("109", "3")), asks=levels(("111", "1")))
        self.assertEqual(recovered.ewma_move_bps, D("500"))
        with self.assertRaises(ValueError):
            self.update(observed_monotonic=4.0)
        with self.assertRaises(ValueError):
            self.market.snapshot()

    def test_unusable_microprice_arithmetic_falls_back_to_mid(self):
        with localcontext() as context:
            context.Emax = 4
            snapshot = self.update(bids=levels(("99", "90000")),
                                   asks=levels(("101", "90000")))
        self.assertEqual(snapshot.microprice, D("100"))

    def test_initial_snapshot_unavailable_and_metadata_validated(self):
        with self.assertRaises(ValueError):
            self.market.snapshot()
        for value in (D("0"), D("-1"), D("NaN"), 0.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MarketState("BTC", tick_size=value, size_step=D("1"), min_order_size=D("1"))


if __name__ == "__main__":
    unittest.main()
