"""Session economics contracts with exact hand-calculated, incremental fills."""

from dataclasses import replace
from decimal import Decimal as D
import unittest
from unittest.mock import Mock

from core.services.market_maker_v2.domain import (
    AccountSnapshot, CashflowEvent, CashflowKind, FillEvent, LiquidityRole, MarkEvent, Side,
)
from core.services.market_maker_v2.session_ledger import LedgerError, SessionLedger


def account(time=0.0, equity="100", **changes):
    base = AccountSnapshot("BTC", time, D("0"), D(equity), D("0.0001"),
                           D("0.0003"), 0, True)
    return replace(base, **changes)


def fill(identifier, side, size, price, time, *, fee=None, role=LiquidityRole.MAKER,
         order=None, reference=None, flatten=None):
    size, price = D(size), D(price)
    exact_fee = size * price * (D("0.0001") if role == LiquidityRole.MAKER else D("0.0003"))
    return FillEvent(identifier, order or identifier, "BTC", side, size, price,
                     exact_fee if fee is None else D(fee), role, time,
                     None if reference is None else D(reference), flatten)


class SessionLedgerTests(unittest.TestCase):
    def test_observed_side_uptime_keeps_union_and_unknown_coverage_distinct(self):
        ledger = self.ledger()
        for when, sides in ((1, (Side.BUY,)), (3, (Side.BUY, Side.SELL)),
                            (7, (Side.SELL,)), (10, ())):
            ledger.observe(MarkEvent("BTC", when, D("100"), bool(sides), sides))
        report = ledger.snapshot(now=12)
        self.assertEqual((report.quote_uptime_seconds, report.buy_quote_seconds,
                          report.sell_quote_seconds, report.two_sided_quote_seconds),
                         (D("9"), D("6"), D("7"), D("4")))
        ledger.observe(MarkEvent("BTC", 12, D("100"), True))
        self.assertIsNone(ledger.snapshot(now=13).two_sided_quote_seconds)
        for sides in ((Side.BUY, Side.BUY), ("buy",), ()):
            with self.assertRaises(ValueError):
                MarkEvent("BTC", 14, D("100"), True, sides)

    def test_finalization_rejects_stale_underlying_account_inputs(self):
        ledger = self.ledger()
        final = account(100.0, inputs_observed_monotonic=0.0)
        self.assertFalse(final.fresh(100.0))
        report = ledger.finalize(final, now=100.0)
        self.assertFalse(report.complete)
        self.assertIsNone(report.all_in_net_pnl)

    def ledger(self, **kwargs):
        return SessionLedger(account(), **kwargs)

    def roundtrip(self, ledger, *, sell="101", reference=None):
        ledger.ingest_fill(fill("buy", Side.BUY, "1", "100", 1.0, reference=reference))
        ledger.ingest_fill(fill("sell", Side.SELL, "1", sell, 2.0, reference=reference))

    def test_same_timestamp_partial_fills_use_incremental_quantity_and_average_cost(self):
        ledger = self.ledger()
        for event in (fill("b1", Side.BUY, "1", "100", 1.0),
                      fill("b2", Side.BUY, "1", "102", 1.0),
                      fill("s1", Side.SELL, ".5", "104", 2.0, order="exit"),
                      fill("s2", Side.SELL, "1.5", "100", 3.0, order="exit")):
            self.assertTrue(ledger.ingest_fill(event))
        report = ledger.finalize(account(4.0, "99.9596"), now=4.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.maker_fill_count, 4)
        self.assertEqual(report.ledger_position, 0)
        self.assertEqual(report.maker_buy_turnover, D("202"))
        self.assertEqual(report.maker_sell_turnover, D("202"))
        self.assertEqual(report.maker_turnover_total, D("404"))
        self.assertEqual(report.realized_gross_pnl, 0)
        self.assertEqual(report.maker_fee, D(".0404"))
        self.assertEqual(report.all_in_net_pnl, D("-.0404"))
        self.assertEqual(report.all_in_net_cost_bps, D("1"))

    def test_maker_reversal_closes_old_inventory_and_prices_only_new_remainder(self):
        ledger = self.ledger()
        ledger.ingest_fill(fill("b", Side.BUY, "1", "100", 1.0))
        ledger.ingest_fill(fill("reverse", Side.SELL, "2", "101", 2.0))
        diagnostic = ledger.snapshot(now=2.0)
        self.assertEqual(diagnostic.ledger_position, D("-1"))
        self.assertEqual(diagnostic.realized_gross_pnl, D("1"))
        ledger.ingest_fill(fill("close", Side.BUY, "1", "100", 3.0))
        report = ledger.finalize(account(4.0, "101.9598"), now=4.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.realized_gross_pnl, D("2"))
        self.assertEqual(report.maker_turnover_total, D("402"))
        self.assertEqual(report.all_in_net_pnl, D("1.9598"))

    def test_repeating_average_cost_preserves_exact_flat_cashflow_gross(self):
        ledger = self.ledger()
        ledger.ingest_fill(fill("b1", Side.BUY, "1", "33", 1.0))
        ledger.ingest_fill(fill("b2", Side.BUY, "2", "33.5", 2.0))
        for index in range(3):
            ledger.ingest_fill(fill(f"s{index}", Side.SELL, "1", "34", 3.0 + index))
        report = ledger.finalize(account(6.0, "101.9798"), now=6.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.realized_gross_pnl, D("102") - D("100"))
        self.assertEqual(report.equity_reconciliation_difference, 0)
        self.assertEqual(report.all_in_net_pnl, D("1.9798"))

    def test_losing_roundtrip_is_complete_without_a_per_trade_profit_gate(self):
        ledger = self.ledger()
        self.roundtrip(ledger, sell="99")
        report = ledger.finalize(account(3.0, "98.9801"), now=3.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, D("-1.0199"))
        self.assertLess(report.fee_cover_ratio, 0)
        self.assertFalse(report.failed)

    def test_partial_taker_flatten_is_one_group_and_all_costs_are_counted_once(self):
        ledger = self.ledger()
        ledger.ingest_fill(fill("entry", Side.SELL, "2", "100", 1.0))
        ledger.ingest_fill(fill("exit1", Side.BUY, ".5", "101", 2.0,
                                role=LiquidityRole.TAKER, flatten="stop"))
        ledger.ingest_fill(fill("exit2", Side.BUY, "1.5", "102", 3.0,
                                role=LiquidityRole.TAKER, flatten="stop"))
        report = ledger.finalize(account(4.0, "96.41895"), now=4.0)
        self.assertTrue(report.complete)
        self.assertEqual((report.maker_fill_count, report.taker_fill_count), (1, 2))
        self.assertEqual(report.maker_turnover_total, D("200"))
        self.assertEqual(report.taker_flatten_turnover, D("203.5"))
        self.assertEqual(report.realized_gross_pnl, D("-3.5"))
        self.assertEqual(report.taker_fee, D(".06105"))
        self.assertEqual(report.all_in_net_pnl, D("-3.58105"))
        self.assertEqual(report.all_in_net_cost_bps, D("179.0525"))
        self.assertEqual(report.forced_flatten_count, 1)
        self.assertEqual(report.forced_flatten_loss, D("3.56105"))

    def test_taker_requires_flatten_identity_and_cannot_increase_or_cross_inventory(self):
        with self.assertRaises(ValueError):
            fill("unowned", Side.BUY, "1", "100", 1.0, role=LiquidityRole.TAKER)
        for side, size in ((Side.BUY, "1"), (Side.SELL, "2")):
            with self.subTest(side=side, size=size):
                ledger = self.ledger()
                ledger.ingest_fill(fill("entry", Side.BUY, "1", "100", 1.0))
                with self.assertRaises(LedgerError):
                    ledger.ingest_fill(fill("bad", side, size, "100", 2.0,
                                            role=LiquidityRole.TAKER, flatten="stop"))
                report = ledger.snapshot(now=2.0)
                self.assertTrue(report.failed)
                self.assertEqual(report.ledger_position, D("1"))
                self.assertEqual(report.taker_fill_count, 0)
                self.assertEqual(report.taker_fee, 0)

    def test_funding_is_net_pnl_but_external_transfer_is_not_pnl_or_fee_cover(self):
        ledger = self.ledger()
        self.roundtrip(ledger)
        ledger.ingest_cashflow(CashflowEvent("fund", "BTC", 3.0, D("-.005"), CashflowKind.FUNDING))
        ledger.ingest_cashflow(CashflowEvent("deposit", "BTC", 4.0, D("10"), CashflowKind.TRANSFER))
        report = ledger.finalize(account(5.0, "110.9749"), now=5.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.funding, D("-.005"))
        self.assertEqual(report.external_transfers, D("10"))
        self.assertEqual(report.all_in_net_pnl, D(".9749"))
        self.assertEqual(report.fee_cover_ratio, D("1") / D(".0201"))
        self.assertEqual(report.equity_reconciliation_difference, 0)

    def test_exact_duplicate_fill_is_noop_even_after_time_has_advanced(self):
        sink = Mock()
        ledger = self.ledger(telemetry=sink)
        event = fill("entry", Side.BUY, "1", "100", 1.0)
        self.assertTrue(ledger.ingest_fill(event))
        ledger.observe(MarkEvent("BTC", 5.0, D("100"), True))
        emitted = sink.emit.call_count
        self.assertFalse(ledger.ingest_fill(event))
        self.assertFalse(ledger.ingest_fill(replace(event, reference_price=D("100.5"))))
        self.assertFalse(ledger.ingest_fill(replace(event, reference_price=D("101"))))
        self.assertEqual(sink.emit.call_count, emitted)
        report = ledger.snapshot(now=6.0)
        self.assertFalse(report.failed)
        self.assertEqual(report.maker_fill_count, 1)
        self.assertEqual(report.maker_fee, D(".01"))
        self.assertFalse(report.decomposition_complete)

    def test_conflicting_fill_id_latches_failure_without_partially_updating_totals(self):
        ledger = self.ledger()
        event = fill("entry", Side.BUY, "1", "100", 1.0)
        ledger.ingest_fill(event)
        with self.assertRaises(LedgerError):
            ledger.ingest_fill(replace(event, size=D("2")))
        report = ledger.snapshot(now=2.0)
        self.assertTrue(report.failed)
        self.assertEqual(report.maker_turnover_total, D("100"))
        self.assertEqual(report.ledger_position, D("1"))
        with self.assertRaises(LedgerError):
            ledger.ingest_fill(fill("later", Side.SELL, "1", "100", 3.0))
        self.assertEqual(ledger.snapshot(now=3.0).maker_fill_count, 1)

    def test_new_out_of_order_fill_or_cashflow_is_rejected_atomically(self):
        for event in (fill("late", Side.SELL, "1", "101", 4.0),
                      CashflowEvent("late", "BTC", 4.0, D("1"), CashflowKind.FUNDING)):
            with self.subTest(event=type(event).__name__):
                ledger = self.ledger()
                ledger.ingest_fill(fill("entry", Side.BUY, "1", "100", 1.0))
                ledger.observe(MarkEvent("BTC", 5.0, D("100"), False))
                ingest = ledger.ingest_fill if isinstance(event, FillEvent) else ledger.ingest_cashflow
                with self.assertRaises(LedgerError):
                    ingest(event)
                report = ledger.snapshot(now=5.0)
                self.assertTrue(report.failed)
                self.assertEqual(report.maker_turnover_total, D("100"))
                self.assertEqual(report.maker_fee, D(".01"))
                self.assertEqual(report.funding, 0)

    def test_cashflow_duplicate_is_noop_but_conflict_preserves_accepted_amount(self):
        ledger = self.ledger()
        event = CashflowEvent("fund", "BTC", 1.0, D("-.005"), CashflowKind.FUNDING)
        self.assertTrue(ledger.ingest_cashflow(event))
        ledger.observe(MarkEvent("BTC", 5.0, D("100"), False))
        self.assertFalse(ledger.ingest_cashflow(event))
        with self.assertRaises(LedgerError):
            ledger.ingest_cashflow(replace(event, amount=D("-.5")))
        report = ledger.snapshot(now=5.0)
        self.assertTrue(report.failed)
        self.assertEqual(report.funding, D("-.005"))

    def test_quoting_union_and_inventory_use_their_correct_time_denominators(self):
        ledger = self.ledger()
        ledger.observe(MarkEvent("BTC", 0.0, D("100"), True))
        ledger.ingest_fill(fill("buy", Side.BUY, "1", "100", 1.0, fee="0"))
        ledger.observe(MarkEvent("BTC", 3.0, D("100"), False))
        ledger.ingest_fill(fill("sell", Side.SELL, "1", "100", 5.0, fee="0"))
        ledger.observe(MarkEvent("BTC", 7.0, D("100"), True))
        report = ledger.finalize(account(10.0), now=10.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.duration_seconds, D("10"))
        self.assertEqual(report.quote_uptime_seconds, D("6"))
        self.assertEqual(report.average_abs_inventory, D(".4"))
        self.assertEqual(report.p95_abs_inventory, D("1"))
        self.assertEqual(report.maker_turnover_per_quote_hour, D("120000"))
        self.assertEqual(report.fills_per_quote_hour, D("1200"))

    def test_inventory_age_resets_only_on_flat_or_reversal_and_snapshot_does_not_advance_time(self):
        ledger = self.ledger()
        ledger.ingest_fill(fill("entry", Side.BUY, "1", "100", 1.0))
        self.assertEqual(ledger.snapshot(now=20.0).inventory_age, D("19"))
        ledger.ingest_fill(fill("add", Side.BUY, "1", "100", 4.0))
        ledger.ingest_fill(fill("reduce", Side.SELL, ".5", "100", 6.0))
        self.assertEqual(ledger.snapshot(now=6.0).inventory_age, D("5"))
        ledger.ingest_fill(fill("reverse", Side.SELL, "2", "100", 8.0))
        self.assertEqual(ledger.snapshot(now=9.0).inventory_age, D("1"))
        ledger.ingest_fill(fill("flat", Side.BUY, ".5", "100", 10.0))
        self.assertEqual(ledger.snapshot(now=11.0).inventory_age, 0)

    def test_drawdown_includes_marked_loss_and_funding_but_not_external_transfer(self):
        ledger = self.ledger()
        ledger.ingest_fill(fill("entry", Side.BUY, "1", "100", 1.0))
        ledger.observe(MarkEvent("BTC", 2.0, D("98"), False))
        ledger.ingest_cashflow(CashflowEvent("fund", "BTC", 3.0, D("-.1"), CashflowKind.FUNDING))
        ledger.ingest_cashflow(CashflowEvent("deposit", "BTC", 4.0, D("10"), CashflowKind.TRANSFER))
        ledger.ingest_fill(fill("exit", Side.SELL, "1", "100", 5.0))
        report = ledger.finalize(account(6.0, "109.88"), now=6.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.max_drawdown, D("2.11"))
        self.assertEqual(report.all_in_net_pnl, D("-.12"))

    def test_bad_final_account_boundaries_keep_diagnostics_without_final_economics(self):
        cases = (({"authenticated": False}, 5.0), ({"symbol": "ETH"}, 5.0),
                 ({"open_order_count": 1}, 5.0), ({"equity": D("101")}, 5.0),
                 ({"position": D("1"), "entry_price": D("100")}, 5.0),
                 ({"observed_monotonic": 4.0}, 5.0), ({}, 16.0))
        for changes, now in cases:
            with self.subTest(changes=changes, now=now):
                ledger = self.ledger()
                self.roundtrip(ledger)
                ledger.observe(MarkEvent("BTC", 5.0, D("101"), False))
                final = replace(account(5.0, "100.9799"), **changes)
                report = ledger.finalize(final, now=now)
                self.assertFalse(report.complete)
                self.assertIsNone(report.all_in_net_pnl)
                self.assertIsNone(report.all_in_net_cost_bps)
                self.assertIsNone(report.fee_cover_ratio)
                self.assertEqual(report.maker_fill_count, 2)
                self.assertEqual(report.realized_gross_pnl, D("1"))

    def test_nonflat_ledger_cannot_borrow_external_flat_to_create_success(self):
        ledger = self.ledger()
        ledger.ingest_fill(fill("entry", Side.SELL, "1", "100", 1.0))
        ledger.observe(MarkEvent("BTC", 2.0, D("102"), False))
        report = ledger.finalize(account(3.0, "97.99"), now=3.0)
        self.assertFalse(report.complete)
        self.assertEqual(report.ledger_position, D("-1"))
        self.assertEqual(report.maker_fill_count, 1)
        self.assertIsNone(report.all_in_net_pnl)
        self.assertIsNone(report.all_in_net_cost_bps)

    def test_missing_fill_reference_keeps_financials_without_inventing_decomposition(self):
        ledger = self.ledger()
        self.roundtrip(ledger)
        report = ledger.finalize(account(3.0, "100.9799"), now=3.0)
        self.assertTrue(report.complete)
        self.assertFalse(report.failed)
        self.assertEqual(report.all_in_net_pnl, D(".9799"))
        self.assertFalse(report.decomposition_complete)
        self.assertIsNone(report.spread_capture)
        self.assertIsNone(report.inventory_markout)
        self.assertIsNone(report.flatten_concession)

    def test_complete_reference_decomposition_conserves_session_gross(self):
        ledger = self.ledger()
        self.roundtrip(ledger, sell="102", reference="101")
        report = ledger.finalize(account(3.0, "101.9798"), now=3.0)
        self.assertTrue(report.complete)
        self.assertTrue(report.decomposition_complete)
        self.assertEqual(report.spread_capture, D("2"))
        self.assertEqual(report.inventory_markout, 0)
        self.assertEqual(report.flatten_concession, 0)
        self.assertEqual(report.realized_gross_pnl, D("2"))

    def test_telemetry_sink_failure_never_discards_fill_or_breaks_accounting(self):
        sink = Mock()
        sink.emit.side_effect = RuntimeError("DO_NOT_ECHO_SECRET")
        ledger = self.ledger(telemetry=sink)
        self.roundtrip(ledger)
        report = ledger.finalize(account(3.0, "100.9799"), now=3.0)
        self.assertTrue(report.complete)
        self.assertFalse(report.failed)
        self.assertEqual(report.maker_fill_count, 2)
        self.assertEqual(report.all_in_net_pnl, D(".9799"))
        self.assertGreaterEqual(report.telemetry_errors, 3)
        self.assertNotIn("DO_NOT_ECHO_SECRET", repr(report))

    def test_zero_turnover_terminal_has_no_ratios_and_cannot_be_reopened(self):
        ledger = self.ledger()
        report = ledger.finalize(account(10.0), now=10.0)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, 0)
        self.assertIsNone(report.all_in_net_cost_bps)
        self.assertIsNone(report.fee_cover_ratio)
        self.assertIsNone(report.maker_turnover_per_quote_hour)
        with self.assertRaises(LedgerError):
            ledger.ingest_fill(fill("later", Side.BUY, "1", "100", 11.0))
        with self.assertRaises(LedgerError):
            ledger.finalize(account(12.0), now=12.0)


if __name__ == "__main__":
    unittest.main()
