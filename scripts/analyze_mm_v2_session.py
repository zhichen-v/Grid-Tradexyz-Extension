"""Offline V2 JSONL accounting and one-candidate aggregation; never authorizes trading.

Example: python scripts/analyze_mm_v2_session.py run1.jsonl run2.jsonl
  --candidate edge_0.2 --mode replay --planned-seconds 1800
  --wall-seconds 1812 1790 [--allocated-capital 50]

Each file is one session of the same candidate/window. Mode, whole-process wall
duration and capital are historical operator claims, never authenticated here.
Early stops keep the planned window; overruns extend it. No network or trading.
Optional --config with explicit market/fee/capital inputs produces quantity
tables; --market-observations adds public BBO and source-clock fill diagnostics.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from dataclasses import asdict, fields, is_dataclass
from decimal import Decimal as D
from enum import Enum
import json
from math import isfinite
from pathlib import Path
import sys
from typing import get_args, get_origin

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.services.market_maker_v2.domain import (
    AccountSnapshot, BoundedExitReport, CashflowEvent, ExecutionResult,
    ExecutionHealth, ExecutionSnapshot, FillAccounting, InventoryDecision,
    MarkEvent, MarketStateSnapshot, QuotePlan, SessionReport, Side, WorkingOrder, FailureDiagnostic,
)
from core.services.market_maker_v2.inventory_governor import InventoryGovernor
from core.services.market_maker_v2.quote_policy import VolumeQuotePolicy
from core.services.market_maker_v2.session_ledger import SessionLedger


EVENTS = dict(account_snapshot=AccountSnapshot, bounded_exit=BoundedExitReport,
              cashflow=CashflowEvent, execution_result=ExecutionResult,
              fill=FillAccounting, inventory_decision=InventoryDecision,
              mark=MarkEvent, quote_plan=QuotePlan, session_report=SessionReport,
              failure_diagnostic=FailureDiagnostic)
TOTALS = (
    "maker_buy_turnover", "maker_sell_turnover", "maker_turnover_total",
    "taker_flatten_turnover", "maker_fill_count", "taker_fill_count",
    "realized_gross_pnl", "maker_fee", "taker_fee", "funding",
    "external_transfers", "realized_net_pnl", "forced_flatten_count",
    "forced_flatten_loss", "quote_uptime_seconds", "duration_seconds",
)
CHECKED = TOTALS + ("ledger_position", "spread_capture", "inventory_markout",
                    "flatten_concession")


def _number(value):
    if type(value) not in (str, int, D):
        raise ValueError("decimal string required")
    number = D(value)
    if not number.is_finite():
        raise ValueError("finite decimal required")
    return number


def _decode(kind, value):
    """Decode only the existing DTO types, keeping their boundary validation."""
    args = get_args(kind)
    if type(None) in args:
        return None if value is None else _decode(next(t for t in args if t is not type(None)), value)
    if kind is D:
        if type(value) is not str:
            raise ValueError("financial telemetry must use decimal strings")
        return _number(value)
    if get_origin(kind) is tuple:
        if type(value) is not list:
            raise ValueError("telemetry tuple must be a JSON array")
        return tuple(_decode(args[0], item) for item in value)
    if is_dataclass(kind):
        if type(value) is not dict:
            raise ValueError("telemetry data must be an object")
        model_fields = {field.name: field.type for field in fields(kind)}
        if value.keys() - model_fields.keys():
            raise ValueError("unknown telemetry field")
        return kind(**{key: _decode(model_fields[key], item) for key, item in value.items()})
    if isinstance(kind, type) and issubclass(kind, Enum):
        return kind(value)
    return value


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON field")
    return result


def _reject_constant(_):
    raise ValueError("non-finite JSON number")


def _events(path):
    # ponytail: one short session in memory; stream if Phase 11 files outgrow RAM.
    content = Path(path).read_text(encoding="utf-8-sig")
    for line in content.splitlines():
        if not line.strip():
            continue
        row = json.loads(line, object_pairs_hook=_object, parse_constant=_reject_constant)
        if (type(row) is not dict or set(row) != {"schema", "event", "data"}
                or row["schema"] != "mm_v2_event_v1" or row["event"] not in EVENTS
                or type(row["data"]) is not dict):
            raise ValueError("unsupported V2 event")
        if row["event"] == "session_report" and set(CHECKED) - row["data"].keys():
            raise ValueError("session report lacks accounting fields")
        yield _decode(EVENTS[row["event"]], row["data"])


def _session(path, mode, planned, wall, capital):
    events = iter(_events(path))
    initial = next(events, None)
    if type(initial) is not AccountSnapshot:
        raise ValueError("starting account snapshot required")
    ledger = SessionLedger(initial)
    final, recorded, simulated = None, None, False
    last_time = initial.observed_monotonic
    submissions = cancellations = plans = 0
    seen_orders, lifetimes, revisions, timing_available = {}, [], 0, True
    execution_observations, previous_execution_time = 0, None
    diagnostics = []
    for event in events:
        if type(event) is FailureDiagnostic:
            if event.symbol != initial.symbol:
                raise ValueError("mixed session symbols")
            diagnostics.append(asdict(event))
            continue  # Disconnect faults may follow the final accounting report.
        if recorded is not None:
            raise ValueError("events after final report or mixed sessions")
        if hasattr(event, "symbol") and event.symbol != initial.symbol:
            raise ValueError("mixed session symbols")
        last_time = max(last_time, getattr(event, "observed_monotonic", last_time))
        if type(event) is FillAccounting:
            if not ledger.ingest_fill(event.fill):
                raise ValueError("duplicate recorded fill")
            last_time = max(last_time, event.fill.observed_monotonic)
        elif type(event) is CashflowEvent:
            if not ledger.ingest_cashflow(event):
                raise ValueError("duplicate recorded cashflow")
        elif type(event) is MarkEvent:
            ledger.observe(event)
        elif type(event) is BoundedExitReport:
            ledger.record_exit(event)
        elif type(event) is AccountSnapshot:
            final = event
        elif type(event) is ExecutionResult:
            if event.snapshot.symbol not in (None, initial.symbol):
                raise ValueError("execution snapshot symbol differs from session")
            execution_observations += 1
            simulated |= event.snapshot.simulated
            submissions += event.submitted_count
            cancellations += event.cancelled_count
            revisions += bool(event.submitted_count and event.cancelled_count)
            timing_available &= event.snapshot.orders is not None and event.snapshot.observed_monotonic is not None
            if timing_available:
                when = D(str(event.snapshot.observed_monotonic))
                if previous_execution_time is not None and when < previous_execution_time:
                    raise ValueError("execution snapshot time regressed")
                previous_execution_time = when
                ids = {order.order_id for order in event.snapshot.orders}
                for identifier in set(seen_orders) - ids:
                    lifetimes.append(when - seen_orders.pop(identifier))
                for identifier in ids:
                    seen_orders.setdefault(identifier, when)
            if event.snapshot.observed_monotonic is not None:
                last_time = max(last_time, event.snapshot.observed_monotonic)
        elif type(event) is QuotePlan:
            plans += 1
        elif type(event) is SessionReport:
            recorded = event
    if mode == "live" and simulated:
        raise ValueError("simulated execution cannot be labeled live")
    duration = (recorded.duration_seconds if recorded else
                D(str(last_time)) - D(str(initial.observed_monotonic)))
    observed_span = D(str(last_time)) - D(str(initial.observed_monotonic))
    if duration < observed_span or duration < 0 or wall < duration:
        raise ValueError("reported/window duration does not cover recorded observations")
    end = float(D(str(initial.observed_monotonic)) + duration)
    observed = (ledger.finalize(final, now=end) if recorded and recorded.complete
                else ledger.snapshot(now=end))
    reasons = []
    if recorded is None:
        reasons.append("missing_final_report")
    elif any(getattr(recorded, name) != getattr(observed, name) for name in CHECKED):
        reasons.append("recorded_events_do_not_reconcile_to_report")
    if not recorded or not recorded.complete or not observed.complete:
        reasons.append("incomplete_final_accounting")
    if recorded and (recorded.failed or recorded.telemetry_errors):
        reasons.append("runtime_or_telemetry_failure")
    if diagnostics:
        reasons.append("runtime_failure_diagnostic")
    if recorded and recorded.complete and (
            recorded.all_in_net_pnl != observed.all_in_net_pnl
            or recorded.equity_reconciliation_difference != observed.equity_reconciliation_difference):
        reasons.append("final_equity_or_net_mismatch")
    if mode == "dry_run" and (observed.maker_fill_count or observed.taker_fill_count):
        raise ValueError("dry run cannot contain fills")
    metrics = asdict(observed)
    window = max(planned, wall)
    return {
        "file": Path(path).name, "symbol": initial.symbol, "mode": mode,
        "accounting_complete": not reasons, "incomplete_reasons": reasons,
        "failure_diagnostics": diagnostics,
        "economics_evaluated": mode == "live" and not reasons,
        "initial_account_equity_usdg": initial.equity,
        "allocated_capital_usdg": capital,
        "planned_seconds": planned, "whole_process_wall_seconds": wall,
        "comparison_window_seconds": window,
        "recorded_metrics": metrics,
        "actual_maker_turnover_usdg": observed.maker_turnover_total if mode == "live" else None,
        "maker_turnover_per_wall_hour": observed.maker_turnover_total * 3600 / window,
        "turnover_over_allocated_capital": observed.maker_turnover_total / capital if capital else None,
        "quote_plan_count": plans, "submitted_count": submissions,
        "cancelled_count": cancellations, "simulated_execution_observed": simulated,
        "observed_order_lifetimes": {"timing_available": timing_available and execution_observations > 0, "replacement_results": revisions,
            "closed_order_count": len(lifetimes), "right_censored_order_count": len(seen_orders),
            "min_snapshot_seconds": min(lifetimes) if lifetimes and timing_available else None,
            "mean_snapshot_seconds": sum(lifetimes) / len(lifetimes) if lifetimes and timing_available else None,
            "max_snapshot_seconds": max(lifetimes) if lifetimes and timing_available else None,
            "method": "first observed to first absent snapshot; sampled lifetimes, not exact exchange times; dry/replay remain simulated"},
        "markout_1s": None, "markout_5s": None,
        "external_quote_distance_ticks": None, "maximum_gross_exposure_usdg": None,
    }


def _aggregate(sessions):
    total = {name: sum((s["recorded_metrics"][name] for s in sessions), D(0)) for name in TOTALS}
    window = sum((s["comparison_window_seconds"] for s in sessions), D(0))
    valid = all(s["economics_evaluated"] for s in sessions)
    turnover = total["maker_turnover_total"]
    fees = total["maker_fee"] + total["taker_fee"]
    net = total["realized_net_pnl"] if valid else None
    capital = sessions[0]["allocated_capital_usdg"]
    return {
        "session_count": len(sessions),
        "incomplete_session_count": sum(not s["accounting_complete"] for s in sessions),
        "economics_evaluated": valid, "observed_totals": total,
        "comparison_window_seconds": window,
        "maker_turnover_per_wall_hour": turnover * 3600 / window,
        "turnover_over_allocated_capital": turnover / capital if capital else None,
        "all_in_net_pnl": net,
        "all_in_net_cost_per_10000_usdg": -net * 10000 / turnover if valid and turnover else None,
        "fee_cover_ratio": total["realized_gross_pnl"] / fees if valid and fees else None,
        "fee_neutral_observed": (net >= 0 and (not fees or total["realized_gross_pnl"] >= fees)
                                 if valid and turnover else None),
        "worst_session_drawdown_usdg": max(s["recorded_metrics"]["max_drawdown"] for s in sessions),
        "cross_session_drawdown_usdg": None,
        "volume_target": None, "objective_met": None,
    }


def build_report(paths, *, candidate, mode, planned_seconds, wall_seconds, allocated_capital=None):
    """Return JSON-compatible historical evidence; retain incomplete session costs."""
    try:
        if (not paths or len(paths) != len(wall_seconds) or mode not in {"live", "dry_run", "replay"}
                or not isinstance(candidate, str) or not candidate.strip()):
            raise ValueError("candidate, mode, and one wall duration per file required")
        if len({Path(path).resolve() for path in paths}) != len(paths):
            raise ValueError("duplicate session file")
        planned = _number(planned_seconds)
        walls = [_number(value) for value in wall_seconds]
        capital = None if allocated_capital is None else _number(allocated_capital)
        if planned <= 0 or any(wall <= 0 for wall in walls) or capital is not None and capital <= 0:
            raise ValueError("positive duration and allocated capital required")
        sessions = [_session(path, mode, planned, wall, capital) for path, wall in zip(paths, walls)]
        if len({session["symbol"] for session in sessions}) != 1:
            raise ValueError("candidate sessions must share a symbol")
        report = {
            "schema": "mm_v2_session_analysis_v1", "candidate": candidate,
            "mode": mode, "source": "historical_records_not_reauthenticated",
            "limitations": [
                "Mode, planned window, process duration and allocated capital are operator claims.",
                "Fixed windows retain failed/early sessions; total ratios are sums divided by sums.",
                "Replay metrics are simulated; dry quote plans are not fills or actual turnover.",
                "Source-clock fill markouts require optional public observations and fill source timestamps; quote distance and gross-exposure coverage are unavailable.",
                "Inventory markout is the ledger inventory-drift decomposition, not post-fill 1s/5s markout.",
                "Cross-session chronology/drawdown and independent confirmation are unavailable; no strategy promotion.",
            ],
            "sessions": sessions, "aggregate": _aggregate(sessions),
        }
        return json.loads(json.dumps(report, default=str, allow_nan=False))
    except (OSError, UnicodeError, TypeError, ArithmeticError, KeyError, StopIteration):
        raise ValueError("invalid session evidence or numeric domain") from None


def candidate_quantity_table(config, *, external_bid, external_ask, tick_size, size_step,
                             min_order_size, min_notional, maker_fee_rate, taker_fee_rate,
                             allocated_capital, target_edges=None):
    """Synthetic flat/soft/hard states through the actual governor and quote policy.

    Entry equals current mid, age/realized loss/drawdown are zero and no
    volatility buffer is assumed. These arithmetic inputs are not authenticated.
    Existing-order rows conservatively retain the state's accepted initial quotes
    while proposing replacements; they do not assume cancellation already happened.
    """
    market = MarketStateSnapshot(config.symbol, 0.0, *map(_number,
        (external_bid, external_ask, tick_size, size_step, min_order_size)), True)
    capital, minimum = _number(allocated_capital), _number(min_notional)
    maker, taker = _number(maker_fee_rate), _number(taker_fee_rate)
    if capital <= 0 or minimum < 0:
        raise ValueError("positive allocated capital and nonnegative minimum notional required")
    cfg, rows = config, []
    mid = (market.external_bid + market.external_ask) / 2
    startup_ok = (cfg.quote.order_size >= market.min_order_size
                  and cfg.quote.order_size % market.size_step == 0
                  and cfg.quote.order_size * market.external_bid >= minimum)
    edges = ([_number(edge) for edge in target_edges] if target_edges
             else [cfg.quote.target_net_edge_bps])
    for edge in edges:
        policy = VolumeQuotePolicy(order_size=cfg.quote.order_size, target_net_edge_bps=edge,
            volatility_multiplier=cfg.quote.volatility_multiplier,
            hard_inventory_limit=cfg.inventory.hard_limit, skew_bps_at_hard=cfg.inventory.skew_bps_at_hard)
        for state, position in (("flat", D(0)), ("long_soft", cfg.inventory.soft_limit),
                                ("short_soft", -cfg.inventory.soft_limit),
                                ("long_hard", cfg.inventory.hard_limit),
                                ("short_hard", -cfg.inventory.hard_limit)):
            orders = ()
            for exposure in ("no_working_orders", "initial_quotes_still_working"):
                account = AccountSnapshot(cfg.symbol, 0.0, position, capital, maker, taker,
                    len(orders), True, mid if position else None,
                    open_order_ids=tuple(order.order_id for order in orders))
                report = SessionReport(cfg.symbol, False, position, len(orders), ledger_position=position)
                execution = ExecutionSnapshot(ExecutionHealth.HEALTHY, len(orders), True,
                                              cfg.symbol, 0.0, orders)
                governor = InventoryGovernor(order_size=cfg.quote.order_size,
                    soft_limit=cfg.inventory.soft_limit, hard_limit=cfg.inventory.hard_limit,
                    stop_loss_usdg=cfg.flatten.stop_loss_usdg,
                    max_hold_seconds=cfg.flatten.max_hold_seconds, cooldown_seconds=cfg.session.cooldown_seconds,
                    max_session_loss_usdg=cfg.session.max_loss_usdg, session_started_monotonic=0.0,
                    session_deadline_monotonic=float(cfg.session.duration_seconds),
                    ioc_slippage_ticks=cfg.flatten.ioc_slippage_ticks)
                decision = governor.evaluate(market, account, report, execution, now=0.0)
                proposed = policy.propose(market, account, decision, now=0.0).quotes
                accepted = tuple(q for q in proposed if q.price * q.size >= minimum) if startup_ok else ()
                buys = sum((o.remaining_size for o in orders if o.side is Side.BUY), D(0))
                sells = sum((o.remaining_size for o in orders if o.side is Side.SELL), D(0))
                worst = max(abs(position), abs(position + buys + sum(
                    (q.size for q in accepted if q.side is Side.BUY), D(0))),
                    abs(position - sells - sum((q.size for q in accepted if q.side is Side.SELL), D(0))))
                price = max([market.external_bid, market.external_ask] + [q.price for q in accepted]
                            + [o.price for o in orders])
                rows.append({"target_net_edge_bps": edge, "inventory_state": state,
                    "position": position, "working_order_case": exposure,
                    "governor_state": decision.state.value,
                    "buy_capacity": decision.buy_capacity, "sell_capacity": decision.sell_capacity,
                    "quotes": [{"side": q.side.value, "price": q.price, "size": q.size,
                                "notional_usdg": q.price * q.size, "reduce_only": q.reduce_only}
                               for q in accepted],
                    "removed_below_minimum_notional": [q.side.value for q in proposed
                                                       if q.price * q.size < minimum],
                    "existing_order_count": len(orders), "worst_position_with_existing_and_new": worst,
                    "worst_gross_exposure_usdg": worst * price,
                    "gross_exposure_over_allocated_capital": worst * price / capital,
                    "within_hard_inventory": worst <= cfg.inventory.hard_limit})
                orders = tuple(WorkingOrder(f"table-{q.side.value}", q.side, q.size, q.price, q.reduce_only)
                               for q in accepted)
    return json.loads(json.dumps({"schema": "mm_v2_candidate_quantity_v1", "symbol": cfg.symbol,
        "source": "synthetic_inputs_not_exchange_evidence", "configured_startup_executable": startup_ok,
        "allocated_capital_usdg": capital, "session_loss_limit_usdg": cfg.session.max_loss_usdg,
        "inventory_stop_loss_usdg": cfg.flatten.stop_loss_usdg,
        "assumptions": "zero age/realized loss/drawdown/volatility; entry=current mid; actual governor applies touch loss, old-order exposure and exit-loss reserve; POST_ONLY proposals apply runner minimum-notional filter",
        "limitations": "arithmetic executability only; no liquidity, margin, fee-tier, API-reserve or trading authorization proof",
        "rows": rows}, default=lambda value: value.value if isinstance(value, Enum) else str(value)))


def analyze_public_books(path, *, session_paths=()):
    """Receipt-clock market diagnostics, without fills, own-order subtraction or interpolation."""
    records, count = [], 0
    required = {"schema", "symbol", "observed_monotonic", "source_timestamp_ms", "nonce",
                "bid", "ask", "bid_size", "ask_size"}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line, object_pairs_hook=_object, parse_constant=_reject_constant)
        if (type(row) is not dict or set(row) != required
                or row["schema"] != "mm_v2_public_book_v1" or row["symbol"] != "BTC"):
            raise ValueError("unsupported public book record")
        when = row["observed_monotonic"]
        if type(when) not in (int, float) or not isfinite(when) or when < 0:
            raise ValueError("invalid book receipt time")
        if any(type(row[key]) is not int or row[key] < 0 for key in ("source_timestamp_ms", "nonce")):
            raise ValueError("invalid book source time/nonce")
        prices = []
        for key in ("bid", "ask", "bid_size", "ask_size"):
            if type(row[key]) is not str or _number(row[key]) <= 0:
                raise ValueError("positive decimal-string book prices/sizes required")
            prices.append(_number(row[key]))
        if prices[0] >= prices[1]:
            raise ValueError("locked/crossed public book")
        record = (D(str(when)), row["source_timestamp_ms"], row["nonce"], *prices)
        count += 1
        if records:
            previous = records[-1]
            if any(record[index] < previous[index] for index in (0, 1, 2)):
                raise ValueError("book receipt/source time/nonce regressed")
            if record[0] == previous[0]:
                if record != previous:
                    raise ValueError("conflicting book at same receipt time")
                continue
        records.append(record)
    if not records:
        raise ValueError("public book observations required")

    def summary(values):
        return {"count": len(values), "min": min(values) if values else None,
                "mean": values[0] + sum((value - values[0] for value in values), D(0)) / len(values) if values else None,
                "max": max(values) if values else None}

    times = [row[0] for row in records]
    source_times = [D(row[1]) / 1000 for row in records]
    mids = [(row[3] + row[4]) / 2 for row in records]
    fills = [event.fill for file in session_paths for event in _events(file)
             if type(event) is FillAccounting and event.fill.liquidity.value == "maker"]
    if any(fill.symbol != "BTC" for fill in fills) or len({fill.fill_id for fill in fills}) != len(fills):
        raise ValueError("mixed symbol or duplicate maker fills in markout input")
    sourced = [fill for fill in fills if fill.source_timestamp_ms is not None]
    horizons = {}
    for horizon in (1, 5):
        values, eligible = [], 0
        for index, when in enumerate(times):
            target = when + horizon
            if target > times[-1]:
                continue
            eligible += 1
            endpoint = bisect_left(times, target, lo=index + 1)
            if times[endpoint] - target <= D(".25"):
                values.append((mids[endpoint] - mids[index]) * 10000 / mids[index])
        horizons[f"{horizon}s"] = {"eligible_starts": eligible, "matched_pairs": len(values),
            "pair_coverage": D(len(values)) / eligible if eligible else None,
            "mid_return_bps": summary(values)}
        matched = []
        for fill in sourced:
            target = D(fill.source_timestamp_ms) / 1000 + horizon
            endpoint = bisect_left(source_times, target)
            if endpoint < len(records) and source_times[endpoint] - target <= D(".25"):
                signed_move = (1 if fill.side is Side.BUY else -1) * (mids[endpoint] - fill.price)
                matched.append((signed_move * fill.size, fill.size * fill.price))
        turnover = sum((item[1] for item in matched), D(0))
        horizons[f"{horizon}s"]["recorded_maker_fill_markout"] = {
            "total_fills": len(fills), "source_timestamp_available": len(sourced),
            "matched_fills": len(matched), "coverage": D(len(matched)) / len(fills) if fills else None,
            "matched_turnover_usdg": turnover,
            "turnover_weighted_bps": sum((item[0] for item in matched), D(0)) * 10000 / turnover if turnover else None}
    output = {"schema": "mm_v2_public_book_analysis_v1", "symbol": "BTC", "record_count": count,
        "distinct_receipt_count": len(records), "duplicate_receipt_count": count - len(records),
        "observed_span_seconds": times[-1] - times[0],
        "source_span_seconds": D(records[-1][1] - records[0][1]) / 1000,
        "distinct_source_nonce_pairs": len({(row[1], row[2]) for row in records}),
        "receipt_gap_seconds": summary([b - a for a, b in zip(times, times[1:])]),
        "spread_bps": summary([(row[4] - row[3]) * 10000 / mid for row, mid in zip(records, mids)]),
        "horizons": horizons,
        "pairing": "first receipt at/after horizon, maximum lateness 0.25s; no interpolation",
        "fill_pairing": "first public source timestamp at/after fill source+horizon within0.25s; favorable side-signed future mid minus fill price, before fees; historical records only",
        "source_age_verified": False,
        "limitations": "unadjusted public BBO; receipt-clock returns separate from source-clock recorded-fill markouts; no public trades, queue fills, own-order removal, wall-clock age proof or economic promotion"}
    return json.loads(json.dumps(output, default=str, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", nargs="*")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--mode", choices=("live", "dry_run", "replay"))
    parser.add_argument("--planned-seconds")
    parser.add_argument("--wall-seconds", nargs="+")
    parser.add_argument("--allocated-capital")
    parser.add_argument("--config", help="Optional V2 config for the synthetic executable-quantity table")
    for name in ("external-bid", "external-ask", "tick-size", "size-step", "min-order-size",
                 "min-notional", "maker-fee-rate", "taker-fee-rate"):
        parser.add_argument("--" + name, help="Explicit historical/synthetic decimal string for --config")
    parser.add_argument("--target-edges", nargs="+", help="Table spread candidates; defaults to config edge")
    parser.add_argument("--market-observations", help="Optional mm_v2_public_book_v1 JSONL diagnostics")
    args = parser.parse_args(argv)
    try:
        if not args.sessions and not args.config and not args.market_observations:
            raise ValueError("sessions, candidate config or public observations required")
        report = (build_report(args.sessions, candidate=args.candidate, mode=args.mode,
                    planned_seconds=args.planned_seconds, wall_seconds=args.wall_seconds,
                    allocated_capital=args.allocated_capital) if args.sessions else {"candidate": args.candidate})
        if args.config:
            from core.services.market_maker_v2.config import load_config
            report["candidate_quantity_table"] = candidate_quantity_table(load_config(args.config),
                **{name: getattr(args, name) for name in ("external_bid", "external_ask", "tick_size",
                    "size_step", "min_order_size", "min_notional", "maker_fee_rate", "taker_fee_rate",
                    "allocated_capital")}, target_edges=args.target_edges)
        if args.market_observations:
            report["public_book_observations"] = analyze_public_books(args.market_observations, session_paths=args.sessions)
    except (ValueError, TypeError, ArithmeticError, OSError):
        print("Invalid V2 session evidence; check the documented event and window contracts.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
