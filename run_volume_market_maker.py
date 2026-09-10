"""Bounded V2 session entry point. Dry-run is the example/default; no V1 runner."""

import argparse
import asyncio
from contextlib import contextmanager
from decimal import Decimal
import json
import logging
from pathlib import Path
import re
import signal
import sys
import time

from core.services.market_maker_v2 import orchestrator
from core.services.market_maker_v2.config import load_config, require_authorization
from core.services.market_maker_v2.domain import (
    AccountSnapshot, BoundedExitReport, ExecutionResult, FailureDiagnostic,
    FillAccounting, InventoryDecision, SessionReport,
)
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink
from lighter_preflight import build_adapter, load_settings, ROBINHOOD_NETWORKS


ROOT = Path(__file__).resolve().parent


def parse_cli(argv=None):
    parser = argparse.ArgumentParser(description="Lighter V2 bounded volume session (dry-run default)")
    parser.add_argument("--config", type=Path,
                        default=ROOT / "config/market_maker_v2/lighter_btc_volume.example.yaml")
    parser.add_argument("--exchange-config", type=Path,
                        default=ROOT / "config/exchanges/lighter_config.yaml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True, help="new, exclusive JSONL output path")
    parser.add_argument("--authorize-bounded-flatten", action="store_true",
                        help="authorize this live run's bounded reduce-only exit")
    parser.add_argument("--allow-delayed-dry-book", action="store_true",
                        help="dry testing only: allow book source age -100..10000ms; records the relaxed profile")
    parser.add_argument("--progress", action="store_true",
                        help="show important events and a 60-second heartbeat; no extra API reads")
    return parser.parse_args(argv)


def _validate_identity(settings):
    network, testnet = settings.get("network"), settings.get("testnet")
    if (network not in ROBINHOOD_NETWORKS or type(testnet) is not bool
            or testnet != network.endswith("_testnet")):
        raise ValueError("known USDG network and matching testnet flag required")
    address = settings.get("expected_l1_address")
    if (type(address) is not str
            or re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None):
        raise ValueError("expected L1 address required")
    if type(settings.get("account_index")) is not int or settings["account_index"] < 0:
        raise ValueError("nonnegative account index required")


@contextmanager
def _stop_signals(event):
    """Windows and POSIX: request cleanup, never cancel the session task."""
    loop = asyncio.get_running_loop()
    installed = {}
    def request_stop(_signum, _frame):
        loop.call_soon_threadsafe(event.set)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            installed[sig] = signal.signal(sig, request_stop)
        yield
    finally:
        for sig, previous in installed.items():
            signal.signal(sig, previous)


class _ConsoleProgress:
    """Compact operator view; the complete typed journal remains authoritative."""

    def __init__(self, sink):
        self.sink = sink
        self.started = time.monotonic()
        self.last_status = self.started
        self.phase = None
        self.inventory_state = None
        self.account = None
        self.account_at = None
        self.turnover = {"maker": Decimal("0"), "taker": Decimal("0")}
        self.fees = Decimal("0")
        self.fills = 0
        self.enabled = True

    def _write(self, text):
        if self.enabled:
            try:
                print(f"MM V2: elapsed={time.monotonic() - self.started:.0f}s {text}",
                      file=sys.stderr, flush=True)
            except (OSError, ValueError):
                self.enabled = False  # A closed console must not interrupt bounded cleanup.

    def _account(self, account):
        if (type(account) is AccountSnapshot
                and (self.account_at is None or account.observed_monotonic >= self.account_at)):
            self.account, self.account_at = account, account.observed_monotonic

    def emit(self, event):
        self.sink.emit(event)  # Never sample, deduplicate, or drop financial evidence.
        if type(event) is AccountSnapshot:
            self._account(event)
        elif type(event) is ExecutionResult:
            self._account(event.account_snapshot)
        elif type(event) is FillAccounting:
            fill = event.fill
            self.fills += 1
            self.turnover[fill.liquidity.value] += fill.size * fill.price
            self.fees += fill.fee
            self._write(f"fill={fill.liquidity.value}/{fill.side.value} "
                        f"size={fill.size} price={fill.price} fee={fill.fee}")
        elif type(event) is InventoryDecision:
            if event.state != self.inventory_state:
                self.inventory_state = event.state
                self._write(f"inventory={event.state.value}")
        elif type(event) is BoundedExitReport:
            if event.final_result is not None:
                self._account(event.final_result.account_snapshot)
            account = event.final_result.account_snapshot if event.final_result else None
            proof = (f"position={account.position} orders={account.open_order_count} "
                     f"authenticated={account.authenticated}" if account else "account=unconfirmed")
            self._write(f"exit={event.status.value} exit_id={event.flatten_id} attempts={event.attempts} {proof}")
        elif type(event) is FailureDiagnostic:
            self._write(f"error={event.error_type} stage={event.stage}; details in JSONL")

    def status(self, phase):
        # These are steps of one quote cycle, not operator-visible state changes.
        if phase in {"running", "syncing_orders", "authorizing_quotes",
                     "reconciling_quotes", "waiting"}:
            phase = "quoting"
        now = time.monotonic()
        if phase == self.phase and now - self.last_status < 60:
            return
        self.phase, self.last_status = phase, now
        account = (f" account_position={self.account.position} "
                   f"orders={self.account.open_order_count} "
                   f"account_age={now - self.account_at:.0f}s" if self.account else "")
        self._write(f"phase={phase} fills={self.fills} "
                    f"maker_volume={self.turnover['maker']:.2f} "
                    f"taker_volume={self.turnover['taker']:.2f} fees={self.fees:.6f}{account}")


async def _show_progress(session, console):
    while True:
        console._account(getattr(session, "final_account", None))
        console.status(session.phase)
        await asyncio.sleep(1)


async def run_session(config, settings, *, output, authorized=False, stop_event=None,
                      allow_delayed_dry_book=False, progress=False):
    """No settings contents are recorded; the coordinator owns connection cleanup."""
    require_authorization(config, authorized, allow_delayed_dry_book=allow_delayed_dry_book)
    _validate_identity(settings)
    event = stop_event if stop_event is not None else asyncio.Event()
    previous_logging = logging.root.manager.disable
    # Shared SDK logs can contain raw responses; typed V2 JSONL is the diagnostic path.
    logging.disable(logging.CRITICAL)
    try:
        with (JsonlTelemetrySink(output) as sink, _stop_signals(event),
              Path(str(output) + ".budget.json").open("x", encoding="utf-8") as budget_output):
            adapter = build_adapter(settings)
            console = _ConsoleProgress(sink) if progress else None
            session = orchestrator.VolumeSession(
                config, adapter, account_index=settings["account_index"],
                expected_l1_address=settings["expected_l1_address"],
                authorize_bounded_flatten=authorized, telemetry=console or sink,
                allow_delayed_dry_book=allow_delayed_dry_book,
            )
            progress_task = asyncio.create_task(_show_progress(session, console)) if progress else None
            try:
                return await session.run(event)
            finally:
                if progress_task is not None:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                budget = session.api_budget.snapshot()
                diagnostics = getattr(session.market.stream, "source_time_diagnostics", lambda: None)
                budget["source_time"] = {
                    "profile": "delayed_dry" if allow_delayed_dry_book else "strict",
                    "observations": diagnostics(),
                }
                json.dump(budget, budget_output, allow_nan=False, sort_keys=True)
    finally:
        logging.disable(previous_logging)


def _summary(config, result):
    if (type(result.dry_run) is not bool or result.dry_run != config.dry_run
            or type(result.completed) is not bool):
        raise ValueError("invalid session result")
    report = result.report
    delayed = getattr(result, "delayed_dry_book", False)
    if type(delayed) is not bool or delayed and not config.dry_run:
        raise ValueError("invalid delayed dry result")
    economics = (not config.dry_run and result.completed and result.failure is None
                 and type(report) is SessionReport and report.complete)
    summary = {"mode": "dry_run" if config.dry_run else "live",
               "completed": result.completed, "failed": result.failure is not None,
               "economics_evaluated": economics,
               "source_time_profile": "delayed_dry" if delayed else "strict"}
    stop_reason = getattr(result, "stop_reason", None)
    if stop_reason is not None:
        if stop_reason not in {"risk_capacity_exhausted", "api_backpressure_repeated"}:
            raise ValueError("invalid session stop reason")
        summary["stop_reason"] = stop_reason
    account = result.final_account
    cleanup = getattr(result, "cleanup_account", None)
    if type(cleanup) is AccountSnapshot:
        summary.update(cleanup_authenticated=cleanup.authenticated,
                       cleanup_position=str(cleanup.position), cleanup_open_orders=cleanup.open_order_count)
    if type(account) is AccountSnapshot:
        summary.update(final_position=str(account.position),
                       final_open_orders=account.open_order_count,
                       final_authenticated=account.authenticated)
    if economics:
        summary.update(all_in_net_pnl=str(report.all_in_net_pnl),
                       all_in_net_cost_bps=(str(report.all_in_net_cost_bps)
                                            if report.all_in_net_cost_bps is not None else None),
                       maker_turnover=str(report.maker_turnover_total),
                       maker_fee=str(report.maker_fee), taker_fee=str(report.taker_fee),
                       forced_flatten_loss=str(report.forced_flatten_loss))
    return summary


def main(argv=None):
    args = parse_cli(argv)
    try:
        config = load_config(args.config)
        # This must precede even secret-bearing settings reads, not just connect.
        require_authorization(config, args.authorize_bounded_flatten,
                              allow_delayed_dry_book=args.allow_delayed_dry_book)
        settings = load_settings(args.exchange_config, env_path=args.env_file)
        result = asyncio.run(run_session(config, settings, output=args.output,
                                        authorized=args.authorize_bounded_flatten,
                                        allow_delayed_dry_book=args.allow_delayed_dry_book,
                                        progress=args.progress))
        print(json.dumps(_summary(config, result), allow_nan=False, sort_keys=True))
        return 0 if result.completed and result.failure is None else 1
    except KeyboardInterrupt:
        print("V2 session interrupted; authenticated flat is not confirmed.", file=sys.stderr)
        return 130
    except Exception:
        print("V2 session failed; inspect sanitized session output. Flat is not confirmed.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
