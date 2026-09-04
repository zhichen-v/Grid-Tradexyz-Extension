"""Offline public-contract tests for the one-layer, inventory-aware V2 policy."""

from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from core.services.market_maker_v2.domain import (
    AccountSnapshot, FlattenIntent, InventoryDecision, MarketStateSnapshot,
    Side, StrategyState,
)
from core.services.market_maker_v2.quote_policy import QuoteUnavailable, VolumeQuotePolicy
from core.services.market_maker_v2.market_state import MarketState
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink


class VolumeQuotePolicyTests(unittest.TestCase):
    def market(self, **changes):
        values = dict(symbol="BTC", observed_monotonic=100.0,
                      external_bid=D("9999"), external_ask=D("10001"),
                      tick_size=D("0.1"), size_step=D("0.001"),
                      min_order_size=D("0.002"), trusted=True)
        return MarketStateSnapshot(**(values | changes))

    def account(self, position="0", **changes):
        values = dict(symbol="BTC", observed_monotonic=100.0,
                      position=D(position), equity=D("100"),
                      maker_fee_rate=D("0.00012"), taker_fee_rate=D("0.0004"),
                      open_order_count=0, authenticated=True,
                      entry_price=D("10000") if D(position) else None)
        return AccountSnapshot(**(values | changes))

    def risk(self, state=StrategyState.QUOTING, **changes):
        values = dict(state=state, buy_capacity=D("0.10"), sell_capacity=D("0.10"))
        return InventoryDecision(**(values | changes))

    def policy(self, **changes):
        values = dict(order_size=D("0.02"), target_net_edge_bps=D("0.8"),
                      volatility_multiplier=D("2"), hard_inventory_limit=D("0.10"),
                      skew_bps_at_hard=D("4"))
        return VolumeQuotePolicy(**(values | changes))

    def propose(self, *, policy=None, market=None, account=None, risk=None, now=100.0):
        return (policy or self.policy()).propose(
            market or self.market(), account or self.account(), risk or self.risk(), now=now)

    def by_side(self, plan):
        return {quote.side: quote for quote in plan.quotes}

    def test_calm_fee_floor_edge_and_single_post_only_layer(self):
        plan = self.propose()
        quotes = self.by_side(plan)
        self.assertEqual(plan.symbol, "BTC")
        self.assertEqual(set(quotes), {Side.BUY, Side.SELL})
        self.assertEqual(quotes[Side.BUY].price, D("9998.4"))
        self.assertEqual(quotes[Side.SELL].price, D("10001.6"))
        for quote in plan.quotes:
            self.assertEqual(quote.size, D("0.02"))
            self.assertEqual(quote.time_in_force, "POST_ONLY")
            self.assertFalse(quote.reduce_only)

    def test_current_authenticated_fee_changes_spread_without_policy_restart(self):
        policy = self.policy()
        before = self.by_side(self.propose(policy=policy))
        after = self.by_side(self.propose(policy=policy,
                                        account=self.account(maker_fee_rate=D("0.0002"))))
        self.assertEqual(after[Side.BUY].price, D("9997.6"))
        self.assertEqual(after[Side.SELL].price, D("10002.4"))
        self.assertLess(after[Side.BUY].price, before[Side.BUY].price)
        self.assertGreater(after[Side.SELL].price, before[Side.SELL].price)

    def test_valid_microprice_is_used_and_invalid_microprice_falls_back_to_mid(self):
        quotes = self.by_side(self.propose(market=self.market(microprice=D("10000.5"))))
        self.assertEqual(quotes[Side.BUY].price, D("9998.8"))
        self.assertEqual(quotes[Side.SELL].price, D("10002.2"))
        mid_plan = self.propose()
        for microprice in (None, D("0"), D("-1"), D("9998.9"), D("10001.1")):
            with self.subTest(microprice=microprice):
                self.assertEqual(self.propose(market=self.market(microprice=microprice)), mid_plan)

    def test_volatility_buffer_increases_spread_and_caps_at_five_bps(self):
        quotes = self.by_side(self.propose(market=self.market(ewma_move_bps=D("1"))))
        self.assertEqual(quotes[Side.BUY].price, D("9996.4"))
        self.assertEqual(quotes[Side.SELL].price, D("10003.6"))
        capped = self.propose(market=self.market(ewma_move_bps=D("2.5")))
        self.assertEqual(capped, self.propose(market=self.market(ewma_move_bps=D("1000000"))))
        capped_quotes = self.by_side(capped)
        self.assertEqual(capped_quotes[Side.BUY].price, D("9993.4"))
        self.assertEqual(capped_quotes[Side.SELL].price, D("10006.6"))

    def test_long_short_reservation_shift_keeps_two_sides_without_entry_anchor(self):
        flat = self.by_side(self.propose())
        for position, direction in (("0.05", -1), ("-0.05", 1)):
            with self.subTest(position=position):
                account = self.account(position)
                plan = self.propose(account=account)
                quotes = self.by_side(plan)
                self.assertEqual(len(quotes), 2)
                for side in (Side.BUY, Side.SELL):
                    self.assertGreater(direction * (quotes[side].price - flat[side].price), D("0"))
                    self.assertFalse(quotes[side].reduce_only)
                self.assertEqual(plan, self.propose(account=replace(
                    account, entry_price=D("50000"), unrealized_pnl=D("-500"))))

    def test_soft_band_halves_only_risk_increasing_size(self):
        for position, increasing in (("0.05", Side.BUY), ("-0.05", Side.SELL)):
            with self.subTest(position=position):
                quotes = self.by_side(self.propose(account=self.account(position),
                                                  risk=self.risk(StrategyState.SKEWED)))
                self.assertEqual(quotes[increasing].size, D("0.01"))
                reducing = Side.SELL if increasing == Side.BUY else Side.BUY
                self.assertEqual(quotes[reducing].size, D("0.02"))

    def test_explicit_side_capacity_bounds_desired_quotes_and_defaults_fail_closed(self):
        self.assertEqual(self.propose(risk=InventoryDecision(StrategyState.QUOTING)).quotes, ())
        quotes = self.by_side(self.propose(risk=self.risk(
            buy_capacity=D("0.004"), sell_capacity=D("0"))))
        self.assertEqual(set(quotes), {Side.BUY})
        self.assertEqual(quotes[Side.BUY].size, D("0.004"))
        # Existing orders are covered by the governor's aggregate desired capacities;
        # the quote policy neither assumes all are buys nor reserves them twice.
        self.assertEqual(self.propose(account=self.account(open_order_count=2)), self.propose())

    def test_sizes_floor_to_step_and_never_inflate_subminimum_capacity(self):
        quotes = self.by_side(self.propose(policy=self.policy(order_size=D("0.0059")),
            risk=self.risk(buy_capacity=D("0.0039"), sell_capacity=D("0.0019"))))
        self.assertEqual(set(quotes), {Side.BUY})
        self.assertEqual(quotes[Side.BUY].size, D("0.003"))
        # Half the configured size is below minimum: omit the soft-band adding side.
        quotes = self.by_side(self.propose(policy=self.policy(order_size=D("0.003")),
            account=self.account("0.05"), risk=self.risk(StrategyState.SKEWED)))
        self.assertEqual(set(quotes), {Side.SELL})
        self.assertEqual(quotes[Side.SELL].size, D("0.003"))

    def test_high_precision_capacity_and_price_do_not_round_across_tick_or_lot(self):
        capacity = D("0.199999999999999999999999999999999999999")
        policy = self.policy(order_size=D("1"), hard_inventory_limit=D("1"),
                             target_net_edge_bps=D("0"), volatility_multiplier=D("0"),
                             skew_bps_at_hard=D("0"))
        market = self.market(size_step=D("0.1"), min_order_size=D("0.1"))
        plan = self.propose(policy=policy, market=market,
            risk=self.risk(buy_capacity=capacity, sell_capacity=D("0")))
        self.assertEqual(len(plan.quotes), 1)
        self.assertEqual(plan.quotes[0].size, D("0.1"))
        self.assertLessEqual(plan.quotes[0].size, capacity)
        tiny_position_quotes = self.by_side(self.propose(policy=policy, market=market,
            account=self.account("1E-40"),
            risk=self.risk(buy_capacity=D("1"), sell_capacity=D("1"))))
        self.assertEqual(tiny_position_quotes[Side.BUY].size, D("0.9"))
        reducing = self.propose(policy=policy, market=market,
            account=self.account(str(capacity)),
            risk=self.risk(StrategyState.REDUCE_ONLY, sell_capacity=D("1"))).quotes
        self.assertEqual(len(reducing), 1)
        self.assertTrue(reducing[0].reduce_only)
        self.assertEqual(reducing[0].size, D("0.1"))
        market = replace(market, external_bid=D("1"), external_ask=D("3"),
            tick_size=D("1"), microprice=D("1.999999999999999999999999999999999999999"))
        quotes = self.by_side(self.propose(policy=policy, market=market,
            account=self.account(maker_fee_rate=D("0")),
            risk=self.risk(buy_capacity=D("1"), sell_capacity=D("1"))))
        self.assertEqual(quotes[Side.BUY].price, D("1"))
        self.assertEqual(quotes[Side.SELL].price, D("2"))

    def test_hard_position_bounds_are_not_overridden_by_generous_capacity(self):
        quotes = self.by_side(self.propose(account=self.account("0.099")))
        self.assertEqual(set(quotes), {Side.SELL})
        for position in ("0.10", "-0.10"):
            for state in (StrategyState.QUOTING, StrategyState.SKEWED):
                with self.subTest(position=position, state=state):
                    with self.assertRaises(QuoteUnavailable):
                        self.propose(account=self.account(position), risk=self.risk(state))
        for position in ("0.101", "-0.101"):
            with self.subTest(position=position):
                self.assertEqual(self.propose(account=self.account(position)).quotes, ())

    def test_reduce_only_uses_passive_touch_and_can_realize_a_loss(self):
        for position, side, price in (("0.10", Side.SELL, "10001"),
                                      ("-0.10", Side.BUY, "9999")):
            with self.subTest(position=position):
                account = self.account(position, entry_price=D("50000"))
                plan = self.propose(account=account, risk=self.risk(StrategyState.REDUCE_ONLY))
                self.assertEqual(len(plan.quotes), 1)
                quote = plan.quotes[0]
                self.assertEqual((quote.side, quote.price, quote.size), (side, D(price), D("0.02")))
                self.assertTrue(quote.reduce_only)
                self.assertEqual(quote.time_in_force, "POST_ONLY")
                self.assertEqual(plan, self.propose(account=replace(account, entry_price=D("1")),
                    risk=self.risk(StrategyState.REDUCE_ONLY)))

    def test_reduce_only_caps_residual_and_capacity_without_rounding_up(self):
        quotes = self.propose(account=self.account("-0.0099"),
            risk=self.risk(StrategyState.REDUCE_ONLY, buy_capacity=D("0.0049"))).quotes
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].size, D("0.004"))
        for position in ("0", "0.0019", "-0.0019"):
            with self.subTest(position=position):
                self.assertEqual(self.propose(account=self.account(position),
                    risk=self.risk(StrategyState.REDUCE_ONLY)).quotes, ())

    def test_inactive_states_never_emit_quotes(self):
        decisions = [InventoryDecision(StrategyState.COOLDOWN),
                     InventoryDecision(StrategyState.SESSION_COMPLETE),
                     InventoryDecision(StrategyState.FLATTENING, FlattenIntent(
                         "BTC", Side.SELL, D("0.02"), D("9900"), 105.0))]
        for risk in decisions:
            with self.subTest(state=risk.state):
                self.assertEqual(self.propose(account=self.account("0.02"), risk=risk).quotes, ())

    def test_large_inventory_shift_clamps_to_strict_external_passive_boundary(self):
        policy = self.policy(skew_bps_at_hard=D("100"))
        long = self.by_side(self.propose(policy=policy, account=self.account("0.05")))
        short = self.by_side(self.propose(policy=policy, account=self.account("-0.05")))
        self.assertEqual(long[Side.SELL].price, D("9999.1"))
        self.assertEqual(short[Side.BUY].price, D("10000.9"))
        self.assertLess(long[Side.BUY].price, long[Side.SELL].price)
        self.assertLess(short[Side.BUY].price, short[Side.SELL].price)

    def test_zero_fee_edge_and_buffer_still_never_self_cross(self):
        quotes = self.by_side(self.propose(
            policy=self.policy(target_net_edge_bps=D("0")),
            account=self.account(maker_fee_rate=D("0"))))
        self.assertEqual(quotes[Side.BUY].price, D("10000"))
        self.assertEqual(quotes[Side.SELL].price, D("10000.1"))

    def test_untrusted_stale_future_mismatched_or_untyped_inputs_fail_closed(self):
        invalid = [dict(market=self.market(trusted=False)),
                   dict(account=self.account(authenticated=False)),
                   dict(market=self.market(observed_monotonic=96.999)),
                   dict(account=self.account(observed_monotonic=89.999)),
                   dict(market=self.market(observed_monotonic=100.001)),
                   dict(account=self.account(observed_monotonic=100.001)),
                   dict(account=self.account(symbol="ETH")),
                   dict(account=self.account(maker_fee_rate=D("1E-5000"))),
                   dict(now=-1), dict(now=float("nan")), dict(now=float("inf")), dict(now=True)]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(QuoteUnavailable):
                    self.propose(**changes)
        for args in (({}, self.account(), self.risk()),
                     (self.market(), {}, self.risk()),
                     (self.market(), self.account(), {})):
            with self.subTest(untyped=args):
                with self.assertRaises(QuoteUnavailable):
                    self.policy().propose(*args, now=100.0)

    def test_exact_freshness_boundaries_are_accepted(self):
        self.assertEqual(self.propose(market=self.market(observed_monotonic=97.0),
            account=self.account(observed_monotonic=90.0)), self.propose())

    def test_invalid_financial_policy_parameters_are_rejected(self):
        invalid = [dict(order_size=D("0")), dict(hard_inventory_limit=D("0")),
                   dict(target_net_edge_bps=D("-0.1")), dict(volatility_multiplier=D("-1")),
                   dict(skew_bps_at_hard=D("-1")), dict(order_size=0.02),
                   dict(target_net_edge_bps=D("NaN")), dict(skew_bps_at_hard=D("Infinity"))]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.policy(**changes)
        with self.assertRaises(QuoteUnavailable):
            self.propose(account=self.account(maker_fee_rate=D("0.99999")))

    def test_inventory_capacities_require_finite_nonnegative_decimals(self):
        for capacity in (D("-1"), D("NaN"), D("Infinity"), 0.1, None):
            for name in ("buy_capacity", "sell_capacity"):
                with self.subTest(capacity=capacity, name=name):
                    with self.assertRaises(ValueError):
                        self.risk(**{name: capacity})
        for state in (StrategyState.COOLDOWN, StrategyState.SESSION_COMPLETE):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    self.risk(state)

    def test_parameter_grid_preserves_tick_lot_passivity_and_inventory_bounds(self):
        for tick in (D("0.1"), D("0.5"), D("1")):
            for fee in (D("0"), D("0.00012"), D("0.001")):
                for position in ("-0.099", "-0.05", "0", "0.05", "0.099"):
                    for state in (StrategyState.QUOTING, StrategyState.SKEWED):
                        with self.subTest(tick=tick, fee=fee, position=position, state=state):
                            market = self.market(tick_size=tick)
                            risk = self.risk(state, buy_capacity=D("0.0179"), sell_capacity=D("0.0139"))
                            quotes = self.by_side(self.propose(market=market,
                                account=self.account(position, maker_fee_rate=fee), risk=risk))
                            self.assertLessEqual(len(quotes), 2)
                            for side, quote in quotes.items():
                                self.assertEqual(quote.price % tick, D("0"))
                                self.assertEqual(quote.size % market.size_step, D("0"))
                                self.assertGreaterEqual(quote.size, market.min_order_size)
                                self.assertEqual(quote.time_in_force, "POST_ONLY")
                                if side == Side.BUY:
                                    self.assertLess(quote.price, market.external_ask)
                                    self.assertLessEqual(quote.size, risk.buy_capacity)
                                    self.assertLessEqual(D(position) + quote.size, D("0.10"))
                                else:
                                    self.assertGreater(quote.price, market.external_bid)
                                    self.assertLessEqual(quote.size, risk.sell_capacity)
                                    self.assertGreaterEqual(D(position) - quote.size, D("-0.10"))
                            if len(quotes) == 2:
                                self.assertLess(quotes[Side.BUY].price, quotes[Side.SELL].price)

    def test_policy_is_repeatable_and_does_not_mutate_inputs(self):
        policy, market, account, risk = self.policy(), self.market(), self.account("0.02"), self.risk()
        before = (market, account, risk)
        first = policy.propose(market, account, risk, now=100.0)
        self.assertEqual(first, policy.propose(market, account, risk, now=100.0))
        self.assertEqual((market, account, risk), before)

    def test_external_book_to_policy_to_typed_jsonl_contract(self):
        market_state = MarketState("BTC", tick_size=D("0.1"),
                                   size_step=D("0.001"), min_order_size=D("0.002"))
        market_state.update(
            bids=((D("10000"), D("0.02")), (D("9999"), D("0.10"))),
            asks=((D("10001"), D("0.10")),),
            own_bids=((D("10000"), D("0.02")),), own_asks=(),
            observed_monotonic=100.0, trusted=True)
        market = market_state.snapshot()
        self.assertEqual((market.external_bid, market.external_ask), (D("9999"), D("10001")))
        plan = self.propose(market=market)
        self.assertEqual(plan, self.propose())
        self.assertTrue(all(quote.time_in_force == "POST_ONLY" for quote in plan.quotes))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with JsonlTelemetrySink(path) as sink:
                sink.emit(plan)
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["event"], "quote_plan")
        self.assertEqual(record["data"]["symbol"], "BTC")
        self.assertEqual([quote["price"] for quote in record["data"]["quotes"]],
                         ["9998.4", "10001.6"])
        self.assertTrue(all(isinstance(quote["size"], str) for quote in record["data"]["quotes"]))


if __name__ == "__main__":
    unittest.main()
