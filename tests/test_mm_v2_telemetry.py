"""One typed stream, exact decimal encoding and no secret-bearing raw payloads."""

from dataclasses import dataclass
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from core.services.market_maker_v2.domain import (
    AccountSnapshot, FillAccounting, FillEvent, LiquidityRole, MarkEvent, QuotePlan, Side,
)
from core.services.market_maker_v2.session_ledger import SessionLedger
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink, TelemetryError


class JsonlTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"

    def test_one_stream_appends_typed_events_with_decimal_strings(self):
        fill = FillEvent("f1", "o1", "BTC", Side.BUY, D("0.0002"), D("100"),
                         D("0.000002"), LiquidityRole.MAKER, 1.0, D("101"))
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(QuotePlan("BTC"))
            prefix = self.path.read_bytes()
            sink.emit(FillAccounting(fill, D("0"), D("0.0002"), D("0"), D("0")))
            sink.emit(MarkEvent("BTC", 2.0, D("102"), False))
            self.assertTrue(self.path.read_bytes().startswith(prefix))
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["quote_plan", "fill", "mark"])
        self.assertEqual(rows[1]["data"]["fill"]["fee"], "0.000002")
        self.assertEqual(rows[1]["data"]["fill"]["side"], "buy")
        self.assertEqual(rows[1]["data"]["fill"]["liquidity"], "maker")

    def test_existing_file_cannot_be_overwritten_or_used_for_another_session(self):
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(QuotePlan("BTC"))
        original = self.path.read_bytes()
        with self.assertRaises(TelemetryError):
            JsonlTelemetrySink(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_raw_dicts_or_subclass_extra_credentials_are_never_serialized(self):
        @dataclass(frozen=True)
        class UnsafePlan(QuotePlan):
            credentials: str = "DO_NOT_EXPOSE_SECRET"

        with JsonlTelemetrySink(self.path) as sink:
            for event in ({"credentials": "DO_NOT_EXPOSE_SECRET"}, UnsafePlan("BTC")):
                with self.assertRaises(TelemetryError) as error:
                    sink.emit(event)
                self.assertNotIn("DO_NOT_EXPOSE_SECRET", str(error.exception))
            sink.emit(QuotePlan("BTC"))
        self.assertNotIn("DO_NOT_EXPOSE_SECRET", self.path.read_text())
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_closed_stream_explicitly_rejects_further_events(self):
        sink = JsonlTelemetrySink(self.path)
        sink.close()
        with self.assertRaises(TelemetryError):
            sink.emit(QuotePlan("BTC"))

    def test_ledger_records_each_fill_once_and_a_final_report_in_the_same_stream(self):
        initial = AccountSnapshot("BTC", 0.0, D("0"), D("100"), D("0.0001"),
                                  D("0.0003"), 0, True)
        first = FillEvent("f1", "o1", "BTC", Side.BUY, D("1"), D("100"),
                          D("0.01"), LiquidityRole.MAKER, 1.0, D("100"))
        last = FillEvent("f2", "o2", "BTC", Side.SELL, D("1"), D("101"),
                         D("0.0101"), LiquidityRole.MAKER, 2.0, D("101"))
        with JsonlTelemetrySink(self.path) as sink:
            ledger = SessionLedger(initial, telemetry=sink)
            ledger.observe(MarkEvent("BTC", 0.0, D("100"), True))
            ledger.ingest_fill(first)
            ledger.ingest_fill(first)
            ledger.ingest_fill(last)
            ledger.observe(MarkEvent("BTC", 3.0, D("101"), False))
            final = AccountSnapshot("BTC", 3.0, D("0"), D("100.9799"),
                                    D("0.0001"), D("0.0003"), 0, True)
            self.assertTrue(ledger.finalize(final, now=3.0).complete)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows],
                         ["account_snapshot", "mark", "fill", "fill", "mark",
                          "account_snapshot", "session_report"])
        self.assertEqual(D(rows[-2]["data"]["equity"]), D("100.9799"))
        self.assertEqual(rows[-2]["data"]["observed_monotonic"], 3.0)
        self.assertEqual(D(rows[-2]["data"]["equity"]) - D(rows[0]["data"]["equity"]),
                         D(rows[-1]["data"]["all_in_net_pnl"]))
        self.assertEqual(D(rows[-1]["data"]["all_in_net_pnl"]), D("0.9799"))


if __name__ == "__main__":
    unittest.main()
