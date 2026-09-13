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
from core.services.market_maker_v2.domain import (
    AccountSnapshot, BoundedExitReport, ExitStatus, FailureDiagnostic, FillAccounting,
    FillEvent, InventoryDecision, LiquidityRole, MarkEvent, SessionReport, Side, StrategyState,
)
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink
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
              patch.object(cli, "_git_build", return_value={
                  "status": "available", "commit": "a" * 40, "dirty": True}),
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

    def test_run_provenance_records_only_effective_public_strategy_values(self):
        configured = replace(self.config,
            quote=replace(self.config.quote, order_size=Decimal("0.00017000"),
                          target_net_edge_bps=Decimal("2.500")),
            session=replace(self.config.session, duration_seconds=3600))
        with TemporaryDirectory() as folder:
            output = Path(folder) / "session.jsonl"
            result = self.call_main(["--output", str(output)], config=configured)
            self.assertEqual(result[0], 0)
            budget = json.loads(Path(str(output) + ".budget.json").read_text(encoding="utf-8"))
        provenance = budget["provenance"]
        self.assertEqual(set(provenance), {"schema", "capture", "build", "effective_config"})
        self.assertEqual(provenance["schema"], "mm_v2_run_provenance_v1")
        self.assertEqual(provenance["capture"], "run_start")
        self.assertEqual(provenance["build"], {"status": "available", "commit": "a" * 40, "dirty": True})
        effective = provenance["effective_config"]
        self.assertEqual(set(effective), {"symbol", "profile", "dry_run", "quote", "inventory", "flatten", "session"})
        self.assertEqual(effective["quote"], {"order_size": "0.00017000", "target_net_edge_bps": "2.500",
            "volatility_multiplier": str(configured.quote.volatility_multiplier),
            "reprice_threshold_ticks": configured.quote.reprice_threshold_ticks,
            "max_quote_age_ms": configured.quote.max_quote_age_ms})
        self.assertEqual(effective["inventory"], {"soft_limit": str(configured.inventory.soft_limit),
            "hard_limit": str(configured.inventory.hard_limit), "skew_bps_at_hard": str(configured.inventory.skew_bps_at_hard)})
        self.assertEqual(effective["flatten"], {"max_hold_seconds": configured.flatten.max_hold_seconds,
            "stop_loss_usdg": str(configured.flatten.stop_loss_usdg),
            "passive_grace_seconds": configured.flatten.passive_grace_seconds,
            "ioc_slippage_ticks": configured.flatten.ioc_slippage_ticks})
        self.assertEqual(effective["session"], {"duration_seconds": 3600,
            "max_loss_usdg": str(configured.session.max_loss_usdg),
            "cooldown_seconds": configured.session.cooldown_seconds})
        recorded = json.dumps(provenance)
        for forbidden in ("private-sentinel", self.settings["expected_l1_address"], "account_index",
                          "expected_l1_address", "api_key", "network", "config_path", ".env"):
            self.assertNotIn(forbidden, recorded)

    def test_git_build_is_sanitized_and_unavailable_on_failed_or_invalid_read(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), patch.object(cli.subprocess, "run", side_effect=[
                    SimpleNamespace(stdout="b" * 40 + "\n"),
                    SimpleNamespace(stdout="?? private-sentinel\n" if dirty else "")]) as run:
                self.assertEqual(cli._git_build(), {"status": "available", "commit": "b" * 40, "dirty": dirty})
                self.assertTrue(all(call.kwargs["cwd"] == cli.ROOT and call.kwargs["capture_output"]
                                    and call.kwargs["timeout"] == 3 for call in run.call_args_list))
        for failure in (FileNotFoundError("private-sentinel"),
                        cli.subprocess.TimeoutExpired("private-sentinel", 3),
                        cli.subprocess.CalledProcessError(1, "git", stderr="private-sentinel")):
            with self.subTest(failure=type(failure).__name__), patch.object(cli.subprocess, "run", side_effect=failure):
                self.assertEqual(cli._git_build(), {"status": "unavailable", "commit": None, "dirty": None})
        with patch.object(cli.subprocess, "run", return_value=SimpleNamespace(stdout="private-sentinel")):
            self.assertEqual(cli._git_build(), {"status": "unavailable", "commit": None, "dirty": None})

    def test_provenance_is_flushed_before_adapter_and_start_version_survives_failure(self):
        async def check(folder, fails):
            output = Path(folder) / "session.jsonl"
            sidecar = Path(str(output) + ".budget.json")
            original = {"status": "available", "commit": "c" * 40, "dirty": False}

            def construct(settings):
                self.assertEqual(json.loads(sidecar.read_text())["provenance"]["build"], original)
                if fails:
                    raise RuntimeError("private-sentinel")
                return object()

            session = SimpleNamespace(run=AsyncMock(return_value=self.result),
                                      api_budget=ApiBudget(lambda: 0), market=SimpleNamespace(stream=None))
            with (patch.object(cli, "_git_build", side_effect=[original, {"status": "unavailable"}]) as build,
                  patch.object(cli, "build_adapter", side_effect=construct),
                  patch.object(cli.orchestrator, "VolumeSession", return_value=session)):
                if fails:
                    with self.assertRaises(RuntimeError):
                        await cli.run_session(self.config, self.settings, output=output)
                else:
                    await cli.run_session(self.config, self.settings, output=output)
                build.assert_called_once_with()
            saved = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(saved["provenance"]["build"], original)
            self.assertNotIn("private-sentinel", json.dumps(saved))
        for fails in (False, True):
            with self.subTest(fails=fails), TemporaryDirectory() as folder:
                asyncio.run(check(folder, fails))

    def test_progress_prints_local_phase_without_changing_json_output(self):
        stderr = io.StringIO()
        async def check():
            with (redirect_stderr(stderr),
                  patch.object(cli.asyncio, "sleep", side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await cli._show_progress(SimpleNamespace(phase="api_quarantine_60s"),
                                             cli._ConsoleProgress(None))
        asyncio.run(check())
        self.assertIn("phase=api_quarantine_60s", stderr.getvalue())
        self.assertNotIn("private-sentinel", stderr.getvalue())
        self.assertTrue(cli.parse_cli(["--output", "unused.jsonl", "--progress"]).progress)

    def test_progress_task_is_cancelled_on_session_failure(self):
        async def check(folder):
            started, stopped = asyncio.Event(), asyncio.Event()
            async def progress(session, console):
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

    def test_heartbeat_uses_existing_session_account_without_restamping_or_reading(self):
        d = Decimal
        account = AccountSnapshot("BTC", 45.0, d("0"), d("299"),
                                  d("0.00012"), d("0.00035"), 0, True)
        stale = replace(account, observed_monotonic=40.0)
        session = SimpleNamespace(phase="waiting", final_account=account)
        stderr = io.StringIO()
        async def check():
            with (redirect_stderr(stderr), patch.object(cli.time, "monotonic", return_value=50),
                  patch.object(cli.asyncio, "sleep", side_effect=asyncio.CancelledError)):
                console = cli._ConsoleProgress(None)
                with self.assertRaises(asyncio.CancelledError):
                    await cli._show_progress(session, console)
                self.assertIs(console.account, account)
                self.assertEqual(console.account_at, 45.0)
                console._account(stale)
                console._account(None)
                self.assertIs(console.account, account)
        asyncio.run(check())
        self.assertIn("account_age=5s", stderr.getvalue())

    def test_progress_coalesces_quote_steps_and_keeps_one_minute_heartbeat(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(cli.time, "monotonic", return_value=0) as clock:
            console = cli._ConsoleProgress(None)
            console.status("waiting")
            for second in range(1, 60):
                clock.return_value = second
                console.status(("syncing_orders", "authorizing_quotes", "reconciling_quotes")[second % 3])
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            clock.return_value = 60
            console.status("waiting")
            clock.return_value = 61
            console.status("api_cooldown")
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("elapsed=60s phase=quoting", lines[1])
        self.assertIn("phase=api_cooldown", lines[2])

    def test_optional_wait_does_not_print_each_monitor_step(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(cli.time, "monotonic", return_value=0) as clock:
            console = cli._ConsoleProgress(None)
            console.status("api_wait")
            for second in range(1, 60):
                clock.return_value = second
                console.status(("syncing_orders", "authorizing_quotes", "reconciling_quotes")[second % 3])
                console.status("api_wait")
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            clock.return_value = 60
            console.status("api_wait")
            clock.return_value = 61
            console.status("waiting")
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("elapsed=60s phase=api_wait", lines[1])
        self.assertIn("phase=quoting", lines[2])

    def test_console_keeps_every_journal_event_and_exact_fill_totals(self):
        from core.services.market_maker_v2.domain import DiagnosticValue
        d = Decimal
        fill = FillEvent("f1", "o1", "BTC", Side.BUY, d("0.00017"), d("79594.1"),
                         d("0.00162371964"), LiquidityRole.MAKER, 1.0)
        events = [AccountSnapshot("BTC", 0.0, d("0"), d("299"), d("0.00012"),
                    d("0.00035"), 0, True),
                  InventoryDecision(StrategyState.QUOTING),
                  InventoryDecision(StrategyState.QUOTING),
                  MarkEvent("BTC", 1.0, d("79594.1"), True),
                  FillAccounting(fill, d("0"), None, None, None),
                  BoundedExitReport("exit-1", "BTC", 2.0, ExitStatus.BLOCKED, 0),
                  FailureDiagnostic("BTC", "bounded_exit", "ValueError", values=(
                      DiagnosticValue("cancel_stage_send", d(1)),
                      DiagnosticValue("cancel_error_timeout", d(1)),
                      DiagnosticValue("cancel_error_private_token", d(1))))]
        stderr = io.StringIO()
        with (TemporaryDirectory() as folder, redirect_stderr(stderr),
              patch.object(cli.time, "monotonic", return_value=50)):
            quiet, verbose = Path(folder) / "quiet.jsonl", Path(folder) / "verbose.jsonl"
            with JsonlTelemetrySink(quiet) as plain, JsonlTelemetrySink(verbose) as sink:
                console = cli._ConsoleProgress(sink)
                for event in events:
                    plain.emit(event)
                    console.emit(event)
                console.status("waiting")
            self.assertEqual(quiet.read_bytes(), verbose.read_bytes())
        self.assertEqual(console.fills, 1)
        self.assertEqual(console.turnover["maker"], d("13.530997"))
        self.assertEqual(console.fees, fill.fee)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("fill=maker/buy size=0.00017 price=79594.1 fee=0.00162371964", lines[1])
        self.assertIn("exit=blocked exit_id=exit-1 attempts=0 account=unconfirmed", lines[2])
        self.assertIn("error=ValueError stage=bounded_exit", lines[3])
        self.assertIn("stage_send=1 error_timeout=1", lines[3])
        self.assertNotIn("private_token", stderr.getvalue())
        self.assertIn("account_age=50s", lines[4])
        self.assertNotIn("all_in_net_pnl", stderr.getvalue())

    def test_closed_console_does_not_drop_journal_or_interrupt_cleanup(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "session.jsonl"
            with JsonlTelemetrySink(path) as sink, patch("builtins.print", side_effect=BrokenPipeError):
                console = cli._ConsoleProgress(sink)
                console.status("bounded_exit")
                console.emit(BoundedExitReport("exit-1", "BTC", 2.0, ExitStatus.BLOCKED, 0))
            self.assertFalse(console.enabled)
            self.assertEqual(json.loads(path.read_text())["event"], "bounded_exit")

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
        for reason in ("risk_capacity_exhausted", "api_backpressure_repeated"):
            result = SimpleNamespace(**vars(self.result), stop_reason=reason)
            summary = cli._summary(self.config, result)
            self.assertEqual(summary["stop_reason"], reason)
        result.stop_reason = "private-sentinel"
        with self.assertRaisesRegex(ValueError, "invalid session stop reason"):
            cli._summary(self.config, result)


if __name__ == "__main__":
    unittest.main()
