"""Public DTO and inventory exit contracts; no network or live account."""

from dataclasses import asdict, FrozenInstanceError, replace
from decimal import Decimal as D
import unittest

from core.services.market_maker_v2.domain import (
    AccountSnapshot,
    FlattenIntent, InventoryDecision, MarketStateSnapshot,
    QuoteIntent, QuotePlan, SessionReport, Side, StrategyState,
)


def market():
    return MarketStateSnapshot("BTC", 100.0, D("999.9"), D("1000.1"), D("0.1"),
                               D("0.00001"), D("0.00020"), True)


def account():
    return AccountSnapshot("BTC", 100.0, D("0"), D("100"), D("0.00012"),
                           D("0.00035"), 0, True)


class DomainContractTests(unittest.TestCase):
    def test_immutable_explicit_models_have_no_credential_or_raw_payload(self):
        snapshot = account()
        with self.assertRaises(FrozenInstanceError):
            snapshot.equity = D("200")
        with self.assertRaises(TypeError):
            replace(snapshot, credentials="do-not-retain")
        self.assertFalse(hasattr(snapshot, "__dict__"))
        self.assertTrue(set(asdict(snapshot)).isdisjoint({"raw_data", "signer", "credentials", "account_index"}))

    def test_financial_values_are_decimal_and_reject_nonfinite_or_invalid_shapes(self):
        for value in (1.0, "1", D("NaN"), D("Infinity")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(account(), position=value)
        for changes in ({"external_bid": D("1000.1")}, {"tick_size": D("0")},
                        {"external_ask": D("1000.15")}, {"trusted": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(market(), **changes)

    def test_one_post_only_quote_per_side_and_no_own_cross(self):
        buy = QuoteIntent(Side.BUY, D("999"), D("0.0002"))
        sell = QuoteIntent(Side.SELL, D("1001"), D("0.0002"))
        self.assertEqual(buy.time_in_force, "POST_ONLY")
        self.assertEqual(len(QuotePlan("BTC", (buy, sell)).quotes), 2)
        for quotes in ((buy, buy), (buy, replace(sell, price=buy.price)), [buy]):
            with self.subTest(quotes=quotes), self.assertRaises(ValueError):
                QuotePlan("BTC", quotes)

    def test_bounded_flatten_intent_is_reducing_limit_ioc_and_typed(self):
        intent = FlattenIntent("BTC", Side.BUY, D("0.0002"), D("1001"), 120.0)
        self.assertTrue(intent.reduce_only)
        self.assertEqual(intent.time_in_force, "IOC")
        self.assertEqual(InventoryDecision(StrategyState.FLATTENING, intent).flatten, intent)
        with self.assertRaises(ValueError):
            InventoryDecision(StrategyState.QUOTING, intent)
        # Flat stop still needs cancel + terminal proof before session complete.
        pending_cancel = InventoryDecision(StrategyState.FLATTENING)
        self.assertIsNone(pending_cancel.flatten)
        self.assertEqual((pending_cancel.buy_capacity, pending_cancel.sell_capacity), (D("0"), D("0")))

    def test_session_report_does_not_promote_nonflat_or_invent_economics(self):
        report = SessionReport("BTC", False, D("-0.0002"), 0)
        self.assertIsNone(report.all_in_net_pnl)
        self.assertIsNone(report.all_in_net_cost_bps)
        with self.assertRaises(ValueError):
            replace(report, complete=True)
        with self.assertRaises(ValueError):
            SessionReport("BTC", True, D("0"), 1)


class InventoryExitScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_rally_skew_reduce_loss_exit_and_flat_deadline_close(self):
        import test_mm_v2_bounded_execution as fixtures
        from core.services.market_maker_v2.domain import FillEvent, LiquidityRole
        from core.services.market_maker_v2.inventory_governor import InventoryGovernor
        from core.services.market_maker_v2.orchestrator import bounded_exit
        from core.services.market_maker_v2.quote_policy import VolumeQuotePolicy
        from core.services.market_maker_v2.session_ledger import SessionLedger

        fixture = fixtures.BoundedExecutionTests()
        fixture.setUp()  # Real frozen manager, fake exchange only; no network client.
        fixture.position = D("0")
        initial = await fixture.account_snapshot()
        ledger = SessionLedger(initial)
        governor = InventoryGovernor(order_size=D("0.1"), soft_limit=D("0.1"),
            hard_limit=D("0.2"), stop_loss_usdg=D("0.15"), max_hold_seconds=60,
            cooldown_seconds=5, max_session_loss_usdg=D("10"),
            session_started_monotonic=100, session_deadline_monotonic=110,
            ioc_slippage_ticks=2)
        policy = VolumeQuotePolicy(order_size=D("0.1"), target_net_edge_bps=D("0.2"),
            volatility_multiplier=D("1"), hard_inventory_limit=D("0.2"), skew_bps_at_hard=D("10"))
        for index, bid, ask, expected in (
                (1, "99.9", "100.1", StrategyState.SKEWED),
                (2, "100.4", "100.5", StrategyState.REDUCE_ONLY)):
            fixture.time.value = 100 + index
            fixture.position -= D("0.1")
            ledger.ingest_fill(FillEvent(f"entry-{index}", f"entry-order-{index}", "BTC",
                Side.SELL, D("0.1"), D("100"), D("0.0012"), LiquidityRole.MAKER,
                fixture.time(), D("100")))
            current = await fixture.account_snapshot()
            book = replace(fixture.market_snapshot(), external_bid=D(bid), external_ask=D(ask))
            risk = governor.evaluate(book, current, ledger.snapshot(now=fixture.time()),
                fixture.port.snapshot(), now=fixture.time())
            self.assertEqual(risk.state, expected)
            quotes = policy.propose(book, current, risk, now=fixture.time()).quotes
            if expected == StrategyState.REDUCE_ONLY:
                self.assertEqual(len(quotes), 1)
                self.assertEqual((quotes[0].side, quotes[0].price, quotes[0].reduce_only),
                                 (Side.BUY, D("100.4"), True))
                self.assertGreater(quotes[0].price, current.entry_price)  # No breakeven lock.

        await fixture.seed_maker()
        fixture.time.value = 103
        current = replace(await fixture.account_snapshot(), open_order_count=1)
        risk = governor.evaluate(fixture.market_snapshot(), current, ledger.snapshot(now=103),
                                  fixture.port.snapshot(), now=103)
        self.assertEqual(risk.state, StrategyState.FLATTENING)
        self.assertEqual(policy.propose(fixture.market_snapshot(), current, risk, now=103).quotes, ())

        fixture.fill_size = D("0.1")  # Force a proved partial followed by a residual IOC.
        balance = D("99.9976")  # Independent fake-exchange cash after the two maker fees.
        original_create = fixture.create

        async def create_and_record(symbol, side, order_type, amount, price, params):
            nonlocal balance
            before = fixture.position
            order = await original_create(symbol, side, order_type, amount, price, params)
            fill = abs(before - fixture.position)
            fee = fill * price * D("0.0004")
            balance += fill * (D("100") - price) - fee
            ledger.ingest_fill(FillEvent(f"exit-fill-{fixture.sequence}", str(fixture.sequence),
                "BTC", Side.BUY, fill, price, fee, LiquidityRole.TAKER,
                fixture.time(), D("100.95"), "risk-1"))
            return order

        async def account_truth():
            snapshot = await fixture.account_snapshot()
            unrealized = fixture.position * D("0.95")
            return replace(snapshot, equity=balance + unrealized, unrealized_pnl=unrealized)

        fixture.adapter.create_order.side_effect = create_and_record
        fixture.account.snapshot.side_effect = account_truth
        fixture.time.value = 104
        report = await bounded_exit(fixture.port, fixture.market, fixture.clock, symbol="BTC",
            flatten_id="risk-1", deadline_monotonic=governor.exit_deadline,
            ioc_slippage_ticks=2, authorize_bounded_flatten=True)
        self.assertTrue(report.complete)
        self.assertEqual(report.attempts, 2)
        self.assertLess(fixture.events.index("cancel"), fixture.events.index("ioc"))
        ledger.record_exit(report)
        self.assertEqual(governor.confirm_exit(report.final_result, now=104).state, StrategyState.COOLDOWN)
        self.assertEqual(ledger.snapshot(now=104).forced_flatten_count, 1)
        self.assertEqual(ledger.snapshot(now=104).realized_net_pnl, D("-0.250496"))

        fixture.time.value = 110
        current = await account_truth()
        risk = governor.evaluate(fixture.market_snapshot(), current, ledger.snapshot(now=110),
            fixture.port.snapshot(), now=110)
        self.assertEqual(risk.state, StrategyState.FLATTENING)
        self.assertIsNone(risk.flatten)  # Even flat deadline needs cancellation proof.
        fixture.time.value = 111
        final_exit = await bounded_exit(fixture.port, fixture.market, fixture.clock, symbol="BTC",
            flatten_id="deadline-1", deadline_monotonic=governor.exit_deadline,
            ioc_slippage_ticks=2, authorize_bounded_flatten=True)
        self.assertTrue(final_exit.complete)
        self.assertEqual(final_exit.attempts, 0)
        ledger.record_exit(final_exit)
        self.assertEqual(governor.confirm_exit(final_exit.final_result, now=111).state,
                         StrategyState.SESSION_COMPLETE)
        final = ledger.finalize(final_exit.final_result.account_snapshot, now=111)
        self.assertTrue(final.complete)
        self.assertEqual((final.maker_fee, final.taker_fee, final.realized_gross_pnl),
                         (D("0.0024"), D("0.008096"), D("-0.24")))
        self.assertEqual(final.all_in_net_pnl, D("-0.250496"))


if __name__ == "__main__":
    unittest.main()
