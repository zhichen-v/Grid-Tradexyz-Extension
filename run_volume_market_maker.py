"""Bounded V2 session entry point. Dry-run is the example/default; no V1 runner."""

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import re
import signal
import sys

from core.services.market_maker_v2 import orchestrator
from core.services.market_maker_v2.config import load_config, require_authorization
from core.services.market_maker_v2.domain import AccountSnapshot, SessionReport
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


async def run_session(config, settings, *, output, authorized=False, stop_event=None):
    """No settings contents are recorded; the coordinator owns connection cleanup."""
    require_authorization(config, authorized)
    _validate_identity(settings)
    event = stop_event if stop_event is not None else asyncio.Event()
    previous_logging = logging.root.manager.disable
    # Shared SDK logs can contain raw responses; typed V2 JSONL is the diagnostic path.
    logging.disable(logging.CRITICAL)
    try:
        with JsonlTelemetrySink(output) as sink, _stop_signals(event):
            adapter = build_adapter(settings)
            session = orchestrator.VolumeSession(
                config, adapter, account_index=settings["account_index"],
                expected_l1_address=settings["expected_l1_address"],
                authorize_bounded_flatten=authorized, telemetry=sink,
            )
            return await session.run(event)
    finally:
        logging.disable(previous_logging)


def _summary(config, result):
    if (type(result.dry_run) is not bool or result.dry_run != config.dry_run
            or type(result.completed) is not bool):
        raise ValueError("invalid session result")
    report = result.report
    economics = (not config.dry_run and type(report) is SessionReport and report.complete)
    summary = {"mode": "dry_run" if config.dry_run else "live",
               "completed": result.completed, "failed": result.failure is not None,
               "economics_evaluated": economics}
    account = result.final_account
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
        require_authorization(config, args.authorize_bounded_flatten)
        settings = load_settings(args.exchange_config, env_path=args.env_file)
        result = asyncio.run(run_session(config, settings, output=args.output,
                                        authorized=args.authorize_bounded_flatten))
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
