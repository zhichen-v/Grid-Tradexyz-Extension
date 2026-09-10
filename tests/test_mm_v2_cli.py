"""CLI contract tests; every adapter/session is fake and no credentials are read."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
import io
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import run_volume_market_maker as cli
from core.services.market_maker_v2.config import load_config
from core.services.market_maker_v2.domain import AccountSnapshot, SessionReport
from core.services.market_maker_v2.api_budget import ApiBudget


class CliTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(cli.ROOT / "config/market_maker_v2/lighter_btc_volume.example.yaml")
        self.settings = {"network": "robinhood_testnet", "testnet": True,
                         "expected_l1_address": "0x" + "1" * 40, "account_index": 1,
                         "api_key_private_key": "private-sentinel"}
        self.result = SimpleNamespace(dry_run=True, completed=True, report=None,
                                      final_account=None, failure=None)

    def call_main(self, argv, *, config=None, result=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.object(cli, "load_config", return_value=config or self.config),
              patch.object(cli, "load_settings", return_value=self.settings) as settings,
              patch.object(cli, "build_adapter") as factory,
              patch.object(cli.orchestrator, "VolumeSession", create=True) as session,
              redirect_stdout(stdout), redirect_stderr(stderr)):
            session.return_value.run = AsyncMock(return_value=result or self.result)
            session.return_value.api_budget = ApiBudget(lambda: 0)
            session.return_value.market = SimpleNamespace(stream=None)
            status = cli.main(argv)
        return status, stdout.getvalue(), stderr.getvalue(), settings, factory, session

    def test_defaults_are_dry_example_and_authorization_flag_is_not_a_mode_switch(self):
        args = cli.parse_cli(["--output", "unused.jsonl"])
        self.assertTrue(load_config(args.config).dry_run)
        self.assertFalse(args.authorize_bounded_flatten)
        self.assertFalse(args.allow_delayed_dry_book)
        self.assertEqual(args.exchange_config, cli.ROOT / "config/exchanges/lighter_config.yaml")
        with TemporaryDirectory() as folder:
            result = self.call_main(["--output", str(Path(folder) / "run.jsonl"),
                                     "--authorize-bounded-flatten"])
            budget = json.loads((Path(folder) / "run.jsonl.budget.json").read_text(encoding="utf-8"))
            self.assertEqual(budget["scope"], "owned_python_transports")
            self.assertNotIn("private-sentinel", json.dumps(budget))
        self.assertEqual(result[0], 0)
        self.assertEqual(json.loads(result[1])["mode"], "dry_run")
        self.assertFalse(json.loads(result[1])["economics_evaluated"])

    def test_progress_prints_local_phase_without_changing_json_output(self):
        stderr = io.StringIO()
        async def check():
            with (redirect_stderr(stderr),
                  patch.object(cli.asyncio, "sleep", side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await cli._show_progress(SimpleNamespace(phase="api_quarantine_60s"))
        asyncio.run(check())
        self.assertIn("phase=api_quarantine_60s elapsed=", stderr.getvalue())
        self.assertNotIn("private-sentinel", stderr.getvalue())
        self.assertTrue(cli.parse_cli(["--output", "unused.jsonl", "--progress"]).progress)

    def test_progress_task_is_cancelled_on_session_failure(self):
        async def check(folder):
            started, stopped = asyncio.Event(), asyncio.Event()
            async def progress(session):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            async def run(event):
                await started.wait()
                raise RuntimeError("private-sentinel")
            session = SimpleNamespace(run=run, api_budget=ApiBudget(lambda: 0),
                                      market=SimpleNamespace(stream=None))
            with (patch.object(cli, "build_adapter"),
                  patch.object(cli.orchestrator, "VolumeSession", return_value=session),
                  patch.object(cli, "_show_progress", progress)):
                with self.assertRaises(RuntimeError):
                    await cli.run_session(self.config, self.settings,
                        output=Path(folder) / "session.jsonl", progress=True)
            self.assertTrue(stopped.is_set())
        with TemporaryDirectory() as folder:
            asyncio.run(check(folder))

    def test_delayed_data_is_explicit_dry_only_and_recorded_without_live_authority(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "dry.jsonl"
            result = self.call_main(["--output", str(output), "--allow-delayed-dry-book"],
                result=SimpleNamespace(**vars(self.result), delayed_dry_book=True))
            self.assertEqual(result[0], 0)
            self.assertTrue(result[5].call_args.kwargs["allow_delayed_dry_book"])
            self.assertEqual(json.loads(result[1])["source_time_profile"], "delayed_dry")
            budget = json.loads(Path(str(output) + ".budget.json").read_text())
            self.assertEqual(budget["source_time"], {"profile": "delayed_dry", "observations": None})
        for authorized in (False, True):
            with self.subTest(authorized=authorized), TemporaryDirectory() as folder:
                output = Path(folder) / "live.jsonl"
                args = ["--output", str(output), "--allow-delayed-dry-book"]
                if authorized:
                    args.append("--authorize-bounded-flatten")
                result = self.call_main(args, config=replace(self.config, dry_run=False))
                self.assertEqual(result[0], 1)
                self.assertFalse(output.exists())
                for unused in result[3:]:
                    unused.assert_not_called()

    def test_unauthorized_live_rejected_before_settings_factory_or_output_creation(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "run.jsonl"
            status, stdout, stderr, settings, factory, session = self.call_main(
                ["--output", str(output)], config=replace(self.config, dry_run=False))
            self.assertFalse(output.exists())
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("private-sentinel", stderr)
        settings.assert_not_called()
        factory.assert_not_called()
        session.assert_not_called()

    def test_output_is_exclusive_before_adapter_construction(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "run.jsonl"
            output.write_text("existing evidence", encoding="utf-8")
            status, _, _, _, factory, session = self.call_main(["--output", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), "existing evidence")
        self.assertEqual(status, 1)
        factory.assert_not_called()
        session.assert_not_called()

    def test_network_wallet_and_testnet_are_required_before_factory(self):
        for changes in ({"network": "mainnet"}, {"testnet": False}, {"testnet": 1},
                        {"expected_l1_address": None}, {"expected_l1_address": "invalid"},
                        {"account_index": True}):
            with self.subTest(changes=changes), TemporaryDirectory() as folder:
                original = self.settings
                self.settings = {**original, **changes}
                result = self.call_main(["--output", str(Path(folder) / "run.jsonl")])
                self.settings = original
                self.assertEqual(result[0], 1)
                result[4].assert_not_called()

    def test_live_flag_identity_and_stop_event_are_forwarded_to_session(self):
        with TemporaryDirectory() as folder:
            live_result = SimpleNamespace(**{**vars(self.result), "dry_run": False})
            result = self.call_main(["--output", str(Path(folder) / "run.jsonl"),
                                     "--authorize-bounded-flatten"],
                                    config=replace(self.config, dry_run=False), result=live_result)
        self.assertEqual(result[0], 0)
        session = result[5]
        self.assertTrue(session.call_args.kwargs["authorize_bounded_flatten"])
        self.assertEqual(session.call_args.kwargs["account_index"], 1)
        self.assertEqual(session.call_args.kwargs["expected_l1_address"], self.settings["expected_l1_address"])
        self.assertIsInstance(session.return_value.run.call_args.args[0], asyncio.Event)
        self.assertFalse(json.loads(result[1])["economics_evaluated"])

    def test_operator_signal_requests_stop_without_cancelling_and_handlers_logging_restore(self):
        handlers, previous = {}, {}
        def install(sig, handler):
            if sig not in handlers:
                previous[sig] = object()
                handlers[sig] = handler
                return previous[sig]
            self.assertIs(handler, previous[sig])
            return handlers[sig]
        async def fake_run(event):
            self.assertEqual(logging.root.manager.disable, logging.CRITICAL)
            handlers[cli.signal.SIGINT](cli.signal.SIGINT, None)
            await asyncio.wait_for(event.wait(), 1)
            return self.result
        before = logging.root.manager.disable
        async def invoke(output):
            with (patch.object(cli.signal, "signal", side_effect=install),
                  patch.object(cli, "build_adapter"),
                  patch.object(cli.orchestrator, "VolumeSession", create=True) as session):
                session.return_value.run = fake_run
                session.return_value.api_budget = ApiBudget(lambda: 0)
                session.return_value.market = SimpleNamespace(stream=None)
                return await cli.run_session(self.config, self.settings, output=output)
        with TemporaryDirectory() as folder:
            result = asyncio.run(invoke(Path(folder) / "run.jsonl"))
        self.assertIs(result, self.result)
        self.assertEqual(logging.root.manager.disable, before)
        self.assertEqual(len(handlers), 2)

    def test_session_errors_and_interrupts_do_not_print_credentials_or_tracebacks(self):
        for failure, expected_status in ((RuntimeError("private-sentinel"), 1),
                                         (KeyboardInterrupt("private-sentinel"), 130)):
            with self.subTest(failure=type(failure).__name__), TemporaryDirectory() as folder:
                stderr = io.StringIO()
                with (patch.object(cli, "load_settings", return_value=self.settings),
                      patch.object(cli, "build_adapter", side_effect=failure),
                      redirect_stderr(stderr)):
                    self.assertEqual(cli.main(["--output", str(Path(folder) / "run.jsonl")]), expected_status)
                self.assertNotIn("private-sentinel", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_cleanup_proof_is_visible_without_claiming_final_economic_success(self):
        cleanup = AccountSnapshot("BTC", 1.0, Decimal("0"), Decimal("99"),
            Decimal("0.00012"), Decimal("0.00035"), 0, True, open_order_ids=())
        result = SimpleNamespace(**{**vars(self.result), "dry_run": False,
            "completed": False, "failure": "accounting_incomplete", "final_account": None,
            "cleanup_account": cleanup})
        summary = cli._summary(replace(self.config, dry_run=False), result)
        self.assertFalse(summary["completed"] or summary["economics_evaluated"])
        self.assertTrue(summary["cleanup_authenticated"])
        self.assertEqual((summary["cleanup_position"], summary["cleanup_open_orders"]), ("0", 0))
        self.assertNotIn("final_authenticated", summary)

    def test_incomplete_or_dry_result_cannot_publish_economics(self):
        for dry in (True, False):
            report = SessionReport("BTC", False, Decimal("1"), 0, None, None)
            result = SimpleNamespace(**{**vars(self.result), "dry_run": dry, "report": report,
                                        "failure": "private-sentinel"})
            summary = cli._summary(replace(self.config, dry_run=dry), result)
            self.assertFalse(summary["economics_evaluated"])
            self.assertTrue(summary["failed"])
            self.assertNotIn("private-sentinel", json.dumps(summary))
            self.assertNotIn("all_in_net_pnl", summary)
        complete = SessionReport("BTC", True, Decimal("0"), 0, Decimal("0"), None,
                                 ledger_position=Decimal("0"), final_authenticated=True,
                                 equity_reconciliation_difference=Decimal("0"))
        for dry in (True, False):
            result = SimpleNamespace(**{**vars(self.result), "dry_run": dry, "report": complete})
            summary = cli._summary(replace(self.config, dry_run=dry), result)
            self.assertEqual(summary["economics_evaluated"], not dry)
            self.assertEqual("all_in_net_pnl" in summary, not dry)
            if not dry:
                self.assertEqual(summary["all_in_net_pnl"], "0")
                self.assertIsNone(summary["all_in_net_cost_bps"])

        stopped = SimpleNamespace(**{**vars(self.result), "dry_run": False, "report": complete,
                                     "completed": False, "failure": "session_failed_closed"})
        summary = cli._summary(replace(self.config, dry_run=False), stopped)
        self.assertFalse(summary["economics_evaluated"])
        self.assertNotIn("all_in_net_pnl", summary)

    def test_risk_capacity_stop_is_explicit_without_echoing_arbitrary_text(self):
        result = SimpleNamespace(**vars(self.result), stop_reason="risk_capacity_exhausted")
        summary = cli._summary(self.config, result)
        self.assertEqual(summary["stop_reason"], "risk_capacity_exhausted")
        result.stop_reason = "private-sentinel"
        with self.assertRaisesRegex(ValueError, "invalid session stop reason"):
            cli._summary(self.config, result)


if __name__ == "__main__":
    unittest.main()
