"""Public-contract checks for the offline V2 fee/spread report; no exchange access."""

import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts.mm_v2_feasibility import build_report, main


class FeasibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fee_record = {
            "authenticated": True,
            "observed_at_utc": "2026-09-03T13:58:18Z",
            "maker_fee_rate": "0.000120",
            "taker_fee_rate": "0.000350",
        }
        self.fees = self.write_json("fees.json", self.fee_record)

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def snapshot(self, timestamp, bid="100000", ask="100001", **extra):
        return dict(timestamp=timestamp, symbol="BTC", external_bid=bid,
                    external_ask=ask, tick_size="0.1", **extra)

    def stream(self, rows, name="bbo.jsonl"):
        path = self.root / name
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                        encoding="utf-8")
        return path

    def report(self, rows, edges=("0", "1")):
        return build_report([self.stream(rows)], self.fees, list(edges))

    def test_authenticated_historical_fee_floor_and_disclaimer(self):
        report = self.report([self.snapshot(0), self.snapshot(1)])
        self.assertEqual(report["disclaimer"], "not a queue-fill backtest")
        self.assertTrue(report["fees"]["historical"])
        for key, value in (("maker_fee_bps", "1.2"), ("taker_fee_bps", "3.5"),
                           ("maker_roundtrip_fee_floor_bps", "2.4")):
            self.assertEqual(Decimal(report["fees"][key]), Decimal(value))

    def test_candidate_spreads_and_outward_tick_rounding(self):
        report = self.report([self.snapshot(0), self.snapshot(1)])
        for candidate, full, distance in zip(report["candidates"], ("2.4", "3.4"),
                                             ("116", "166")):
            self.assertEqual(Decimal(candidate["minimum_full_spread_bps"]), Decimal(full))
            self.assertEqual(Decimal(candidate["half_spread_bps"]), Decimal(full) / 2)
            for side in ("bid", "ask"):
                self.assertEqual(Decimal(candidate[f"{side}_distance_from_touch_ticks"]["p50"]),
                                 Decimal(distance))
            self.assertGreaterEqual(Decimal(candidate["effective_full_spread_bps"]["min"]),
                                    Decimal(full))

    def test_negative_touch_distance_means_price_improvement(self):
        report = self.report([self.snapshot(0, "99000", "101000")])
        for side in ("bid", "ask"):
            self.assertLess(Decimal(report["candidates"][0][f"{side}_distance_from_touch_ticks"]["p50"]), 0)

    def test_zero_fee_is_supported_without_inventing_taker_cost(self):
        self.fee_record.update(maker_fee_rate="0", taker_fee_rate="0")
        self.write_json("fees.json", self.fee_record)
        report = self.report([self.snapshot(0)])
        self.assertEqual(Decimal(report["fees"]["maker_roundtrip_fee_floor_bps"]), 0)

    def test_dense_series_reports_observed_one_and_five_second_moves(self):
        rows = [self.snapshot(i / 2, str(10000 + i), str(10002 + i)) for i in range(21)]
        report = self.report(rows)
        # The final 5s pair spans 4.5s, exactly within the disclosed 10% tolerance.
        for horizon, count in (("1s", 19), ("5s", 12)):
            moves = report["moves"][horizon]
            self.assertEqual(moves["status"], "available")
            self.assertEqual(moves["matched_count"], count)
            self.assertEqual(Decimal(moves["coverage_ratio"]), 1)
            self.assertGreater(Decimal(moves["signed_bps"]["min"]), 0)
            self.assertEqual(moves["signed_bps"], moves["absolute_bps"])

    def test_sparse_ten_second_samples_do_not_fabricate_horizon_moves(self):
        report = self.report([self.snapshot(i * 10) for i in range(20)])
        self.assertEqual(Decimal(report["market"]["sampling_gap_seconds"]["p50"]), 10)
        for moves in report["moves"].values():
            self.assertEqual(moves["status"], "insufficient_observations")
            self.assertEqual(moves["matched_count"], 0)
            self.assertIsNone(moves["signed_bps"])
            self.assertIsNone(moves["absolute_bps"])

    def test_near_endpoint_tolerance_and_actual_elapsed_are_disclosed(self):
        report = self.report([self.snapshot(t) for t in (0, 1.05, 2.1, 3.15)])
        moves = report["moves"]["1s"]
        self.assertEqual(moves["matched_count"], 3)
        self.assertEqual(Decimal(moves["endpoint_tolerance_seconds"]), Decimal("0.1"))
        self.assertEqual(Decimal(moves["actual_elapsed_seconds"]["p50"]), Decimal("1.05"))

    def test_outside_tolerance_is_not_relabelled_one_second(self):
        report = self.report([self.snapshot(t) for t in (0, 1.2, 2.4, 3.6)])
        self.assertEqual(report["moves"]["1s"]["matched_count"], 0)

    def test_separate_streams_never_make_cross_run_move_pairs(self):
        paths = [self.stream([self.snapshot(0)], "a.jsonl"),
                 self.stream([self.snapshot(1)], "b.jsonl")]
        report = build_report(paths, self.fees, ["0"])
        self.assertEqual(report["market"]["stream_count"], 2)
        self.assertEqual(report["moves"]["1s"]["matched_count"], 0)

    def test_exact_duplicate_snapshot_is_deduplicated(self):
        row = self.snapshot(0)
        report = self.report([row, row, self.snapshot(1)])
        self.assertEqual(report["market"]["snapshot_count"], 2)

    def test_conflicting_or_out_of_order_timestamps_are_rejected(self):
        for rows in ([self.snapshot(0), self.snapshot(0, "100001", "100002")],
                     [self.snapshot(1), self.snapshot(0)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.report(rows)

    def test_rejects_untrusted_fee_record_and_does_not_echo_extra_fields(self):
        self.fee_record["authenticated"] = False
        self.fee_record["credential"] = "DO_NOT_ECHO_SECRET"
        self.write_json("fees.json", self.fee_record)
        with self.assertRaises(ValueError) as error:
            self.report([self.snapshot(0)])
        self.assertNotIn("DO_NOT_ECHO_SECRET", str(error.exception))

    def test_extra_account_fields_are_not_exposed_in_report(self):
        self.fee_record["credential"] = "DO_NOT_ECHO_SECRET"
        self.write_json("fees.json", self.fee_record)
        report = self.report([self.snapshot(0, credential="DO_NOT_ECHO_SECRET")])
        self.assertNotIn("DO_NOT_ECHO_SECRET", json.dumps(report))
        self.assertNotIn("credential", json.dumps(report))

    def test_negative_or_nonfinite_fee_is_rejected(self):
        for rate in ("-0.0001", "NaN", "Infinity"):
            with self.subTest(rate=rate):
                self.fee_record["maker_fee_rate"] = rate
                self.write_json("fees.json", self.fee_record)
                with self.assertRaises(ValueError):
                    self.report([self.snapshot(0)])

    def test_nonfinite_crossed_nonpositive_and_boolean_prices_rejected(self):
        for bid, ask in (("NaN", "100001"), ("Infinity", "100001"),
                         ("100002", "100001"), ("0", "100001"),
                         (True, "100001")):
            with self.subTest(bid=bid, ask=ask), self.assertRaises(ValueError):
                self.report([self.snapshot(0, bid, ask)])

    def test_mixed_symbol_and_tick_size_are_rejected(self):
        for key, value in (("symbol", "ETH"), ("tick_size", "1")):
            row = self.snapshot(1)
            row[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.report([self.snapshot(0), row])

    def test_negative_edge_and_empty_input_are_rejected(self):
        with self.assertRaises(ValueError):
            self.report([self.snapshot(0)], edges=["-1"])
        with self.assertRaises(ValueError):
            self.report([])

    def test_cli_prints_json_without_mutating_inputs(self):
        bbo = self.stream([self.snapshot(i) for i in range(7)])
        before = (bbo.read_bytes(), self.fees.read_bytes())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main([str(bbo), "--fee-evidence", str(self.fees),
                           "--target-edge-bps", "0", "1"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["schema"], "mm_v2_feasibility_v1")
        self.assertEqual(before, (bbo.read_bytes(), self.fees.read_bytes()))

    def test_removed_gate_fee_schema_is_rejected(self):
        self.write_json("fees.json", {
            "schema": "market_maker_gate_bundle_v1",
            "fresh_read_only_preflight": {
                "local_time": "2026-09-03T21:58:18+08:00",
                "authenticated_position_count": 0, "authenticated_open_order_count": 0,
                "maker_fee_tick": 120, "taker_fee_tick": 350,
            },
        })
        with self.assertRaises(ValueError):
            self.report([self.snapshot(0)])

    def test_json_array_input_is_rejected(self):
        path = self.write_json("array.json", [self.snapshot(0)])
        with self.assertRaises(ValueError):
            build_report([path], self.fees, ["0"], tick_size="0.1")

    def test_explicit_tick_fallback_is_required_and_cannot_override_row_tick(self):
        row = self.snapshot(0)
        del row["tick_size"]
        path = self.stream([row])
        with self.assertRaises(ValueError):
            build_report([path], self.fees, ["0"])
        report = build_report([path], self.fees, ["0"], tick_size="0.1")
        self.assertEqual(Decimal(report["market"]["tick_size"]), Decimal("0.1"))
        path = self.stream([self.snapshot(0)])
        with self.assertRaises(ValueError):
            build_report([path], self.fees, ["0"], tick_size="1")

    def test_decimal_json_numbers_are_parsed_without_binary_float_roundoff(self):
        path = self.root / "numeric.jsonl"
        path.write_text('{"timestamp":0,"symbol":"BTC","external_bid":1000.1,'
                        '"external_ask":1000.2,"tick_size":0.1}', encoding="utf-8")
        report = build_report([path], self.fees, ["0"])
        self.assertEqual(Decimal(report["market"]["tick_size"]), Decimal("0.1"))
        self.assertEqual(Decimal(report["market"]["spread_bps"]["p50"]),
                         Decimal("0.1") / Decimal("1000.15") * 10000)

    def test_iso_timestamps_and_missing_timezone(self):
        report = self.report([self.snapshot(f"2026-09-04T00:00:0{i}Z") for i in range(3)])
        self.assertEqual(report["moves"]["1s"]["matched_count"], 2)
        with self.assertRaises(ValueError):
            self.report([self.snapshot("2026-09-04T00:00:00")])


if __name__ == "__main__":
    unittest.main()
