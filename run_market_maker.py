"""Standalone Lighter market-maker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import signal
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from core.logging.logger import BaseLogger, LogConfig
from core.services.market_maker.config import (
    MarketMakerConfig,
    load_market_maker_config,
)
from core.services.market_maker.coordinator import MarketMakerCoordinator
from core.services.market_maker.models import RuntimeState
from lighter_preflight import ROBINHOOD_NETWORKS, build_adapter, load_settings


DEFAULT_LIGHTER_CONFIG = Path("config/exchanges/lighter_config.yaml")
WALLET_PROFILES_DIR = Path(".env.wallets")
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_REQUIRED_PROFILE_KEYS = (
    "LIGHTER_API_KEY_PRIVATE_KEY",
    "LIGHTER_ACCOUNT_INDEX",
    "LIGHTER_API_KEY_INDEX",
    "LIGHTER_NETWORK",
)


class WalletProfileError(RuntimeError):
    """A named Lighter credential profile is missing or invalid."""


def _validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or _PROFILE_NAME.fullmatch(name) is None:
        raise ValueError(
            "wallet profile must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return name


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _parse_non_negative_index(value: str, field: str, *, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise WalletProfileError(f"{field} must be a non-negative integer") from None
    if parsed < 0 or (maximum is not None and parsed > maximum):
        suffix = f" between 0 and {maximum}" if maximum is not None else " non-negative"
        raise WalletProfileError(f"{field} must be{suffix}")
    return parsed


def _optional_wallet_address(value: str | None) -> str | None:
    if not value:
        return None
    address = value.strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None:
        raise WalletProfileError(
            "LIGHTER_EXPECTED_L1_ADDRESS must be a 20-byte 0x EVM address"
        )
    return address.lower()


def load_wallet_profile(
    wallet_name: str,
    *,
    profiles_dir: Path = WALLET_PROFILES_DIR,
) -> dict[str, Any]:
    """Load one self-contained profile without consulting env vars or YAML."""
    name = _validate_profile_name(wallet_name)
    path = profiles_dir / f"{name}.env"
    if not path.is_file():
        raise WalletProfileError(f"wallet profile not found: {name}")
    values = _read_env_file(path)
    missing = [
        key for key in _REQUIRED_PROFILE_KEYS if not values.get(key, "").strip()
    ]
    if missing:
        raise WalletProfileError(
            f"wallet profile {name} is missing required keys: {', '.join(missing)}"
        )

    network = values["LIGHTER_NETWORK"].strip().lower()
    if network not in ROBINHOOD_NETWORKS:
        raise WalletProfileError(
            "LIGHTER_NETWORK must be robinhood or robinhood_testnet"
        )
    return {
        "network": network,
        "testnet": network.endswith("_testnet"),
        "api_key_private_key": values["LIGHTER_API_KEY_PRIVATE_KEY"].strip(),
        "account_index": _parse_non_negative_index(
            values["LIGHTER_ACCOUNT_INDEX"], "LIGHTER_ACCOUNT_INDEX"
        ),
        "api_key_index": _parse_non_negative_index(
            values["LIGHTER_API_KEY_INDEX"],
            "LIGHTER_API_KEY_INDEX",
            maximum=254,
        ),
        "expected_l1_address": _optional_wallet_address(
            values.get("LIGHTER_EXPECTED_L1_ADDRESS")
        ),
    }


def load_lighter_settings(
    wallet_name: str | None,
    *,
    profiles_dir: Path = WALLET_PROFILES_DIR,
    default_config: Path = DEFAULT_LIGHTER_CONFIG,
) -> dict[str, Any]:
    if wallet_name is not None:
        return load_wallet_profile(wallet_name, profiles_dir=profiles_dir)
    return load_settings(default_config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the standalone Lighter market maker",
        allow_abbrev=False,
    )
    parser.add_argument("config", type=Path, help="Market-maker YAML config path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry_run=true; this flag can never enable live trading",
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--wallet-name", "--walletname", dest="wallet_name")
    return parser


def parse_cli(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _parser()
    args, unknown = parser.parse_known_args(argv)
    if not unknown:
        return args
    if len(unknown) != 1:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    shortcut = unknown[0]
    if not shortcut.startswith("--") or shortcut == "--" or "=" in shortcut:
        parser.error(f"unrecognized argument: {shortcut}")
    try:
        name = _validate_profile_name(shortcut[2:])
    except ValueError as exc:
        parser.error(str(exc))
    if args.wallet_name is not None:
        parser.error("wallet profile shortcut cannot be combined with --wallet-name")
    args.wallet_name = name
    return args


def _logger(debug: bool) -> BaseLogger:
    level = "DEBUG" if debug else "INFO"
    return BaseLogger(
        "market_maker",
        LogConfig(
            log_dir="logs",
            level=level,
            console_level=level,
            file_level="DEBUG",
        ),
    )


def _status_logger(
    logger: BaseLogger, settings: dict[str, Any]
) -> Callable[[dict[str, Any]], None]:
    return lambda status: logger.info(
        f"Market maker status: {_redact(str(status), settings)}"
    )


def _install_stop_signals(
    event: asyncio.Event,
    request_stop: Callable[[], None],
) -> list[signal.Signals]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def handle_stop() -> None:
        event.set()
        request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)
    return installed


def _validate_live_safety(config: MarketMakerConfig) -> None:
    if not config.dry_run and config.account_audit_interval_seconds <= 0:
        raise ValueError(
            "live market maker requires account_audit_interval_seconds > 0"
        )


async def run_market_maker(
    config: MarketMakerConfig,
    settings: dict[str, Any],
    *,
    adapter_factory: Callable[[dict[str, Any]], Any] = build_adapter,
    coordinator_factory: Callable[..., MarketMakerCoordinator] = MarketMakerCoordinator,
    stop_event: asyncio.Event | None = None,
    logger: BaseLogger | None = None,
) -> MarketMakerCoordinator:
    """Build the SDK adapter in the active loop and own graceful shutdown."""
    _validate_live_safety(config)
    previous_record_factory = _install_log_redaction(settings)
    try:
        adapter = adapter_factory(settings)
        enable_terminal_outcomes = getattr(
            adapter, "enable_market_maker_cancellation_outcomes", None
        )
        if callable(enable_terminal_outcomes):
            enable_terminal_outcomes()
        coordinator = coordinator_factory(
            adapter,
            config,
            status_callback=(
                _status_logger(logger, settings) if logger is not None else None
            ),
        )
        event = stop_event or asyncio.Event()
        installed = (
            _install_stop_signals(event, coordinator.request_stop)
            if stop_event is None
            else []
        )
        try:
            await coordinator.start()
            if logger is not None:
                logger.info(
                    f"Market maker started: symbol={config.symbol} "
                    f"quote_mode={config.quote_mode} dry_run={config.dry_run}"
                )
            while coordinator.running and not event.is_set():
                try:
                    await asyncio.wait_for(event.wait(), timeout=0.25)
                except TimeoutError:
                    pass
        finally:
            try:
                await coordinator.stop()
            finally:
                loop = asyncio.get_running_loop()
                for sig in installed:
                    loop.remove_signal_handler(sig)
        if coordinator.state is RuntimeState.PAUSED_ERROR:
            detail = coordinator.fatal_exception
            raise RuntimeError(
                "market maker stopped after a fatal runtime error"
                + (f": {detail}" if detail is not None else "")
            )
        if logger is not None:
            logger.info("Market maker stopped cleanly")
        return coordinator
    finally:
        logging.setLogRecordFactory(previous_record_factory)


def _redact(message: str, settings: dict[str, Any]) -> str:
    secret = settings.get("api_key_private_key")
    return message.replace(secret, "<redacted>") if isinstance(secret, str) and secret else message


def _install_log_redaction(
    settings: dict[str, Any],
) -> Callable[..., logging.LogRecord]:
    """Redact the active signer key from every log record and traceback."""
    previous = logging.getLogRecordFactory()
    secret = settings.get("api_key_private_key")
    if not isinstance(secret, str) or not secret:
        return previous

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.msg = _redact(record.getMessage(), settings)
        record.args = ()
        if record.exc_info:
            record.exc_text = _redact(
                "".join(traceback.format_exception(*record.exc_info)),
                settings,
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _redact(record.exc_text, settings)
        return record

    logging.setLogRecordFactory(redacting_factory)
    return previous


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter_factory: Callable[[dict[str, Any]], Any] = build_adapter,
    run: Callable[[Awaitable[Any]], Any] = asyncio.run,
    runtime: Callable[..., Awaitable[Any]] = run_market_maker,
    logger_factory: Callable[[bool], BaseLogger] = _logger,
) -> int:
    args = parse_cli(argv)
    settings: dict[str, Any] = {}
    logger: BaseLogger | None = None
    try:
        logger = logger_factory(args.debug)
        config = load_market_maker_config(args.config)
        if args.dry_run and not config.dry_run:
            config = replace(config, dry_run=True)
        _validate_live_safety(config)
        settings = load_lighter_settings(args.wallet_name)
        run(
            runtime(
                config,
                settings,
                adapter_factory=adapter_factory,
                logger=logger,
            )
        )
    except KeyboardInterrupt:
        if logger is not None:
            logger.info("Market maker stopped after operator interrupt")
        return 0
    except Exception as exc:
        message = _redact(str(exc), settings)
        if logger is not None:
            logger.error(f"Market maker failed: {message}")
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
