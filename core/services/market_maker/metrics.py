from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import RuntimeState


_TEN_THOUSAND = Decimal("10000")
_MARKOUT_HORIZONS = (1, 5, 15, 60)
_MARKOUT_EVENT_LIMIT = 100
_MARKOUT_PROGRESS_LIMIT = 500


def _default_counters() -> dict[str, int]:
    return {
        name: 0
        for name in (
            "create_attempts",
            "create_success",
            "would_place",
            "post_only_cancellations",
            "cancel_attempts",
            "cancel_success",
            "would_cancel",
            "partial_fills",
            "full_fills",
            "ambiguous_submissions",
            "ambiguous_cancellations",
            "resolved_ambiguous_cancellations",
            "unresolved_cancellations",
            "reconciliation_success",
            "reconciliation_failure",
            "unknown_orders",
            "mutation_limiter_blocks",
            "http_429",
            "active_unwind_attempts",
            "active_unwind_success",
            "active_unwind_no_fill",
            "active_unwind_partial_fill",
            "active_unwind_ambiguous",
            "active_unwind_blocks",
            "would_active_unwind",
            "episode_cap_blocked",
        )
    }


@dataclass
class MarketMakerMetrics:
    """Small in-memory metrics store for the MVP runtime."""

    started_monotonic: float
    runtime_state: RuntimeState = RuntimeState.STARTING
    pause_reason: str | None = None
    state_transition_count: int = 0
    last_successful_cycle_monotonic: float | None = None
    consecutive_errors: int = 0
    cycles: int = 0
    successful_cycles: int = 0
    failed_cycles: int = 0
    book_age_seconds: float | None = None
    position_age_seconds: float | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid: Decimal | None = None
    raw_spread_ticks: Decimal | None = None
    raw_spread_bps: Decimal | None = None
    reference_includes_own_quote: bool = False
    ws_reconnect_count: int = 0
    signed_position: Decimal | None = None
    inventory_ratio: Decimal | None = None
    worst_long: Decimal | None = None
    worst_short: Decimal | None = None
    max_position_utilization: Decimal | None = None
    reservation_price: Decimal | None = None
    target_bid: Decimal | None = None
    target_ask: Decimal | None = None
    quote_reason: str | None = None
    live_bid: Decimal | None = None
    live_ask: Decimal | None = None
    live_buy_remaining: Decimal = Decimal("0")
    live_sell_remaining: Decimal = Decimal("0")
    quote_spread_ticks: Decimal | None = None
    quote_spread_bps: Decimal | None = None
    round_trip_fee_bps: Decimal = Decimal("0")
    min_profit_buffer_bps: Decimal = Decimal("0")
    quote_edge_after_fees_bps: Decimal | None = None
    eligible_quote_seconds: float = 0.0
    skew_ticks: Decimal | None = None
    risk_increasing_side_multiplier: Decimal | None = None
    reduce_only_mode: bool = False
    counters: dict[str, int] = field(default_factory=_default_counters)
    account_audit: dict[str, Any] = field(default_factory=dict)
    inventory_unwind: dict[str, Any] = field(default_factory=dict)
    fill_markouts: list[dict[str, Any]] = field(default_factory=list)
    _maker_fill_progress: dict[str, tuple[Decimal, Decimal]] = field(
        default_factory=dict
    )
    _maker_fill_open_ids: set[str] = field(default_factory=set)

    def transition(self, state: RuntimeState, reason: str | None = None) -> None:
        if state is not self.runtime_state:
            self.state_transition_count += 1
        self.runtime_state = state
        self.pause_reason = reason

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def record_success(self, now: float) -> None:
        self.cycles += 1
        self.successful_cycles += 1
        self.consecutive_errors = 0
        self.last_successful_cycle_monotonic = now

    def record_error(self) -> int:
        self.cycles += 1
        self.failed_cycles += 1
        self.consecutive_errors += 1
        return self.consecutive_errors

    def record_maker_fill_markout(
        self,
        *,
        order_id: str,
        side: str,
        cumulative_filled: Decimal,
        cumulative_cost: Decimal,
        average_price: Decimal | None,
        now: float,
        mid: Decimal,
        source: str,
        terminal: bool,
    ) -> bool:
        if side not in {"buy", "sell"} or not order_id:
            return False
        if (
            not isinstance(cumulative_filled, Decimal)
            or not cumulative_filled.is_finite()
            or cumulative_filled <= 0
        ):
            return False
        if (
            isinstance(cumulative_cost, Decimal)
            and cumulative_cost.is_finite()
            and cumulative_cost > 0
        ):
            cumulative_notional = cumulative_cost
        elif (
            isinstance(average_price, Decimal)
            and average_price.is_finite()
            and average_price > 0
        ):
            cumulative_notional = cumulative_filled * average_price
        else:
            return False

        prior_filled, prior_notional = self._maker_fill_progress.get(
            order_id, (Decimal("0"), Decimal("0"))
        )
        delta_filled = cumulative_filled - prior_filled
        delta_notional = cumulative_notional - prior_notional
        if delta_filled <= 0 or delta_notional <= 0:
            if terminal and order_id in self._maker_fill_progress:
                progress = self._maker_fill_progress.pop(order_id)
                self._maker_fill_progress[order_id] = progress
                self._maker_fill_open_ids.discard(order_id)
                self._trim_maker_fill_progress()
            return False
        fill_price = delta_notional / delta_filled
        if not fill_price.is_finite() or fill_price <= 0:
            return False

        self._maker_fill_progress.pop(order_id, None)
        self._maker_fill_progress[order_id] = (
            cumulative_filled,
            cumulative_notional,
        )
        if terminal:
            self._maker_fill_open_ids.discard(order_id)
        else:
            self._maker_fill_open_ids.add(order_id)
        self._trim_maker_fill_progress()
        event: dict[str, Any] = {
            "order_id": order_id,
            "side": side,
            "fill_amount": delta_filled,
            "fill_price": fill_price,
            "observation_source": source,
            "started_monotonic": now,
            "mae_bps": None,
            "mfe_bps": None,
        }
        for horizon in _MARKOUT_HORIZONS:
            event[f"markout_{horizon}s_bps"] = None
        self.fill_markouts.append(event)
        del self.fill_markouts[:-_MARKOUT_EVENT_LIMIT]
        self.update_fill_markouts(now=now, mid=mid)
        return True

    def _trim_maker_fill_progress(self) -> None:
        while len(self._maker_fill_progress) > _MARKOUT_PROGRESS_LIMIT:
            evicted = next(
                (
                    candidate
                    for candidate in self._maker_fill_progress
                    if candidate not in self._maker_fill_open_ids
                ),
                None,
            )
            if evicted is None:
                break
            self._maker_fill_progress.pop(evicted)

    def sync_open_maker_order_ids(self, order_ids: set[str]) -> None:
        self._maker_fill_open_ids.intersection_update(order_ids)
        self._trim_maker_fill_progress()

    def update_fill_markouts(self, *, now: float, mid: Decimal) -> None:
        if not isinstance(mid, Decimal) or not mid.is_finite() or mid <= 0:
            return
        for event in self.fill_markouts:
            if event["markout_60s_bps"] is not None:
                continue
            age = max(0.0, now - event["started_monotonic"])
            fill_price = event["fill_price"]
            markout = (
                (mid - fill_price) / fill_price * _TEN_THOUSAND
                if event["side"] == "buy"
                else (fill_price - mid) / fill_price * _TEN_THOUSAND
            )
            event["mae_bps"] = (
                markout
                if event["mae_bps"] is None
                else min(event["mae_bps"], markout)
            )
            event["mfe_bps"] = (
                markout
                if event["mfe_bps"] is None
                else max(event["mfe_bps"], markout)
            )
            for horizon in _MARKOUT_HORIZONS:
                key = f"markout_{horizon}s_bps"
                if event[key] is None and age >= horizon:
                    event[key] = markout

    def _side_markout_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for side in ("buy", "sell"):
            side_summary: dict[str, Any] = {}
            side_events = [
                event for event in self.fill_markouts if event["side"] == side
            ]
            for horizon in _MARKOUT_HORIZONS:
                values = [
                    event[f"markout_{horizon}s_bps"]
                    for event in side_events
                    if event[f"markout_{horizon}s_bps"] is not None
                ]
                side_summary[f"{horizon}s"] = {
                    "count": len(values),
                    "mean_bps": (
                        sum(values, Decimal("0")) / len(values)
                        if values
                        else None
                    ),
                }
            summary[side] = side_summary
        return summary

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "runtime_state": self.runtime_state.value,
            "pause_reason": self.pause_reason,
            "state_transition_count": self.state_transition_count,
            "uptime_seconds": max(0.0, now - self.started_monotonic),
            "last_successful_cycle_monotonic": self.last_successful_cycle_monotonic,
            "consecutive_errors": self.consecutive_errors,
            "cycles": self.cycles,
            "successful_cycles": self.successful_cycles,
            "failed_cycles": self.failed_cycles,
            "book_age_seconds": self.book_age_seconds,
            "position_age_seconds": self.position_age_seconds,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid": self.mid,
            "raw_spread_ticks": self.raw_spread_ticks,
            "raw_spread_bps": self.raw_spread_bps,
            "reference_includes_own_quote": self.reference_includes_own_quote,
            "ws_reconnect_count": self.ws_reconnect_count,
            "signed_position": self.signed_position,
            "inventory_ratio": self.inventory_ratio,
            "worst_long": self.worst_long,
            "worst_short": self.worst_short,
            "max_position_utilization": self.max_position_utilization,
            "reservation_price": self.reservation_price,
            "target_bid": self.target_bid,
            "target_ask": self.target_ask,
            "quote_reason": self.quote_reason,
            "live_bid": self.live_bid,
            "live_ask": self.live_ask,
            "live_buy_remaining": self.live_buy_remaining,
            "live_sell_remaining": self.live_sell_remaining,
            "quote_spread_ticks": self.quote_spread_ticks,
            "quote_spread_bps": self.quote_spread_bps,
            "round_trip_fee_bps": self.round_trip_fee_bps,
            "min_profit_buffer_bps": self.min_profit_buffer_bps,
            "quote_edge_after_fees_bps": self.quote_edge_after_fees_bps,
            "eligible_quote_seconds": self.eligible_quote_seconds,
            "skew_ticks": self.skew_ticks,
            "risk_increasing_side_multiplier": self.risk_increasing_side_multiplier,
            "reduce_only_mode": self.reduce_only_mode,
            "counters": dict(self.counters),
            "account_audit": dict(self.account_audit),
            "inventory_unwind": dict(self.inventory_unwind),
            "fill_markouts": [
                {
                    **event,
                    "age_seconds": max(
                        0.0, now - event["started_monotonic"]
                    ),
                }
                for event in self.fill_markouts
            ],
            "fill_markout_coverage": {
                "unit": "observed_order_fill_delta",
                "sources": (
                    "websocket_order_update",
                    "reconciliation",
                    "rest_open_order_sync",
                ),
                "time_origin": "observation_monotonic",
                "retained_events": len(self.fill_markouts),
            },
            "side_markout_summary": self._side_markout_summary(),
        }
