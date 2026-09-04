"""Read-only fee/spread feasibility diagnostics, not a queue-fill backtest.

Accept V1 shadow JSON arrays or external-BBO JSONL. No network, credentials,
account mutations, campaign validation, or interpolation is involved.

CLI: python scripts/mm_v2_feasibility.py bbo.jsonl --fee-evidence fees.json
     --target-edge-bps 0 0.2 0.5 [--tick-size 0.1]
JSONL rows: timestamp (timezone-qualified ISO or epoch seconds), symbol,
external_bid, external_ask, tick_size. Use decimal strings for financial values.
Fee JSON: authenticated=true, observed_at_utc, maker_fee_rate, taker_fee_rate;
also accepts the historical market_maker_gate_bundle_v1 preflight fee ticks.
All fee inputs are historical claims, never freshly authenticated by this tool.
Each snapshot file is one stream; horizons are never paired across files.
Legacy input needs explicit tick size; tick-based results are conditional on it.
Candidates use external mid, zero inventory skew and zero volatility buffer;
they are arithmetic baselines, not live parameter recommendations.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import re
import sys


D = Decimal
BPS = D("10000")


def _decimal(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, D)):
        raise ValueError(f"invalid {field}")
    try:
        number = D(str(value))
    except InvalidOperation:
        raise ValueError(f"invalid {field}") from None
    if not number.is_finite():
        raise ValueError(f"non-finite {field}")
    return number


def _positive(value, field):
    number = _decimal(value, field)
    if number <= 0:
        raise ValueError(f"{field} must be positive")
    return number


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must include timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def _timestamp(value):
    if isinstance(value, str) and "T" in value:
        elapsed = _utc(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        number = D(elapsed.days * 86400 + elapsed.seconds) + D(elapsed.microseconds) / 1000000
    else:
        number = _decimal(value, "timestamp")
    if number < 0:
        raise ValueError("timestamp must be nonnegative")
    return number


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("cannot read input file") from None


def _json(text):
    def reject_constant(_):
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(text, parse_float=D, parse_constant=reject_constant)
    except json.JSONDecodeError:
        raise ValueError("invalid JSON input") from None


def _record(value):
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _fees(path):
    record = _record(_json(_read(path)))
    if record.get("schema") == "market_maker_gate_bundle_v1":
        preflight = _record(record.get("fresh_read_only_preflight"))
        for name in ("authenticated_position_count", "authenticated_open_order_count"):
            if _decimal(preflight.get(name), "authenticated count") < 0:
                raise ValueError("invalid authenticated count")
        maker_tick = _decimal(preflight.get("maker_fee_tick"), "maker fee tick")
        taker_tick = _decimal(preflight.get("taker_fee_tick"), "taker fee tick")
        if maker_tick != maker_tick.to_integral_value() or taker_tick != taker_tick.to_integral_value():
            raise ValueError("fee ticks must be integral")
        maker, taker = maker_tick / 1000000, taker_tick / 1000000
        observed = preflight.get("local_time")
        source_type = "gate_b_bundle"
    else:
        if record.get("authenticated") is not True:
            raise ValueError("historical authenticated fee evidence required")
        maker = _decimal(record.get("maker_fee_rate"), "maker fee rate")
        taker = _decimal(record.get("taker_fee_rate"), "taker fee rate")
        observed = record.get("observed_at_utc")
        source_type = "historical_authenticated_record"
    if not (0 <= maker < 1 and 0 <= taker < 1):
        raise ValueError("fee rates must be nonnegative and below one; rebates unsupported")
    return {
        "source_type": source_type,
        "evidence_file": Path(path).name,
        "observed_at_utc": _utc(observed).isoformat().replace("+00:00", "Z"),
        "historical": True,
        "authentication": "historical_record_not_reauthenticated",
        "maker_fee_rate": str(maker),
        "taker_fee_rate": str(taker),
        "maker_fee_bps": str(maker * BPS),
        "taker_fee_bps": str(taker * BPS),
        "maker_roundtrip_fee_floor_bps": str(2 * maker * BPS),
        "rebate_policy": "unsupported_negative_fees_rejected",
    }


def _snapshot(timestamp, symbol, bid, ask, tick):
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9_:/-]{1,32}", symbol):
        raise ValueError("invalid symbol")
    bid, ask = _positive(bid, "external bid"), _positive(ask, "external ask")
    tick = _positive(tick, "tick size")
    if bid >= ask:
        raise ValueError("external BBO must be non-crossed with positive spread")
    if bid % tick != 0 or ask % tick != 0:
        raise ValueError("external BBO must align to tick size")
    return (_timestamp(timestamp), symbol, bid, ask, tick)


def _legacy(rows, tick):
    if tick is None:
        raise ValueError("legacy snapshots require explicit --tick-size")
    snapshots, runs = [], set()
    for row in rows:
        row = _record(row)
        identity = (row.get("started_at_utc"), row.get("event_sequence_run_id"))
        if not any(identity) or any(value is not None and (
                not isinstance(value, str) or not value.strip()) for value in identity):
            raise ValueError("legacy snapshots require a nonempty stream identity")
        if identity[0] is not None:
            _utc(identity[0])
        runs.add(identity)
        features = [row.get("controller_feature_snapshot")]
        for context in row.get("quote_contexts", []):
            features.append(_record(context).get("feature_snapshot"))
        for history in row.get("controller_decision_history", []):
            features.append(_record(history).get("features"))
        for feature in features:
            if not feature:
                continue
            feature = _record(feature)
            snapshots.append(_snapshot(
                feature.get("received_monotonic"), row.get("symbol"),
                feature.get("external_best_bid"), feature.get("external_best_ask"), tick,
            ))
    if len(runs) > 1:
        raise ValueError("legacy input must contain one monotonic stream")
    return sorted(snapshots)


def _stream(path, tick):
    text = _read(path)
    if text.lstrip().startswith("["):
        snapshots = _legacy(_json(text), tick)
    else:
        snapshots = []
        for line in text.splitlines():
            if not line.strip():
                continue
            row = _record(_json(line))
            row_tick = row.get("tick_size", tick)
            if tick is not None and row_tick is not None and _decimal(row_tick, "tick size") != tick:
                raise ValueError("CLI and snapshot tick size disagree")
            snapshots.append(_snapshot(
                row.get("timestamp"), row.get("symbol"),
                row.get("external_bid"), row.get("external_ask"), row_tick,
            ))
    unique = []
    for snapshot in snapshots:
        if unique and snapshot[0] < unique[-1][0]:
            raise ValueError("snapshot timestamps must be increasing")
        if unique and snapshot[0] == unique[-1][0]:
            if snapshot != unique[-1]:
                raise ValueError("conflicting snapshots at same timestamp")
            continue
        unique.append(snapshot)
    if not unique:
        raise ValueError("input has no external BBO snapshots")
    return unique


def _distribution(values):
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "count": count, "min": str(ordered[0]),
        "p50": str(ordered[(count - 1) // 2]),
        "p95": str(ordered[(95 * count + 99) // 100 - 1]),
        "max": str(ordered[-1]),
    }


def _moves(streams, horizon):
    tolerance = horizon / 10
    moves, elapsed = [], []
    eligible, matched_span = 0, D(0)
    for stream in streams:
        times = [row[0] for row in stream]
        matched_starts = []
        for index, row in enumerate(stream):
            target = row[0] + horizon
            if target - tolerance > times[-1]:
                continue
            eligible += 1
            offset = bisect_left(times, target, lo=index + 1)
            endpoints = [k for k in (offset - 1, offset) if index < k < len(stream)]
            if not endpoints:
                continue
            end = min(endpoints, key=lambda k: (abs(times[k] - target), times[k]))
            if abs(times[end] - target) > tolerance:
                continue
            move = ((stream[end][2] + stream[end][3]) / (row[2] + row[3]) - 1) * BPS
            moves.append(move)
            elapsed.append(times[end] - row[0])
            matched_starts.append(row[0])
        if matched_starts:
            matched_span += matched_starts[-1] - matched_starts[0]
    available = len(moves) >= 2
    return {
        "status": "available" if available else "insufficient_observations",
        "matched_count": len(moves), "eligible_start_count": eligible,
        "coverage_ratio": str(D(len(moves)) / eligible) if eligible else "0",
        "endpoint_tolerance_seconds": str(tolerance),
        "matched_start_span_seconds": str(matched_span),
        "actual_elapsed_seconds": _distribution(elapsed),
        "signed_bps": _distribution(moves) if available else None,
        "absolute_bps": _distribution([abs(value) for value in moves]) if available else None,
    }


def _candidate(rows, maker_fee_bps, edge):
    half = maker_fee_bps + edge / 2
    bid_distances, ask_distances, spreads = [], [], []
    for _, _, external_bid, external_ask, tick in rows:
        mid = (external_bid + external_ask) / 2
        bid = (mid * (1 - half / BPS) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        ask = (mid * (1 + half / BPS) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        bid, ask = min(bid, external_ask - tick), max(ask, external_bid + tick)
        if bid == ask:
            bid -= tick
        if bid <= 0 or bid >= ask:
            raise ValueError("candidate cannot produce valid positive non-crossed quotes")
        bid_distances.append((external_bid - bid) / tick)
        ask_distances.append((ask - external_ask) / tick)
        spreads.append((ask - bid) / mid * BPS)
    return {
        "target_edge_bps": str(edge), "volatility_buffer_bps": "0",
        "minimum_full_spread_bps": str(2 * half), "half_spread_bps": str(half),
        "bid_distance_from_touch_ticks": _distribution(bid_distances),
        "ask_distance_from_touch_ticks": _distribution(ask_distances),
        "effective_full_spread_bps": _distribution(spreads),
    }


def _build_report(snapshot_paths, fee_evidence_path, target_edges_bps, tick_size=None):
    if not snapshot_paths or not target_edges_bps:
        raise ValueError("at least one snapshot file and target edge required")
    tick = None if tick_size is None else _positive(tick_size, "tick size")
    fees = _fees(fee_evidence_path)
    streams = [_stream(path, tick) for path in snapshot_paths]
    rows = [row for stream in streams for row in stream]
    if len({row[1] for row in rows}) != 1 or len({row[4] for row in rows}) != 1:
        raise ValueError("all snapshots must share symbol and tick size")
    edges = [_decimal(edge, "target edge") for edge in target_edges_bps]
    if any(edge < 0 for edge in edges):
        raise ValueError("target edges must be nonnegative")
    gaps = [stream[i][0] - stream[i - 1][0] for stream in streams for i in range(1, len(stream))]
    return {
        "schema": "mm_v2_feasibility_v1", "disclaimer": "not a queue-fill backtest",
        "method": {
            "percentiles": "nearest_rank", "interpolation": False,
            "move_matching": "nearest observed endpoint within 10% of horizon; no cross-file pairing",
            "move_minimum_pairs": 2,
            "touch_distance": "signed ticks; negative means inside current external touch",
            "limits": "sampled distributions only; availability is not statistical sufficiency or touch/fill frequency",
        },
        "fees": fees,
        "market": {
            "symbol": rows[0][1], "snapshot_count": len(rows), "stream_count": len(streams),
            "observed_span_seconds": str(sum((stream[-1][0] - stream[0][0] for stream in streams), D(0))),
            "tick_size": str(rows[0][4]),
            "spread_bps": _distribution([(row[3] - row[2]) / ((row[2] + row[3]) / 2) * BPS for row in rows]),
            "sampling_gap_seconds": _distribution(gaps),
        },
        "moves": {f"{h}s": _moves(streams, D(h)) for h in (1, 5)},
        "candidates": [_candidate(rows, D(fees["maker_fee_bps"]), edge) for edge in edges],
    }


def build_report(snapshot_paths, fee_evidence_path, target_edges_bps, tick_size=None):
    """Build a sanitized report; invalid input raises ValueError, never writes files."""
    try:
        return _build_report(snapshot_paths, fee_evidence_path, target_edges_bps, tick_size)
    except (TypeError, ArithmeticError):
        raise ValueError("invalid input structure or numeric domain") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", nargs="+")
    parser.add_argument("--fee-evidence", required=True)
    parser.add_argument("--target-edge-bps", nargs="+", required=True)
    parser.add_argument("--tick-size", help="Required for legacy input; never inferred")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.snapshots, args.fee_evidence, args.target_edge_bps, args.tick_size)
    except (ValueError, InvalidOperation, OverflowError, TypeError):
        print("Invalid feasibility input; check documented market, timestamp and fee contracts.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
