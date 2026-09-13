"""One typed stream, exact decimal encoding and no secret-bearing raw payloads."""

from dataclasses import dataclass, replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS

from core.services.market_maker_v2.domain import (
    AccountSnapshot, FillAccounting, FillEvent, FlattenIntent, InventoryDecision,
    LiquidityRole, MarkEvent, QuotePlan, Side, StrategyState,
    MarketStateSnapshot, OrderEvidence, PublicBookObservation, GovernorDiagnostic,
)
from core.services.market_maker_v2.session_ledger import SessionLedger
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink, TelemetryError, failure_diagnostic
from core.services.market_maker_v2.lighter_runtime import LighterReadError
from core.services.market_maker_v2.execution_port import ExecutionUnavailable
from core.services.market_maker_v2.order_manager import ReconcileAction, ReconcileResult
from core.services.market_maker_v2.execution_models import OrderSlotState, RuntimeState
from core.adapters.exchanges.models import OrderSide
from scripts.analyze_mm_v2_session import _events


class JsonlTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"

    def test_market_failure_keeps_fixed_numeric_causes_for_external_and_wrapped_errors(self):
        for error in (RuntimeError("secret-provider-payload"), LighterReadError("sanitized", values={
                "market_opening_nonce": D(10), "secret-token": D(7)})):
            with self.subTest(error=type(error).__name__):
                diagnostic = failure_diagnostic("BTC", "authorizing_quotes", error, market_values={
                    "market_book_wait_timeout": D(1), "market_book_nonce": D(11),
                    "market_closing_nonce": D(10), "market_stream_reason_protocol_error": D(1),
                    "secret-token": D(9), "market_source_age_ms": D("NaN"),
                    "market_clock_error_ms": "secret-clock", "market_book_invalid": True})
                values = {row.name: row.value for row in diagnostic.values}
                self.assertEqual(values["market_book_wait_timeout"], D(1))
                self.assertEqual(values["market_stream_reason_protocol_error"], D(1))
                self.assertNotIn("secret-token", values)
                self.assertNotIn("market_source_age_ms", values)
                self.assertNotIn("market_clock_error_ms", values)
                self.assertNotIn("market_book_invalid", values)
                with JsonlTelemetrySink(self.path) as sink:
                    sink.emit(diagnostic)
                text = self.path.read_text()
                self.assertNotIn("secret", text)
                self.assertEqual(list(_events(self.path)), [diagnostic])
                self.path.unlink()

    def test_order_market_and_governor_evidence_round_trip_without_changing_fill_reference(self):
        market = MarketStateSnapshot("BTC", 1.0, D("77113.2"), D("77113.3"),
            D("0.1"), D("0.00001"), D("0.00020"), True, source_timestamp_ms=1000)
        order = OrderEvidence("BTC", "o1", Side.BUY, D("77113.2"), D("0.00040"),
            False, "POST_ONLY", 1.1, 1.3, market, StrategyState.QUOTING)
        public = PublicBookObservation("BTC", 1.5, 1500, 7,
            D("77113.2"), D("77113.3"), D("0.01"), D("0.02"))
        decision = GovernorDiagnostic("BTC", 2.0, StrategyState.QUOTING, StrategyState.SKEWED,
            "reducing_capacity_only", D("0.00020"), sell_capacity=D("0.00040"),
            candidate_sell=D("0.00040"), total_reserve=D("0.1725"),
            remaining_loss_headroom=D("0.043"))
        fill = FillAccounting(FillEvent("f1", "o1", "BTC", Side.BUY, D("0.00040"),
            D("77113.2"), D("0.00370143360"), LiquidityRole.MAKER, 2.0,
            source_timestamp_ms=1400), D("0"), None, None, None)
        with JsonlTelemetrySink(self.path) as sink:
            for event in (order, public, decision, fill):
                sink.emit(event)
        self.assertEqual(list(_events(self.path)), [order, public, decision, fill])
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(rows[0]["data"]["market"]["source_timestamp_ms"], 1000)
        self.assertEqual(rows[2]["data"]["total_reserve"], "0.1725")
        self.assertIsNone(rows[2]["data"]["stop_reserve"])
        self.assertIsNone(rows[3]["data"]["fill"]["reference_price"])

    def test_evidence_rejects_stale_future_or_untyped_financial_sources(self):
        market = MarketStateSnapshot("BTC", 1.0, D("99"), D("101"),
            D("1"), D(".1"), D(".1"), True)
        order = OrderEvidence("BTC", "o1", Side.BUY, D("99"), D(".1"),
            False, "POST_ONLY", 2.0, 2.1, market)
        for changes in ({"submitted_monotonic": .5}, {"submitted_monotonic": 5.0,
                         "confirmed_monotonic": 5.1}, {"confirmed_monotonic": 1.9},
                        {"time_in_force": "IOC"}, {"size": .1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(order, **changes)
        for timestamp in (True, -1, 1.5):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                replace(market, source_timestamp_ms=timestamp)
        with self.assertRaises(ValueError):
            GovernorDiagnostic("BTC", 1.0, StrategyState.QUOTING, StrategyState.REDUCE_ONLY,
                "provider-private-message", D(".1"))

    def test_failure_records_code_locations_and_states_without_exception_payloads(self):
        manager = NS(known_order_ids=frozenset(), has_uncertain_state=True,
                     has_unknown_order_state=False,
                     snapshot=lambda: [NS(side=NS(value="buy"), state=NS(value="uncertain_submission"),
                                          order_id="DO_NOT_EXPOSE_SECRET")])
        try:
            try:
                raise RuntimeError("DO_NOT_EXPOSE_SECRET")
            except RuntimeError:
                QuotePlan("BTC", ({"credential": "DO_NOT_EXPOSE_SECRET"},))
        except ValueError as error:
            diagnostic = failure_diagnostic("BTC", "authorizing_quotes", error, manager=manager)
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(diagnostic)
        encoded = self.path.read_text()
        self.assertNotIn("DO_NOT_EXPOSE_SECRET", encoded)
        self.assertNotIn("Traceback", encoded)
        self.assertNotIn("credential", encoded)
        row = json.loads(encoded)
        self.assertEqual(row["event"], "failure_diagnostic")
        self.assertEqual(row["data"]["error_type"], "ValueError")
        self.assertTrue(any(source.startswith("domain:") for source in row["data"]["source"]))
        self.assertEqual(row["data"]["order_states"], ["buy:uncertain_submission:unconfirmed"])

    def test_read_failure_values_use_decimal_strings_and_exclude_identity_or_payloads(self):
        error = LighterReadError("private-provider-detail", values={
            "cash": D("298.79515983036"), "account_position": D("0.00040"),
            "account_index": D("123456"), "token": "private-token"})
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(failure_diagnostic("BTC", "final_account", error))
        encoded = self.path.read_text()
        self.assertNotIn("private-", encoded)
        self.assertNotIn("123456", encoded)
        self.assertEqual(json.loads(encoded)["data"]["values"], [
            {"name": "cash", "value": "298.79515983036"},
            {"name": "account_position", "value": "0.00040"}])

    def test_exit_diagnostic_excludes_unapproved_fields_and_nonfinancial_payloads(self):
        error = ExecutionUnavailable("private-provider-detail", values={
            "exit_limit": D("79874.2"), "exit_bid": D("79872.6"),
            "exit_book_age_ms": "private-token", "account_index": D("123456")})
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(failure_diagnostic("BTC", "flatten_ioc", error))
        encoded = self.path.read_text()
        self.assertNotIn("private-", encoded)
        self.assertNotIn("123456", encoded)
        self.assertEqual(json.loads(encoded)["data"]["values"], [
            {"name": "exit_limit", "value": "79874.2"},
            {"name": "exit_bid", "value": "79872.6"}])

    def cancel_manager(self, errors, *, action_changes=None, pending=True):
        slot = NS(side=OrderSide.BUY, state=OrderSlotState.UNCERTAIN_CANCELLATION,
                  order_id="o1", cancellation_uncertain=pending)
        action = ReconcileAction(OrderSide.BUY, "cancel", "private-action-reason", order_id="o1")
        if action_changes:
            action = replace(action, **action_changes)
        return NS(known_order_ids=frozenset({"o1"}), has_uncertain_state=pending,
                  has_unknown_order_state=False, snapshot=lambda: (slot,),
                  last_result=ReconcileResult((action,), RuntimeState.PAUSED_ORDER_STATE, errors))

    def test_cancel_diagnostic_maps_exact_categories_for_matching_pending_action_only(self):
        cases = (
            ("cancel outcome is not terminal", "cancel_nonterminal_response"),
            ("exact cancellation terminal proof could not be confirmed", "cancel_terminal_unconfirmed"),
            ("cancel rejected: http_429", "cancel_http_429"),
            ("cancel outcome uncertain: TimeoutError", "cancel_timeout"),
            ("cancel outcome uncertain: ConnectionResetError", "cancel_network_error"),
            ("cancel outcome uncertain: RuntimeError", "cancel_other_error"),
            ("cancel outcome uncertain: PrivateProviderCredentialError", "cancel_reason_unknown"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                manager = self.cancel_manager((message,))
                diagnostic = failure_diagnostic("BTC", "reconciling_quotes", manager=manager)
                values = {value.name: value.value for value in diagnostic.values}
                self.assertEqual(values, {"cancel_pending_count": D(1),
                    "cancel_action_matched_count": D(1), expected: D(1)})

    def test_cancel_diagnostic_does_not_reuse_stale_other_side_or_non_cancel_errors(self):
        for changes in ({"order_id": "old-order"}, {"side": OrderSide.SELL},
                        {"operation": "create"}, {"success": True}):
            with self.subTest(changes=changes):
                manager = self.cancel_manager(("cancel outcome uncertain: TimeoutError",),
                                              action_changes=changes)
                values = {value.name: value.value for value in
                          failure_diagnostic("BTC", "exit_health", manager=manager).values}
                self.assertEqual(values, {"cancel_pending_count": D(1),
                    "cancel_action_matched_count": D(0), "cancel_reason_unknown": D(1)})
        manager = self.cancel_manager(("cancel outcome is not terminal",))
        for stage in ("authorizing_quotes", "final_account", "flatten_ioc"):
            self.assertEqual(failure_diagnostic("BTC", stage, manager=manager).values, ())
        manager = self.cancel_manager(("cancel outcome is not terminal",), pending=False)
        self.assertEqual(failure_diagnostic("BTC", "cancel_managed_orders", manager=manager).values, ())
        manager = self.cancel_manager(("cancel outcome is not terminal",))
        original = manager.snapshot()
        manager.snapshot = lambda: (*original, NS(side=OrderSide.SELL,
            state=OrderSlotState.UNCERTAIN_CANCELLATION, order_id="unmatched", cancellation_uncertain=True))
        values = {value.name: value.value for value in
                  failure_diagnostic("BTC", "cancel_managed_orders", manager=manager).values}
        self.assertEqual(values, {"cancel_pending_count": D(2), "cancel_action_matched_count": D(1),
                                 "cancel_nonterminal_response": D(1), "cancel_reason_unknown": D(1)})

    def test_cancel_flags_roundtrip_without_messages_or_provider_objects_and_legacy_remains_valid(self):
        class PrivateProvider:
            def __str__(self):
                raise AssertionError("provider object must never be formatted")

        manager = self.cancel_manager(("cancel outcome uncertain: TimeoutError: private-token",
                                      "cancel outcome is not terminal", PrivateProvider()))
        diagnostic = failure_diagnostic("BTC", "exit_order_sync",
            RuntimeError("private-exception-token"), manager=manager)
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(diagnostic)
        text = self.path.read_text()
        for forbidden in ("private-", "PrivateProvider", "cancel outcome"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(list(_events(self.path)), [diagnostic])
        values = {item["name"]: item["value"] for item in json.loads(text)["data"]["values"]}
        self.assertEqual(values, {"cancel_pending_count": "1", "cancel_action_matched_count": "1",
                                 "cancel_nonterminal_response": "1", "cancel_reason_unknown": "1"})
        row = json.loads(text)
        del row["data"]["values"]
        self.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(next(_events(self.path)).values, ())

    def test_cancel_receipt_details_are_allowlisted_and_bound_to_current_failed_action(self):
        details = (("cancel_receipt_pending", 1), ("cancel_history_attempts", 4),
                   ("cancel_history_read_errors", 0), ("cancel_submission_acknowledged", 1),
                   ("private-token", 1), ("cancel_exact_history_matches", "private-value"),
                   ("cancel_captured_terminal", True), ("cancel_terminal_valid", -1))
        manager = self.cancel_manager(("cancel outcome is not terminal",),
                                      action_changes={"diagnostic_values": details})
        diagnostic = failure_diagnostic("BTC", "exit_health", manager=manager)
        values = {item.name: item.value for item in diagnostic.values}
        self.assertEqual(values, {"cancel_pending_count": D(1), "cancel_action_matched_count": D(1),
            "cancel_nonterminal_response": D(1), "cancel_receipt_pending": D(1),
            "cancel_history_attempts": D(4), "cancel_history_read_errors": D(0),
            "cancel_submission_acknowledged": D(1)})
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(diagnostic)
        self.assertNotIn("private-", self.path.read_text())
        self.assertEqual(list(_events(self.path)), [diagnostic])

    def test_definite_no_send_diagnostic_retains_stage_without_inventing_uncertainty(self):
        stale = self.cancel_manager(("cancel outcome is not terminal",),
            action_changes={"order_id": "old-order", "diagnostic_values": (("cancel_history_attempts", 4),)})
        self.assertFalse(any(item.name.startswith("cancel_history") for item in
                             failure_diagnostic("BTC", "exit_health", manager=stale).values))
        manager = self.cancel_manager(("cancel definitively not sent",), pending=False,
            action_changes={"success": False, "cancellation_not_sent": True,
                            "diagnostic_values": (("cancel_stage_sign", 1),
                                                  ("cancel_error_local_sign_error", 1),
                                                  ("cancel_stage_private-token", 1))})
        diagnostic = failure_diagnostic("BTC", "reconciling_quotes", manager=manager)
        values = {item.name: item.value for item in diagnostic.values}
        self.assertEqual(values, {"cancel_pending_count": D(0), "cancel_action_matched_count": D(1),
            "cancel_not_sent_count": D(1), "cancel_not_sent": D(1),
            "cancel_stage_sign": D(1), "cancel_error_local_sign_error": D(1)})
        self.assertFalse(diagnostic.uncertain)
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(diagnostic)
        self.assertNotIn("private-", self.path.read_text())
        self.assertEqual(list(_events(self.path)), [diagnostic])

    def test_recovery_diagnostic_keeps_bounded_numeric_evidence_without_provider_details(self):
        from core.services.market_maker_v2.execution_port import ExecutionUnavailable
        error = ExecutionUnavailable("private-token", values={
            "recovery_reads": D(2), "recovery_terminal_pending": D(1),
            "recovery_pending_count": D(1), "recovery_deadline_remaining_ms": D("9200"),
            "private-detail": D(1)})
        diagnostic = failure_diagnostic("BTC", "exit_order_sync", error)
        self.assertEqual({item.name: item.value for item in diagnostic.values}, {
            "recovery_reads": D(2), "recovery_terminal_pending": D(1),
            "recovery_pending_count": D(1), "recovery_deadline_remaining_ms": D("9200")})
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(diagnostic)
        self.assertNotIn("private-", self.path.read_text())

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

    def test_inventory_state_and_nested_flatten_keep_typed_decimal_contract(self):
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(InventoryDecision(StrategyState.QUOTING, buy_capacity=D("0.00020"),
                                        sell_capacity=D("0.00020")))
            sink.emit(InventoryDecision(StrategyState.FLATTENING,
                flatten=FlattenIntent("BTC", Side.BUY, D("0.00010"), D("80100.1"), 30.0)))
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["inventory_decision"] * 2)
        self.assertEqual(rows[0]["data"], {"state": "quoting", "flatten": None,
            "buy_capacity": "0.00020", "sell_capacity": "0.00020"})
        self.assertEqual(rows[1]["data"], {"state": "flattening", "buy_capacity": "0",
            "sell_capacity": "0", "flatten": {"symbol": "BTC", "side": "buy",
                "size": "0.00010", "limit_price": "80100.1", "deadline_monotonic": 30.0}})

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

    def test_exchange_realization_survives_journal_replay_and_legacy_missing_field(self):
        fill = FillEvent("f1", "o1", "BTC", Side.BUY, D("0.00040"), D("77577.7"),
                         D("0.00372372960"), LiquidityRole.MAKER, 1.0,
                         realized_pnl=D("0.006774"))
        accounting = FillAccounting(fill, D("0.006774"), None, None, None)
        with JsonlTelemetrySink(self.path) as sink:
            sink.emit(accounting)
        self.assertEqual(list(_events(self.path)), [accounting])
        row = json.loads(self.path.read_text())
        self.assertEqual(row["data"]["fill"]["realized_pnl"], "0.006774")
        del row["data"]["fill"]["realized_pnl"]
        self.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertIsNone(next(_events(self.path)).fill.realized_pnl)


if __name__ == "__main__":
    unittest.main()
