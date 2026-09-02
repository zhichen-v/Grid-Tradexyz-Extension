#!/usr/bin/env python3
"""Aggregate fail-closed Market Maker calibration evidence from local files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # Supports both ``python -m`` and direct ``python scripts/...`` use.
    from scripts import analyze_market_maker_strategy as strategy_analyzer
except ModuleNotFoundError:  # pragma: no cover - exercised by the CLI subprocess test
    import analyze_market_maker_strategy as strategy_analyzer


_REQUIRED_MANIFEST_FIELDS = (
    "campaign_id",
    "candidate_id",
    "expected_commit_sha",
    "expected_config_sha256",
    "symbol",
    "controller_profile_id",
    "maker_fee_rate",
    "taker_fee_rate",
    "max_cumulative_flat_loss_usdg",
    "inputs",
)
_SECRET_KEY_PARTS = (
    "apikey",
    "credential",
    "mnemonic",
    "password",
    "privatekey",
    "secret",
    "seedphrase",
    "signer",
    "token",
    "wallet",
)
_INTENT_ROLES = {
    "base_entry": (frozenset({"entry", "risk_increasing"}), False),
    "controller_entry": (frozenset({"entry", "risk_increasing"}), False),
    "passive_exit": (frozenset({"passive_exit"}), False),
    "active_exit": (frozenset({"active_exit"}), True),
}
_PASSIVE_EXIT_STAGES = frozenset(
    {
        "strict_profit",
        "surplus_funded_passive",
        "bounded_passive_loss",
        "inventory_hold",
    }
)
_EXIT_BINDING_CONSTRAINTS = frozenset(
    {
        "normal_passive",
        "episode_cap",
        "session_surplus",
        "session_loss_cap",
        "drawdown_cap",
        "active_slippage",
        "attempt_cap",
        "data_untrusted",
    }
)
_SCORE_BINS = (
    ("0", Decimal("0"), Decimal("0"), True),
    ("(0,1)", Decimal("0"), Decimal("1"), False),
    ("[1,2)", Decimal("1"), Decimal("2"), True),
    ("[2,3)", Decimal("2"), Decimal("3"), True),
    ("[3,+inf)", Decimal("3"), None, True),
)
_REQUIRED_ZERO_COUNTERS = frozenset(
    {
        "active_unwind_ambiguous",
        "ambiguous_cancellations",
        "ambiguous_submissions",
        "http_429",
        "markout_telemetry_errors",
        "mutation_limiter_blocks",
        "reconciliation_failure",
        "unknown_orders",
        "unresolved_cancellations",
    }
)
_REQUIRED_ZERO_SCALARS = (
    "consecutive_errors",
    "controller_error_count",
    "failed_cycles",
)
_CUMULATIVE_ZERO_SCALARS = ("controller_error_count", "failed_cycles")
_MIN_MEANINGFUL_BIN_ENTRIES = 5
_MAX_DECIMAL_DIGITS = 1000
_MAX_DECIMAL_ADJUSTED_EXPONENT = 1000
_DECIMAL_WORK_PRECISION = 4 * _MAX_DECIMAL_DIGITS + 100


class CampaignValidationError(ValueError):
    """The manifest is unsafe or cannot define a campaign."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant rejected: {value}")


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CampaignValidationError(f"{field} must be a decimal string")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CampaignValidationError(f"{field} must be a finite decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive " if positive else "finite "
        raise CampaignValidationError(f"{field} must be a {qualifier}decimal")
    if (
        len(result.as_tuple().digits) > _MAX_DECIMAL_DIGITS
        or abs(result.adjusted()) > _MAX_DECIMAL_ADJUSTED_EXPONENT
    ):
        raise CampaignValidationError(f"{field} decimal magnitude is out of bounds")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if (
        not result.is_finite()
        or len(result.as_tuple().digits) > _MAX_DECIMAL_DIGITS
        or abs(result.adjusted()) > _MAX_DECIMAL_ADJUSTED_EXPONENT
    ):
        return None
    return result


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _reject_secret_keys(value: Any, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                raise CampaignValidationError(f"secret-like manifest key rejected: {path}.{key}")
            _reject_secret_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_secret_keys(nested, f"{path}[{index}]")


def _local_path_text(value: str | Path, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise CampaignValidationError(f"{field} must be a non-empty local path")
    if "://" in text or text.replace("/", "\\").startswith("\\\\"):
        raise CampaignValidationError(f"{field} must not use a network path")
    return text


def _validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    _reject_secret_keys(manifest)
    missing = [field for field in _REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise CampaignValidationError("missing manifest fields: " + ", ".join(missing))
    result = dict(manifest)
    for field in (
        "campaign_id",
        "candidate_id",
        "expected_commit_sha",
        "expected_config_sha256",
        "symbol",
        "controller_profile_id",
    ):
        value = result[field]
        if not isinstance(value, str) or not value.strip():
            raise CampaignValidationError(f"{field} must be a non-empty string")
        if "placeholder" in value.lower() or "operator_supplied" in value.lower():
            raise CampaignValidationError(f"{field} contains a placeholder")
        result[field] = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", result["expected_commit_sha"]) is None:
        raise CampaignValidationError(
            "expected_commit_sha must be 40 hex characters"
        )
    if re.fullmatch(r"[0-9a-fA-F]{64}", result["expected_config_sha256"]) is None:
        raise CampaignValidationError(
            "expected_config_sha256 must be 64 hex characters"
        )
    result["expected_commit_sha"] = result["expected_commit_sha"].lower()
    result["expected_config_sha256"] = result["expected_config_sha256"].lower()
    for field in ("maker_fee_rate", "taker_fee_rate", "max_cumulative_flat_loss_usdg"):
        value = result[field]
        if not isinstance(value, str):
            raise CampaignValidationError(f"{field} must be a decimal string")
        if "placeholder" in value.lower() or "operator_supplied" in value.lower():
            raise CampaignValidationError(f"{field} contains a placeholder")
    result["maker_fee_rate"] = _decimal(result["maker_fee_rate"], "maker_fee_rate")
    result["taker_fee_rate"] = _decimal(result["taker_fee_rate"], "taker_fee_rate")
    result["max_cumulative_flat_loss_usdg"] = _decimal(
        result["max_cumulative_flat_loss_usdg"],
        "max_cumulative_flat_loss_usdg",
        positive=True,
    )
    if result["maker_fee_rate"] < 0 or result["taker_fee_rate"] < 0:
        raise CampaignValidationError("fee rates must be non-negative")
    inputs = result["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise CampaignValidationError("inputs must be a non-empty list")
    if any(not isinstance(item, str) for item in inputs):
        raise CampaignValidationError("every input must be a non-empty local path")
    result["inputs"] = [
        _local_path_text(item, "input") for item in inputs
    ]
    return result


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(_local_path_text(path, "manifest path"))
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise CampaignValidationError(f"cannot read campaign manifest: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise CampaignValidationError("campaign manifest must be a JSON object")
    try:
        return _validate_manifest(raw)
    except RecursionError as exc:
        raise CampaignValidationError(
            "campaign manifest nesting exceeds the supported limit"
        ) from exc


def _load_evidence_once(path: Path) -> tuple[str, list[dict[str, Any]]]:
    raw = path.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"empty metrics input: {path}")
    try:
        value = json.loads(
            stripped,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSONL record at {path}:{line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"non-object JSONL record at {path}:{line_number}"
                )
            records.append(value)
        if not records:
            raise ValueError(f"no metrics snapshots found: {path}")
        return source_sha256, records
    if isinstance(value, dict):
        return source_sha256, [value]
    if isinstance(value, list) and value and all(
        isinstance(item, dict) for item in value
    ):
        return source_sha256, value
    raise ValueError(f"metrics JSON must contain only snapshot objects: {path}")


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _first(mapping: Mapping[str, Any], paths: Iterable[tuple[str, ...]]) -> Any:
    for path in paths:
        value = _nested(mapping, *path)
        if value is not None:
            return value
    return None


def _final_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(index_record: tuple[int, dict[str, Any]]) -> tuple[Decimal, int, int]:
        index, record = index_record
        uptime = _optional_decimal(record.get("uptime_seconds")) or Decimal("-1")
        cycles = record.get("cycles")
        return uptime, cycles if type(cycles) is int else -1, index

    return max(enumerate(records), key=rank)[1]


def _runtime_run_id(records: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    values: list[str] = []
    missing = False
    for record in records:
        value = record.get("event_sequence_run_id")
        if not isinstance(value, str) or not value.strip():
            missing = True
        else:
            values.append(value.strip())
    unique = sorted(set(values))
    reasons: list[str] = []
    if missing or not unique:
        reasons.append("old_or_missing_runtime_run_id")
    if len(unique) > 1:
        reasons.append("multiple_runtime_run_ids_in_input")
    return unique[0] if len(unique) == 1 and not missing else None, reasons


def _identity(final: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": final.get("campaign_id"),
        "candidate_id": final.get("candidate_id"),
        "commit_sha": final.get("commit_sha"),
        "config_sha256": final.get("semantic_config_sha256", final.get("config_sha256")),
        "controller_profile_id": final.get("controller_profile_id"),
        "symbol": final.get("symbol"),
        "maker_fee_rate": final.get("maker_fee_rate"),
        "taker_fee_rate": final.get("taker_fee_rate"),
        "started_at_utc": final.get("started_at_utc"),
        "ended_at_utc": final.get("ended_at_utc"),
    }


def _identity_reasons(
    records: Iterable[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> list[str]:
    reasons: list[str] = []
    utc_periods: set[tuple[str, str]] = set()
    expected = {
        "campaign_id": manifest["campaign_id"],
        "candidate_id": manifest["candidate_id"],
        "commit_sha": manifest["expected_commit_sha"],
        "config_sha256": manifest["expected_config_sha256"],
        "controller_profile_id": manifest["controller_profile_id"],
        "symbol": manifest["symbol"],
    }
    for index, record in enumerate(records):
        identity = _identity(record)
        for field, wanted in expected.items():
            actual = identity.get(field)
            if not isinstance(actual, str) or not actual.strip():
                reasons.append(f"snapshot_{index}_missing_{field}")
            elif (
                actual.strip().lower() if field in {"commit_sha", "config_sha256"} else actual.strip()
            ) != wanted:
                reasons.append(f"snapshot_{index}_mismatched_{field}")
        for field in ("maker_fee_rate", "taker_fee_rate"):
            raw = identity.get(field)
            actual = _optional_decimal(raw)
            if not isinstance(raw, str) or actual is None:
                reasons.append(f"snapshot_{index}_missing_{field}")
            elif actual != manifest[field]:
                reasons.append(f"snapshot_{index}_mismatched_{field}")
        start = _utc_datetime(identity.get("started_at_utc"))
        end = _utc_datetime(identity.get("ended_at_utc"))
        if start is None:
            reasons.append(f"snapshot_{index}_missing_or_invalid_started_at_utc")
        if end is None:
            reasons.append(f"snapshot_{index}_missing_or_invalid_ended_at_utc")
        if start is not None and end is not None and end <= start:
            reasons.append(f"snapshot_{index}_nonpositive_utc_period")
        if start is not None and end is not None:
            utc_periods.add((start.isoformat(), end.isoformat()))
        uptime = _optional_decimal(record.get("uptime_seconds"))
        eligible = _optional_decimal(record.get("eligible_quote_seconds"))
        if uptime is None or uptime <= 0:
            reasons.append(f"snapshot_{index}_missing_or_invalid_uptime_seconds")
        if eligible is None or eligible < 0:
            reasons.append(
                f"snapshot_{index}_missing_or_invalid_eligible_quote_seconds"
            )
        elif uptime is not None and eligible > uptime:
            reasons.append(f"snapshot_{index}_eligible_quote_seconds_exceed_uptime")
        if start is not None and end is not None and uptime is not None:
            wall_seconds = Decimal(str((end - start).total_seconds()))
            if uptime > wall_seconds + Decimal("2"):
                reasons.append(f"snapshot_{index}_uptime_exceeds_utc_period")
    if len(utc_periods) > 1:
        reasons.append("snapshot_utc_period_identity_conflict")
    return reasons


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _open_order_count(final: Mapping[str, Any]) -> int | None:
    value = final.get("authenticated_open_orders")
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, list):
        return len(value)
    return None


def _safety_reasons(final: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    audit = final.get("account_audit")
    if not isinstance(audit, Mapping) or audit.get("last_audit_authenticated") is not True:
        reasons.append("final_audit_not_authenticated")
        audit = audit if isinstance(audit, Mapping) else {}
    account_state = audit.get("state")
    economic_state = audit.get("economic_state")
    economic_reason = audit.get("economic_reason")
    total_read_failures = audit.get("total_read_failures")
    if type(total_read_failures) is not int or total_read_failures < 0:
        reasons.append("missing_or_invalid_total_read_failures")
    healthy = (
        account_state == "healthy"
        and "reason" in audit
        and audit.get("reason") is None
        and isinstance(economic_state, str)
        and economic_state in {"collecting", "fee_and_equity_gate_go"}
    )
    authenticated_economic_stop = (
        account_state == "hard_stop"
        and economic_state == "no_go"
        and isinstance(economic_reason, str)
        and bool(economic_reason.strip())
        and isinstance(audit.get("reason"), str)
        and audit["reason"] == economic_reason
    )
    if not (healthy or authenticated_economic_stop):
        reasons.append("final_account_state_not_safe_or_economic_stop")
    for phase in ("preflight", "postflight"):
        evidence = final.get(phase)
        if not isinstance(evidence, Mapping) or evidence.get("authenticated") is not True:
            reasons.append(f"{phase}_not_authenticated")
            continue
        position = _optional_decimal(evidence.get("position"))
        if position is None or position != 0:
            reasons.append(f"{phase}_position_non_flat_or_invalid")
        orders = evidence.get("open_orders")
        if type(orders) is not int or orders != 0:
            reasons.append(f"{phase}_open_orders_nonzero_or_invalid")
    for field, raw in (
        ("audited_position", audit.get("audited_position")),
        ("ledger_position", audit.get("ledger_position")),
        ("signed_position", final.get("signed_position")),
    ):
        value = _optional_decimal(raw)
        if value is None:
            reasons.append(f"missing_final_{field}")
        elif value != 0:
            reasons.append(f"final_{field}_non_flat")
    open_orders = _open_order_count(final)
    if open_orders is None:
        reasons.append("missing_final_authenticated_open_orders")
    elif open_orders != 0:
        reasons.append("final_authenticated_open_orders_nonzero")

    counters = final.get("counters")
    if not isinstance(counters, Mapping):
        reasons.append("missing_hard_safety_counter_evidence")
    else:
        missing = sorted(_REQUIRED_ZERO_COUNTERS - set(counters))
        reasons.extend(f"missing_hard_counter:{name}" for name in missing)
        for name in sorted(_REQUIRED_ZERO_COUNTERS & set(counters)):
            value = counters[name]
            if type(value) is not int or value != 0:
                reasons.append(f"hard_counter_nonzero_or_invalid:{name}")
    for name in _REQUIRED_ZERO_SCALARS:
        if name not in final:
            reasons.append(f"missing_hard_counter:{name}")
            continue
        value = final[name]
        if type(value) is not int or value != 0:
            reasons.append(f"hard_counter_nonzero_or_invalid:{name}")
    return reasons


def _historical_hard_counter_reasons(
    records: Iterable[Mapping[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    for index, record in enumerate(records):
        counters = record.get("counters")
        if not isinstance(counters, Mapping):
            reasons.append(f"snapshot_{index}_missing_hard_safety_counter_evidence")
        else:
            for name in sorted(_REQUIRED_ZERO_COUNTERS):
                value = counters.get(name)
                if type(value) is not int or value != 0:
                    reasons.append(
                        f"snapshot_{index}_hard_counter_nonzero_or_invalid:{name}"
                    )
        for name in _CUMULATIVE_ZERO_SCALARS:
            value = record.get(name)
            if type(value) is not int or value != 0:
                reasons.append(
                    f"snapshot_{index}_hard_counter_nonzero_or_invalid:{name}"
                )
    return reasons


def _account_state_transition_reasons(
    records: Iterable[Mapping[str, Any]],
) -> list[str]:
    snapshots: list[tuple[Decimal, int, Mapping[str, Any]]] = []
    for index, record in enumerate(records):
        uptime = _optional_decimal(record.get("uptime_seconds"))
        audit = record.get("account_audit")
        if uptime is not None and isinstance(audit, Mapping):
            snapshots.append((uptime, index, audit))
    stopped = False
    reasons: list[str] = []
    for _, index, audit in sorted(snapshots):
        if audit.get("last_audit_authenticated") is not True:
            continue
        hard_stopped = audit.get("state") == "hard_stop"
        economic_stopped = audit.get("economic_state") == "no_go"
        if stopped and not (hard_stopped or economic_stopped):
            reasons.append(f"snapshot_{index}_account_stop_state_regressed")
        stopped |= hard_stopped or economic_stopped
    return reasons


def _controller_history_reasons(
    records: Iterable[Mapping[str, Any]], run_id: str
) -> list[str]:
    record_list = list(records)
    reasons: list[str] = []
    versions: dict[tuple[str, int], set[str]] = {}
    scored_decision_present = False
    fill_events: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    decisions_by_sequence: dict[int, list[Mapping[str, Any]]] = {}
    for record in record_list:
        raw_events = record.get("fill_markouts")
        if isinstance(raw_events, list):
            for event in raw_events:
                if not isinstance(event, Mapping):
                    continue
                event_run_id = event.get("event_sequence_run_id")
                sequence = event.get("fill_observation_event_sequence")
                if isinstance(event_run_id, str) and type(sequence) is int:
                    fill_events.setdefault((event_run_id, sequence), []).append(
                        event
                    )
        history = record.get("controller_decision_history")
        if isinstance(history, list):
            for item in history:
                if (
                    isinstance(item, Mapping)
                    and item.get("event") != "maker_fill"
                    and item.get("event_sequence_run_id") == run_id
                    and type(item.get("event_sequence")) is int
                ):
                    decisions_by_sequence.setdefault(
                        item["event_sequence"], []
                    ).append(item)
    for record_index, record in enumerate(record_list):
        total = record.get("controller_decision_history_total")
        if type(total) is not int or total < 0:
            reasons.append(
                f"snapshot_{record_index}_invalid_controller_history_total"
            )
        history = record.get("controller_decision_history")
        if not isinstance(history, list):
            reasons.append(
                f"snapshot_{record_index}_invalid_controller_history"
            )
            continue
        for item_index, item in enumerate(history):
            prefix = f"snapshot_{record_index}_history_{item_index}"
            if not isinstance(item, Mapping):
                reasons.append(f"{prefix}_not_object")
                continue
            history_run_id = item.get("event_sequence_run_id")
            event_sequence = item.get("event_sequence")
            if history_run_id != run_id:
                reasons.append(f"{prefix}_run_id_mismatch")
            if type(event_sequence) is not int or event_sequence <= 0:
                reasons.append(f"{prefix}_invalid_event_sequence")
                continue
            key = (run_id, event_sequence)
            versions.setdefault(key, set()).add(
                json.dumps(dict(item), default=str, sort_keys=True)
            )
            if item.get("event") == "maker_fill":
                fill_order_id = item.get("fill_order_id")
                placement = item.get("placement_quote_context")
                fill_sequence = item.get("fill_observation_event_sequence")
                decision_sequence = item.get(
                    "last_controller_decision_event_sequence"
                )
                matching_fills = (
                    fill_events.get((run_id, fill_sequence), [])
                    if type(fill_sequence) is int
                    else []
                )
                matching_decisions = (
                    decisions_by_sequence.get(decision_sequence, [])
                    if type(decision_sequence) is int
                    else []
                )
                placement_sequence = (
                    placement.get("placement_event_sequence")
                    if isinstance(placement, Mapping)
                    else None
                )
                canonical_fill = next(
                    (
                        event
                        for event in matching_fills
                        if event.get("order_id") == fill_order_id
                    ),
                    None,
                )
                canonical_decision = (
                    matching_decisions[0]
                    if len(
                        {
                            json.dumps(dict(decision), default=str, sort_keys=True)
                            for decision in matching_decisions
                        }
                    )
                    == 1
                    and matching_decisions
                    else None
                )
                maker_fill_valid = (
                    isinstance(fill_order_id, str)
                    and bool(fill_order_id)
                    and isinstance(placement, Mapping)
                    and placement.get("event_sequence_run_id") == run_id
                    and placement.get("order_id") == fill_order_id
                    and type(placement_sequence) is int
                    and type(fill_sequence) is int
                    and type(decision_sequence) is int
                    and placement_sequence > 0
                    and decision_sequence > 0
                    and decision_sequence < placement_sequence < fill_sequence < event_sequence
                    and canonical_fill is not None
                    and canonical_fill.get("quote_context") == placement
                    and canonical_decision is not None
                    and item.get("decision_id")
                    == canonical_decision.get("decision_id")
                    and item.get("bid") == canonical_decision.get("bid")
                    and item.get("ask") == canonical_decision.get("ask")
                )
                if not maker_fill_valid:
                    reasons.append(f"{prefix}_invalid_maker_fill_snapshot")
                continue
            decision_id = item.get("decision_id")
            bid = item.get("bid")
            ask = item.get("ask")
            if type(decision_id) is not int or decision_id <= 0:
                reasons.append(f"{prefix}_invalid_decision_id")
            if not isinstance(bid, Mapping) or not isinstance(ask, Mapping):
                reasons.append(f"{prefix}_invalid_side_decisions")
                continue
            bid_score = _optional_decimal(bid.get("toxicity_score_ticks"))
            ask_score = _optional_decimal(ask.get("toxicity_score_ticks"))
            if item.get("error") is not None:
                reasons.append(f"{prefix}_controller_error_present")
            for side, raw, score in (
                ("bid", bid.get("toxicity_score_ticks"), bid_score),
                ("ask", ask.get("toxicity_score_ticks"), ask_score),
            ):
                if raw is not None and (score is None or score < 0):
                    reasons.append(f"{prefix}_invalid_{side}_toxicity_score")
            scores_complete = (
                bid_score is not None
                and bid_score >= 0
                and ask_score is not None
                and ask_score >= 0
            )
            explicitly_unscored = (
                item.get("ready") is False
                or item.get("entry_applicable") is False
            )
            if not scores_complete and not explicitly_unscored:
                reasons.append(f"{prefix}_missing_decision_toxicity_scores")
            scored_decision_present |= scores_complete
    reasons.extend(
        f"conflicting_controller_history_event:{event_run_id}:{sequence}"
        for (event_run_id, sequence), values in sorted(versions.items())
        if len(values) > 1
    )
    if not scored_decision_present:
        reasons.append("controller_history_has_no_complete_scored_decision")
    return reasons


def _raw_event_conflict_reasons(
    records: Iterable[Mapping[str, Any]],
) -> list[str]:
    versions: dict[tuple[str, int], dict[str, set[str]]] = {}
    immutable_fields = (
        "order_id",
        "side",
        "fill_amount",
        "fill_price",
        "observation_source",
        "started_monotonic",
        "raw_mid_at_start",
        "external_mid_at_start",
    )
    evolving_once_fields = (
        "external_mid_markout_5s_bps",
        "external_mid_markout_15s_bps",
        "attribution_signature",
        "quote_context",
    )
    for record in records:
        raw_events = record.get("fill_markouts")
        if not isinstance(raw_events, list):
            continue
        for event in raw_events:
            if not isinstance(event, Mapping):
                continue
            run_id = event.get("event_sequence_run_id")
            sequence = event.get("fill_observation_event_sequence")
            if not isinstance(run_id, str) or type(sequence) is not int:
                continue
            fields = versions.setdefault((run_id, sequence), {})
            immutable = {field: event.get(field) for field in immutable_fields}
            fields.setdefault("immutable", set()).add(
                json.dumps(immutable, default=str, sort_keys=True)
            )
            for field in evolving_once_fields:
                value = event.get(field)
                if value is None:
                    continue
                fields.setdefault(field, set()).add(
                    json.dumps(value, default=str, sort_keys=True)
                )
    return [
        f"conflicting_raw_fill_event:{run_id}:{sequence}:{field}"
        for (run_id, sequence), fields in sorted(versions.items())
        for field, values in sorted(fields.items())
        if len(values) > 1
    ]


def _evidence_reasons(
    final: Mapping[str, Any], *, merged_event_count: int
) -> list[str]:
    audit = final.get("account_audit")
    if not isinstance(audit, Mapping):
        return ["missing_account_audit"]
    reasons: list[str] = []
    required = {
        "eligible_quote_seconds": final.get("eligible_quote_seconds"),
        "maker_turnover": audit.get("maker_turnover"),
        "completed_net_ex_funding": audit.get("completed_net_ex_funding"),
        "flat_equity_change": audit.get("flat_equity_change"),
        "max_observed_drawdown": audit.get("max_observed_drawdown"),
    }
    for field, raw in required.items():
        value = _optional_decimal(raw)
        if value is None:
            reasons.append(f"missing_or_invalid_{field}")
        elif field == "eligible_quote_seconds" and value <= 0:
            reasons.append("nonpositive_eligible_quote_seconds")
        elif field in {"maker_turnover", "max_observed_drawdown"} and value < 0:
            reasons.append(f"negative_{field}")
    if not _financial_identity_valid(audit):
        reasons.append("account_financial_identity_incomplete_or_inconsistent")
    maker_fills = audit.get("unique_maker_fills")
    if type(maker_fills) is not int or maker_fills < 0:
        reasons.append("missing_or_invalid_unique_maker_fills")
    taker_fills = audit.get("unique_taker_fills")
    if type(taker_fills) is not int or taker_fills < 0:
        reasons.append("missing_or_invalid_unique_taker_fills")
    policy_missing = audit.get("policy_context_missing_count")
    if type(policy_missing) is not int or policy_missing != 0:
        reasons.append("policy_context_missing_or_invalid")
    coverage = final.get("fill_markout_coverage")
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("unit") != "observed_order_fill_delta"
    ):
        reasons.append("missing_or_invalid_fill_markout_coverage_unit")
    elif (
        type(coverage.get("observed_event_total")) is not int
        or coverage["observed_event_total"] != merged_event_count
    ):
        reasons.append("fill_markout_history_incomplete")
    return reasons


def _episode_conflict_reasons(records: Iterable[Mapping[str, Any]]) -> list[str]:
    versions: dict[tuple[str, int], str] = {}
    conflicts: set[tuple[str, int]] = set()
    for record in records:
        ledger = _nested(record, "account_audit", "completed_episode_ledger")
        if not isinstance(ledger, list):
            continue
        for episode in ledger:
            if not isinstance(episode, Mapping) or (key := _episode_key(episode)) is None:
                continue
            stable = json.dumps(dict(episode), default=str, sort_keys=True)
            if key in versions and versions[key] != stable:
                conflicts.add(key)
            else:
                versions[key] = stable
    return [f"conflicting_duplicate_episode:{session_id}:{sequence}" for session_id, sequence in sorted(conflicts)]


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * percentile
    lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def _summary(values: Iterable[Any]) -> dict[str, Any]:
    parsed = [value for item in values if (value := _optional_decimal(item)) is not None]
    if not parsed:
        return {"count": 0, "mean": None, "median": None, "p25": None, "p75": None}
    return {
        "count": len(parsed),
        "mean": sum(parsed, Decimal("0")) / Decimal(len(parsed)),
        "median": statistics.median(parsed),
        "p25": _percentile(parsed, Decimal("0.25")),
        "p75": _percentile(parsed, Decimal("0.75")),
    }


def _score_bin(value: Any) -> str | None:
    score = _optional_decimal(value)
    if score is None or score < 0:
        return None
    for label, lower, upper, inclusive_lower in _SCORE_BINS:
        lower_ok = score >= lower if inclusive_lower else score > lower
        if lower_ok and (upper is None or score < upper or (upper == 0 and score == 0)):
            return label
    return None


def _toxicity_score(value: Mapping[str, Any]) -> Any:
    return _nested(value, "quote_context", "toxicity_score_ticks")


def _markouts(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "5": value.get("external_mid_markout_5s_bps"),
        "15": value.get("external_mid_markout_15s_bps"),
    }


def _episode_key(episode: Mapping[str, Any]) -> tuple[str, int] | None:
    session_id = episode.get("session_id")
    sequence = episode.get("episode_sequence")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if type(sequence) is not int or sequence <= 0:
        return None
    return session_id, sequence


def _deduplicate_episodes(episodes: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for episode in episodes:
        key = _episode_key(episode)
        if key is None:
            continue
        candidate = dict(episode)
        current = result.get(key)
        if current is None or (
            sum(value is not None for value in candidate.values()),
            json.dumps(candidate, default=str, sort_keys=True),
        ) > (
            sum(value is not None for value in current.values()),
            json.dumps(current, default=str, sort_keys=True),
        ):
            result[key] = candidate
    return result


def _complete_campaign_episode(episode: Mapping[str, Any]) -> bool:
    if not strategy_analyzer._complete_policy_episode(episode):
        return False
    side = str(episode.get("entry_side", "")).lower()
    entry = _optional_decimal(episode.get("entry_vwap"))
    exit_price = _optional_decimal(episode.get("exit_vwap"))
    quantity = _optional_decimal(episode.get("quantity"))
    gross = _optional_decimal(episode.get("gross"))
    maker_fee = _optional_decimal(episode.get("maker_fee"))
    taker_fee = _optional_decimal(episode.get("taker_fee"))
    net = _optional_decimal(episode.get("net_ex_funding"))
    if (
        side not in {"buy", "sell"}
        or None
        in {entry, exit_price, quantity, gross, maker_fee, taker_fee, net}
        or quantity <= 0
        or maker_fee < 0
        or taker_fee < 0
    ):
        return False
    expected_gross = (
        (exit_price - entry) * quantity
        if side == "buy"
        else (entry - exit_price) * quantity
    )
    return gross == expected_gross and net == gross - maker_fee - taker_fee


def _financial_identity_valid(audit: Mapping[str, Any]) -> bool:
    baseline = _optional_decimal(audit.get("baseline_equity"))
    current = _optional_decimal(audit.get("current_equity"))
    flat_change = _optional_decimal(audit.get("flat_equity_change"))
    completed_gross = _optional_decimal(audit.get("completed_gross"))
    completed_fee = _optional_decimal(audit.get("completed_exact_fee"))
    completed_net = _optional_decimal(audit.get("completed_net_ex_funding"))
    current_drawdown = _optional_decimal(audit.get("current_drawdown"))
    max_drawdown = _optional_decimal(audit.get("max_observed_drawdown"))
    if None in {
        baseline,
        current,
        flat_change,
        completed_gross,
        completed_fee,
        completed_net,
        current_drawdown,
        max_drawdown,
    }:
        return False
    expected_drawdown = max(Decimal("0"), baseline - current)
    return (
        completed_fee >= 0
        and current_drawdown == expected_drawdown
        and max_drawdown >= current_drawdown
        and flat_change == current - baseline
        and completed_net == completed_gross - completed_fee
    )


def _episode_attribution_conserved(
    episode: Mapping[str, Any],
    trade_attributions: Iterable[Mapping[str, Any]],
) -> bool:
    sequence = episode.get("episode_sequence")
    entry_side = str(episode.get("entry_side", "")).lower()
    final_stage = episode.get("final_exit_stage")
    final_binding = episode.get("final_binding_constraint")
    quantity = _optional_decimal(episode.get("quantity"))
    if (
        type(sequence) is not int
        or entry_side not in {"buy", "sell"}
        or quantity is None
        or quantity <= 0
    ):
        return False
    attributions = [
        attribution
        for attribution in trade_attributions
        if attribution.get("episode_sequence") == sequence
    ]
    if not attributions:
        return False
    entry_quantity = sum(
        (
            abs(attribution["next_position"] - attribution["prior_position"])
            for attribution in attributions
            if attribution.get("role") in {"entry", "risk_increasing"}
        ),
        Decimal("0"),
    )
    exit_quantity = sum(
        (
            abs(attribution["next_position"] - attribution["prior_position"])
            for attribution in attributions
            if attribution.get("role") in {"passive_exit", "active_exit"}
        ),
        Decimal("0"),
    )
    expected_role = "active_exit" if final_stage == "active_ioc" else "passive_exit"
    expected_side = "sell" if entry_side == "buy" else "buy"
    matching_final_exit = any(
        attribution.get("role") == expected_role
        and attribution.get("side") == expected_side
        and attribution.get("exit_stage") == final_stage
        and attribution.get("binding_constraint") == final_binding
        and _optional_decimal(attribution.get("next_position")) == 0
        for attribution in attributions
    )
    return (
        attributions[0].get("role") == "entry"
        and attributions[0].get("side") == entry_side
        and quantity == entry_quantity == exit_quantity
        and matching_final_exit
    )


def _completed_episode_aggregate_valid(
    final: Mapping[str, Any],
    trade_attributions: Iterable[Mapping[str, Any]],
    merged_episodes: Mapping[tuple[str, int], Mapping[str, Any]],
) -> bool:
    audit = final.get("account_audit")
    if not isinstance(audit, Mapping):
        return False
    ledger = audit.get("completed_episode_ledger")
    completed_round_trips = audit.get("completed_round_trips")
    episode_flat_success = audit.get("episode_flat_success")
    if (
        not isinstance(ledger, list)
        or type(completed_round_trips) is not int
        or type(episode_flat_success) is not int
        or completed_round_trips < 0
        or episode_flat_success < 0
        or completed_round_trips != len(ledger)
        or episode_flat_success != len(ledger)
    ):
        return False
    if any(not isinstance(item, Mapping) for item in ledger):
        return False
    episodes = [dict(item) for item in ledger]
    keys = [_episode_key(item) for item in episodes]
    sequences = [item.get("episode_sequence") for item in episodes]
    expected_keys = {
        key
        for key, item in merged_episodes.items()
        if _complete_campaign_episode(item)
    }
    if (
        any(key is None for key in keys)
        or len(set(keys)) != len(keys)
        or set(keys) != expected_keys
        or len(set(sequences)) != len(sequences)
        or any(not _complete_campaign_episode(item) for item in episodes)
        or any(
            not _episode_attribution_conserved(item, trade_attributions)
            for item in episodes
        )
    ):
        return False
    gross = sum(
        (_optional_decimal(item.get("gross")) or Decimal("0") for item in episodes),
        Decimal("0"),
    )
    exact_fee = sum(
        (
            (_optional_decimal(item.get("maker_fee")) or Decimal("0"))
            + (_optional_decimal(item.get("taker_fee")) or Decimal("0"))
            for item in episodes
        ),
        Decimal("0"),
    )
    net = sum(
        (
            _optional_decimal(item.get("net_ex_funding")) or Decimal("0")
            for item in episodes
        ),
        Decimal("0"),
    )
    return (
        _optional_decimal(audit.get("completed_gross")) == gross
        and _optional_decimal(audit.get("completed_exact_fee")) == exact_fee
        and _optional_decimal(audit.get("completed_net_ex_funding")) == net
    )


def _typed_attributions(
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], bool, int, int, list[dict[str, Any]]]:
    by_trade: dict[str, dict[str, Any]] = {}
    invalid_orders: set[str] = set()
    invalid_without_order = False
    for record in records:
        raw = _nested(record, "account_audit", "authenticated_fill_attributions")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                invalid_without_order = True
                continue
            order_id = str(item.get("order_id", "") or "").strip()
            trade_id = str(item.get("trade_id", "") or "").strip()
            side = str(item.get("side", "") or "").lower()
            role = str(item.get("role", "") or "")
            intent = str(item.get("intent_kind", "") or "")
            sequence = item.get("episode_sequence")
            active_unwind = item.get("active_unwind")
            prior_position = _optional_decimal(item.get("prior_position"))
            next_position = _optional_decimal(item.get("next_position"))
            policy_decision_id = item.get("policy_decision_id")
            exit_stage = item.get("exit_stage")
            binding_constraint = item.get("binding_constraint")
            controller_decision_id = item.get("controller_decision_id")
            expected = _INTENT_ROLES.get(intent)
            exit_policy_valid = role not in {"passive_exit", "active_exit"} or (
                type(policy_decision_id) is int
                and policy_decision_id > 0
                and isinstance(binding_constraint, str)
                and binding_constraint in _EXIT_BINDING_CONSTRAINTS
                and isinstance(exit_stage, str)
                and (
                    exit_stage == "active_ioc"
                    if role == "active_exit"
                    else exit_stage in _PASSIVE_EXIT_STAGES
                )
            )
            position_transition_valid = (
                prior_position is not None
                and next_position is not None
                and prior_position * next_position >= 0
                and (
                    next_position > prior_position
                    if side == "buy"
                    else next_position < prior_position
                )
                and (
                    abs(next_position) > abs(prior_position)
                    if role in {"entry", "risk_increasing"}
                    else abs(next_position) < abs(prior_position)
                    if role in {"passive_exit", "active_exit"}
                    else False
                )
            )
            controller_binding_valid = intent != "controller_entry" or (
                type(controller_decision_id) is int
                and controller_decision_id > 0
            )
            optional_context_valid = (
                (
                    policy_decision_id is None
                    or (type(policy_decision_id) is int and policy_decision_id > 0)
                )
                and (exit_stage is None or isinstance(exit_stage, str))
                and (
                    binding_constraint is None
                    or isinstance(binding_constraint, str)
                )
                and (
                    controller_decision_id is None
                    or (
                        type(controller_decision_id) is int
                        and controller_decision_id > 0
                    )
                )
                and (
                    role in {"passive_exit", "active_exit"}
                    or (
                        policy_decision_id is None
                        and exit_stage is None
                        and binding_constraint is None
                    )
                )
                and (
                    intent == "controller_entry"
                    or controller_decision_id is None
                )
            )
            valid = (
                bool(order_id)
                and bool(trade_id)
                and side in {"buy", "sell"}
                and type(sequence) is int
                and sequence > 0
                and type(active_unwind) is bool
                and item.get("position_flip") is False
                and expected is not None
                and role in expected[0]
                and active_unwind is expected[1]
                and exit_policy_valid
                and position_transition_valid
                and controller_binding_valid
                and optional_context_valid
            )
            if not valid:
                if order_id:
                    invalid_orders.add(order_id)
                else:
                    invalid_without_order = True
                continue
            normalized = {
                "trade_id": trade_id,
                "order_id": order_id,
                "side": side,
                "role": role,
                "episode_sequence": sequence,
                "active_unwind": active_unwind,
                "intent_kind": intent,
                "policy_decision_id": policy_decision_id,
                "exit_stage": exit_stage,
                "binding_constraint": binding_constraint,
                "controller_decision_id": controller_decision_id,
                "prior_position": prior_position,
                "next_position": next_position,
            }
            previous = by_trade.get(trade_id)
            if previous is not None and previous != normalized:
                invalid_orders.update(
                    {previous["order_id"], normalized["order_id"]}
                )
            else:
                by_trade[trade_id] = normalized

    by_episode: dict[int, list[dict[str, Any]]] = {}
    for attribution in by_trade.values():
        by_episode.setdefault(attribution["episode_sequence"], []).append(
            attribution
        )
    for attributions in by_episode.values():
        entry_order_ids = {
            attribution["order_id"]
            for attribution in attributions
            if attribution["role"] == "entry"
        }
        chain_valid = (
            bool(attributions)
            and attributions[0]["role"] == "entry"
            and attributions[0]["prior_position"] == 0
            and entry_order_ids == {attributions[0]["order_id"]}
            and attributions[-1]["role"] in {"passive_exit", "active_exit"}
            and attributions[-1]["next_position"] == 0
            and all(
                attribution["next_position"] != 0
                for attribution in attributions[:-1]
            )
            and all(
                current["prior_position"] == previous["next_position"]
                for previous, current in zip(attributions, attributions[1:])
            )
        )
        if not chain_valid:
            invalid_orders.update(
                attribution["order_id"] for attribution in attributions
            )

    by_order: dict[str, dict[str, Any]] = {}
    signatures: dict[str, set[tuple[Any, ...]]] = {}
    for attribution in by_trade.values():
        signatures.setdefault(attribution["order_id"], set()).add(
            (
                attribution["side"],
                attribution["role"],
                attribution["episode_sequence"],
                attribution["active_unwind"],
                attribution["intent_kind"],
                attribution["policy_decision_id"],
                attribution["exit_stage"],
                attribution["binding_constraint"],
                attribution["controller_decision_id"],
            )
        )
    for order_id, values in signatures.items():
        if order_id in invalid_orders or len(values) != 1:
            invalid_orders.add(order_id)
            continue
        (
            side,
            role,
            sequence,
            active_unwind,
            intent,
            policy_decision_id,
            exit_stage,
            binding_constraint,
            controller_decision_id,
        ) = next(iter(values))
        by_order[order_id] = {
            "side": side,
            "role": role,
            "episode_sequence": sequence,
            "active_unwind": active_unwind,
            "intent_kind": intent,
            "policy_decision_id": policy_decision_id,
            "exit_stage": exit_stage,
            "binding_constraint": binding_constraint,
            "controller_decision_id": controller_decision_id,
        }
    for order_id in invalid_orders:
        by_order.pop(order_id, None)
    maker_trade_count = sum(
        attribution["active_unwind"] is False
        for attribution in by_trade.values()
    )
    taker_trade_count = sum(
        attribution["active_unwind"] is True
        for attribution in by_trade.values()
    )
    return (
        by_order,
        invalid_without_order or bool(invalid_orders),
        maker_trade_count,
        taker_trade_count,
        list(by_trade.values()),
    )


def _event_has_typed_provenance(
    event: Mapping[str, Any],
    attribution: Mapping[str, Any] | None,
    run_id: str,
    controller_decisions: Mapping[tuple[str, int], Mapping[str, Any]],
    invalid_controller_decisions: set[tuple[str, int]],
) -> bool:
    if attribution is None or event.get("attribution_state") != "authenticated":
        return False
    order_id = str(event.get("order_id", "") or "").strip()
    side = str(event.get("side", "") or "").lower()
    event_sequence = event.get("fill_observation_event_sequence")
    fill_amount = _optional_decimal(event.get("fill_amount"))
    fill_price = _optional_decimal(event.get("fill_price"))
    signature = event.get("attribution_signature")
    expected_signature = {
        "side": attribution["side"],
        "role": attribution["role"],
        "episode_sequence": attribution["episode_sequence"],
        "active_unwind": attribution["active_unwind"],
    }
    if (
        not order_id
        or fill_amount is None
        or fill_amount <= 0
        or fill_price is None
        or fill_price <= 0
        or event.get("event_sequence_run_id") != run_id
        or type(event_sequence) is not int
        or event_sequence <= 0
        or side != attribution["side"]
        or (event.get("fill_role") or event.get("role")) != attribution["role"]
        or event.get("episode_sequence") != attribution["episode_sequence"]
        or event.get("active_unwind") is not attribution["active_unwind"]
        or not isinstance(signature, Mapping)
        or dict(signature) != expected_signature
    ):
        return False
    if attribution["role"] != "entry":
        return True
    context = event.get("quote_context")
    if not isinstance(context, Mapping):
        return False
    if (
        attribution["intent_kind"] == "controller_entry"
        and context.get("decision_id") != attribution["controller_decision_id"]
    ):
        return False
    if attribution["intent_kind"] == "controller_entry":
        decision_key = (run_id, attribution["controller_decision_id"])
        decision = controller_decisions.get(decision_key)
        side_name = "bid" if side == "buy" else "ask"
        side_decision = (
            decision.get(side_name) if isinstance(decision, Mapping) else None
        )
        decision_sequence = (
            decision.get("event_sequence")
            if isinstance(decision, Mapping)
            else None
        )
        placement_sequence = context.get("placement_event_sequence")
        bound_numeric_fields = (
            "base_price",
            "shadow_price",
            "applied_price",
            "extra_spread_ticks",
            "toxicity_score_ticks",
        )
        controller_fields_match = isinstance(side_decision, Mapping) and all(
            _optional_decimal(side_decision.get(field)) is not None
            and _optional_decimal(side_decision.get(field))
            == _optional_decimal(context.get(field))
            for field in bound_numeric_fields
        )
        if (
            decision_key in invalid_controller_decisions
            or not isinstance(side_decision, Mapping)
            or type(decision_sequence) is not int
            or type(placement_sequence) is not int
            or decision_sequence <= 0
            or decision_sequence >= placement_sequence
            or decision.get("ready") is not True
            or decision.get("entry_applicable") is not True
            or decision.get("error") is not None
            or decision.get("mode") != "active"
            or context.get("controller_mode") != "active"
            or side_decision.get("blocked") is not False
            or context.get("reduce_only") is not False
            or not controller_fields_match
        ):
            return False
    placement_sequence = context.get("placement_event_sequence")
    return (
        context.get("event_sequence_run_id") == run_id
        and context.get("order_id") == order_id
        and str(context.get("side", "") or "").lower() == side
        and type(placement_sequence) is int
        and 0 < placement_sequence < event_sequence
    )


def _fill_samples(
    records: list[dict[str, Any]], run_id: str
) -> tuple[list[dict[str, Any]], bool, bool, int, int, list[dict[str, Any]]]:
    (
        attributions,
        invalid_attributions,
        maker_trade_count,
        taker_trade_count,
        trade_attributions,
    ) = (
        _typed_attributions(records)
    )
    samples: list[dict[str, Any]] = []
    matched_orders: set[str] = set()
    attribution_evidence_complete = not invalid_attributions
    controller_decision_versions: dict[
        tuple[str, int], dict[str, Mapping[str, Any]]
    ] = {}
    for record in records:
        history = record.get("controller_decision_history")
        if not isinstance(history, list):
            continue
        for item in history:
            if not isinstance(item, Mapping):
                continue
            history_run_id = item.get("event_sequence_run_id")
            decision_id = item.get("decision_id")
            if not isinstance(history_run_id, str) or type(decision_id) is not int:
                continue
            controller_decision_versions.setdefault(
                (history_run_id, decision_id), {}
            )[json.dumps(dict(item), default=str, sort_keys=True)] = item
    invalid_controller_decisions = {
        key
        for key, versions in controller_decision_versions.items()
        if len(versions) != 1
    }
    controller_decisions = {
        key: next(iter(versions.values()))
        for key, versions in controller_decision_versions.items()
        if len(versions) == 1
    }
    quote_context_versions: dict[str, set[str]] = {}
    invalid_quote_context_orders: set[str] = set()
    entry_order_ids = {
        order_id
        for order_id, attribution in attributions.items()
        if attribution["role"] == "entry"
    }
    for record in records:
        raw_events = record.get("fill_markouts")
        if not isinstance(raw_events, list):
            continue
        for event in raw_events:
            if not isinstance(event, Mapping):
                continue
            order_id = str(event.get("order_id", "") or "").strip()
            if order_id not in entry_order_ids:
                continue
            context = event.get("quote_context")
            if not isinstance(context, Mapping):
                invalid_quote_context_orders.add(order_id)
                continue
            quote_context_versions.setdefault(order_id, set()).add(
                json.dumps(dict(context), default=str, sort_keys=True)
            )
    invalid_quote_context_orders.update(
        order_id
        for order_id, versions in quote_context_versions.items()
        if len(versions) != 1
    )
    attribution_evidence_complete &= not invalid_quote_context_orders
    events = strategy_analyzer._merge_events(records)
    sequence_counts = Counter(
        sequence
        for event in events
        if type(sequence := event.get("fill_observation_event_sequence")) is int
    )
    for index, event in enumerate(events, start=1):
        order_id = str(event.get("order_id", "") or "").strip()
        attribution = attributions.get(order_id)
        event_sequence = event.get("fill_observation_event_sequence")
        valid = (
            type(event_sequence) is int
            and sequence_counts[event_sequence] == 1
            and order_id not in invalid_quote_context_orders
            and _event_has_typed_provenance(
                event,
                attribution,
                run_id,
                controller_decisions,
                invalid_controller_decisions,
            )
        )
        attribution_evidence_complete &= valid
        if attribution is not None:
            matched_orders.add(order_id)
        samples.append(
            {
                **event,
                **(dict(attribution) if valid else {}),
                "sample_id": f"{run_id}:fill-delta:{index}",
                "attribution_state": "authenticated" if valid else "pending",
                "fill_role": attribution["role"] if valid else None,
                "active_unwind": (
                    attribution["active_unwind"] if valid else None
                ),
                "markouts": _markouts(event),
                "toxicity_score_ticks": _toxicity_score(event),
            }
        )
    unmatched_orders = sorted(
        order_id
        for order_id, attribution in attributions.items()
        if attribution["active_unwind"] is False
        and order_id not in matched_orders
    )
    attribution_evidence_complete &= not unmatched_orders
    maker_amount_by_order: dict[str, Decimal] = {}
    active_order_ids = {
        attribution["order_id"]
        for attribution in trade_attributions
        if attribution["active_unwind"] is True
    }
    for attribution in trade_attributions:
        if attribution["active_unwind"] is True:
            continue
        order_id = attribution["order_id"]
        amount = abs(
            attribution["next_position"] - attribution["prior_position"]
        )
        maker_amount_by_order[order_id] = (
            maker_amount_by_order.get(order_id, Decimal("0")) + amount
        )
    observed_amount_by_order: dict[str, Decimal] = {}
    conservation_valid = True
    for event in events:
        order_id = str(event.get("order_id", "") or "").strip()
        if order_id in active_order_ids:
            continue
        amount = _optional_decimal(event.get("fill_amount"))
        if not order_id or amount is None or amount <= 0:
            conservation_valid = False
            continue
        observed_amount_by_order[order_id] = (
            observed_amount_by_order.get(order_id, Decimal("0")) + amount
        )
    attribution_evidence_complete &= (
        conservation_valid
        and observed_amount_by_order == maker_amount_by_order
    )
    return (
        samples,
        attribution_evidence_complete,
        not invalid_attributions,
        maker_trade_count,
        taker_trade_count,
        trade_attributions,
    )


def _empty_bin() -> dict[str, Any]:
    return {"entries": [], "episodes": {}}


def _summarize_bin(bucket: Mapping[str, Any]) -> dict[str, Any]:
    entries = list(bucket["entries"])
    episodes = list(bucket["episodes"].values())
    markout_5 = [_optional_decimal(item["markouts"].get("5")) for item in entries]
    markout_15 = [_optional_decimal(item["markouts"].get("15")) for item in entries]

    def markout_summary(values: list[Decimal | None]) -> dict[str, Any]:
        available = [value for value in values if value is not None]
        result = _summary(available)
        result["negative_probability"] = (
            Decimal(sum(value < 0 for value in available)) / Decimal(len(available))
            if available
            else None
        )
        return result

    complete_episodes = [
        episode for episode in episodes if _complete_campaign_episode(episode)
    ]
    stages = Counter(str(episode["final_exit_stage"]) for episode in complete_episodes)
    return {
        "entry_count": len(entries),
        "external_markout_5s_bps": markout_summary(markout_5),
        "external_markout_15s_bps": markout_summary(markout_15),
        "episode_gross_usdg": _summary(episode.get("gross") for episode in complete_episodes),
        "episode_net_usdg": _summary(
            episode.get("net_ex_funding", episode.get("net")) for episode in complete_episodes
        ),
        "inventory_duration_seconds": _summary(
            episode.get("inventory_duration_seconds") for episode in complete_episodes
        ),
        "final_exit_stage_distribution": dict(sorted(stages.items())),
        "inventory_hold_rate": (
            Decimal(sum(episode.get("entered_inventory_hold") is True for episode in complete_episodes))
            / Decimal(len(complete_episodes))
            if complete_episodes
            else None
        ),
        "active_exit_rate": (
            Decimal(sum(episode.get("final_exit_stage") == "active_ioc" for episode in complete_episodes))
            / Decimal(len(complete_episodes))
            if complete_episodes
            else None
        ),
        "passive_loss_used_usdg": _summary(
            episode.get("passive_loss_used") for episode in complete_episodes
        ),
    }


def _run_payload(
    source: str,
    records: list[dict[str, Any]],
    report: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    final = _final_record(records)
    audit = final.get("account_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    episode_details = report.get("episodes", {}).get("episode_details", [])
    episodes = _deduplicate_episodes(
        episode for episode in episode_details if isinstance(episode, Mapping)
    )
    (
        samples,
        attribution_evidence_complete,
        attribution_schema_valid,
        maker_trade_count,
        taker_trade_count,
        trade_attributions,
    ) = _fill_samples(records, run_id)
    maker_fills = (
        audit["unique_maker_fills"]
        if type(audit.get("unique_maker_fills")) is int
        else 0
    )
    taker_fills = (
        audit["unique_taker_fills"]
        if type(audit.get("unique_taker_fills")) is int
        else 0
    )
    attribution_evidence_complete &= (
        maker_trade_count == maker_fills
        and taker_trade_count == taker_fills
    )
    return {
        "source": source,
        "run_id": run_id,
        "records": records,
        "report": report,
        "final": final,
        "identity": _identity(final),
        "samples": samples,
        "attribution_evidence_complete": attribution_evidence_complete,
        "attribution_schema_valid": attribution_schema_valid,
        "authenticated_maker_trade_count": maker_trade_count,
        "authenticated_active_exit_trade_count": taker_trade_count,
        "authenticated_trade_attributions": trade_attributions,
        "merged_event_count": len(strategy_analyzer._merge_events(records)),
        "episodes": episodes,
        "eligible_quote_seconds": _optional_decimal(final.get("eligible_quote_seconds")) or Decimal("0"),
        "maker_fills": maker_fills,
        "maker_turnover": _optional_decimal(audit.get("maker_turnover")) or Decimal("0"),
        "completed_net": _optional_decimal(audit.get("completed_net_ex_funding")) or Decimal("0"),
        "flat_equity_change": _optional_decimal(audit.get("flat_equity_change")),
        "max_drawdown": _optional_decimal(audit.get("max_observed_drawdown")) or Decimal("0"),
    }


def _risk_payload(
    source: str,
    source_sha256: str,
    records: list[dict[str, Any]],
    run_id: str | None,
) -> dict[str, Any]:
    final = _final_record(records)
    audit = final.get("account_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    return {
        "source": source,
        "source_sha256": source_sha256,
        "run_id": run_id,
        "final": final,
        "flat_equity_change": _optional_decimal(audit.get("flat_equity_change")),
        "completed_net": _optional_decimal(audit.get("completed_net_ex_funding")),
        "max_drawdown": _optional_decimal(audit.get("max_observed_drawdown")),
    }


def _has_authenticated_flat_risk_evidence(item: Mapping[str, Any]) -> bool:
    final = item.get("final")
    if not isinstance(final, Mapping):
        return False
    audit = final.get("account_audit")
    postflight = final.get("postflight")
    positions = (
        audit.get("audited_position") if isinstance(audit, Mapping) else None,
        audit.get("ledger_position") if isinstance(audit, Mapping) else None,
        final.get("signed_position"),
        postflight.get("position") if isinstance(postflight, Mapping) else None,
    )
    return (
        isinstance(audit, Mapping)
        and audit.get("last_audit_authenticated") is True
        and isinstance(postflight, Mapping)
        and postflight.get("authenticated") is True
        and all(_optional_decimal(value) == 0 for value in positions)
        and _optional_decimal(audit.get("flat_equity_change")) is not None
        and _optional_decimal(audit.get("completed_net_ex_funding")) is not None
        and _optional_decimal(audit.get("max_observed_drawdown")) is not None
        and _financial_identity_valid(audit)
    )


def _conservative_risk_items(
    items: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if not _has_authenticated_flat_risk_evidence(item):
            continue
        run_id = item.get("run_id")
        source = str(item.get("source", ""))
        source_sha256 = str(item.get("source_sha256", ""))
        key = (
            ("run_id", run_id)
            if isinstance(run_id, str) and run_id
            else ("source_sha256", source_sha256 or source)
        )
        flat_change = _optional_decimal(item.get("flat_equity_change"))
        completed_net = _optional_decimal(item.get("completed_net"))
        max_drawdown = _optional_decimal(item.get("max_drawdown"))
        if flat_change is None or completed_net is None or max_drawdown is None:
            continue
        current = grouped.get(key)
        if current is None:
            grouped[key] = {
                "run_id": run_id if isinstance(run_id, str) and run_id else None,
                "sources": {source},
                "source_sha256": {source_sha256} if source_sha256 else set(),
                "flat_equity_change": flat_change,
                "completed_net": completed_net,
                "max_drawdown": max_drawdown,
            }
            continue
        current["sources"].add(source)
        if source_sha256:
            current["source_sha256"].add(source_sha256)
        current["flat_equity_change"] = min(
            current["flat_equity_change"], flat_change
        )
        current["completed_net"] = min(current["completed_net"], completed_net)
        current["max_drawdown"] = max(current["max_drawdown"], max_drawdown)
    return list(grouped.values())


def _candidate_payload(
    source: str,
    source_sha256: str,
    records: list[dict[str, Any]],
    run_id: str,
    initial_reasons: Iterable[str],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    reasons = list(initial_reasons)
    report = strategy_analyzer.analyze(records)
    payload = _run_payload(source, records, report, run_id)
    payload["source_sha256"] = source_sha256
    identity_reasons = _identity_reasons(records, manifest)
    payload["identity_matches_campaign"] = not identity_reasons
    reasons.extend(identity_reasons)
    reasons.extend(_safety_reasons(payload["final"]))
    reasons.extend(_historical_hard_counter_reasons(records))
    reasons.extend(_account_state_transition_reasons(records))
    reasons.extend(_controller_history_reasons(records, run_id))
    reasons.extend(_raw_event_conflict_reasons(records))
    if payload["attribution_schema_valid"] is not True:
        reasons.append("malformed_authenticated_attribution")
    reasons.extend(
        _evidence_reasons(
            payload["final"],
            merged_event_count=payload["merged_event_count"],
        )
    )
    reasons.extend(_episode_conflict_reasons(records))
    if not _completed_episode_aggregate_valid(
        payload["final"],
        payload["authenticated_trade_attributions"],
        payload["episodes"],
    ):
        reasons.append("completed_episode_aggregate_incomplete_or_inconsistent")
    coverage = report.get("coverage", {})
    reported_history = coverage.get("controller_history_reported_total")
    merged_history = coverage.get("controller_history_merged")
    if (
        coverage.get("controller_history_complete") is not True
        or type(reported_history) is not int
        or type(merged_history) is not int
        or reported_history <= 0
        or merged_history != reported_history
    ):
        reasons.append("controller_history_incomplete")
    payload["reasons"] = sorted(set(reasons))
    return payload


def analyze_campaign(
    manifest: Mapping[str, Any] | str | Path,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = _DECIMAL_WORK_PRECISION
        return _analyze_campaign(manifest, base_dir=base_dir)


def _analyze_campaign(
    manifest: Mapping[str, Any] | str | Path,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    if isinstance(manifest, (str, Path)):
        manifest_path = Path(manifest)
        validated = load_manifest(manifest_path)
        root = manifest_path.resolve().parent
    else:
        validated = _validate_manifest(manifest)
        root = Path(_local_path_text(base_dir or ".", "base_dir")).resolve()

    candidates: list[dict[str, Any]] = []
    risk_candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    input_evidence: list[dict[str, Any]] = []
    for raw_path in validated["inputs"]:
        path = Path(raw_path)
        path = path if path.is_absolute() else root / path
        source = raw_path
        source_sha256: str | None = None
        try:
            source_sha256, records = _load_evidence_once(path)
            run_id, reasons = _runtime_run_id(records)
        except Exception as exc:
            input_evidence.append(
                {"source": source, "sha256": source_sha256, "run_id": None}
            )
            diagnostics.append(
                {
                    "source": source,
                    "source_sha256": source_sha256,
                    "run_id": None,
                    "reasons": [
                        f"unreadable_or_invalid_input:{type(exc).__name__}"
                    ],
                }
            )
            continue
        risk_candidates.append(
            _risk_payload(source, source_sha256, records, run_id)
        )
        input_evidence.append(
            {"source": source, "sha256": source_sha256, "run_id": run_id}
        )
        if run_id is None:
            diagnostics.append(
                {
                    "source": source,
                    "source_sha256": source_sha256,
                    "run_id": None,
                    "reasons": reasons,
                }
            )
            continue
        try:
            candidates.append(
                _candidate_payload(
                    source,
                    source_sha256,
                    records,
                    run_id,
                    reasons,
                    validated,
                )
            )
        except Exception as exc:
            diagnostics.append(
                {
                    "source": source,
                    "source_sha256": source_sha256,
                    "run_id": run_id,
                    "reasons": [
                        f"unreadable_or_invalid_input:{type(exc).__name__}"
                    ],
                }
            )

    duplicate_ids = {
        run_id for run_id, count in Counter(item["run_id"] for item in candidates).items() if count > 1
    }
    included: list[dict[str, Any]] = []
    for item in candidates:
        reasons = list(item["reasons"])
        if item["run_id"] in duplicate_ids:
            reasons.append("duplicate_runtime_run_id")
        if reasons:
            diagnostics.append(
                {
                    "source": item["source"],
                    "source_sha256": item["source_sha256"],
                    "run_id": item["run_id"],
                    "reasons": sorted(set(reasons)),
                }
            )
        else:
            included.append(item)

    evidence_digest_material = {
        "campaign_id": validated["campaign_id"],
        "candidate_id": validated["candidate_id"],
        "expected_commit_sha": validated["expected_commit_sha"],
        "expected_config_sha256": validated["expected_config_sha256"],
        "controller_profile_id": validated["controller_profile_id"],
        "symbol": validated["symbol"],
        "maker_fee_rate": str(validated["maker_fee_rate"]),
        "taker_fee_rate": str(validated["taker_fee_rate"]),
        "max_cumulative_flat_loss_usdg": str(
            validated["max_cumulative_flat_loss_usdg"]
        ),
        "inputs": input_evidence,
    }
    campaign_evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence_digest_material,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    risk_items = _conservative_risk_items(risk_candidates)

    campaign_episodes = {
        (item["run_id"], session_id, sequence): episode
        for item in included
        for (session_id, sequence), episode in item["episodes"].items()
        if _complete_campaign_episode(episode)
        and _episode_attribution_conserved(
            episode, item["authenticated_trade_attributions"]
        )
    }
    by_side: dict[str, dict[str, dict[str, Any]]] = {
        side: {label: _empty_bin() for label, *_ in _SCORE_BINS}
        for side in ("buy", "sell")
    }
    denominator = {
        "authenticated_buy_entries": 0,
        "authenticated_sell_entries": 0,
        "excluded_passive_exits": 0,
        "excluded_active_exits": 0,
        "pending_or_indeterminate": 0,
    }
    markout_complete = True
    score_complete = True
    episode_economics_complete = True
    attribution_evidence_complete = bool(included) and all(
        item["attribution_evidence_complete"] for item in included
    )
    for item in included:
        denominator["excluded_active_exits"] += item[
            "authenticated_active_exit_trade_count"
        ]
        episodes_by_sequence: dict[int, list[dict[str, Any]]] = {}
        for episode in item["episodes"].values():
            sequence = episode.get("episode_sequence")
            if type(sequence) is int:
                episodes_by_sequence.setdefault(sequence, []).append(episode)
        for sample in item["samples"]:
            authenticated = sample.get("attribution_state") == "authenticated"
            role = sample.get("role", sample.get("fill_role"))
            active_unwind = sample.get("active_unwind")
            passive_exit = role == "passive_exit"
            active_exit = role == "active_exit" or active_unwind is True
            authenticated_entry = role == "entry" and active_unwind is False
            if not authenticated:
                denominator["pending_or_indeterminate"] += 1
                continue
            if passive_exit:
                denominator["excluded_passive_exits"] += 1
                continue
            if active_exit:
                continue
            if not authenticated_entry:
                denominator["pending_or_indeterminate"] += 1
                continue
            side = str(sample.get("side", "")).lower()
            if side not in by_side:
                denominator["pending_or_indeterminate"] += 1
                continue
            denominator[f"authenticated_{side}_entries"] += 1
            markouts = sample.get("markouts", {})
            if _optional_decimal(markouts.get("5")) is None or _optional_decimal(markouts.get("15")) is None:
                markout_complete = False
            label = _score_bin(sample.get("toxicity_score_ticks"))
            if label is None:
                score_complete = False
                continue
            bucket = by_side[side][label]
            bucket["entries"].append(sample)
            sequence = sample.get("episode_sequence")
            candidates_for_episode = (
                episodes_by_sequence.get(sequence, [])
                if type(sequence) is int
                else []
            )
            episode = (
                candidates_for_episode[0]
                if len(candidates_for_episode) == 1
                else None
            )
            if (
                episode is None
                or str(episode.get("entry_side", "")).lower() != side
                or not _complete_campaign_episode(episode)
                or not _episode_attribution_conserved(
                    episode, item["authenticated_trade_attributions"]
                )
            ):
                episode_economics_complete = False
            if episode is not None and (key := _episode_key(episode)) is not None:
                bucket["episodes"][(item["run_id"], *key)] = episode

    total_entries = sum(
        denominator[f"authenticated_{side}_entries"]
        for side in ("buy", "sell")
    )
    markout_complete &= total_entries > 0
    score_complete &= total_entries > 0
    episode_economics_complete &= total_entries > 0

    summarized_bins = {
        side: {label: _summarize_bin(bins[label]) for label, *_ in _SCORE_BINS}
        for side, bins in by_side.items()
    }
    eligible_seconds = sum((item["eligible_quote_seconds"] for item in included), Decimal("0"))
    eligible_hours = eligible_seconds / Decimal("3600") if eligible_seconds > 0 else None
    maker_fills = sum(item["maker_fills"] for item in included)
    maker_turnover = sum((item["maker_turnover"] for item in included), Decimal("0"))
    completed_net = sum((item["completed_net"] for item in included), Decimal("0"))
    risk_completed_net = sum(
        (item["completed_net"] for item in risk_items), Decimal("0")
    )
    flat_changes = [
        item["flat_equity_change"]
        for item in risk_items
        if item["flat_equity_change"] is not None
    ]
    cumulative_flat = sum(flat_changes, Decimal("0"))
    realized_losses = sum((-value for value in flat_changes if value < 0), Decimal("0"))
    budget = validated["max_cumulative_flat_loss_usdg"]
    remaining_budget = budget - realized_losses
    risk_evidence_complete = bool(included) and not diagnostics
    within_risk = risk_evidence_complete and remaining_budget > 0
    side_threshold_met = all(
        denominator[f"authenticated_{side}_entries"] >= 30 for side in ("buy", "sell")
    )
    periods = {
        (item["identity"].get("started_at_utc"), item["identity"].get("ended_at_utc"))
        for item in included
    }
    intervals = sorted(
        (start, end)
        for item in included
        if (start := _utc_datetime(item["identity"].get("started_at_utc")))
        is not None
        and (end := _utc_datetime(item["identity"].get("ended_at_utc")))
        is not None
    )
    non_overlapping_periods = len(intervals) == len(included) and all(
        prior[1] <= current[0]
        for prior, current in zip(intervals, intervals[1:])
    )
    multiple_fresh_runs = (
        len(included) >= 2
        and len(periods) >= 2
        and non_overlapping_periods
    )
    meaningful_bins = score_complete and all(
        sum(
            summary["entry_count"] >= _MIN_MEANINGFUL_BIN_ENTRIES
            for summary in summarized_bins[side].values()
        )
        >= 2
        for side in ("buy", "sell")
    )
    incomplete_reasons = []
    if not side_threshold_met:
        incomplete_reasons.append("authenticated_entry_side_threshold_not_met")
    if not markout_complete:
        incomplete_reasons.append("external_markout_coverage_incomplete")
    if not multiple_fresh_runs:
        incomplete_reasons.append("multiple_fresh_runs_not_proven")
    if not meaningful_bins:
        incomplete_reasons.append("meaningful_score_bins_not_proven")
    if not episode_economics_complete:
        incomplete_reasons.append("completed_episode_economics_incomplete")
    if denominator["pending_or_indeterminate"]:
        incomplete_reasons.append("pending_or_indeterminate_evidence_present")
    if not attribution_evidence_complete:
        incomplete_reasons.append("typed_attribution_evidence_incomplete")
    if risk_evidence_complete and not within_risk:
        incomplete_reasons.append("campaign_loss_cap_exceeded")
    if not risk_evidence_complete:
        incomplete_reasons.append("campaign_risk_evidence_incomplete")
    if not included:
        incomplete_reasons.append("no_eligible_runs")
    if diagnostics:
        incomplete_reasons.append("diagnostic_inputs_present")
    incomplete_reasons = sorted(set(incomplete_reasons))
    counterfactual_runs = [
        item["report"].get("shadow_counterfactual", {}) for item in included
    ]

    def sum_mapping(field: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for proxy in counterfactual_runs:
            values = proxy.get(field)
            if not isinstance(values, Mapping):
                continue
            for key, value in values.items():
                if type(value) is int and value >= 0:
                    counts[str(key)] += value
        return dict(sorted(counts.items()))

    return {
        "analysis_contract": {
            "local_read_only": True,
            "network_access": False,
            "exchange_mutation": False,
            "entry_sample_unit": "observed_order_fill_delta",
            "required_top_level_per_snapshot": [
                "event_sequence_run_id",
                "campaign_id",
                "candidate_id",
                "commit_sha",
                "semantic_config_sha256 (or config_sha256)",
                "controller_profile_id",
                "symbol",
                "maker_fee_rate",
                "taker_fee_rate",
                "started_at_utc",
                "ended_at_utc",
            ],
            "required_final_authenticated_evidence": [
                "preflight.authenticated=true, position=0, open_orders=0",
                "postflight.authenticated=true, position=0, open_orders=0",
                "account_audit.last_audit_authenticated=true",
                "account_audit.audited_position=0",
                "account_audit.ledger_position=0",
                "signed_position=0",
                "authenticated_open_orders=0",
                "runtime counter map contains every required key, all zero",
            ],
            "required_zero_runtime_counters": sorted(_REQUIRED_ZERO_COUNTERS),
            "required_zero_runtime_scalars": list(_REQUIRED_ZERO_SCALARS),
            "counterfactual_label": "proxy, not queue-fill backtest",
            "true_queue_fill_backtest": False,
            "live_profile_generated_or_applied": False,
        },
        "campaign": {
            "campaign_id": validated["campaign_id"],
            "candidate_id": validated["candidate_id"],
            "campaign_evidence_sha256": campaign_evidence_sha256,
            "expected_commit_sha": validated["expected_commit_sha"],
            "expected_config_sha256": validated["expected_config_sha256"],
            "controller_profile_id": validated["controller_profile_id"],
            "symbol": validated["symbol"],
            "maker_fee_rate": validated["maker_fee_rate"],
            "taker_fee_rate": validated["taker_fee_rate"],
            "status": "calibration_incomplete" if incomplete_reasons else "calibration_complete",
            "calibration_incomplete": bool(incomplete_reasons),
            "incomplete_reasons": incomplete_reasons,
            "recommendation_allowed": not incomplete_reasons,
        },
        "runs": {
            "included_count": len(included),
            "included_run_ids": sorted(item["run_id"] for item in included),
            "included_evidence": [
                {
                    "source": item["source"],
                    "source_sha256": item["source_sha256"],
                    "run_id": item["run_id"],
                }
                for item in sorted(included, key=lambda value: value["run_id"])
            ],
            "fresh_run_count": len(included),
            "utc_periods": sorted(
                {
                    f"{item['identity']['started_at_utc']}/{item['identity']['ended_at_utc']}"
                    for item in included
                }
            ),
            "diagnostic_only": sorted(diagnostics, key=lambda item: (item["source"], str(item["run_id"]))),
        },
        "denominator": denominator,
        "score_bins": summarized_bins,
        "calibration_coverage": {
            "minimum_authenticated_entries_per_side": 30,
            "side_threshold_met": side_threshold_met,
            "external_5s_15s_markout_coverage_complete": markout_complete,
            "placement_score_coverage_complete": score_complete,
            "typed_attribution_evidence_complete": (
                attribution_evidence_complete
            ),
            "completed_episode_economics_complete": (
                episode_economics_complete
            ),
            "meaningful_score_bins": meaningful_bins,
            "minimum_entries_per_meaningful_bin": (
                _MIN_MEANINGFUL_BIN_ENTRIES
            ),
            "multiple_fresh_utc_periods": multiple_fresh_runs,
            "utc_periods_non_overlapping": non_overlapping_periods,
            "diagnostic_input_count": len(diagnostics),
        },
        "counterfactual_proxy": {
            "label": "proxy, not queue-fill backtest",
            "true_queue_fill_backtest": False,
            "classification_counts": sum_mapping("classification_counts"),
            "excluded_fill_counts": sum_mapping("excluded_fill_counts"),
            "virtual_lifecycle_action_counts": sum_mapping(
                "virtual_lifecycle_action_counts"
            ),
        },
        "episode_coverage": {
            "deduplicated_completed_episode_count": len(campaign_episodes),
            "identity": "(run_id, session_id, episode_sequence)",
        },
        "rates": {
            "eligible_quote_hours": eligible_hours,
            "maker_fills_per_eligible_hour": (
                Decimal(maker_fills) / eligible_hours if eligible_hours is not None else None
            ),
            "maker_turnover_usdg_per_eligible_hour": (
                maker_turnover / eligible_hours if eligible_hours is not None else None
            ),
            "net_usdg_per_eligible_hour": (
                completed_net / eligible_hours if eligible_hours is not None else None
            ),
        },
        "risk": {
            "accounted_run_ids": sorted(
                item["run_id"]
                for item in risk_items
                if item["run_id"] is not None
            ),
            "unidentified_evidence_sources": sorted(
                source
                for item in risk_items
                if item["run_id"] is None
                for source in item["sources"]
            ),
            "cumulative_flat_equity_change_usdg": cumulative_flat,
            "cumulative_completed_net_usdg": risk_completed_net,
            "worst_session_drawdown_usdg": max(
                (item["max_drawdown"] for item in risk_items),
                default=Decimal("0"),
            ),
            "sum_session_realized_losses_usdg": realized_losses,
            "operator_campaign_loss_budget_usdg": budget,
            "risk_evidence_complete": risk_evidence_complete,
            "remaining_operator_budget_usdg": (
                remaining_budget if risk_evidence_complete else None
            ),
            "within_campaign_loss_cap": within_risk,
        },
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, default=_json_default, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="local campaign manifest JSON")
    args = parser.parse_args(argv)
    print(render_json(analyze_campaign(args.manifest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
