from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import RuntimeState


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
        }
