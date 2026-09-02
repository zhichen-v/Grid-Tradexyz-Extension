#!/usr/bin/env python3
"""Analyze local Market Maker metrics without exchange or credential access."""

from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


_DECIMAL_RE = re.compile(r"Decimal\((['\"])(.*?)\1\)")
_ROLES = frozenset({"entry", "risk_increasing", "passive_exit", "active_exit"})
_FEATURE_FIELDS = (
    "spread_ticks",
    "return_1s_ticks",
    "return_5s_ticks",
    "return_15s_ticks",
    "return_60s_ticks",
    "rms_1s_move_15s_ticks",
    "rms_1s_move_60s_ticks",
    "microprice_shift_ticks",
    "depth_imbalance",
)
_VIRTUAL_SHADOW_ACTIONS = (
    "would_place",
    "would_reprice",
    "would_block",
    "would_cancel",
    "would_resume",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _read_json_records(text: str) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return [loaded]
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _read_python_log_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        marker = line.find("{'runtime_state'")
        if marker < 0:
            continue
        candidate = _DECIMAL_RE.sub(lambda match: repr(match.group(2)), line[marker:])
        try:
            item = ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _legacy_markdown_snapshot(text: str) -> dict[str, Any] | None:
    """Parse the durable human summary conservatively; absent joins stay pending."""

    def match(pattern: str) -> re.Match[str] | None:
        return re.search(pattern, text, re.MULTILINE)

    fills = match(
        r"Completed maker fills / round trips / authenticated-flat episodes:\s*"
        r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)"
    )
    economics = {
        key: match(pattern)
        for key, pattern in {
            "completed_turnover": r"Completed turnover:\s*([-+]?\d+(?:\.\d+)?)",
            "completed_gross": r"Completed gross:\s*([-+]?\d+(?:\.\d+)?)",
            "completed_exact_fee": r"Exact fee:\s*([-+]?\d+(?:\.\d+)?)",
            "completed_net_ex_funding": r"Completed net ex funding:\s*([-+]?\d+(?:\.\d+)?)",
            "completed_net_turnover_bps": r"Completed net turnover bps:\s*([-+]?\d+(?:\.\d+)?)",
            "completed_fee_cover_ratio": r"Fee-cover ratio:\s*([-+]?\d+(?:\.\d+)?)",
        }.items()
    }
    retained = match(r"Retained maker fill events:\s*(\d+)")
    if fills is None and not any(economics.values()) and retained is None:
        return None

    audit: dict[str, Any] = {}
    if fills is not None:
        audit.update(
            completed_fills=int(fills.group(1)),
            unique_maker_fills=int(fills.group(1)),
            completed_round_trips=int(fills.group(2)),
            authenticated_flat_episodes=int(fills.group(3)),
        )
    for key, found in economics.items():
        if found is not None:
            audit[key] = found.group(1)
    retained_count = int(retained.group(1)) if retained is not None else 0
    return {
        "account_audit": audit,
        "fill_markouts": [],
        "fill_markout_coverage": {"retained_events": retained_count},
        "legacy_pending_attribution": retained_count,
        "legacy_source": "human_markdown_summary",
    }


def load_local_records(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8")
        parsed = _read_json_records(text) or _read_python_log_records(text)
        if parsed:
            records.extend(parsed)
            continue
        legacy = _legacy_markdown_snapshot(text)
        if legacy is None:
            raise ValueError(f"unsupported metrics format: {path}")
        records.append(legacy)
    if not records:
        raise ValueError("no metrics snapshots found")
    return records


def _describe(values: Iterable[Any]) -> dict[str, Any]:
    decimals = [value for item in values if (value := _decimal(item)) is not None]
    if not decimals:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(decimals),
        "mean": sum(decimals, Decimal("0")) / Decimal(len(decimals)),
        "median": Decimal(str(statistics.median(decimals))),
        "min": min(decimals),
        "max": max(decimals),
    }


def _stable_text(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, dict):
            return {
                str(key): normalize(child)
                for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    return json.dumps(
        normalize(value), sort_keys=True, separators=(",", ":"), default=str
    )


def _event_sequence_run_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    run_id = value.get("event_sequence_run_id")
    return run_id if isinstance(run_id, str) and run_id.strip() else None


def _record_rank(record: dict[str, Any]) -> tuple[Any, ...]:
    audit = record.get("account_audit")
    if not isinstance(audit, dict):
        audit = {}

    def number(value: Any) -> Decimal:
        return _decimal(value) or Decimal("-1")

    return (
        number(record.get("uptime_seconds")),
        number(audit.get("completed_fills")),
        number(audit.get("unique_maker_fills")),
        number(audit.get("unique_taker_fills")),
        number(audit.get("completed_round_trips")),
        len(record.get("fill_markouts", ()))
        if isinstance(record.get("fill_markouts"), list)
        else 0,
        len(record.get("controller_decision_history", ()))
        if isinstance(record.get("controller_decision_history"), list)
        else 0,
        _stable_text(record),
    )


def _event_key(event: dict[str, Any]) -> tuple[str, ...]:
    return (
        _stable_text(_event_sequence_run_id(event)),
        _stable_text(event.get("fill_observation_event_sequence")),
        _stable_text(event.get("trade_id")),
        _stable_text(event.get("order_id")),
        _stable_text(event.get("side")),
        _stable_text(event.get("fill_amount")),
        _stable_text(event.get("fill_price", event.get("price"))),
        _stable_text(event.get("started_monotonic")),
        _stable_text(event.get("observation_source", event.get("source"))),
    )


def _merge_events(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    versions: dict[
        tuple[str, ...], list[tuple[tuple[Any, ...], dict[str, Any]]]
    ] = {}
    for record in records:
        events = record.get("fill_markouts")
        if not isinstance(events, list):
            continue
        for event in events:
            if isinstance(event, dict):
                versions.setdefault(_event_key(event), []).append(
                    (_record_rank(record), event)
                )

    merged_events: list[dict[str, Any]] = []
    for key in sorted(versions):
        ranked_candidates = versions[key]
        candidates = [event for _, event in ranked_candidates]

        def quality(event: dict[str, Any]) -> tuple[Any, ...]:
            markouts = sum(
                value is not None for value in _event_markouts(event).values()
            )
            populated = sum(value is not None for value in event.values())
            return (
                event.get("attribution_state") == "authenticated",
                markouts,
                isinstance(event.get("quote_context"), dict),
                _decimal(event.get("age_seconds")) or Decimal("-1"),
                populated,
                _stable_text(event),
            )

        merged: dict[str, Any] = {}
        for event in sorted(candidates, key=quality):
            merged.update(
                {name: value for name, value in event.items() if value is not None}
            )

        signatures = {
            (
                event.get("fill_role") or event.get("role"),
                event.get("episode_sequence"),
                event.get("active_unwind"),
            )
            for event in candidates
            if event.get("attribution_state") == "authenticated"
        }
        conflicted = (
            len(signatures) > 1
            or any(event.get("attribution_conflict") is True for event in candidates)
            or any(event.get("position_flip") is True for event in candidates)
        )
        latest = max(
            ranked_candidates,
            key=lambda item: (item[0], quality(item[1])),
        )[1]
        if conflicted:
            merged.update(
                attribution_state="pending",
                fill_role=None,
                episode_sequence=None,
                active_unwind=None,
            )
        elif (
            len(signatures) == 1
            and latest.get("attribution_state") == "authenticated"
        ):
            role, episode_sequence, active_unwind = next(iter(signatures))
            merged.update(
                attribution_state="authenticated",
                fill_role=role,
                episode_sequence=episode_sequence,
                active_unwind=active_unwind,
            )
        else:
            merged["attribution_state"] = "pending"
        merged_events.append(merged)
    return merged_events


def _merge_history(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        history = record.get("controller_decision_history")
        if not isinstance(history, list):
            continue
        for item in history:
            if not isinstance(item, dict):
                continue
            key = tuple(
                _stable_text(item.get(name))
                for name in (
                    "event_sequence",
                    "decision_id",
                    "recorded_monotonic",
                    "event",
                    "fill_order_id",
                    "mode",
                    "controller",
                )
            ) + (_stable_text(_event_sequence_run_id(item)),)
            current = merged.get(key)
            item_quality = (len(_stable_text(item)), _stable_text(item))
            current_quality = (
                (len(_stable_text(current)), _stable_text(current))
                if current is not None
                else None
            )
            if current_quality is None or item_quality > current_quality:
                merged[key] = dict(item)
    return [merged[key] for key in sorted(merged)]


def _quote_contexts(
    records: Iterable[dict[str, Any]], events: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    contexts: dict[tuple[str, ...], dict[str, Any]] = {}
    candidates: list[Any] = []
    for record in records:
        record_contexts = record.get("quote_contexts")
        if isinstance(record_contexts, list):
            candidates.extend(record_contexts)
    candidates.extend(event.get("quote_context") for event in events)
    for context in candidates:
        if not isinstance(context, dict):
            continue
        key = tuple(
            _stable_text(context.get(name))
            for name in (
                "order_id",
                "decision_id",
                "side",
                "created_monotonic",
            )
        ) + (_stable_text(_event_sequence_run_id(context)),)
        current = contexts.get(key)
        context_quality = (len(_stable_text(context)), _stable_text(context))
        current_quality = (
            (len(_stable_text(current)), _stable_text(current))
            if current is not None
            else None
        )
        if current_quality is None or context_quality > current_quality:
            contexts[key] = dict(context)
    return [contexts[key] for key in sorted(contexts)]


def _event_role(event: dict[str, Any]) -> str | None:
    if event.get("attribution_state") != "authenticated":
        return None
    role = event.get("fill_role") or event.get("role")
    return str(role) if role in _ROLES else None


def _event_markouts(event: dict[str, Any]) -> dict[str, Any]:
    """Return only own-size-subtracted markouts used for strategy analysis."""

    nested = event.get("external_mid_markouts")
    if isinstance(nested, dict):
        return nested
    return {
        match.group(1): value
        for key, value in event.items()
        if (
            match := re.fullmatch(
                r"external_mid_markout_(\d+)s_bps", str(key)
            )
        )
        and value is not None
    }


def _event_raw_markouts(event: dict[str, Any]) -> dict[str, Any]:
    """Return raw-book markouts for diagnostics, never promotion analysis."""

    markouts = {
        match.group(1): value
        for key, value in event.items()
        if (match := re.fullmatch(r"raw_mid_markout_(\d+)s_bps", str(key)))
        and value is not None
    }
    nested = event.get("markouts") or event.get("markout_bps")
    if isinstance(nested, dict):
        for horizon, value in nested.items():
            if value is not None:
                markouts.setdefault(str(horizon), value)
    for key, value in event.items():
        match = re.fullmatch(r"markout_(\d+)s_bps", str(key))
        if match and value is not None:
            markouts.setdefault(match.group(1), value)
    return markouts


def _markout_analysis(events: list[dict[str, Any]], legacy_pending: int) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    event_pending = 0
    external_reference_missing = 0
    sources: Counter[str] = Counter()
    for event in events:
        source = event.get("observation_source", event.get("source"))
        if source:
            sources[str(source)] += 1
        role = _event_role(event)
        side = str(event.get("side", "unknown")).lower()
        if role is None:
            event_pending += 1
            continue
        markouts = _event_markouts(event)
        if not markouts:
            external_reference_missing += 1
            continue
        role_group = grouped.setdefault(role, {}).setdefault(side, {})
        for horizon, value in markouts.items():
            if value is None:
                continue
            role_group.setdefault(str(horizon), []).append(value)
    summarized = {
        role: {
            side: {horizon: _describe(values) for horizon, values in horizons.items()}
            for side, horizons in sides.items()
        }
        for role, sides in grouped.items()
    }
    pending = max(legacy_pending, event_pending)
    return {
        "analysis_reference": "external_mid_own_size_subtracted",
        "authenticated_by_role_side_horizon": summarized,
        "entry_by_side_horizon": summarized.get("entry", {}),
        "risk_increasing_by_side_horizon": summarized.get(
            "risk_increasing", {}
        ),
        "exit_by_role_side_horizon": {
            role: summarized[role]
            for role in ("passive_exit", "active_exit")
            if role in summarized
        },
        "pending_attribution_count": pending,
        "external_reference_missing_count": external_reference_missing,
        "observation_source_distribution": dict(sorted(sources.items())),
    }


def _complete_policy_episode(episode: dict[str, Any]) -> bool:
    if episode.get("close_policy_coverage") is not True:
        return False
    if not isinstance(episode.get("session_id"), str) or not episode[
        "session_id"
    ].strip():
        return False
    sequence = episode.get("episode_sequence")
    if type(sequence) is not int or sequence <= 0:
        return False
    if episode.get("entry_side") not in {"buy", "sell"}:
        return False
    for field in ("entry_vwap", "exit_vwap", "quantity"):
        value = _decimal(episode.get(field))
        if value is None or value <= 0:
            return False
    for field in ("gross", "net_ex_funding"):
        if _decimal(episode.get(field)) is None:
            return False
    for field in (
        "maker_fee",
        "taker_fee",
        "inventory_duration_seconds",
        "passive_loss_used",
        "surplus_spent",
        "max_unlocked_episode_loss",
    ):
        value = _decimal(episode.get(field))
        if value is None or value < 0:
            return False
    if episode.get("final_exit_stage") not in {
        "strict_profit",
        "surplus_funded_passive",
        "bounded_passive_loss",
        "inventory_hold",
        "active_ioc",
        "flat_pending_audit",
        "completed",
    }:
        return False
    if episode.get("final_binding_constraint") not in {
        "normal_passive",
        "episode_cap",
        "session_surplus",
        "session_loss_cap",
        "drawdown_cap",
        "active_slippage",
        "attempt_cap",
        "data_untrusted",
    }:
        return False
    if "entered_inventory_hold" not in episode:
        return False
    entered_hold = episode["entered_inventory_hold"]
    if type(entered_hold) is not bool:
        return False
    active_attempts = episode.get("active_attempts")
    return type(active_attempts) is int and active_attempts >= 0


def _episode_analysis(
    episodes: Any, *, policy_context_missing_count: int = 0
) -> dict[str, Any]:
    if not isinstance(episodes, list):
        episodes = []
    valid = [episode for episode in episodes if isinstance(episode, dict)]
    covered = [episode for episode in valid if _complete_policy_episode(episode)]
    details = [
        {
            key: episode.get(key)
            for key in (
                "session_id",
                "episode_sequence",
                "entry_side",
                "entry_vwap",
                "exit_vwap",
                "quantity",
                "gross",
                "maker_fee",
                "taker_fee",
                "net_ex_funding",
                "inventory_duration_seconds",
                "final_exit_stage",
                "final_binding_constraint",
                "passive_loss_used",
                "surplus_spent",
                "max_unlocked_episode_loss",
                "entered_inventory_hold",
                "active_attempts",
                "close_policy_coverage",
            )
        }
        for episode in valid
    ]
    incomplete = len(valid) - len(covered)
    return {
        "count": len(valid),
        "gross": _describe(episode.get("gross") for episode in valid),
        "net": _describe(
            episode.get("net_ex_funding", episode.get("net")) for episode in valid
        ),
        "close_type_distribution": dict(
            Counter(
                str(episode.get("close_type", episode.get("final_close_lane", "unknown")))
                for episode in valid
            )
        ),
        "active_involvement_count": sum(
            1
            for episode in valid
            if episode.get("active_unwind_used") is True
            or episode.get("active_involvement") is True
        ),
        "policy_covered_count": len(covered),
        "policy_incomplete_count": incomplete,
        "policy_context_missing_count": policy_context_missing_count,
        "promotion_eligible": bool(covered)
        and incomplete == 0
        and policy_context_missing_count == 0,
        "promotion_episode_count": len(covered),
        "promotion_gross": _describe(
            episode.get("gross") for episode in covered
        ),
        "promotion_net": _describe(
            episode.get("net_ex_funding", episode.get("net"))
            for episode in covered
        ),
        "final_exit_stage_distribution": dict(
            Counter(
                str(episode.get("final_exit_stage", "unknown"))
                for episode in covered
            )
        ),
        "binding_constraint_distribution": dict(
            Counter(
                str(episode.get("final_binding_constraint", "unknown"))
                for episode in covered
            )
        ),
        "decomposition": {
            "entry_quality": {
                "entry_vwap": _describe(
                    episode.get("entry_vwap") for episode in covered
                ),
                "entry_side_distribution": dict(
                    Counter(
                        str(episode.get("entry_side", "unknown"))
                        for episode in covered
                    )
                ),
            },
            "inventory_drift": {
                "duration_seconds": _describe(
                    episode.get("inventory_duration_seconds")
                    for episode in covered
                ),
                "entered_hold_count": sum(
                    episode.get("entered_inventory_hold") is True
                    for episode in covered
                ),
            },
            "exit_concession": {
                "exit_vwap": _describe(
                    episode.get("exit_vwap") for episode in covered
                ),
                "passive_loss_used": _describe(
                    episode.get("passive_loss_used")
                    for episode in covered
                ),
                "surplus_spent": _describe(
                    episode.get("surplus_spent") for episode in covered
                ),
            },
            "fee": {
                "maker": _describe(
                    episode.get("maker_fee") for episode in covered
                ),
                "taker": _describe(
                    episode.get("taker_fee") for episode in covered
                ),
            },
            "final_net": _describe(
                episode.get("net_ex_funding", episode.get("net"))
                for episode in covered
            ),
        },
        "episode_details": details,
    }


def _episode_identity(episode: dict[str, Any]) -> tuple[str, str] | None:
    session_id = episode.get("session_id")
    sequence = episode.get("episode_sequence")
    if session_id is None or not str(session_id).strip() or sequence is None:
        return None
    return str(session_id), _stable_text(sequence)


def _merge_episodes(
    records: list[dict[str, Any]], final: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    versions: dict[
        tuple[str, str], list[tuple[tuple[Any, ...], dict[str, Any]]]
    ] = {}
    for record in records:
        audit = record.get("account_audit")
        if not isinstance(audit, dict):
            continue
        ledger = audit.get("completed_episode_ledger")
        if not isinstance(ledger, list):
            continue
        for episode in ledger:
            if not isinstance(episode, dict):
                continue
            identity = _episode_identity(episode)
            if identity is not None:
                versions.setdefault(identity, []).append(
                    (_record_rank(record), episode)
                )

    identified = {
        identity: dict(
            max(
                candidates,
                key=lambda item: (
                    item[0],
                    sum(value is not None for value in item[1].values()),
                    _stable_text(item[1]),
                ),
            )[1]
        )
        for identity, candidates in versions.items()
    }

    final_audit = final.get("account_audit")
    if not isinstance(final_audit, dict):
        final_audit = {}
    final_ledger = final_audit.get("completed_episode_ledger")
    legacy = (
        [
            dict(episode)
            for episode in final_ledger
            if isinstance(episode, dict) and _episode_identity(episode) is None
        ]
        if isinstance(final_ledger, list)
        else []
    )
    episodes = [identified[key] for key in sorted(identified)] + legacy
    return episodes, {
        "merged_unique_episodes": len(episodes),
        "identified_episodes": len(identified),
        "legacy_final_snapshot_episodes": len(legacy),
    }


def _episode_fee_total(episodes: Any, key: str) -> Decimal | None:
    if not isinstance(episodes, list):
        return None
    values = [
        value
        for episode in episodes
        if isinstance(episode, dict)
        and (value := _decimal(episode.get(key))) is not None
    ]
    return sum(values, Decimal("0")) if values else None


def _controller_analysis(
    records: list[dict[str, Any]],
    history: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    decisions = list(history)
    for record in records:
        current = record.get("quote_controller")
        if isinstance(current, dict) and current:
            decisions.append(current)

    scores: dict[str, dict[tuple[str, ...], Decimal]] = {
        "bid": {},
        "ask": {},
    }
    feature_samples: dict[tuple[str, str], dict[str, Any]] = {}
    feature_health: dict[tuple[str, str], str] = {}

    def decision_key(item: dict[str, Any]) -> str:
        decision_id = item.get("decision_id")
        return (
            f"decision:{decision_id}"
            if decision_id is not None
            else f"content:{_stable_text(item)}"
        )

    def add_features(key: str, features: Any, health: Any = None) -> None:
        if isinstance(features, dict):
            sample_key = (key, _stable_text(features))
            feature_samples[sample_key] = features
            health = features.get("health", health)
        if health is not None:
            feature_health[(key, str(health))] = str(health)

    for item in decisions:
        key = decision_key(item)
        for side in ("bid", "ask"):
            nested = item.get(side)
            value = (
                nested.get("toxicity_score_ticks")
                if isinstance(nested, dict)
                else item.get(f"{side}_score_ticks")
            )
            if (decimal_value := _decimal(value)) is not None:
                scores[side][(key, str(decimal_value))] = decimal_value
        features = item.get("feature_snapshot", item.get("features"))
        add_features(key, features, item.get("feature_health"))

    for context in contexts:
        side = {"buy": "bid", "sell": "ask"}.get(
            str(context.get("side", "")).lower()
        )
        key = decision_key(context)
        if side is not None and (
            score := _decimal(context.get("toxicity_score_ticks"))
        ) is not None:
            scores[side][(key, str(score))] = score
        add_features(key, context.get("feature_snapshot"))

    for record in records:
        features = record.get("controller_feature_snapshot")
        if isinstance(features, dict) and features:
            add_features(f"current:{_stable_text(features)}", features)

    return {
        "available": bool(decisions or contexts or feature_samples),
        "decision_count": len(history),
        "placement_count": len(contexts),
        "bid_score_ticks": _describe(scores["bid"].values()),
        "ask_score_ticks": _describe(scores["ask"].values()),
        "feature_distributions": {
            name: _describe(
                sample.get(name) for sample in feature_samples.values()
            )
            for name in _FEATURE_FIELDS
        },
        "feature_health_distribution": dict(
            sorted(Counter(feature_health.values()).items())
        ),
    }


def _later_shadow_change(
    event: dict[str, Any],
    context: dict[str, Any],
    history: list[dict[str, Any]],
) -> str | None:
    created = _decimal(context.get("created_monotonic"))
    filled = _decimal(event.get("started_monotonic"))
    side = {"buy": "bid", "sell": "ask"}.get(
        str(event.get("side", "")).lower()
    )
    if side is None:
        return None

    placement_sequence = _decimal(context.get("placement_event_sequence"))
    fill_sequence = _decimal(event.get("fill_observation_event_sequence"))
    placement_run_id = _event_sequence_run_id(context)
    fill_run_id = _event_sequence_run_id(event)
    decision_history = [
        decision
        for decision in history
        if decision.get("event") != "maker_fill"
    ]
    sequence_fields_present = (
        "placement_event_sequence" in context
        or "fill_observation_event_sequence" in event
        or any("event_sequence" in decision for decision in decision_history)
    )
    if sequence_fields_present:
        if (
            placement_sequence is None
            or fill_sequence is None
            or placement_run_id is None
            or fill_run_id != placement_run_id
        ):
            return "event_sequence_incomplete"
        if placement_sequence <= 0 or fill_sequence <= placement_sequence:
            return "event_sequence_invalid"
    elif created is None or filled is None or filled < created:
        return None

    placement_extra = _decimal(context.get("extra_spread_ticks"))
    placement_shadow = _decimal(context.get("shadow_price"))
    changes: list[tuple[Decimal, str]] = []
    for decision in decision_history:
        if sequence_fields_present:
            recorded = _decimal(decision.get("event_sequence"))
            decision_run_id = _event_sequence_run_id(decision)
            if recorded is None or decision_run_id is None:
                return "event_sequence_incomplete"
            if decision_run_id != placement_run_id:
                continue
            if not (placement_sequence < recorded < fill_sequence):
                continue
        else:
            recorded = _decimal(decision.get("recorded_monotonic"))
            if recorded is None or not (created < recorded <= filled):
                continue
        if decision.get("ready") is False or decision.get("error") is not None:
            changes.append((recorded, "later_shadow_unavailable"))
            continue
        if decision.get("entry_applicable") is False:
            changes.append((recorded, "later_entry_inapplicable"))
            continue
        side_decision = decision.get(side)
        if not isinstance(side_decision, dict):
            continue
        if side_decision.get("blocked") is True:
            changes.append((recorded, "later_shadow_block"))
            continue
        if "shadow_price" in side_decision and (
            later_shadow := _decimal(side_decision.get("shadow_price"))
        ) != placement_shadow:
            changes.append((recorded, "later_shadow_reprice"))
            continue
        later_extra = _decimal(side_decision.get("extra_spread_ticks"))
        if (
            placement_extra is not None
            and later_extra is not None
            and later_extra != placement_extra
        ):
            changes.append((recorded, "later_shadow_reprice"))
    return min(changes)[1] if changes else None


def _virtual_shadow_lifecycle(
    event: dict[str, Any],
    context: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    history_complete: bool,
) -> dict[str, Any] | None:
    """Fold same-run shadow decisions without simulating queue priority."""

    decision_history = [
        decision
        for decision in history
        if decision.get("event") != "maker_fill"
    ]
    sequence_fields_present = (
        "placement_event_sequence" in context
        or "fill_observation_event_sequence" in event
        or _event_sequence_run_id(context) is not None
        or _event_sequence_run_id(event) is not None
        or any("event_sequence" in decision for decision in decision_history)
    )
    if not sequence_fields_present:
        return None

    counts: Counter[str] = Counter()

    def result(
        *,
        complete: bool,
        reason: str | None,
        live: bool | None = None,
        price: Decimal | None = None,
    ) -> dict[str, Any]:
        return {
            "complete": complete,
            "reason": reason,
            "virtual_live_at_fill": live,
            "virtual_price_at_fill": price if live is True else None,
            "action_counts": {
                action: counts.get(action, 0)
                for action in _VIRTUAL_SHADOW_ACTIONS
            },
        }

    if not history_complete:
        return result(complete=False, reason="controller_history_incomplete")

    placement_sequence = _decimal(context.get("placement_event_sequence"))
    fill_sequence = _decimal(event.get("fill_observation_event_sequence"))
    placement_run_id = _event_sequence_run_id(context)
    fill_run_id = _event_sequence_run_id(event)
    if (
        placement_sequence is None
        or fill_sequence is None
        or placement_run_id is None
        or fill_run_id != placement_run_id
    ):
        return result(complete=False, reason="event_sequence_incomplete")
    if placement_sequence <= 0 or fill_sequence <= placement_sequence:
        return result(complete=False, reason="event_sequence_invalid")
    if "shadow_price" not in context:
        return result(complete=False, reason="event_sequence_incomplete")

    initial_shadow = context.get("shadow_price")
    virtual_price = _decimal(initial_shadow)
    if initial_shadow is not None and virtual_price is None:
        return result(complete=False, reason="event_sequence_incomplete")
    virtual_live = virtual_price is not None
    if virtual_live:
        counts["would_place"] += 1
    else:
        counts["would_block"] += 1

    decisions: list[tuple[Decimal, dict[str, Any]]] = []
    for decision in decision_history:
        decision_run_id = _event_sequence_run_id(decision)
        if decision_run_id is None:
            return result(complete=False, reason="event_sequence_incomplete")
        if decision_run_id != placement_run_id:
            continue
        recorded = _decimal(decision.get("event_sequence"))
        if recorded is None or recorded <= 0:
            return result(complete=False, reason="event_sequence_incomplete")
        if placement_sequence < recorded < fill_sequence:
            decisions.append((recorded, decision))
    decisions.sort(key=lambda item: item[0])
    if any(
        earlier[0] >= later[0]
        for earlier, later in zip(decisions, decisions[1:])
    ):
        return result(complete=False, reason="event_sequence_invalid")

    side = {"buy": "bid", "sell": "ask"}.get(
        str(event.get("side", "")).lower()
    )
    if side is None:
        return result(complete=False, reason="insufficient_evidence")

    for _, decision in decisions:
        available = (
            decision.get("ready") is True
            and decision.get("error") is None
            and decision.get("entry_applicable") is True
        )
        if not available:
            if virtual_live:
                counts["would_cancel"] += 1
            virtual_live = False
            virtual_price = None
            continue

        side_decision = decision.get(side)
        if not isinstance(side_decision, dict):
            return result(complete=False, reason="event_sequence_incomplete")
        if side_decision.get("blocked") is True:
            if virtual_live:
                counts["would_block"] += 1
                counts["would_cancel"] += 1
            virtual_live = False
            virtual_price = None
            continue
        if side_decision.get("blocked") is not False:
            return result(complete=False, reason="event_sequence_incomplete")
        if "shadow_price" not in side_decision:
            return result(complete=False, reason="event_sequence_incomplete")
        next_price = _decimal(side_decision.get("shadow_price"))
        if next_price is None:
            return result(complete=False, reason="event_sequence_incomplete")
        if virtual_live:
            if next_price != virtual_price:
                counts["would_reprice"] += 1
                virtual_price = next_price
        else:
            counts["would_place"] += 1
            counts["would_resume"] += 1
            virtual_live = True
            virtual_price = next_price

    return result(
        complete=True,
        reason=None,
        live=virtual_live,
        price=virtual_price,
    )


def _counterfactual(
    events: list[dict[str, Any]],
    pending: int,
    history: list[dict[str, Any]],
    *,
    history_complete: bool,
    history_complete_by_run: dict[str, bool] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    lifecycle_actions: Counter[str] = Counter()
    lifecycle_fill_states: Counter[str] = Counter()
    represented_pending = 0
    for event in events:
        context = event.get("quote_context")
        role = _event_role(event)
        authenticated_entry = (
            role == "entry" and event.get("active_unwind") is False
        )
        exclusion_reason = None
        if role is None:
            represented_pending += 1
            exclusion_reason = "pending_or_unauthenticated"
        elif role == "passive_exit":
            exclusion_reason = "passive_exit"
        elif role == "active_exit" or event.get("active_unwind") is True:
            exclusion_reason = "active_exit"
        elif role == "entry" and not authenticated_entry:
            exclusion_reason = "pending_or_unauthenticated"
        elif role != "entry":
            exclusion_reason = role
        if exclusion_reason is not None:
            excluded[exclusion_reason] += 1
        label = "indeterminate" if authenticated_entry else "excluded"
        side = str(event.get("side", "")).lower()
        fill_price = _decimal(event.get("fill_price", event.get("price")))
        base = (
            _decimal(context.get("base_price"))
            if isinstance(context, dict)
            else None
        )
        shadow = (
            _decimal(context.get("shadow_price"))
            if isinstance(context, dict)
            else None
        )
        run_id = _event_sequence_run_id(event)
        event_history_complete = (
            history_complete_by_run.get(run_id, False)
            if run_id is not None and history_complete_by_run is not None
            else history_complete
        )
        lifecycle = (
            _virtual_shadow_lifecycle(
                event,
                context,
                history,
                history_complete=event_history_complete,
            )
            if authenticated_entry and isinstance(context, dict)
            else None
        )
        later_change = (
            _later_shadow_change(event, context, history)
            if authenticated_entry
            and isinstance(context, dict)
            and lifecycle is None
            else None
        )
        farther: bool | None = None
        if (
            authenticated_entry
            and side in {"buy", "sell"}
            and base is not None
            and isinstance(context, dict)
            and "shadow_price" in context
        ):
            farther = (
                True
                if shadow is None
                else (side == "buy" and shadow < base)
                or (side == "sell" and shadow > base)
            )

        classification_reason = None
        if exclusion_reason is not None:
            classification_reason = f"excluded_{exclusion_reason}"
        elif lifecycle is not None:
            if lifecycle["complete"] is not True:
                label = "indeterminate"
                classification_reason = lifecycle["reason"]
            elif lifecycle["virtual_live_at_fill"] is False:
                label = "likely_filtered"
                classification_reason = "virtual_not_live_at_fill"
            else:
                virtual_price = lifecycle["virtual_price_at_fill"]
                reachable = (
                    fill_price is not None
                    and virtual_price is not None
                    and (
                        (side == "buy" and fill_price <= virtual_price)
                        or (side == "sell" and fill_price >= virtual_price)
                    )
                )
                if reachable:
                    label = "still_reachable"
                    classification_reason = "virtual_live_reachable_at_fill"
                elif fill_price is not None and virtual_price is not None:
                    label = "likely_filtered"
                    classification_reason = "virtual_live_price_not_reached"
                else:
                    label = "indeterminate"
                    classification_reason = "insufficient_evidence"
        else:
            if authenticated_entry and side in {"buy", "sell"}:
                if (
                    fill_price is not None
                    and base is not None
                    and shadow is None
                    and isinstance(context, dict)
                    and "shadow_price" in context
                ):
                    label = "likely_filtered"
                elif (
                    fill_price is not None
                    and base is not None
                    and shadow is not None
                ):
                    still_reachable = (
                        side == "buy" and fill_price <= shadow
                    ) or (side == "sell" and fill_price >= shadow)
                    if still_reachable:
                        label = "still_reachable"
                    elif farther:
                        label = "likely_filtered"
            if later_change is not None:
                label = "indeterminate"
                classification_reason = later_change
            elif authenticated_entry and not event_history_complete:
                label = "indeterminate"
                classification_reason = "controller_history_incomplete"
            elif label != "indeterminate":
                classification_reason = "placement_shadow_proxy"
            else:
                classification_reason = "insufficient_evidence"

        action_counts = {
            action: 0 for action in _VIRTUAL_SHADOW_ACTIONS
        }
        virtual_lifecycle_complete: bool | None = None
        virtual_lifecycle_reason = None
        virtual_live_at_fill = None
        virtual_price_at_fill = None
        if authenticated_entry:
            if lifecycle is None:
                virtual_lifecycle_complete = False
                virtual_lifecycle_reason = "legacy_timestamp_proxy"
                lifecycle_fill_states["unknown"] += 1
            else:
                virtual_lifecycle_complete = lifecycle["complete"]
                virtual_lifecycle_reason = lifecycle["reason"]
                action_counts = dict(lifecycle["action_counts"])
                virtual_live_at_fill = lifecycle["virtual_live_at_fill"]
                virtual_price_at_fill = lifecycle["virtual_price_at_fill"]
                if lifecycle["complete"] is True:
                    lifecycle_actions.update(action_counts)
                    lifecycle_fill_states[
                        "live" if virtual_live_at_fill else "not_live"
                    ] += 1
                else:
                    lifecycle_fill_states["unknown"] += 1
        if authenticated_entry:
            counts[label] += 1
        rows.append(
            {
                "order_id": event.get("order_id"),
                "side": event.get("side"),
                "attribution_state": event.get("attribution_state", "pending"),
                "fill_role": role,
                "active_unwind": event.get("active_unwind"),
                "eligible_authenticated_entry": authenticated_entry,
                "actual_fill_price": fill_price,
                "base_price": base,
                "shadow_price": shadow,
                "shadow_farther": farther,
                "virtual_lifecycle_complete": virtual_lifecycle_complete,
                "virtual_lifecycle_reason": virtual_lifecycle_reason,
                "virtual_live_at_fill": virtual_live_at_fill,
                "virtual_price_at_fill": virtual_price_at_fill,
                "virtual_lifecycle_action_counts": action_counts,
                "classification": label,
                "classification_reason": classification_reason,
                "markouts": _event_markouts(event),
                "raw_markouts": _event_raw_markouts(event),
            }
        )
    missing_pending = max(0, pending - represented_pending)
    if missing_pending:
        excluded["pending_or_unauthenticated"] += missing_pending
    classification_counts = {
        label: counts.get(label, 0)
        for label in ("likely_filtered", "still_reachable", "indeterminate")
    }
    authenticated_entry_count = sum(classification_counts.values())
    return {
        "method": "counterfactual_proxy_with_virtual_lifecycle_not_backtest",
        "denominator_unit": "observed_authenticated_entry_fill_delta",
        "authenticated_entry_count": authenticated_entry_count,
        "classifiable_entry_count": (
            classification_counts["likely_filtered"]
            + classification_counts["still_reachable"]
        ),
        "indeterminate_entry_count": classification_counts["indeterminate"],
        "excluded_fill_counts": {
            label: excluded.get(label, 0)
            for label in (
                "passive_exit",
                "active_exit",
                "risk_increasing",
                "pending_or_unauthenticated",
            )
        },
        "classification_counts": classification_counts,
        "virtual_lifecycle_action_counts": {
            action: lifecycle_actions.get(action, 0)
            for action in _VIRTUAL_SHADOW_ACTIONS
        },
        "virtual_live_at_fill_count": lifecycle_fill_states.get("live", 0),
        "virtual_not_live_at_fill_count": lifecycle_fill_states.get(
            "not_live", 0
        ),
        "virtual_unknown_at_fill_count": lifecycle_fill_states.get(
            "unknown", 0
        ),
        "fills": rows,
    }


def analyze(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = [record for record in records if isinstance(record, dict)]
    if not records:
        raise ValueError("no metrics snapshots found")
    final = max(records, key=_record_rank)
    audit = final.get("account_audit", {})
    if not isinstance(audit, dict):
        audit = {}
    events = _merge_events(records)
    history = _merge_history(records)
    contexts = _quote_contexts(records, events)
    legacy_pending = max(
        (
            int(record.get("legacy_pending_attribution", 0) or 0)
            for record in records
        ),
        default=0,
    )
    markouts = _markout_analysis(events, legacy_pending)
    legacy_reported_controller_history_total = max(
        (
            int(record.get("controller_decision_history_total", 0) or 0)
            for record in records
            if _event_sequence_run_id(record) is None
        ),
        default=0,
    )
    reported_controller_history_by_run: dict[str, int] = {}
    for record in records:
        run_id = _event_sequence_run_id(record)
        if run_id is None:
            continue
        reported_controller_history_by_run[run_id] = max(
            reported_controller_history_by_run.get(run_id, 0),
            int(record.get("controller_decision_history_total", 0) or 0),
        )
    merged_controller_history_by_run = Counter(
        run_id
        for item in history
        if (run_id := _event_sequence_run_id(item)) is not None
    )
    controller_history_complete_by_run = {
        run_id: (
            reported_total == 0
            or merged_controller_history_by_run.get(run_id, 0)
            >= reported_total
        )
        for run_id, reported_total in reported_controller_history_by_run.items()
    }
    reported_controller_history_total = (
        legacy_reported_controller_history_total
        + sum(reported_controller_history_by_run.values())
    )
    controller_history_complete = (
        (
            legacy_reported_controller_history_total == 0
            or sum(
                _event_sequence_run_id(item) is None for item in history
            )
            >= legacy_reported_controller_history_total
        )
        and all(controller_history_complete_by_run.values())
    )
    counterfactual = _counterfactual(
        events,
        markouts["pending_attribution_count"],
        history,
        history_complete=controller_history_complete,
        history_complete_by_run=controller_history_complete_by_run,
    )
    episodes, episode_coverage = _merge_episodes(records, final)
    session_keys = (
        "unique_maker_fills",
        "unique_taker_fills",
        "completed_fills",
        "completed_round_trips",
        "completed_turnover",
        "completed_gross",
        "completed_exact_fee",
        "completed_net_ex_funding",
        "completed_net_turnover_bps",
        "completed_fee_cover_ratio",
        "economic_state",
        "seen_trade_id_registry_size",
        "seen_trade_id_evictions",
        "order_role_binding_registry_size",
        "order_role_binding_evictions",
        "policy_context_missing_count",
    )
    session = {key: audit.get(key) for key in session_keys}
    session["completed_maker_fee"] = audit.get(
        "completed_maker_fee",
        _episode_fee_total(episodes, "maker_fee"),
    )
    session["completed_taker_fee"] = audit.get(
        "completed_taker_fee",
        _episode_fee_total(episodes, "taker_fee"),
    )
    ready_seconds = _decimal(final.get("controller_ready_seconds"))
    both_blocked_seconds = _decimal(final.get("controller_both_blocked_seconds"))
    shadow_active_seconds = None
    if ready_seconds is not None:
        shadow_active_seconds = max(
            Decimal("0"),
            ready_seconds - (both_blocked_seconds or Decimal("0")),
        )
    reported_retained = max(
        (
            int(coverage.get("retained_events", 0) or 0)
            for record in records
            if isinstance((coverage := record.get("fill_markout_coverage")), dict)
        ),
        default=0,
    )
    legacy_sources = sorted(
        {
            str(source)
            for record in records
            if (source := record.get("legacy_source")) is not None
        }
    )
    return {
        "analysis_contract": {
            "local_read_only": True,
            "counterfactual_is_proxy": True,
            "true_queue_backtest": False,
        },
        "snapshot_count": len(records),
        "session": session,
        "episodes": _episode_analysis(
            episodes,
            policy_context_missing_count=int(
                audit.get("policy_context_missing_count", 0) or 0
            ),
        ),
        "markouts": markouts,
        "controller": _controller_analysis(records, history, contexts),
        "shadow_counterfactual": counterfactual,
        "volume_retention_proxy": {
            "shadow_active_quote_seconds": shadow_active_seconds,
            "controller_ready_seconds": final.get("controller_ready_seconds"),
            "bid_blocked_seconds": final.get("controller_bid_blocked_seconds"),
            "ask_blocked_seconds": final.get("controller_ask_blocked_seconds"),
            "both_blocked_seconds": final.get("controller_both_blocked_seconds"),
            "likely_filtered_fills": counterfactual["classification_counts"].get(
                "likely_filtered", 0
            ),
        },
        "coverage": {
            "retained_events": max(len(events), reported_retained),
            "merged_unique_events": len(events),
            "pending_attribution": markouts["pending_attribution_count"],
            "external_reference_missing": markouts[
                "external_reference_missing_count"
            ],
            "controller_history_merged": len(history),
            "controller_history_reported_total": (
                reported_controller_history_total
            ),
            "controller_history_complete": controller_history_complete,
            **episode_coverage,
            "observation_sources": markouts["observation_source_distribution"],
            "legacy_source": (
                legacy_sources[0]
                if len(legacy_sources) == 1
                else legacy_sources or None
            ),
        },
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, default=_json_default, indent=2, sort_keys=True)


def render_markdown(report: dict[str, Any]) -> str:
    session = report["session"]
    episodes = report["episodes"]
    markouts = report["markouts"]
    controller = report["controller"]
    counterfactual = report["shadow_counterfactual"]
    volume = report["volume_retention_proxy"]
    coverage = report["coverage"]
    counts = counterfactual["classification_counts"]
    excluded = counterfactual["excluded_fill_counts"]
    lifecycle_actions = counterfactual["virtual_lifecycle_action_counts"]
    lines = [
        "# Market Maker strategy analysis",
        "",
        "> Read-only local evidence. Shadow classifications are proxies, not a true queue-fill backtest.",
        "> Strategy markout summaries use the own-size-subtracted external mid; raw-book markouts are diagnostic only.",
        "",
        "## Session",
        "",
        f"- Maker / taker fills: {session.get('unique_maker_fills')} / "
        f"{session.get('unique_taker_fills')}",
        f"- Completed fills / round trips: {session.get('completed_fills')} / "
        f"{session.get('completed_round_trips')}",
        f"- Completed turnover: {session.get('completed_turnover')}",
        f"- Completed gross / total fee / net: {session.get('completed_gross')} / "
        f"{session.get('completed_exact_fee')} / {session.get('completed_net_ex_funding')}",
        f"- Completed maker / taker fee: {session.get('completed_maker_fee')} / "
        f"{session.get('completed_taker_fee')}",
        f"- Net turnover bps / fee cover: {session.get('completed_net_turnover_bps')} / "
        f"{session.get('completed_fee_cover_ratio')}",
        f"- Trade-ID registry size / evictions: "
        f"{session.get('seen_trade_id_registry_size')} / "
        f"{session.get('seen_trade_id_evictions')}",
        f"- Order-role registry size / evictions: "
        f"{session.get('order_role_binding_registry_size')} / "
        f"{session.get('order_role_binding_evictions')}",
        "",
        "## Episodes",
        "",
        f"- Count / active involvement: {episodes.get('count')} / "
        f"{episodes.get('active_involvement_count')}",
        f"- Gross distribution: {episodes.get('gross')}",
        f"- Net distribution: {episodes.get('net')}",
        f"- Close types: {episodes.get('close_type_distribution')}",
        f"- Policy covered / incomplete / missing contexts: "
        f"{episodes.get('policy_covered_count')} / "
        f"{episodes.get('policy_incomplete_count')} / "
        f"{episodes.get('policy_context_missing_count')}",
        f"- Promotion eligible / episode count: "
        f"{episodes.get('promotion_eligible')} / "
        f"{episodes.get('promotion_episode_count')}",
        f"- Exit stages / binding constraints: "
        f"{episodes.get('final_exit_stage_distribution')} / "
        f"{episodes.get('binding_constraint_distribution')}",
        f"- Entry→drift→exit→fee→net decomposition: "
        f"{episodes.get('decomposition')}",
        "",
        "## Authenticated entry markouts",
        "",
        f"- Analysis reference: {markouts.get('analysis_reference')}",
        f"- By side / horizon: {markouts.get('entry_by_side_horizon')}",
        "",
        "## Exit markouts",
        "",
        f"- By role / side / horizon: {markouts.get('exit_by_role_side_horizon')}",
        f"- Risk-increasing by side / horizon: "
        f"{markouts.get('risk_increasing_by_side_horizon')}",
        "",
        "## Controller",
        "",
        f"- Available / decision transitions / placements: {controller.get('available')} / "
        f"{controller.get('decision_count')} / {controller.get('placement_count')}",
        f"- Bid score distribution: {controller.get('bid_score_ticks')}",
        f"- Ask score distribution: {controller.get('ask_score_ticks')}",
        f"- Feature health: {controller.get('feature_health_distribution')}",
        f"- Feature distributions: {controller.get('feature_distributions')}",
        "",
        "## Shadow counterfactual proxy",
        "",
        f"- Authenticated entries / classifiable / indeterminate: "
        f"{counterfactual.get('authenticated_entry_count')} / "
        f"{counterfactual.get('classifiable_entry_count')} / "
        f"{counterfactual.get('indeterminate_entry_count')}",
        f"- Excluded passive / active / risk-increasing / pending: "
        f"{excluded.get('passive_exit', 0)} / {excluded.get('active_exit', 0)} / "
        f"{excluded.get('risk_increasing', 0)} / "
        f"{excluded.get('pending_or_unauthenticated', 0)}",
        f"- Likely filtered / still reachable / indeterminate: "
        f"{counts.get('likely_filtered', 0)} / {counts.get('still_reachable', 0)} / "
        f"{counts.get('indeterminate', 0)}",
        f"- Virtual actions place / reprice / block / cancel / resume: "
        f"{lifecycle_actions.get('would_place', 0)} / "
        f"{lifecycle_actions.get('would_reprice', 0)} / "
        f"{lifecycle_actions.get('would_block', 0)} / "
        f"{lifecycle_actions.get('would_cancel', 0)} / "
        f"{lifecycle_actions.get('would_resume', 0)}",
        f"- Virtual live / not-live / unknown at fill: "
        f"{counterfactual.get('virtual_live_at_fill_count', 0)} / "
        f"{counterfactual.get('virtual_not_live_at_fill_count', 0)} / "
        f"{counterfactual.get('virtual_unknown_at_fill_count', 0)}",
    ]
    for row in counterfactual.get("fills", []):
        lines.append(
            f"- {row.get('order_id')} {row.get('side')}: "
            f"fill={row.get('actual_fill_price')}, base={row.get('base_price')}, "
            f"shadow={row.get('shadow_price')}, farther={row.get('shadow_farther')}, "
            f"virtual_live={row.get('virtual_live_at_fill')}, "
            f"virtual_price={row.get('virtual_price_at_fill')}, "
            f"virtual_actions={row.get('virtual_lifecycle_action_counts')}, "
            f"classification={row.get('classification')}, "
            f"reason={row.get('classification_reason')}, "
            f"external_markouts={row.get('markouts')}"
        )
    lines.extend(
        [
            "",
            "## Volume retention proxy",
            "",
            f"- Shadow active quote / bid blocked / ask blocked / both blocked seconds: "
            f"{volume.get('shadow_active_quote_seconds')} / "
            f"{volume.get('bid_blocked_seconds')} / {volume.get('ask_blocked_seconds')} / "
            f"{volume.get('both_blocked_seconds')}",
            f"- Likely filtered fills: {volume.get('likely_filtered_fills')}",
            "",
            "## Coverage and pending attribution",
            "",
            f"- Retained / merged unique / pending events: {coverage.get('retained_events')} / "
            f"{coverage.get('merged_unique_events')} / {coverage.get('pending_attribution')}",
            f"- External-reference-missing events: "
            f"{coverage.get('external_reference_missing')}",
            f"- Controller history merged / reported / complete: "
            f"{coverage.get('controller_history_merged')} / "
            f"{coverage.get('controller_history_reported_total')} / "
            f"{coverage.get('controller_history_complete')}",
            f"- Merged / identified / legacy-final episodes: "
            f"{coverage.get('merged_unique_episodes')} / "
            f"{coverage.get('identified_episodes')} / "
            f"{coverage.get('legacy_final_snapshot_episodes')}",
            f"- Observation sources: {coverage.get('observation_sources')}",
            f"- Legacy source: {coverage.get('legacy_source')}",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="local metrics JSON/JSONL/log/summary")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)

    report = analyze(load_local_records(args.inputs))
    json_text = render_json(report)
    markdown_text = render_markdown(report)
    if args.json_output:
        args.json_output.write_text(json_text + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(markdown_text, encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(json_text)
        print()
        print(markdown_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
