"""Original CLI/SDK/session integration against synthetic raw transport only.

The venue is an independent oracle, not a second use of the production ledger.
These tests exercise live *code paths*, never a live account or native signer.
"""

from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
import io
import importlib
import inspect
import json
import ctypes
import os
import socket
import shutil
import subprocess
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.mm_v2_wire_evidence import call_evidence, production_calls, source_fingerprint, write_evidence


ROOT = Path(__file__).resolve().parents[1]

REQUIRED = {
    "run_volume_market_maker": ("main", "run_session"),
    "lighter_preflight": ("load_settings", "build_adapter"),
    "core.services.market_maker_v2.config": ("load_config",),
    "core.services.market_maker_v2.orchestrator": ("VolumeSession.run", "bounded_exit"),
    "core.services.market_maker_v2.order_manager": ("MarketMakerOrderManager.reconcile", "MarketMakerOrderManager.cancel_managed_orders"),
    "core.services.market_maker_v2.execution_port": ("VolumeExecutionPort.reconcile_quotes", "BoundedExecutionPort.cancel_all_managed"),
    "core.services.market_maker_v2.lighter_runtime": ("LighterAccountPort.snapshot",),
    "core.services.market_maker_v2.session_ledger": ("SessionLedger.finalize",),
    "core.adapters.exchanges.adapters.lighter": ("LighterAdapter.create_order", "LighterAdapter.cancel_order"),
    "core.adapters.exchanges.adapters.lighter_rest": ("LighterRest._parse_order",),
    "core.adapters.exchanges.adapters.lighter_read_stream": ("LighterReadStream._receive", "LighterReadStream._update_book"),
    "lighter.signer_client": ("SignerClient.create_order", "SignerClient.cancel_order", "SignerClient.send_tx"),
    "lighter.api_client": ("ApiClient.response_deserialize",),
    "lighter.rest": ("RESTClientObject.request",),
}


def original_codes():
    codes = set()
    for module, names in REQUIRED.items():
        imported = importlib.import_module(module)
        for name in names:
            value = imported
            for part in name.split("."):
                value = getattr(value, part)
            codes.add(value.__code__)  # Includes the original SDK nonce decorator.
            codes.add(inspect.unwrap(value).__code__)
    return codes


def read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


class WireSessionTests(unittest.TestCase):
    def test_cold_import_does_not_leave_fixture_bound_to_production_stream(self):
        # A fresh interpreter is essential: ordinary tests may already have
        # imported the class, masking a polluted function-default restoration.
        source = '''import sys
sys.path.insert(0, sys.argv[1])
import websockets
from tests.mm_v2_wire_fixture import WireFixture
original = websockets.connect
assert "core.adapters.exchanges.adapters.lighter_read_stream" not in sys.modules
with WireFixture().install():
    from core.adapters.exchanges.adapters.lighter_read_stream import LighterReadStream
    assert LighterReadStream.__init__.__kwdefaults__["connect_factory"] is not original
assert websockets.connect is original
assert LighterReadStream.__init__.__kwdefaults__["connect_factory"] is original
print("cold_import_restored")
'''
        with TemporaryDirectory(prefix="mm_v2_cold_") as folder:
            environment = None
            if os.name == "nt":
                from tests.mm_v2_windows_process import clean_environment
                environment = clean_environment(Path(folder) / "profile")
            result = subprocess.run([sys.executable, "-I", "-c", source, str(ROOT)],
                cwd=folder, env=environment, capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cold_import_restored", result.stdout)

    def run_wire(self, scenario="normal", *, duration=150, defect=None, defect_name="placeholder_cancel"):
        from tests.mm_v2_wire_fixture import WireFixture
        temporary = TemporaryDirectory(prefix="mm_v2_wire_")
        self.addCleanup(temporary.cleanup)
        fixture = WireFixture(scenario=scenario, time_mode="virtual")
        paths = fixture.write_synthetic_workspace(Path(temporary.name), duration_seconds=duration)
        expected_codes = original_codes()
        from core.services.market_maker_v2.config import load_config
        starting_config = load_config(paths["config"])
        starting_source = source_fingerprint(ROOT)
        starting_clock = fixture.clock.monotonic()
        stdout, stderr = io.StringIO(), io.StringIO()
        with fixture.install(), production_calls() as calls:
            import run_volume_market_maker as cli
            arguments = ["--config", str(paths["config"]),
                         "--exchange-config", str(paths["exchange_config"]),
                         "--env-file", str(paths["env_file"]),
                         "--output", str(paths["output"]), "--authorize-bounded-flatten"]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                if defect is None:
                    code = cli.main(arguments)
                else:
                    with defect():
                        code = cli.main(arguments)
            config = load_config(paths["config"])
        self.assertEqual(starting_config, config, "synthetic strategy changed during run")
        summaries = [json.loads(line) for line in stdout.getvalue().splitlines()
                     if line.startswith('{"')]
        self.assertTrue(summaries, stderr.getvalue())
        summary = summaries[-1]
        events = read_events(paths["output"])
        venue = fixture.venue.snapshot()
        budget = json.loads(Path(str(paths["output"]) + ".budget.json").read_text())
        manifest = write_evidence(Path(str(paths["output"]) + ".synthetic.json"), root=ROOT,
            config=config, scenario=defect_name if defect else scenario, time_mode="virtual",
            venue=venue, exit_code=code,
            source_at_start=starting_source,
            loaded_code=call_evidence(calls, selected_codes=expected_codes),
            request_counts=dict(Counter(row["operation"] for row in fixture.venue.requests)))
        target = ROOT / "logs" / ("mm_v2_wire_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                                   + "_" + manifest["scenario"])
        target.mkdir(parents=True)
        for suffix in ("", ".budget.json", ".synthetic.json"):
            shutil.copyfile(Path(str(paths["output"]) + suffix), target / ("session.jsonl" + suffix))
        (target / "venue.json").write_text(json.dumps(venue, sort_keys=True), encoding="utf-8")
        (target / "summary.json").write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
        return dict(code=code, summary=summary, events=events, venue=venue, fixture=fixture,
                    calls=calls, expected_codes=expected_codes, budget=budget, manifest=manifest, paths=paths,
                    elapsed=fixture.clock.monotonic() - starting_clock, evidence_path=target)

    def assert_flat_proof(self, summary, events, venue):
        """A successful client report alone cannot establish exchange state."""
        self.assertTrue(summary["final_authenticated"])
        self.assertEqual(D(summary["final_position"]), D(0))
        self.assertEqual(summary["final_open_orders"], 0)
        self.assertEqual(D(venue["position"]), D(0), "venue still has exposure")
        self.assertEqual(venue["open_order_ids"], [], "venue still has an order")
        accounts = [row["data"] for row in events if row["event"] == "account_snapshot"]
        self.assertTrue(accounts, "missing authenticated account events")
        self.assertTrue(accounts[-1]["authenticated"])
        self.assertEqual(D(accounts[-1]["position"]), D(0))
        self.assertEqual(accounts[-1]["open_order_count"], 0)
        self.assertEqual(D(accounts[-1]["equity"]), D(venue["cash"]))

    def assert_original_chain(self, calls, expected_codes):
        for module, names in REQUIRED.items():
            for name in names:
                self.assertGreater(calls[(module, name)], 0, f"original code not executed: {module}.{name}")
        observed = {code for values in calls.codes.values() for code in values}
        for code in expected_codes:
            self.assertIn(code, observed, f"original code object not executed: {code.co_qualname}")

    def assert_normal_run(self, run):
        self.assertEqual(run["code"], 0)
        self.assertTrue(run["summary"]["completed"])
        self.assertFalse(run["summary"]["failed"])
        self.assertFalse([row for row in run["events"] if row["event"] == "failure_diagnostic"])
        self.assert_flat_proof(run["summary"], run["events"], run["venue"])
        evidence = [row["data"] for row in run["events"] if row["event"] == "order_evidence"]
        self.assertGreaterEqual(len(evidence), 6, "both sides must survive two 60s expiry cycles")
        self.assertEqual({row["time_in_force"] for row in evidence}, {"POST_ONLY"})
        self.assertEqual({row["side"] for row in evidence}, {"buy", "sell"})
        self.assertEqual(len({row["order_id"] for row in evidence}), len(evidence))
        self.assertFalse(any(row["reduce_only"] for row in evidence))
        report = [row["data"] for row in run["events"] if row["event"] == "session_report"][-1]
        self.assertTrue(report["complete"])
        self.assertGreaterEqual(D(report["duration_seconds"]), D(150))
        self.assertEqual(D(report["equity_reconciliation_difference"]), D(0))
        self.assertEqual((report["maker_fill_count"], report["taker_fill_count"]), (0, 0))
        requests = run["venue"]["requests"]
        cancellations = [row for row in requests if row.get("tx_type") == 15]
        orders = run["venue"]["orders"]
        self.assertEqual(Counter(row["order_id"] for row in cancellations),
                         Counter(order["id"] for order in orders), "each order must be canceled exactly once")
        normal_expiries = [row for row in run["venue"]["events"]
                           if row["kind"] == "cancel_accepted" and row["order_age_seconds"] >= 60]
        self.assertGreaterEqual(len(normal_expiries), 4)
        self.assertEqual({order["side"] for order in orders if order["id"] in
                          {row["order_id"] for row in normal_expiries[:4]}}, {"buy", "sell"})
        self.assert_wire_budget(run)

    def assert_wire_budget(self, run):
        requests = run["venue"]["requests"]
        observed = Counter(row["operation"] for row in requests if row["method"] in {"GET", "POST"})
        recorded = {key.removeprefix("rest:"): value for key, value in run["budget"]["attempts"].items()
                    if key.startswith("rest:")}
        self.assertEqual(dict(observed), recorded, "meter must cover both real SDK API clients")
        nonces = [row["transaction_nonce"] for row in requests if row["operation"] == "sendTx"]
        self.assertEqual(nonces, list(range(len(nonces))), "no nonce reuse after ambiguous send")

    def test_original_cli_sdk_and_raw_stream_survive_two_quote_expiries(self):
        run = self.run_wire()
        self.assert_normal_run(run)
        self.assert_original_chain(run["calls"], run["expected_codes"])
        manifest = run["manifest"]
        self.assertTrue(manifest["synthetic"])
        self.assertFalse(manifest["live_economics_evidence"])
        self.assertFalse(manifest["sdk"]["native_executed"])
        self.assertFalse(manifest["isolation"]["os_network_isolation_verified"])
        self.assertIn("run_live_test.ps1", manifest["source"]["files"])
        self.assertEqual(len(manifest["source"]["sha256"]), 64)
        self.assertEqual(manifest["python"]["executable"], sys.executable)
        # The existing analyzer must label this synthetic live-code run as replay.
        from scripts.analyze_mm_v2_session import build_report
        analysis = build_report([run["paths"]["output"]], candidate="synthetic_wire",
            mode="replay", planned_seconds="150", wall_seconds=[str(run["elapsed"])])
        self.assertFalse(analysis["aggregate"]["economics_evaluated"])

    def test_late_terminal_and_opposite_fill_use_fresh_residual_for_one_ioc(self):
        run = self.run_wire("late_cancel_fill")
        self.assertEqual(run["code"], 1, "successful cleanup cannot erase the original session failure")
        self.assertFalse(run["summary"]["completed"])
        self.assertTrue(run["summary"]["failed"])
        self.assertFalse(run["summary"]["economics_evaluated"])
        self.assert_flat_proof(run["summary"], run["events"], run["venue"])
        self.assert_original_chain(run["calls"], run["expected_codes"])
        events = run["venue"]["events"]
        hidden = [row for row in events if row["kind"] == "terminal_history_hidden"]
        self.assertEqual(len(hidden), 5, "four initial polls plus the first cleanup read must miss")
        canceled = hidden[0]["order_id"]
        visible = [row for row in events if row["kind"] == "terminal_history_visible" and row["order_id"] == canceled]
        self.assertEqual(len(visible), 1)
        accepted = [row for row in events if row["kind"] == "create_accepted"]
        self.assertEqual([row["time_in_force"] for row in accepted],
                         ["post-only", "post-only", "immediate-or-cancel"])
        self.assertTrue(accepted[-1]["reduce_only"])
        self.assertLessEqual(visible[0]["monotonic"], accepted[-1]["monotonic"])
        self.assertEqual(D(accepted[-1]["size"]), D(".00040"))
        self.assertEqual(Counter(row["order_id"] for row in run["venue"]["requests"] if row.get("tx_type") == 15),
                         Counter({canceled: 1}))
        fills = [row["data"]["fill"] for row in run["events"] if row["event"] == "fill"]
        self.assertEqual([(row["side"], row["liquidity"], D(row["size"])) for row in fills],
                         [("sell", "maker", D(".00040")), ("buy", "taker", D(".00040"))])
        # Hand-calculated from the fixed synthetic tape, independent of production accounting.
        self.assertEqual([D(row["price"]) for row in fills], [D("77009.3"), D("77000.1")])
        self.assertEqual([D(row["fee"]) for row in fills], [D(".0036964464"), D(".0107800140")])
        self.assertEqual(D(run["venue"]["cash"]), D("999.9892035396"))
        report = [row["data"] for row in run["events"] if row["event"] == "session_report"][-1]
        self.assertEqual(D(report["realized_gross_pnl"]), D(".00368"))
        self.assertEqual(D(report["realized_net_pnl"]), D("-.0107964604"))
        self.assertEqual((report["maker_fill_count"], report["taker_fill_count"]), (1, 1))
        exits = [row["data"] for row in run["events"] if row["event"] == "bounded_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual((exits[0]["status"], exits[0]["attempts"]), ("flat", 1))
        self.assertLess(exits[0]["observed_monotonic"] - hidden[0]["monotonic"], 30)
        self.assert_wire_budget(run)

    def test_exit_waits_for_late_exact_terminal_without_repeating_mutations(self):
        from tests.mm_v2_wire_fixture import WireVenue
        original = WireVenue.accept_tx

        def late_proof(venue, tx_type, tx):
            result = original(venue, tx_type, tx)
            if tx_type == 15 and venue.cancel_count == 1:
                # Two known slots can need separate history queries in one sync.
                # Seven hidden HTTP proofs put visibility after the old two-sync cap.
                venue.delayed[tx["OrderNonce"]] = 7
                venue.event("terminal_delay_injected", hidden_reads=7)
            return result

        with patch.object(WireVenue, "accept_tx", late_proof):
            run = self.run_wire("late_cancel_fill", duration=5)
        self.assertEqual(run["code"], 0)
        self.assertTrue(run["summary"]["completed"])
        self.assertFalse(run["summary"]["failed"])
        self.assert_flat_proof(run["summary"], run["events"], run["venue"])
        self.assert_original_chain(run["calls"], run["expected_codes"])
        events = run["venue"]["events"]
        hidden = [row for row in events if row["kind"] == "terminal_history_hidden"]
        self.assertEqual(len(hidden), 7)
        canceled = hidden[0]["order_id"]
        visible = next(row for row in events if row["kind"] == "terminal_history_visible" and row["order_id"] == canceled)
        accepted = [row for row in events if row["kind"] == "create_accepted"]
        self.assertEqual([row["time_in_force"] for row in accepted],
                         ["post-only", "post-only", "immediate-or-cancel"])
        self.assertTrue(accepted[-1]["reduce_only"])
        self.assertLess(visible["monotonic"], accepted[-1]["monotonic"])
        requests = run["venue"]["requests"]
        self.assertEqual(Counter(row["order_id"] for row in requests if row.get("tx_type") == 15), Counter({canceled: 1}))
        self.assertEqual(run["venue"]["trade_count"], 2)
        report = run["events"][-1]["data"]
        self.assertEqual((report["maker_fill_count"], report["taker_fill_count"]), (1, 1))
        self.assertEqual(D(report["equity_reconciliation_difference"]), D(0))
        exits = [row["data"] for row in run["events"] if row["event"] == "bounded_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual((exits[0]["status"], exits[0]["attempts"]), ("flat", 1))
        self.assertLess(exits[0]["observed_monotonic"] - hidden[0]["monotonic"], 30)
        self.assert_wire_budget(run)

    def test_one_account_http_503_after_maker_fill_recovers_through_original_cli(self):
        # Fault injection for a transient-read gap, not attribution of a past live failure.
        run = self.run_wire("account_503_after_fill")
        venue, events = run["venue"], run["venue"]["events"]
        faults = [row for row in events if row["kind"] == "account_http_503"]
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]["trade_count"], 1)
        maker = [row for row in events if row["kind"] == "fill" and row["liquidity"] == "maker"]
        self.assertEqual(len(maker), 1)
        self.assertLess(maker[0]["monotonic"], faults[0]["monotonic"])
        account_reads = [row for row in venue["requests"] if row["operation"] == "account"]
        failed = [index for index, row in enumerate(account_reads) if row["response_status"] == 503]
        self.assertEqual(len(failed), 1)
        self.assertEqual(account_reads[failed[0] + 1]["response_status"], 200)
        self.assertEqual(run["code"], 0, "one failed GET must not terminate the session after recovery")
        self.assertEqual(run["budget"]["balance_reads"], {"retries": 1, "recoveries": 1})
        self.assertTrue(run["summary"]["completed"])
        self.assertFalse(run["summary"]["failed"])
        self.assert_flat_proof(run["summary"], run["events"], venue)
        self.assert_original_chain(run["calls"], run["expected_codes"])
        report = [row["data"] for row in run["events"] if row["event"] == "session_report"][-1]
        self.assertTrue(report["complete"])
        self.assertGreaterEqual(D(report["duration_seconds"]), D(150))
        self.assertEqual((report["maker_fill_count"], report["taker_fill_count"]), (1, 1))
        fills = [row["data"]["fill"] for row in run["events"] if row["event"] == "fill"]
        self.assertEqual([(row["side"], row["liquidity"], D(row["size"])) for row in fills],
                         [("sell", "maker", D(".00040")), ("buy", "taker", D(".00040"))])
        self.assertEqual(venue["trade_count"], 2)
        self.assertEqual(D(report["equity_reconciliation_difference"]), D(0))
        mutations = [row for row in venue["requests"] if "tx_type" in row]
        self.assertEqual(len({row["transaction_nonce"] for row in mutations}), len(mutations))
        creates = [row["client_order_index"] for row in mutations if row["tx_type"] == 14]
        self.assertEqual(len(set(creates)), len(creates))
        cancels = [row["order_id"] for row in mutations if row["tx_type"] == 15]
        filled_ids = {row["order_id"] for row in events if row["kind"] == "fill"}
        self.assertEqual(Counter(cancels), Counter({row["id"]: 1 for row in venue["orders"]
                                                  if row["id"] not in filled_ids}))
        self.assert_wire_budget(run)

    def test_accepted_cancel_response_loss_reconciles_without_resending(self):
        run = self.run_wire("lost_cancel_response")
        self.assert_normal_run(run)
        lost = [row for row in run["venue"]["events"] if row["kind"] == "response_lost"]
        self.assertEqual(len(lost), 1, "must actually inject the post-acceptance failure")
        attempts = [row for row in run["venue"]["requests"] if row.get("order_id") == lost[0]["order_id"]]
        self.assertEqual(len(attempts), 1, "an accepted cancellation cannot be retried after response loss")

    def assert_book_fault_stops_new_risk(self, run, diagnostic):
        self.assertEqual(run["code"], 1)
        self.assertTrue(run["summary"]["failed"])
        self.assertFalse(run["summary"]["completed"])
        self.assertFalse(run["summary"]["economics_evaluated"])
        faults = [row for row in run["venue"]["events"] if row["kind"] == "book_fault"]
        self.assertEqual(len(faults), 1)
        fault = faults[0]
        quotes = [row["data"] for row in run["events"] if row["event"] == "order_evidence"]
        self.assertEqual(len(quotes), 2, "fault must follow two originally confirmed live quotes")
        self.assertTrue(all(row["confirmed_monotonic"] < fault["monotonic"] for row in quotes))
        submissions = [row for row in run["venue"]["requests"] if row.get("tx_type") == 14]
        self.assertEqual(len(submissions), 2)
        self.assertTrue(all(row["monotonic"] < fault["monotonic"] for row in submissions))
        self.assertTrue(all(row["time_in_force"] == 2 and not row["reduce_only"] for row in submissions))
        failures = [row["data"] for row in run["events"] if row["event"] == "failure_diagnostic"]
        self.assertTrue(failures)
        self.assertTrue(any(value["name"] == diagnostic and D(value["value"]) == 1
                            for row in failures for value in row["values"]), failures)
        canceled = [row["order_id"] for row in run["venue"]["requests"] if row.get("tx_type") == 15]
        filled = {row["order_id"] for row in run["venue"]["events"] if row["kind"] == "fill"}
        self.assertEqual(Counter(canceled), Counter({row["order_id"]: 1 for row in quotes if row["order_id"] not in filled}))
        self.assertEqual(run["venue"]["open_order_ids"], [])
        exits = [row["data"] for row in run["events"] if row["event"] == "bounded_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["attempts"], 0, "untrusted market cannot price an IOC")
        self.assertLess(exits[0]["observed_monotonic"] - fault["monotonic"], 30)
        self.assert_original_chain(run["calls"], run["expected_codes"])
        self.assert_wire_budget(run)
        return failures, exits[0]

    def test_raw_book_failures_stop_quotes_and_cancel_exact_known_orders(self):
        for scenario, diagnostic in (("book_wait_timeout", "market_book_wait_timeout"),
                                     ("book_invalid_nonce", "market_book_invalid"),
                                     ("book_transport_close", "market_transport_unhealthy")):
            with self.subTest(scenario=scenario):
                run = self.run_wire(scenario)
                failures, exit_report = self.assert_book_fault_stops_new_risk(run, diagnostic)
                self.assert_flat_proof(run["summary"], run["events"], run["venue"])
                self.assertEqual(exit_report["status"], "flat")
                if scenario == "book_wait_timeout":
                    retry_counts = [D(value["value"]) for row in failures for value in row["values"]
                                    if value["name"] == "market_book_retry_count"]
                    self.assertTrue(retry_counts)
                    self.assertLessEqual(max(retry_counts), 1)
                    self.assertEqual(run["budget"]["market_reads"], {"retries": 1, "recoveries": 0})
                else:
                    self.assertEqual(run["budget"]["market_reads"], {"retries": 0, "recoveries": 0})

    def test_book_failure_with_concurrent_fill_reports_unflattened_residual(self):
        run = self.run_wire("book_invalid_nonce_fill")
        _, exit_report = self.assert_book_fault_stops_new_risk(run, "market_book_invalid")
        self.assertEqual(exit_report["status"], "blocked")
        self.assertEqual(D(run["venue"]["position"]), D("-.00040"))
        self.assertTrue(run["summary"]["final_authenticated"])
        self.assertEqual(D(run["summary"]["final_position"]), D("-.00040"))
        self.assertEqual(run["summary"]["final_open_orders"], 0)
        fills = [row["data"]["fill"] for row in run["events"] if row["event"] == "fill"]
        self.assertEqual([(row["side"], row["liquidity"], D(row["size"])) for row in fills],
                         [("sell", "maker", D(".00040"))])
        self.assertEqual(D(fills[0]["fee"]), D(".0036964464"))
        self.assertEqual(D(run["venue"]["cash"]), D("999.9963035536"))
        self.assertEqual(run["budget"]["market_reads"], {"retries": 0, "recoveries": 0})

    def test_one_fresh_raw_account_bracket_recovers_public_nonce_ahead(self):
        run = self.run_wire("book_alignment_recovers")
        self.assert_normal_run(run)
        self.assert_original_chain(run["calls"], run["expected_codes"])
        self.assert_wire_budget(run)
        faults = [row for row in run["venue"]["events"] if row["kind"] == "book_fault"]
        available = [row for row in run["venue"]["events"] if row["kind"] == "book_alignment_available"]
        self.assertEqual((len(faults), len(available)), (1, 1))
        self.assertLessEqual(faults[0]["monotonic"], available[0]["monotonic"])
        self.assertEqual(run["budget"]["market_reads"], {"retries": 1, "recoveries": 1})
        requests = run["venue"]["requests"]
        fresh = [row for row in requests if available[0]["monotonic"] < row["monotonic"] < available[0]["monotonic"] + 1]
        brackets = [row for row in fresh if row["operation"] == "subscribe" and row["path"] == "account_orders/1/7"]
        self.assertEqual(len(brackets), 2, "recovery requires a complete new account bracket")
        self.assertTrue(all(row["response_nonce"] == available[0]["nonce"] for row in brackets))
        self.assertTrue(any(row["operation"] == "account" and brackets[0]["monotonic"] < row["monotonic"] < brackets[1]["monotonic"]
                            for row in fresh), "fresh REST cash must be inside the two new order reads")
        creates = [row for row in requests if row.get("tx_type") == 14]
        self.assertTrue(any(row["monotonic"] > available[0]["monotonic"] for row in creates))

    def test_alignment_recovery_contract_detects_the_prior_early_exit_policy(self):
        @contextmanager
        def original_early_exit_policy():
            from core.services.market_maker_v2.lighter_runtime import LighterAccountPort
            original = LighterAccountPort.snapshot

            async def without_required_alignment(account, **kwargs):
                # Restore only the old caller policy. Original account reads,
                # SDK, stream and market validation still execute unchanged.
                kwargs["require_aligned_book"] = False
                return await original(account, **kwargs)

            with patch.object(LighterAccountPort, "snapshot", without_required_alignment):
                yield

        run = self.run_wire("book_alignment_recovers", defect=original_early_exit_policy,
                            defect_name="book_alignment_no_retry")
        with self.assertRaises(AssertionError):
            self.assert_normal_run(run)
        self.assert_book_fault_stops_new_risk(run, "market_outside_watermarks")
        self.assert_flat_proof(run["summary"], run["events"], run["venue"])
        self.assertEqual(run["budget"]["market_reads"], {"retries": 0, "recoveries": 0})

    def test_missing_terminal_proof_stops_new_risk_and_reports_residual_honestly(self):
        run = self.run_wire("unresolved_cancel_response")
        self.assertEqual(run["code"], 1)
        self.assertFalse(run["summary"]["completed"])
        self.assertTrue(run["summary"]["failed"])
        self.assertFalse(run["summary"]["economics_evaluated"])
        self.assertEqual(D(run["venue"]["position"]), D(0))
        self.assertEqual(len(run["venue"]["open_order_ids"]), 1)
        self.assertEqual(run["summary"]["final_open_orders"], 1)
        events = run["venue"]["events"]
        accepted = [row for row in events if row["kind"] == "create_accepted"]
        self.assertEqual([row["time_in_force"] for row in accepted], ["post-only", "post-only"])
        lost = [row for row in events if row["kind"] == "response_lost"]
        self.assertEqual(len(lost), 1)
        cancellations = [row for row in run["venue"]["requests"] if row.get("tx_type") == 15]
        self.assertEqual([row["order_id"] for row in cancellations], [lost[0]["order_id"]])
        self.assertFalse([row for row in events if row["kind"] == "terminal_history_visible"])
        hidden = [row for row in events if row["kind"] == "terminal_history_hidden"]
        self.assertGreaterEqual(len(hidden), 4)
        recovery = [{value["name"]: D(value["value"]) for value in row["data"]["values"]}
                    for row in run["events"] if row["event"] == "failure_diagnostic"
                    and any(value["name"] == "recovery_reads" for value in row["data"]["values"])]
        self.assertEqual(len(recovery), 1)
        reads = int(recovery[0]["recovery_reads"])
        self.assertGreaterEqual(reads, 1)
        self.assertLessEqual(reads, 20)
        reasons = set(recovery[0]) & {"recovery_terminal_pending", "recovery_deadline_exhausted", "recovery_budget_refused"}
        self.assertEqual(len(reasons), 1)
        if "recovery_terminal_pending" in reasons:
            self.assertEqual(reads, 20, "missing proof can stop before the cap only for time or admission")
        self.assertLessEqual(len(hidden), 4 + reads + 2)  # Original polls and final-account observation.
        exits = [row["data"] for row in run["events"] if row["event"] == "bounded_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual((exits[0]["status"], exits[0]["attempts"]), ("blocked", 0))
        self.assertEqual(sum(row["monotonic"] <= exits[0]["observed_monotonic"] for row in hidden), 4 + reads)
        self.assertLess(exits[0]["observed_monotonic"] - lost[0]["monotonic"], 30)
        self.assert_wire_budget(run)

    def test_transport_guard_blocks_escape_and_preserves_original_asyncio_wakeup(self):
        from tests.mm_v2_wire_fixture import WireFixture
        import lighter.signer_client as signer

        original_pair, original_connect = socket.socketpair, socket.socket.connect
        fixture = WireFixture()
        with fixture.install(), patch.object(ctypes, "CDLL", side_effect=AssertionError("native loader reached")) as loader:
            # These calls must fail in Python before reaching DNS, sockets or FFI.
            for address in (("203.0.113.1", 1), ("127.0.0.1", 1)):
                with self.subTest(address=address), socket.socket() as sock:
                    with self.assertRaisesRegex(AssertionError, "physical network forbidden"):
                        sock.connect(address)
                    with self.assertRaisesRegex(AssertionError, "physical network forbidden"):
                        sock.connect_ex(address)
            with self.assertRaisesRegex(AssertionError, "physical network forbidden"):
                socket.getaddrinfo("synthetic.invalid", 443)
            with self.assertRaisesRegex(AssertionError, "physical network forbidden"):
                getattr(signer, "__get_shared_library")()
            loader.assert_not_called()
            # Only the standard library's private loopback pair may cross the guard.
            left, right = socket.socketpair()
            try:
                left.sendall(b"wake")
                self.assertEqual(right.recv(4), b"wake")
            finally:
                left.close()
                right.close()
        self.assertIs(socket.socketpair, original_pair)
        self.assertIs(socket.socket.connect, original_connect)
        self.assertEqual(fixture.venue.requests, [])

    def test_venue_cannot_make_invalid_wire_orders_or_native_modes_look_valid(self):
        from tests.mm_v2_wire_fixture import WireFixture
        from lighter.signer_client import SignerClient

        valid = dict(AccountIndex=7, OrderBookIndex=1, Nonce=0, ApiKeyIndex=0,
            ClientOrderIndex=11, BaseAmount=40, Price=769990, IsAsk=0, OrderType=0,
            TimeInForce=2, ReduceOnly=False, TriggerPrice=0, ExpiredAt=1789260000,
            IntegratorAccountIndex=0, IntegratorMakerFee=0, IntegratorTakerFee=0)
        for changes in ({"AccountIndex": 8}, {"OrderBookIndex": 2}, {"Nonce": 1},
                        {"ApiKeyIndex": 1}, {"BaseAmount": 1}, {"BaseAmount": -40},
                        {"Price": 770001}, {"OrderType": 1}, {"TimeInForce": 0},
                        {"ReduceOnly": 1}, {"ReduceOnly": True}, {"IntegratorMakerFee": 1}):
            with self.subTest(changes=changes):
                venue = WireFixture().venue
                with self.assertRaises(AssertionError):
                    venue.accept_tx(14, {**valid, **changes})
                self.assertEqual((venue.nonce, venue.orders, venue.trades), (0, {}, []))
        fixture = WireFixture()
        fixture.venue.accept_tx(14, valid)
        with self.assertRaisesRegex(AssertionError, "duplicate synthetic create client identity"):
            fixture.venue.accept_tx(14, {**valid, "Nonce": 1})
        self.assertEqual((fixture.venue.nonce, len(fixture.venue.orders)), (1, 1))

        fixture = WireFixture()
        with fixture.install():
            sdk = object.__new__(SignerClient)
            sdk.signer, sdk.account_index = fixture.venue.native, 7
            arguments = dict(market_index=1, client_order_index=11, base_amount=40, price=769990,
                is_ask=False, order_type=0, time_in_force=2, nonce=0, api_key_index=0)
            for changes in ({"skip_nonce": 1}, {"self_trade_behavior_mode": 1},
                            {"self_trade_equality_mode": 1}):
                with self.subTest(changes=changes), self.assertRaisesRegex(AssertionError, "native signing mode"):
                    sdk.sign_create_order(**arguments, **changes)
        self.assertEqual(fixture.venue.native.buffers, {})

    def test_historical_placeholder_cancel_defect_is_caught_by_same_normal_contract(self):
        @contextmanager
        def historical_defect():
            from core.adapters.exchanges.adapters.lighter import LighterAdapter
            from core.adapters.exchanges.adapters.lighter_rest import LighterRest
            from core.adapters.exchanges.models import OrderSide
            original = LighterAdapter.cancel_order

            async def placeholder(adapter, order_id, symbol):
                receipt = await original(adapter, order_id, symbol)
                # Reintroduce the 215907 handoff loss only inside this negative control.
                return replace(receipt, side=OrderSide.BUY, amount=D(0), price=None, client_id=None)

            # The historical defect also lost the complete terminal handoff;
            # retaining today's exact cache would legitimately repair the DTO.
            with (patch.object(LighterAdapter, "cancel_order", placeholder),
                  patch.object(LighterRest, "get_terminal_cancellation_outcome", return_value=None)):
                yield

        run = self.run_wire(defect=historical_defect)
        with self.assertRaises(AssertionError):
            self.assert_normal_run(run)
        self.assertEqual(run["code"], 1)
        self.assertFalse(run["summary"]["completed"])
        self.assertTrue(any(row["event"] == "failure_diagnostic" and
                            row["data"]["stage"] == "reconciling_quotes" for row in run["events"]))

    def test_independent_venue_rejects_a_false_flat_client_report(self):
        run = self.run_wire()
        self.assert_normal_run(run)
        # An omitted exchange order must defeat the same production acceptance check.
        residual = {**run["venue"], "open_order_ids": ["synthetic-omitted-order"]}
        with self.assertRaisesRegex(AssertionError, "venue still has an order"):
            self.assert_flat_proof(run["summary"], run["events"], residual)


if __name__ == "__main__":
    unittest.main()
