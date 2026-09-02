"""Standalone Lighter market-maker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from core.logging.logger import BaseLogger, LogConfig
from core.services.market_maker.config import (
    MarketMakerConfig,
    load_market_maker_config,
    semantic_config_sha256,
)
from core.services.market_maker.coordinator import MarketMakerCoordinator
from core.services.market_maker.models import RuntimeState
from lighter_preflight import ROBINHOOD_NETWORKS, build_adapter, load_settings


DEFAULT_LIGHTER_CONFIG = Path("config/exchanges/lighter_config.yaml")
WALLET_PROFILES_DIR = Path(".env.wallets")
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_CONFIG_SHA = re.compile(r"[0-9a-f]{64}")
_ACCOUNT_IDENTITY_SHA = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_SCHEMA = "market_maker_calibration_evidence_v1"
_EVIDENCE_FINAL_EVENTS = {
    "market_maker_final_account_audit",
    "market_maker_final_dry_run",
}
_SENSITIVE_EVIDENCE_KEY_PARTS = (
    "private_key",
    "secret",
    "token",
    "credential",
)
_REQUIRED_PROFILE_KEYS = (
    "LIGHTER_API_KEY_PRIVATE_KEY",
    "LIGHTER_ACCOUNT_INDEX",
    "LIGHTER_API_KEY_INDEX",
    "LIGHTER_NETWORK",
)


class WalletProfileError(RuntimeError):
    """A named Lighter credential profile is missing or invalid."""


class EvidenceError(RuntimeError):
    """Calibration evidence could not be produced as an authority input."""


class _PeriodicStatusConsoleFilter(logging.Filter):
    """Keep full status evidence in the file without flooding the terminal."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Market maker status:")


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


def _evidence_account_identity(
    settings: Mapping[str, Any],
    exchange: str,
) -> tuple[str, str]:
    """Return the public network and a stable, secret-free account fingerprint."""
    exchange_name = exchange.strip().lower() if isinstance(exchange, str) else ""
    if exchange_name != "lighter":
        raise EvidenceError("calibration evidence requires the Lighter exchange")
    network = settings.get("network")
    if not isinstance(network, str) or network.strip().lower() not in ROBINHOOD_NETWORKS:
        raise EvidenceError(
            "calibration evidence requires a trusted Lighter network"
        )
    network = network.strip().lower()
    account_index = settings.get("account_index")
    if type(account_index) is not int or account_index < 0:
        raise EvidenceError(
            "calibration evidence requires a non-negative account index"
        )
    material = {
        "account_index": account_index,
        "exchange": exchange_name,
        "network": network,
        "schema": "lighter_account_v1",
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return network, digest


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
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="write one immutable calibration evidence JSON artifact",
    )
    parser.add_argument("--campaign-id")
    parser.add_argument("--candidate-id")
    return parser


def parse_cli(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
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
    evidence_values = (
        args.evidence_output,
        args.campaign_id,
        args.candidate_id,
    )
    if any(value is not None for value in evidence_values) and not all(
        value is not None for value in evidence_values
    ):
        parser.error(
            "--evidence-output, --campaign-id, and --candidate-id are required together"
        )
    for label, value in (
        ("campaign ID", args.campaign_id),
        ("candidate ID", args.candidate_id),
    ):
        if value is not None and _PROFILE_NAME.fullmatch(value) is None:
            parser.error(
                f"{label} must start with a letter or number and contain only "
                "letters, numbers, dots, underscores, or hyphens"
            )
    return args


def _logger(debug: bool) -> BaseLogger:
    level = "DEBUG" if debug else "INFO"
    logger = BaseLogger(
        "market_maker",
        LogConfig(
            log_dir="logs",
            level=level,
            console_level=level,
            file_level="DEBUG",
        ),
    )
    for handler in logger.logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.addFilter(_PeriodicStatusConsoleFilter())
    return logger


def _status_logger(
    logger: BaseLogger, settings: dict[str, Any]
) -> Callable[[dict[str, Any]], None]:
    caches: dict[str, dict[tuple[Any, ...], str]] = {}

    def log_status(status: dict[str, Any]) -> None:
        incremental = _incremental_status_snapshot(status, caches)
        logger.info(
            f"Market maker status: {_redact(str(incremental), settings)}"
        )

    return log_status


def _incremental_status_snapshot(
    status: dict[str, Any],
    caches: dict[str, dict[tuple[Any, ...], str]],
) -> dict[str, Any]:
    """Keep each periodic record self-contained without repeating history."""
    snapshot = dict(status)
    fields = (
        (
            "controller_decision_history",
            ("event_sequence_run_id", "event_sequence", "decision_id", "event"),
            (),
        ),
        (
            "quote_contexts",
            (
                "event_sequence_run_id",
                "placement_event_sequence",
                "order_id",
            ),
            (),
        ),
        (
            "fill_markouts",
            (
                "event_sequence_run_id",
                "fill_observation_event_sequence",
                "order_id",
                "started_monotonic",
            ),
            ("age_seconds",),
        ),
    )
    for field, key_fields, volatile_fields in fields:
        records = status.get(field)
        if not isinstance(records, list):
            continue
        snapshot[f"{field}_retained"] = len(records)
        snapshot[field] = _changed_status_records(
            records,
            caches.setdefault(field, {}),
            key_fields=key_fields,
            volatile_fields=volatile_fields,
        )
    return snapshot


def _changed_status_records(
    records: list[Any],
    cache: dict[tuple[Any, ...], str],
    *,
    key_fields: tuple[str, ...],
    volatile_fields: tuple[str, ...],
) -> list[Any]:
    current: dict[tuple[Any, ...], str] = {}
    changed: list[Any] = []
    for index, record in enumerate(records):
        if isinstance(record, dict):
            identity = tuple(record.get(field) for field in key_fields)
            if not any(value not in (None, "") for value in identity):
                identity = ("index", index)
            comparable = {
                key: value
                for key, value in record.items()
                if key not in volatile_fields
            }
        else:
            identity = ("index", index)
            comparable = record
        signature = repr(comparable)
        current[identity] = signature
        if cache.get(identity) != signature:
            changed.append(record)
    cache.clear()
    cache.update(current)
    return changed


def _repository_root() -> Path:
    return Path(__file__).resolve().parent


def _git_command(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise EvidenceError("unable to verify repository state") from exc


def _verified_clean_commit(repo_root: Path) -> str:
    revision = _git_command(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    commit_sha = revision.stdout.strip()
    if revision.returncode != 0 or _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise EvidenceError("unable to verify repository commit")
    status = _git_command(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
    )
    if status.returncode != 0:
        raise EvidenceError("unable to verify repository cleanliness")
    if status.stdout:
        raise EvidenceError("calibration evidence requires a clean worktree")
    return commit_sha


def _resolve_evidence_output(path: Path, repo_root: Path) -> Path:
    if str(path).startswith("\\\\"):
        raise EvidenceError("evidence output must be a local path")
    evidence_root = (repo_root / "logs" / "market_maker_evidence").resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    candidate = (path if path.is_absolute() else repo_root / path).resolve()
    if not candidate.is_relative_to(evidence_root):
        raise EvidenceError(
            "evidence output must be inside logs/market_maker_evidence"
        )
    if candidate.suffix.lower() != ".json":
        raise EvidenceError("evidence output must use a .json suffix")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if not candidate.parent.resolve().is_relative_to(evidence_root):
        raise EvidenceError("evidence output parent escapes the evidence directory")
    if candidate.exists():
        raise EvidenceError("evidence output already exists")
    relative = candidate.relative_to(repo_root).as_posix()
    ignored = _git_command(repo_root, "check-ignore", "-q", "--", relative)
    if ignored.returncode != 0:
        raise EvidenceError("evidence output must be ignored by Git")
    return candidate


def _strict_json_value(
    value: Any,
    *,
    depth: int = 0,
    forbidden_values: tuple[str, ...] = (),
) -> Any:
    if depth > 64:
        raise EvidenceError("evidence status nesting is too deep")
    if type(value) is str:
        if any(secret and secret in value for secret in forbidden_values):
            raise EvidenceError("evidence contains a protected credential value")
        return value
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise EvidenceError("evidence contains a non-finite decimal")
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise EvidenceError("evidence contains a non-finite float")
        return value
    if isinstance(value, Enum):
        return _strict_json_value(
            value.value,
            depth=depth + 1,
            forbidden_values=forbidden_values,
        )
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EvidenceError("evidence contains a timezone-naive datetime")
        return _utc_iso(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            if type(key) is not str:
                raise EvidenceError("evidence object keys must be strings")
            lowered = key.lower()
            if any(part in lowered for part in _SENSITIVE_EVIDENCE_KEY_PARTS):
                raise EvidenceError("evidence contains a forbidden sensitive field")
            result[key] = _strict_json_value(
                nested,
                depth=depth + 1,
                forbidden_values=forbidden_values,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _strict_json_value(
                item,
                depth=depth + 1,
                forbidden_values=forbidden_values,
            )
            for item in value
        ]
    raise EvidenceError(
        f"unsupported evidence status type: {type(value).__name__}"
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _flat_boundary_is_complete(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("authenticated") is not True:
        return False
    try:
        position = Decimal(str(value.get("position")))
    except Exception:
        return False
    return (
        position.is_finite()
        and position == 0
        and type(value.get("open_orders")) is int
        and value["open_orders"] == 0
    )


class CalibrationEvidenceWriter:
    """Buffer compact status records and publish one immutable JSON artifact."""

    def __init__(
        self,
        *,
        output_path: Path,
        campaign_id: str,
        candidate_id: str,
        config: MarketMakerConfig,
        commit_sha: str,
        config_sha256: str,
        network: str,
        account_identity_sha256: str,
        repo_root: Path,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if _PROFILE_NAME.fullmatch(campaign_id) is None:
            raise EvidenceError("invalid evidence campaign ID")
        if _PROFILE_NAME.fullmatch(candidate_id) is None:
            raise EvidenceError("invalid evidence candidate ID")
        if _COMMIT_SHA.fullmatch(commit_sha) is None:
            raise EvidenceError("invalid evidence commit SHA")
        if _CONFIG_SHA.fullmatch(config_sha256) is None:
            raise EvidenceError("invalid evidence config SHA")
        if network not in ROBINHOOD_NETWORKS:
            raise EvidenceError("invalid evidence network")
        if _ACCOUNT_IDENTITY_SHA.fullmatch(account_identity_sha256) is None:
            raise EvidenceError("invalid evidence account identity SHA")
        if semantic_config_sha256(config) != config_sha256:
            raise EvidenceError("evidence config SHA does not match effective config")
        self.output_path = output_path
        self.repo_root = repo_root
        self.config = config
        self._utcnow = utcnow
        self._started_at = utcnow()
        if self._started_at.tzinfo is None:
            raise EvidenceError("evidence clock must be timezone-aware")
        self._identity = {
            "evidence_schema": _EVIDENCE_SCHEMA,
            "campaign_id": campaign_id,
            "candidate_id": candidate_id,
            "commit_sha": commit_sha,
            "semantic_config_sha256": config_sha256,
            "config_sha256": config_sha256,
            "network": network,
            "account_identity_sha256": account_identity_sha256,
            "controller_profile_id": config.toxicity_profile_id,
            "symbol": config.symbol,
            "maker_fee_rate": str(config.maker_fee_rate),
            "taker_fee_rate": str(config.taker_fee_rate),
        }
        self._records: list[dict[str, Any]] = []
        self._caches: dict[str, dict[tuple[Any, ...], str]] = {}
        self._forbidden_values: tuple[str, ...] = ()
        self._invalid_reasons: list[str] = []
        self._finalized = False

    def protect_sensitive_value(self, value: Any) -> None:
        if isinstance(value, str) and value:
            self._forbidden_values = (*self._forbidden_values, value)

    def invalidate(self, reason: str) -> None:
        if self._finalized:
            raise EvidenceError("evidence writer is already finalized")
        if _PROFILE_NAME.fullmatch(reason) is None:
            raise EvidenceError("invalid evidence failure reason")
        if reason not in self._invalid_reasons:
            self._invalid_reasons.append(reason)

    def record(self, status: dict[str, Any]) -> None:
        if self._finalized:
            raise EvidenceError("evidence writer is already finalized")
        if status.get("event") == "market_maker_authenticated_preflight":
            try:
                preflight_uptime = Decimal(str(status.get("uptime_seconds")))
            except Exception:
                preflight_uptime = None
            if (
                preflight_uptime is not None
                and preflight_uptime.is_finite()
                and preflight_uptime <= 0
            ):
                validated = _strict_json_value(
                    status,
                    forbidden_values=self._forbidden_values,
                )
                if not isinstance(validated, dict):
                    raise EvidenceError("evidence status must be an object")
                return
        incremental = _incremental_status_snapshot(status, self._caches)
        converted = _strict_json_value(
            incremental,
            forbidden_values=self._forbidden_values,
        )
        if not isinstance(converted, dict):
            raise EvidenceError("evidence status must be an object")
        self._records.append(converted)

    def finalize(self) -> Path:
        if self._finalized:
            raise EvidenceError("evidence writer is already finalized")
        self._finalized = True
        ended_at = self._utcnow()
        if ended_at.tzinfo is None:
            raise EvidenceError("evidence clock must be timezone-aware")
        if ended_at <= self._started_at:
            ended_at = self._started_at + timedelta(microseconds=1)

        issues = list(self._invalid_reasons)
        if semantic_config_sha256(self.config) != self._identity["config_sha256"]:
            issues.append("config_changed_during_run")
        try:
            final_commit = _verified_clean_commit(self.repo_root)
        except EvidenceError:
            final_commit = None
            issues.append("git_state_untrusted_at_finalization")
        if final_commit != self._identity["commit_sha"]:
            issues.append("git_state_changed_during_run")

        records = list(self._records)
        if not records:
            records.append({"event": "market_maker_evidence_no_status"})
            issues.append("missing_status_records")
        run_ids = {
            record.get("event_sequence_run_id")
            for record in records
            if isinstance(record.get("event_sequence_run_id"), str)
            and record["event_sequence_run_id"]
        }
        if len(run_ids) != 1 or any(
            record.get("event_sequence_run_id") not in run_ids
            for record in records
        ):
            issues.append("missing_or_mixed_event_sequence_run_id")
        final = records[-1]
        if final.get("event") not in _EVIDENCE_FINAL_EVENTS:
            issues.append("missing_terminal_status_event")
        expected_final_event = (
            "market_maker_final_dry_run"
            if self.config.dry_run
            else "market_maker_final_account_audit"
        )
        if final.get("event") != expected_final_event:
            issues.append("unexpected_terminal_status_event_for_mode")

        def rank(record: dict[str, Any], index: int) -> tuple[Decimal, int, int]:
            try:
                uptime = Decimal(str(record.get("uptime_seconds")))
                if not uptime.is_finite():
                    uptime = Decimal("-1")
                elif uptime == 0:
                    uptime = Decimal("-1")
            except Exception:
                uptime = Decimal("-1")
            cycles = record.get("cycles")
            return uptime, cycles if type(cycles) is int else -1, index

        selected_index = max(
            range(len(records)),
            key=lambda index: rank(records[index], index),
        )
        if selected_index != len(records) - 1:
            issues.append("terminal_status_is_not_final_by_campaign_order")
        if not _flat_boundary_is_complete(final.get("preflight")):
            issues.append("incomplete_authenticated_preflight")
        if not _flat_boundary_is_complete(final.get("postflight")):
            issues.append("incomplete_authenticated_postflight")
        if (
            type(final.get("authenticated_open_orders")) is not int
            or final["authenticated_open_orders"] != 0
        ):
            issues.append("missing_final_authenticated_open_orders")
        try:
            signed_position = Decimal(str(final.get("signed_position")))
        except Exception:
            signed_position = None
        if signed_position is None or not signed_position.is_finite() or signed_position != 0:
            issues.append("missing_or_nonflat_final_signed_position")
        audit = final.get("account_audit")
        if (
            not isinstance(audit, Mapping)
            or audit.get("last_audit_authenticated") is not True
        ):
            issues.append("missing_authenticated_final_account_audit")

        identity = dict(self._identity)
        if issues:
            identity["commit_sha"] = None
        started_text = _utc_iso(self._started_at)
        ended_text = _utc_iso(ended_at)
        decorated: list[dict[str, Any]] = []
        for record in records:
            snapshot = dict(record)
            snapshot.update(identity)
            snapshot["started_at_utc"] = started_text
            snapshot["ended_at_utc"] = ended_text
            decorated.append(snapshot)
        if issues:
            decorated[-1]["evidence_integrity_errors"] = issues

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.output_path.parent,
                prefix=f".{self.output_path.name}.",
                suffix=".partial",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(
                    decorated,
                    handle,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary_path, self.output_path)
        except FileExistsError as exc:
            raise EvidenceError("evidence output already exists") from exc
        except OSError as exc:
            raise EvidenceError("evidence output could not be published") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if issues:
            raise EvidenceError(
                "evidence artifact is diagnostic-only: " + ", ".join(issues)
            )
        return self.output_path


def _status_callback_fanout(
    *callbacks: Callable[[dict[str, Any]], Any] | None,
) -> Callable[[dict[str, Any]], Awaitable[None]] | None:
    active = tuple(callback for callback in callbacks if callback is not None)
    if not active:
        return None

    async def emit(status: dict[str, Any]) -> None:
        for callback in active:
            result = callback(status)
            if inspect.isawaitable(result):
                await result

    return emit


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


def _validate_evidence_safety(config: MarketMakerConfig) -> None:
    if config.dry_run:
        return
    if not config.require_flat_start:
        raise ValueError("live evidence requires require_flat_start=true")
    if config.startup_open_order_policy != "abort":
        raise ValueError("live evidence requires startup_open_order_policy=abort")
    if not config.exclusive_symbol_control:
        raise ValueError("live evidence requires exclusive_symbol_control=true")
    if not config.cancel_on_shutdown:
        raise ValueError("live evidence requires cancel_on_shutdown=true")


async def run_market_maker(
    config: MarketMakerConfig,
    settings: dict[str, Any],
    *,
    adapter_factory: Callable[[dict[str, Any]], Any] = build_adapter,
    coordinator_factory: Callable[..., MarketMakerCoordinator] = MarketMakerCoordinator,
    stop_event: asyncio.Event | None = None,
    logger: BaseLogger | None = None,
    status_callback: Callable[[dict[str, Any]], Any] | None = None,
    authenticated_evidence: bool = False,
) -> MarketMakerCoordinator:
    """Build the SDK adapter in the active loop and own graceful shutdown."""
    _validate_live_safety(config)
    if authenticated_evidence:
        _validate_evidence_safety(config)
        if status_callback is None:
            raise ValueError("authenticated evidence requires a status callback")
    previous_record_factory = _install_log_redaction(settings)
    try:
        adapter = adapter_factory(settings)
        enable_terminal_outcomes = getattr(
            adapter, "enable_market_maker_cancellation_outcomes", None
        )
        if callable(enable_terminal_outcomes):
            enable_terminal_outcomes()
        combined_status_callback = _status_callback_fanout(
            status_callback,
            _status_logger(logger, settings) if logger is not None else None,
        )
        coordinator_options: dict[str, Any] = {
            "status_callback": combined_status_callback,
        }
        if authenticated_evidence:
            coordinator_options["authenticated_evidence"] = True
        coordinator = coordinator_factory(adapter, config, **coordinator_options)
        event = stop_event or asyncio.Event()
        installed = (
            _install_stop_signals(event, coordinator.request_stop)
            if stop_event is None
            else []
        )

        async def watch_stop_event() -> None:
            await event.wait()
            coordinator.request_stop()

        if event.is_set():
            coordinator.request_stop()
        stop_watcher = asyncio.create_task(
            watch_stop_event(), name="market-maker-stop-event"
        )
        runtime_error: BaseException | None = None
        startup_completed = False
        try:
            await coordinator.start()
            startup_completed = coordinator.running
            if logger is not None and coordinator.running:
                logger.info(
                    f"Market maker started: symbol={config.symbol} "
                    f"quote_mode={config.quote_mode} dry_run={config.dry_run}"
                )
            while coordinator.running and not event.is_set():
                try:
                    await asyncio.wait_for(event.wait(), timeout=0.25)
                except TimeoutError:
                    pass
        except BaseException as exc:
            runtime_error = exc

        cleanup_error: BaseException | None = None
        try:
            await coordinator.stop()
        except BaseException as exc:
            cleanup_error = exc
        finally:
            stop_watcher.cancel()
            await asyncio.gather(stop_watcher, return_exceptions=True)
            loop = asyncio.get_running_loop()
            for sig in installed:
                loop.remove_signal_handler(sig)

        final_status_error: BaseException | None = None
        if (
            config.dry_run
            and combined_status_callback is not None
            and startup_completed
            and (
                runtime_error is None
                or isinstance(
                    runtime_error,
                    (asyncio.CancelledError, KeyboardInterrupt),
                )
            )
            and cleanup_error is None
            and coordinator.state is RuntimeState.STOPPED
        ):
            try:
                await coordinator.emit_status_once(
                    event="market_maker_final_dry_run"
                )
                if logger is not None:
                    logger.info("market_maker_final_dry_run emitted")
            except BaseException as exc:
                final_status_error = exc
                if logger is not None:
                    logger.warning(
                        "Final dry-run status could not be emitted: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if cleanup_error is not None:
            if runtime_error is not None:
                raise cleanup_error from runtime_error
            raise cleanup_error
        if final_status_error is not None and status_callback is not None:
            if runtime_error is not None:
                raise final_status_error from runtime_error
            raise final_status_error
        if runtime_error is not None:
            raise runtime_error
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
    evidence_writer: CalibrationEvidenceWriter | None = None
    evidence_setup: tuple[Path, Path, str, str] | None = None
    exit_code = 0
    try:
        logger = logger_factory(args.debug)
        config = load_market_maker_config(args.config)
        if args.dry_run and not config.dry_run:
            config = replace(config, dry_run=True)
        _validate_live_safety(config)
        if args.evidence_output is not None:
            _validate_evidence_safety(config)
            repo_root = _repository_root()
            config_sha256 = semantic_config_sha256(config)
            output_path = _resolve_evidence_output(
                args.evidence_output,
                repo_root,
            )
            commit_sha = _verified_clean_commit(repo_root)
            evidence_setup = (
                output_path,
                repo_root,
                commit_sha,
                config_sha256,
            )
        settings = load_lighter_settings(args.wallet_name)
        if evidence_setup is not None:
            output_path, repo_root, commit_sha, config_sha256 = evidence_setup
            network, account_identity_sha256 = _evidence_account_identity(
                settings,
                config.exchange,
            )
            evidence_writer = CalibrationEvidenceWriter(
                output_path=output_path,
                campaign_id=args.campaign_id,
                candidate_id=args.candidate_id,
                config=config,
                commit_sha=commit_sha,
                config_sha256=config_sha256,
                network=network,
                account_identity_sha256=account_identity_sha256,
                repo_root=repo_root,
            )
        if evidence_writer is not None:
            evidence_writer.protect_sensitive_value(
                settings.get("api_key_private_key")
            )
        run(
            runtime(
                config,
                settings,
                adapter_factory=adapter_factory,
                logger=logger,
                status_callback=(
                    evidence_writer.record
                    if evidence_writer is not None
                    else None
                ),
                authenticated_evidence=evidence_writer is not None,
            )
        )
    except KeyboardInterrupt:
        if logger is not None:
            logger.info("Market maker stopped after operator interrupt")
    except Exception as exc:
        if evidence_writer is not None:
            evidence_writer.invalidate("runner_failed")
        message = _redact(str(exc), settings)
        if logger is not None:
            logger.error(f"Market maker failed: {message}")
        print(f"FAIL: {message}", file=sys.stderr)
        exit_code = 1
    finally:
        if evidence_writer is not None:
            try:
                evidence_path = evidence_writer.finalize()
                if logger is not None:
                    logger.info(f"Calibration evidence written: {evidence_path}")
            except Exception as exc:
                message = _redact(str(exc), settings)
                if logger is not None:
                    logger.error(f"Calibration evidence failed: {message}")
                print(f"FAIL: {message}", file=sys.stderr)
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
