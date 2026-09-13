"""Offline report contracts: actual economics, failed windows and exact aggregate costs."""

import contextlib
import asyncio
from dataclasses import asdict, replace
from decimal import Decimal as D
import io
import json
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.services.market_maker_v2.domain import (
    AccountSnapshot, CashflowEvent, CashflowKind, ExecutionHealth, ExecutionResult,
    ExecutionSnapshot, ExecutionStatus, FillEvent, LiquidityRole, MarkEvent, Side, FailureDiagnostic,
    OrderEvidence, MarketStateSnapshot, StrategyState, GovernorDiagnostic,
)
from core.services.market_maker_v2.session_ledger import SessionLedger
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink, _encode
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
        self.assertEqual(totals["diagnostic_trading"]["complete_flat_group_count"], 2)
        self.assertEqual(D(totals["diagnostic_trading"]["trading_net_usdg"]), D("-.04"))
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
        self.assertEqual(D(report["aggregate"]["diagnostic_trading"]["trading_net_usdg"]), D("-1.0397"))
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

    def test_incomplete_authenticated_residual_keeps_recorded_final_facts_after_late_fill(self):
        path = self.root / "residual.jsonl"
        initial = AccountSnapshot("BTC", 0.0, D(0), D(299), D(".00012"), D(".00035"), 0, True)
        fee = D(".00371670240")
        with JsonlTelemetrySink(path) as sink:
            ledger = SessionLedger(initial, telemetry=sink)
            ledger.ingest_fill(FillEvent("fill", "sell", "BTC", Side.SELL,
                D(".00040"), D("77431.3"), fee, LiquidityRole.MAKER, 3.0,
                source_timestamp_ms=2000))
            # As in the live failure, final request-start precedes late fill
            # ingestion, while its response already contains the residual/order.
            final = replace(initial, observed_monotonic=2.5, position=D("-.00040"),
                entry_price=D("77431.3"), equity=initial.equity - fee,
                open_order_count=1, open_order_ids=("remaining",))
            recorded = ledger.finalize(final, now=4.0)
            self.assertFalse(recorded.complete)
            sink.emit(FailureDiagnostic("BTC", "exit_health", "blocked"))
        report = self.analyze([path])
        session = report["sessions"][0]
        metrics = session["recorded_metrics"]
        self.assertEqual((metrics["final_authenticated"], D(metrics["final_position"]),
                          metrics["final_open_order_count"]), (True, D("-.00040"), 1))
        self.assertFalse(metrics["complete"])
        evidence = session["recorded_final_evidence"]
        self.assertEqual(evidence["metrics_final_source"], "session_report")
        self.assertEqual(evidence["session_report"], {"line": 4, "complete": False,
            "final_authenticated": True, "final_position": "-0.00040", "final_open_order_count": 1})
        self.assertEqual(evidence["account_snapshot"], {"line": 3, "observed_monotonic": 2.5,
            "authenticated": True, "position": "-0.00040", "open_order_count": 1})
        self.assertNotIn("open_order_ids", json.dumps(evidence))
        self.assertFalse(report["aggregate"]["economics_evaluated"])
        self.assertIsNone(report["aggregate"]["all_in_net_pnl"])
        self.assertIsNone(report["aggregate"]["fee_cover_ratio"])
        self.assertEqual(D(metrics["maker_fee"]), fee)

    def test_recorded_final_sources_remain_distinct_and_missing_evidence_is_not_invented(self):
        path = self.session(complete=False)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        # This fixture's snapshot report has no final proof, while the preceding
        # standalone account event is authenticated. Preserve the disagreement.
        session = self.analyze([path])["sessions"][0]
        evidence = session["recorded_final_evidence"]
        self.assertFalse(evidence["session_report"]["final_authenticated"])
        self.assertTrue(evidence["account_snapshot"]["authenticated"])
        self.assertFalse(session["recorded_metrics"]["final_authenticated"])
        self.assertFalse(session["economics_evaluated"])
        self.write_rows(path, rows[:-1])
        session = self.analyze([path])["sessions"][0]
        self.assertIsNone(session["recorded_final_evidence"]["session_report"])
        self.assertEqual(session["recorded_final_evidence"]["metrics_final_source"], "account_snapshot")
        self.assertTrue(session["recorded_metrics"]["final_authenticated"])
        self.assertEqual(session["recorded_metrics"]["final_open_order_count"], 0)
        self.assertIn("missing_final_report", session["incomplete_reasons"])
        self.assertFalse(session["economics_evaluated"])
        self.write_rows(path, rows[:-2])
        session = self.analyze([path])["sessions"][0]
        self.assertIsNone(session["recorded_final_evidence"]["session_report"])
        self.assertIsNone(session["recorded_final_evidence"]["account_snapshot"])
        self.assertIsNone(session["recorded_final_evidence"]["metrics_final_source"])
        self.assertFalse(session["economics_evaluated"])

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

    def write_rows(self, path, rows):
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def order_row(self, side, *, reducing=False, ioc=False, submitted=.8, confirmed=1.1):
        market = MarketStateSnapshot("BTC", .5, D(99), D(101), D(1), D(".01"), D(".01"), True,
                                     source_timestamp_ms=99500)
        evidence = OrderEvidence("BTC", side.value, side, D(100), D(1), reducing,
            "IOC" if ioc else "POST_ONLY", submitted, confirmed, market, StrategyState.QUOTING)
        return {"schema": "mm_v2_event_v1", "event": "order_evidence", "data": _encode(evidence)}

    def test_flat_groups_keep_legacy_costs_separate_from_unavailable_formal_economics(self):
        path = self.session(complete=False, sell="100.01", funding="2")
        report = self.analyze([path])
        session = report["sessions"][0]
        diagnostic = session["diagnostic_trading"]
        self.assertEqual(session["journal_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(session["event_count"], len(path.read_text().splitlines()))
        self.assertEqual(session["run_provenance"]["status"], "unavailable")
        self.assertFalse(report["aggregate"]["economics_evaluated"])
        self.assertIsNone(report["aggregate"]["fee_cover_ratio"])
        self.assertIsNone(report["aggregate"]["all_in_net_pnl"])
        self.assertEqual(D(diagnostic["realized_gross_pnl_usdg"]), D(".01"))
        self.assertEqual(D(diagnostic["trading_net_usdg"]), D("-.010001"))
        self.assertEqual(diagnostic["execution_costs"]["unclassified"]["fill_count"], 2)
        self.assertEqual(diagnostic["coverage"]["order_evidence_fills"], 0)
        self.assertEqual(diagnostic["governor"]["status"], "unavailable")
        self.assertIsNone(diagnostic["public_book_markouts"])
        grouped = diagnostic["flat_to_flat"]
        self.assertEqual(grouped["complete_group_count"], 1)
        self.assertIsNone(grouped["open_group"])
        self.assertEqual(grouped["category_totals"]["maker_only"]["positive_gross_below_fees_count"], 1)
        group = grouped["complete_groups"][0]
        self.assertTrue(group["gross_reconciles"])
        self.assertIsNone(group["source_duration_seconds"])

    def test_flat_group_crossing_fill_is_unsplit_and_unfinished_position_is_retained(self):
        path = self.root / "crossing.jsonl"
        initial = AccountSnapshot("BTC", 0.0, D(0), D(299), D(".0001"), D(".0003"), 0, True)
        with JsonlTelemetrySink(path) as sink:
            ledger = SessionLedger(initial, telemetry=sink)
            for index, (side, size, price, timestamp) in enumerate((
                    (Side.BUY, "1", "100", 1000), (Side.SELL, "2", "101", 900),
                    (Side.BUY, "1", "100", 3000), (Side.BUY, ".5", "100", 4000)), 1):
                ledger.ingest_fill(FillEvent(str(index), str(index), "BTC", side, D(size), D(price),
                    D(".01"), LiquidityRole.MAKER, float(index), source_timestamp_ms=timestamp))
            sink.emit(ledger.snapshot(now=4.0))
        grouped = self.analyze([path])["sessions"][0]["diagnostic_trading"]["flat_to_flat"]
        self.assertEqual(grouped["complete_group_count"], 1)
        group = grouped["complete_groups"][0]
        self.assertEqual(group["fill_count"], 3)
        self.assertEqual(D(group["cash_trading_gross_usdg"]), 2)
        self.assertEqual(D(group["trading_net_usdg"]), D("1.97"))
        self.assertFalse(group["source_time_complete_and_ordered"])
        self.assertIsNone(group["source_duration_seconds"])
        self.assertEqual(grouped["open_group"]["fill_count"], 1)
        self.assertEqual(D(grouped["open_group"]["closing_position"]), D(".5"))
        self.assertIsNone(grouped["open_group"]["trading_net_usdg"])

    def test_order_evidence_classifies_actual_fills_and_confirmation_may_follow_receipt(self):
        path = self.session(source_timestamps=(100000, 100500))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        inserted = []
        for row in rows:
            if row["event"] == "fill":
                side = Side(row["data"]["fill"]["side"])
                inserted.append(self.order_row(side, reducing=side is Side.SELL))
            inserted.append(row)
        self.write_rows(path, inserted)
        diagnostic = self.analyze([path])["sessions"][0]["diagnostic_trading"]
        self.assertEqual(diagnostic["execution_costs"]["normal_maker"]["fill_count"], 1)
        reducing = diagnostic["execution_costs"]["passive_reducing_maker"]
        self.assertEqual(reducing["fill_count"], 1)
        self.assertEqual(D(reducing["realized_gross_pnl_usdg"]), 1)
        self.assertEqual(D(reducing["recorded_fees_usdg"]), D(".0101"))
        self.assertEqual(diagnostic["coverage"]["order_evidence_fills"], 2)
        linked = diagnostic["linked_fill_observations"][0]
        self.assertEqual(D(linked["receipt_since_submit_seconds"]), D(".2"))
        self.assertEqual(linked["quote_market_source_timestamp_ms"], 99500)
        self.assertNotIn("order_id", json.dumps(diagnostic))
        taker = self.analyze([self.session("taker_cost.jsonl", taker=True)])["sessions"][0]["diagnostic_trading"]
        self.assertEqual(taker["execution_costs"]["ioc_exit"]["fill_count"], 1)
        self.assertEqual(taker["coverage"]["order_evidence_fills"], 0)
        self.assertEqual(taker["flat_to_flat"]["category_totals"]["contains_taker"]["group_count"], 1)

    def test_late_incompatible_or_future_order_evidence_never_backfills_classification(self):
        for failure in ("after_fill", "wrong_side", "future_submit", "price_outside_limit", "size_exceeds_order"):
            path = self.session(failure + ".jsonl")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            at = next(index for index, row in enumerate(rows) if row["event"] == "fill")
            evidence = self.order_row(Side.BUY)
            if failure == "wrong_side":
                evidence["data"]["side"] = "sell"
            if failure == "future_submit":
                evidence["data"].update(submitted_monotonic=1.5, confirmed_monotonic=1.6)
            if failure == "price_outside_limit":
                evidence["data"]["price"] = "99"
            if failure == "size_exceeds_order":
                evidence["data"]["size"] = ".5"
            rows.insert(at + (failure == "after_fill"), evidence)
            self.write_rows(path, rows)
            diagnostic = self.analyze([path])["sessions"][0]["diagnostic_trading"]
            self.assertEqual(diagnostic["coverage"]["order_evidence_fills"], 0)
            self.assertEqual(diagnostic["execution_costs"]["unclassified"]["fill_count"], 2)

    def test_recorded_fill_gross_tampering_is_diagnosed_but_replayed_costs_are_retained(self):
        path = self.session()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        next(row for row in rows if row["event"] == "fill")["data"]["realized_gross_pnl"] = "999"
        self.write_rows(path, rows)
        report = self.analyze([path])
        session = report["sessions"][0]
        self.assertIn("recorded_fill_accounting_mismatch", session["incomplete_reasons"])
        self.assertEqual(D(session["diagnostic_trading"]["realized_gross_pnl_usdg"]), 1)
        self.assertIsNone(report["aggregate"]["all_in_net_pnl"])

    def test_startup_provenance_is_allowlisted_and_never_inferred_from_current_checkout(self):
        path = self.session()
        config = load_config(Path(__file__).resolve().parents[1]
                             / "config/market_maker_v2/lighter_btc_volume.example.yaml")
        config_row = json.loads(json.dumps(asdict(config), default=str))
        provenance = {"schema": "mm_v2_run_provenance_v1", "capture": "run_start",
            "build": {"status": "available", "commit": "a" * 40, "dirty": True},
            "effective_config": config_row}
        sidecar = Path(str(path) + ".budget.json")
        sidecar.write_text(json.dumps({"provenance": provenance, "unrelated_payload": "PRIVATE_SENTINEL"}))
        result = self.analyze([path])["sessions"][0]["run_provenance"]
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["record"]["build"]["commit"], "a" * 40)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        provenance["build"]["commit"] = "PRIVATE_SENTINEL"
        sidecar.write_text(json.dumps({"provenance": provenance}))
        invalid = self.analyze([path])["sessions"][0]["run_provenance"]
        self.assertEqual(invalid["status"], "invalid")
        self.assertIsNone(invalid["record"])
        provenance["build"] = {"status": "unavailable", "commit": None, "dirty": None}
        del provenance["effective_config"]["dry_run"]
        sidecar.write_text(json.dumps({"provenance": provenance}))
        self.assertEqual(self.analyze([path])["sessions"][0]["run_provenance"]["status"], "invalid")
        sidecar.write_text(json.dumps({"older_budget": True}))
        self.assertEqual(self.analyze([path])["sessions"][0]["run_provenance"]["status"], "unavailable")

    def test_analyzer_cli_reads_budget_sidecar_written_by_real_runner(self):
        import run_volume_market_maker as runner
        from core.services.market_maker_v2.api_budget import ApiBudget

        path = self.root / "runner.jsonl"
        config = load_config(runner.ROOT / "config/market_maker_v2/lighter_btc_volume.example.yaml")
        build = {"status": "available", "commit": "b" * 40, "dirty": True}
        settings = {"network": "robinhood_testnet", "testnet": True,
                    "expected_l1_address": "0x" + "1" * 40, "account_index": 1,
                    "api_key_private_key": "private-sentinel"}

        def session_factory(*args, telemetry, **kwargs):
            initial = AccountSnapshot("BTC", 0.0, D(0), D(299), D(".0001"), D(".0003"), 0, True)
            ledger = SessionLedger(initial, telemetry=telemetry)
            ledger.finalize(replace(initial, observed_monotonic=4.0), now=4.0)
            return SimpleNamespace(run=AsyncMock(return_value=object()),
                api_budget=ApiBudget(lambda: 0), market=SimpleNamespace(stream=None))

        with (patch.object(runner, "build_adapter", return_value=object()),
              patch.object(runner, "_git_build", return_value=build),
              patch.object(runner.orchestrator, "VolumeSession", side_effect=session_factory)):
            asyncio.run(runner.run_session(config, settings, output=path))
        # The runner owns the filename; no test-created sidecar may mask a mismatch.
        self.assertTrue(Path(str(path) + ".budget.json").exists())
        self.assertFalse(path.with_suffix(".budget.json").exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main([str(path), "--candidate", "runner_contract", "--mode", "dry_run",
                           "--planned-seconds", "100", "--wall-seconds", "4"])
        self.assertEqual(status, 0)
        analyzed = json.loads(stdout.getvalue())
        provenance = analyzed["sessions"][0]["run_provenance"]
        self.assertEqual(provenance["status"], "available")
        self.assertEqual(provenance["record"]["build"], build)
        self.assertEqual(provenance["record"]["effective_config"]["quote"]["order_size"],
                         str(config.quote.order_size))
        self.assertNotIn("private-sentinel", stdout.getvalue())

    def test_embedded_public_books_reuse_source_clock_markouts_without_fill_reference_backfill(self):
        path = self.session(source_timestamps=(100000, 100500), complete=False)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row["event"] == "fill":
                row["data"]["fill"]["reference_price"] = None
        book = self.book_file([index / 2 for index in range(8)])
        public_rows = [json.loads(line) for line in book.read_text().splitlines()]
        embedded = [{"schema": "mm_v2_event_v1", "event": "public_book_observation",
                     "data": {key: value for key, value in row.items() if key != "schema"}}
                    for row in public_rows]
        rows[-2:-2] = embedded
        diagnostic = GovernorDiagnostic("BTC", 3.5, StrategyState.QUOTING, StrategyState.REDUCE_ONLY,
            "reducing_capacity_only", D(".1"), sell_capacity=D(".1"), candidate_sell=D(".1"),
            total_reserve=D(".02"), remaining_loss_headroom=D(".03"))
        rows.insert(-2, {"schema": "mm_v2_event_v1", "event": "governor_diagnostic", "data": _encode(diagnostic)})
        self.write_rows(path, rows)
        report = self.analyze([path])
        diagnostic = report["sessions"][0]["diagnostic_trading"]
        self.assertEqual(diagnostic["coverage"]["fill_reference_fills"], 0)
        self.assertEqual(diagnostic["governor"]["reason_counts"], {"reducing_capacity_only": 1})
        self.assertEqual(len(diagnostic["governor"]["state_transitions"]), 1)
        embedded_report = diagnostic["public_book_markouts"]
        external_report = analyze_public_books(book, session_paths=[path])
        self.assertEqual(embedded_report["horizons"], external_report["horizons"])
        self.assertEqual(embedded_report["horizons"]["1s"]["recorded_maker_fill_markout"]["matched_fills"], 2)
        self.assertIsNone(report["aggregate"]["all_in_net_pnl"])


if __name__ == "__main__":
    unittest.main()
