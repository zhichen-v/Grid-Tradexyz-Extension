"""Offline report contracts: actual economics, failed windows and exact aggregate costs."""

import contextlib
from dataclasses import replace
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest

from core.services.market_maker_v2.domain import (
    AccountSnapshot, CashflowEvent, CashflowKind, ExecutionHealth, ExecutionResult,
    ExecutionSnapshot, ExecutionStatus, FillEvent, LiquidityRole, MarkEvent, Side, FailureDiagnostic,
)
from core.services.market_maker_v2.session_ledger import SessionLedger
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink
from core.services.market_maker_v2.config import load_config
from scripts.analyze_mm_v2_session import analyze_public_books, build_report, candidate_quantity_table, main


class SessionAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def session(self, name="session.jsonl", *, sell="101", funding="0", transfer="0",
                complete=True, taker=False, dry=False, source_timestamps=(None, None), failure=False):
        path = self.root / name
        initial = AccountSnapshot("BTC", 0.0, D(0), D(299), D(".0001"), D(".0003"), 0, True)
        with JsonlTelemetrySink(path) as sink:
            ledger = SessionLedger(initial, telemetry=sink)
            if dry:
                sink.emit(ExecutionResult(ExecutionStatus.SIMULATED,
                    ExecutionSnapshot(ExecutionHealth.HEALTHY, 0, True, "BTC", 1.0, ())))
            else:
                ledger.observe(MarkEvent("BTC", .5, D(100), True, (Side.BUY, Side.SELL)))
                ledger.ingest_fill(FillEvent("b", "buy", "BTC", Side.BUY, D(1), D(100),
                    D(".01"), LiquidityRole.MAKER, 1.0, D(100), source_timestamp_ms=source_timestamps[0]))
                ledger.ingest_fill(FillEvent("s", "sell", "BTC", Side.SELL, D(1), D(sell),
                    D(sell) * D(".0003" if taker else ".0001"),
                    LiquidityRole.TAKER if taker else LiquidityRole.MAKER, 2.0, D(100),
                    "exit" if taker else None, source_timestamp_ms=source_timestamps[1]))
                ledger.observe(MarkEvent("BTC", 2.5, D(100), False, ()))
            for identifier, amount, kind in (("f", funding, CashflowKind.FUNDING),
                                               ("t", transfer, CashflowKind.TRANSFER)):
                if D(amount):
                    ledger.ingest_cashflow(CashflowEvent(identifier, "BTC", 3.0, D(amount), kind))
            snapshot = ledger.snapshot(now=4.0)
            final = replace(initial, observed_monotonic=4.0,
                            equity=initial.equity + snapshot.realized_net_pnl + D(transfer))
            if complete and not dry:
                ledger.finalize(final, now=4.0)
            else:
                sink.emit(final)
                sink.emit(snapshot)
            if failure:
                sink.emit(FailureDiagnostic("BTC", "disconnecting", "TimeoutError"))
        return path

    def test_failure_after_final_report_keeps_economics_unverified(self):
        report = self.analyze([self.session(failure=True)])
        session = report["sessions"][0]
        self.assertEqual(session["failure_diagnostics"][0]["stage"], "disconnecting")
        self.assertIn("runtime_failure_diagnostic", session["incomplete_reasons"])
        self.assertFalse(report["aggregate"]["economics_evaluated"])
        self.assertIsNone(report["aggregate"]["all_in_net_pnl"])

    def analyze(self, paths, **changes):
        arguments = dict(candidate="edge_0.2", mode="live", planned_seconds="100",
                         wall_seconds=["4"] * len(paths), allocated_capital="50")
        arguments.update(changes)
        return build_report(paths, **arguments)

    def test_aggregate_keeps_losing_session_and_fixed_windows_sums_then_divides(self):
        profit = self.session("profit.jsonl")
        loss = self.session("loss.jsonl", sell="99")
        report = self.analyze([profit, loss], wall_seconds=["4", "120"])
        totals = report["aggregate"]
        self.assertTrue(totals["economics_evaluated"])
        self.assertEqual(D(totals["observed_totals"]["maker_turnover_total"]), D(400))
        self.assertEqual(D(totals["all_in_net_pnl"]), D("-.04"))
        self.assertEqual(D(totals["all_in_net_cost_per_10000_usdg"]), D(1))
        self.assertEqual(D(totals["fee_cover_ratio"]), D(0))
        self.assertFalse(totals["fee_neutral_observed"])
        self.assertEqual(D(totals["comparison_window_seconds"]), D(220))
        self.assertEqual(D(totals["maker_turnover_per_wall_hour"]), D(400) * 3600 / 220)
        self.assertEqual(D(totals["turnover_over_allocated_capital"]), D(8))
        self.assertIsNone(totals["objective_met"])
        self.assertIsNone(report["sessions"][0]["markout_1s"])

    def test_funding_transfers_and_taker_exit_do_not_become_maker_turnover(self):
        path = self.session(sell="99", funding="2", transfer="10", taker=True)
        report = self.analyze([path])
        metrics = report["sessions"][0]["recorded_metrics"]
        self.assertEqual(D(metrics["maker_turnover_total"]), D(100))
        self.assertEqual(D(metrics["taker_flatten_turnover"]), D(99))
        self.assertEqual(D(metrics["maker_fee"]), D(".01"))
        self.assertEqual(D(metrics["taker_fee"]), D(".0297"))
        self.assertEqual(D(metrics["funding"]), D(2))
        self.assertEqual(D(metrics["external_transfers"]), D(10))
        self.assertEqual(D(report["aggregate"]["all_in_net_pnl"]), D(".9603"))
        # Positive funding cannot hide gross failing to cover the paid fees.
        self.assertFalse(report["aggregate"]["fee_neutral_observed"])
        self.assertEqual(D(metrics["forced_flatten_loss"]), D("1.0297"))

    def test_incomplete_and_truncated_sessions_keep_costs_but_block_aggregate_economics(self):
        good = self.session("good.jsonl")
        incomplete = self.session("incomplete.jsonl", sell="99", complete=False)
        for truncate in (False, True):
            if truncate:
                rows = incomplete.read_text().splitlines()
                incomplete.write_text("\n".join(rows[:-1]) + "\n")
            report = self.analyze([good, incomplete])
            total = report["aggregate"]
            self.assertFalse(total["economics_evaluated"])
            self.assertEqual(total["incomplete_session_count"], 1)
            self.assertEqual(D(total["comparison_window_seconds"]), D(200))
            self.assertEqual(D(total["observed_totals"]["maker_turnover_total"]), D(400))
            self.assertIsNone(total["all_in_net_pnl"])
            self.assertIsNone(total["fee_cover_ratio"])

    def test_replay_and_dry_never_become_actual_fills_or_economic_pass(self):
        replay = self.analyze([self.session("replay.jsonl")], mode="replay")
        self.assertTrue(replay["sessions"][0]["accounting_complete"])
        self.assertIsNone(replay["sessions"][0]["actual_maker_turnover_usdg"])
        self.assertFalse(replay["aggregate"]["economics_evaluated"])
        self.assertIsNone(replay["aggregate"]["fee_neutral_observed"])
        dry = self.session("dry.jsonl", dry=True)
        output = self.analyze([dry], mode="dry_run")
        self.assertTrue(output["sessions"][0]["simulated_execution_observed"])
        self.assertEqual(D(output["aggregate"]["observed_totals"]["maker_turnover_total"]), 0)
        with self.assertRaises(ValueError):
            self.analyze([dry], mode="live")
        with self.assertRaises(ValueError):
            self.analyze([self.root / "replay.jsonl"], mode="dry_run")

    def test_missing_fill_or_stale_final_cannot_reuse_report_complete_flag(self):
        for change in ("missing_fill", "stale_final", "tampered_total", "telemetry_error"):
            with self.subTest(change=change):
                path = self.session(change + ".jsonl")
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                if change == "missing_fill":
                    rows = [row for row in rows if not (row["event"] == "fill"
                            and row["data"]["fill"]["fill_id"] == "s")]
                elif change == "stale_final":
                    rows[-2]["data"]["observed_monotonic"] = 0.0
                elif change == "tampered_total":
                    rows[-1]["data"]["maker_turnover_total"] = "999"
                else:
                    rows[-1]["data"]["telemetry_errors"] = 1
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                result = self.analyze([path])
                self.assertFalse(result["aggregate"]["economics_evaluated"])
                self.assertIsNone(result["aggregate"]["all_in_net_pnl"])

    def test_cli_rejects_malformed_or_duplicate_evidence_without_echoing_content(self):
        path = self.session()
        with self.assertRaises(ValueError):
            self.analyze([path, path])
        with self.assertRaises(ValueError):
            self.analyze([path], wall_seconds=["3"])
        text = path.read_text()
        for changed in (text.replace('"price":"100"', '"price":100'),
                        text.replace('"equity":"299"', '"equity":"NaN"'),
                        '{"schema":"mm_v2_event_v1","event":"session_report","data":[]}\n',
                        '{"schema":"private-token", "schema":"mm_v2_event_v1"}\n'):
            path.write_text(changed)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main([str(path), "--candidate", "c", "--mode", "live",
                             "--planned-seconds", "100", "--wall-seconds", "4"])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertNotIn("private-token", err.getvalue())

    def test_report_window_cannot_precede_recorded_execution_observation(self):
        path = self.session()
        rows = path.read_text().splitlines()
        execution = {"schema": "mm_v2_event_v1", "event": "execution_result", "data": {
            "status": "confirmed", "snapshot": {"health": "healthy", "managed_order_count": 0,
                "simulated": False, "symbol": "BTC", "observed_monotonic": 100.0, "orders": []},
            "submitted_count": 0, "cancelled_count": 0, "account_snapshot": None, "actual_plan": None}}
        rows.insert(-1, json.dumps(execution))
        path.write_text("\n".join(rows) + "\n")
        with self.assertRaises(ValueError):
            self.analyze([path], wall_seconds=["4"])

    def test_quantity_table_exposes_soft_band_minimum_notional_and_keeps_reserve(self):
        config = load_config(Path(__file__).resolve().parents[1]
                             / "config/market_maker_v2/lighter_btc_volume.example.yaml")
        inputs = dict(external_bid="79999.9", external_ask="80000.1", tick_size="0.1",
                      size_step="0.00001", min_order_size="0.00001", min_notional="10",
                      maker_fee_rate="0.00012", taker_fee_rate="0.00035", allocated_capital="50")
        table = candidate_quantity_table(config, **inputs, target_edges=["0", "0.2", "0.5"])
        self.assertEqual(len(table["rows"]), 30)
        self.assertTrue(table["configured_startup_executable"])
        self.assertEqual(table["rows"][1]["existing_order_count"], 2)
        self.assertEqual(D(table["rows"][0]["worst_position_with_existing_and_new"]), D(".00020"))
        self.assertEqual(D(table["rows"][1]["worst_position_with_existing_and_new"]), D(".00040"))
        rows = {row["inventory_state"]: row for row in table["rows"][:10]
                if row["working_order_case"] == "no_working_orders"}
        self.assertEqual([q["side"] for q in rows["flat"]["quotes"]], ["buy", "sell"])
        self.assertEqual([q["side"] for q in rows["long_soft"]["quotes"]], ["sell"])
        self.assertEqual(rows["long_soft"]["removed_below_minimum_notional"], ["buy"])
        self.assertEqual(D(rows["long_soft"]["buy_capacity"]), D(".00010"))
        self.assertEqual(rows["short_soft"]["removed_below_minimum_notional"], ["sell"])
        for state in ("long_hard", "short_hard"):
            self.assertTrue(rows[state]["quotes"][0]["reduce_only"])
        for row in table["rows"]:
            self.assertTrue(row["within_hard_inventory"])
            for quote in row["quotes"]:
                self.assertEqual(D(quote["price"]) % D(".1"), 0)
                self.assertEqual(D(quote["size"]) % D(".00001"), 0)
                self.assertGreaterEqual(D(quote["notional_usdg"]), 10)
        # This is loss-reserve pressure, independent of the allocated capital.
        tight = replace(config, session=replace(config.session, max_loss_usdg=D(".05")))
        reserved = candidate_quantity_table(tight, **inputs)
        self.assertEqual(reserved["rows"][0]["quotes"], [])
        too_small = candidate_quantity_table(config, **{**inputs, "min_notional": "20"})
        self.assertFalse(too_small["configured_startup_executable"])
        self.assertTrue(all(not row["quotes"] for row in too_small["rows"]))

    def book_file(self, times):
        path = self.root / "public.jsonl"
        rows = [{"schema": "mm_v2_public_book_v1", "symbol": "BTC",
                 "observed_monotonic": when, "source_timestamp_ms": 100000 + int(when * 1000),
                 "nonce": index, "bid": str(D(100) + D(index) / 100),
                 "ask": str(D("100.02") + D(index) / 100), "bid_size": "1", "ask_size": "2"}
                for index, when in enumerate(times)]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_public_book_pair_coverage_respects_horizon_lateness_and_duplicate_receipts(self):
        path = self.book_file([index / 2 for index in range(26)])
        lines = path.read_text().splitlines()
        path.write_text("\n".join([lines[0], *lines]) + "\n")
        report = analyze_public_books(path)
        self.assertEqual((report["record_count"], report["distinct_receipt_count"]), (27, 26))
        self.assertEqual(report["duplicate_receipt_count"], 1)
        self.assertEqual(report["horizons"]["1s"]["matched_pairs"], 24)
        self.assertEqual(report["horizons"]["5s"]["matched_pairs"], 16)
        self.assertEqual(D(report["horizons"]["5s"]["pair_coverage"]), 1)
        self.assertGreater(D(report["horizons"]["1s"]["mid_return_bps"]["min"]), 0)
        self.assertFalse(report["source_age_verified"])
        late = analyze_public_books(self.book_file([0.0, 1.3, 5.3]))
        self.assertEqual(late["horizons"]["1s"]["matched_pairs"], 0)
        self.assertEqual(late["horizons"]["5s"]["matched_pairs"], 0)
        boundary = analyze_public_books(self.book_file([0.0, 1.25, 5.25]))
        self.assertEqual(boundary["horizons"]["5s"]["matched_pairs"], 1)
        short = analyze_public_books(self.book_file([0.0, .5]))
        self.assertIsNone(short["horizons"]["5s"]["pair_coverage"])

    def test_public_book_rejects_unusable_book_and_clock_order_without_host_clock_guess(self):
        for key, value in (("observed_monotonic", -.5), ("source_timestamp_ms", 99999),
                           ("nonce", -1), ("bid", "101"), ("ask", "100.01"),
                           ("bid_size", "0"), ("ask_size", "NaN"), ("bid", 100.0)):
            with self.subTest(key=key, value=value):
                path = self.book_file([0.0, .5])
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                rows[-1][key] = value
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                with self.assertRaises(ValueError):
                    analyze_public_books(path)

    def test_constant_spread_average_stays_between_identical_extrema(self):
        path = self.book_file([index / 2 for index in range(7)])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row.update(bid="79928.5", ask="79934.2")
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        spread = analyze_public_books(path)["spread_bps"]
        self.assertEqual(spread["mean"], spread["min"])
        self.assertEqual(spread["mean"], spread["max"])

    def test_fill_source_time_is_optional_strict_and_roundtrips_through_ledger_jsonl(self):
        for value in (-1, True, "1000", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FillEvent("f", "o", "BTC", Side.BUY, D(1), D(100), D(".01"),
                          LiquidityRole.MAKER, 1.0, source_timestamp_ms=value)
        path = self.session(source_timestamps=(100000, 100500))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["data"]["fill"]["source_timestamp_ms"]
                          for row in rows if row["event"] == "fill"], [100000, 100500])
        self.assertTrue(self.analyze([path])["aggregate"]["economics_evaluated"])

    def test_recorded_maker_fill_markouts_use_source_clock_sign_and_turnover_weighting(self):
        books = self.book_file([index / 2 for index in range(26)])
        fills = self.session(source_timestamps=(100000, 100500))
        report = analyze_public_books(books, session_paths=[fills])
        for horizon in ("1s", "5s"):
            markout = report["horizons"][horizon]["recorded_maker_fill_markout"]
            self.assertEqual((markout["total_fills"], markout["source_timestamp_available"],
                              markout["matched_fills"]), (2, 2, 2))
            self.assertEqual(D(markout["coverage"]), 1)
            self.assertEqual(D(markout["matched_turnover_usdg"]), 201)
            self.assertEqual(D(markout["turnover_weighted_bps"]), D(".99") * 10000 / 201)
        legacy = self.session("legacy.jsonl")
        missing = analyze_public_books(books, session_paths=[legacy])["horizons"]["1s"]["recorded_maker_fill_markout"]
        self.assertEqual(missing["source_timestamp_available"], 0)
        self.assertEqual(D(missing["coverage"]), 0)
        self.assertIsNone(missing["turnover_weighted_bps"])
        taker = self.session("taker.jsonl", taker=True, source_timestamps=(100000, 100500))
        self.assertEqual(analyze_public_books(books, session_paths=[taker])["horizons"]["1s"]
                         ["recorded_maker_fill_markout"]["total_fills"], 1)
        sparse = self.book_file([0.0, 1.3, 5.3])
        late = analyze_public_books(sparse, session_paths=[fills])["horizons"]["1s"]["recorded_maker_fill_markout"]
        self.assertEqual(late["matched_fills"], 0)

    def test_snapshot_lifetimes_are_observations_and_missing_execution_is_unavailable(self):
        path = self.session()
        self.assertFalse(self.analyze([path])["sessions"][0]["observed_order_lifetimes"]["timing_available"])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        observations = []
        for when, identifier, submitted, cancelled in ((.25, "a", 1, 0), (1.5, "b", 1, 1), (3.5, None, 0, 1)):
            orders = [{"order_id": identifier, "side": "buy", "remaining_size": "1", "price": "100",
                       "reduce_only": False}] if identifier else []
            observations.append({"schema": "mm_v2_event_v1", "event": "execution_result", "data": {
                "status": "confirmed", "snapshot": {"health": "healthy", "managed_order_count": len(orders),
                    "simulated": False, "symbol": "BTC", "observed_monotonic": when, "orders": orders},
                "submitted_count": submitted, "cancelled_count": cancelled}})
        rows[-2:-2] = observations
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        result = self.analyze([path])["sessions"][0]["observed_order_lifetimes"]
        self.assertTrue(result["timing_available"])
        self.assertEqual((result["replacement_results"], result["closed_order_count"],
                          result["right_censored_order_count"]), (1, 2, 0))
        self.assertEqual(D(result["mean_snapshot_seconds"]), D("1.625"))
        observations[-1]["data"]["snapshot"]["observed_monotonic"] = 1.0
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        with self.assertRaises(ValueError):
            self.analyze([path])


if __name__ == "__main__":
    unittest.main()
