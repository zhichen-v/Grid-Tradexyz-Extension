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
                    "decision_id",
                    "recorded_monotonic",
                    "event",
                    "fill_order_id",
                    "mode",
                    "controller",
                )
            )
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
        )
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


def _episode_analysis(episodes: Any) -> dict[str, Any]:
    if not isinstance(episodes, list):
        episodes = []
    valid = [episode for episode in episodes if isinstance(episode, dict)]
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
    if created is None or filled is None or side is None or filled < created:
        return None

    placement_extra = _decimal(context.get("extra_spread_ticks"))
    placement_shadow = _decimal(context.get("shadow_price"))
    changes: list[tuple[Decimal, str]] = []
    for decision in history:
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


def _counterfactual(
    events: list[dict[str, Any]],
    pending: int,
    history: list[dict[str, Any]],
    *,
    history_complete: bool,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    represented_pending = 0
    for event in events:
        context = event.get("quote_context")
        label = "indeterminate"
        role = _event_role(event)
        authenticated_entry = (
            role == "entry" and event.get("active_unwind") is False
        )
        if role is None:
            represented_pending += 1
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
        later_change = (
            _later_shadow_change(event, context, history)
            if authenticated_entry and isinstance(context, dict)
            else None
        )
        farther: bool | None = None
        if authenticated_entry and side in {"buy", "sell"}:
            if (
                fill_price is not None
                and base is not None
                and shadow is None
                and isinstance(context, dict)
                and "shadow_price" in context
            ):
                farther = True
                label = "likely_filtered"
            elif fill_price is not None and base is not None and shadow is not None:
                farther = (side == "buy" and shadow < base) or (
                    side == "sell" and shadow > base
                )
                still_reachable = (side == "buy" and fill_price <= shadow) or (
                    side == "sell" and fill_price >= shadow
                )
                if still_reachable:
                    label = "still_reachable"
                elif farther:
                    label = "likely_filtered"
        if later_change is not None:
            label = "indeterminate"
        elif authenticated_entry and not history_complete:
            label = "indeterminate"
            later_change = "controller_history_incomplete"
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
                "classification": label,
                "classification_reason": later_change
                or (
                    "placement_shadow_proxy"
                    if label != "indeterminate"
                    else "insufficient_or_ineligible_evidence"
                ),
                "markouts": _event_markouts(event),
                "raw_markouts": _event_raw_markouts(event),
            }
        )
    missing_pending = max(0, pending - represented_pending)
    if missing_pending:
        counts["indeterminate"] += missing_pending
    return {
        "method": "counterfactual_proxy_with_lifecycle_guard_not_backtest",
        "classification_counts": dict(counts),
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
    reported_controller_history_total = max(
        (
            int(record.get("controller_decision_history_total", 0) or 0)
            for record in records
        ),
        default=0,
    )
    controller_history_complete = (
        reported_controller_history_total == 0
        or len(history) >= reported_controller_history_total
    )
    counterfactual = _counterfactual(
        events,
        markouts["pending_attribution_count"],
        history,
        history_complete=controller_history_complete,
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
        "episodes": _episode_analysis(episodes),
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
        f"- Likely filtered / still reachable / indeterminate: "
        f"{counts.get('likely_filtered', 0)} / {counts.get('still_reachable', 0)} / "
        f"{counts.get('indeterminate', 0)}",
    ]
    for row in counterfactual.get("fills", []):
        lines.append(
            f"- {row.get('order_id')} {row.get('side')}: "
            f"fill={row.get('actual_fill_price')}, base={row.get('base_price')}, "
            f"shadow={row.get('shadow_price')}, farther={row.get('shadow_farther')}, "
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
