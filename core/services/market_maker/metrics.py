from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math
from typing import Any, Iterable

from .models import RuntimeState


_TEN_THOUSAND = Decimal("10000")
_MARKOUT_HORIZONS = (1, 5, 15, 60)
_MARKOUT_EVENT_LIMIT = 100
_MARKOUT_PROGRESS_LIMIT = 500
_ATTRIBUTION_LIMIT = 500
_ATTRIBUTION_CONFLICT_LIMIT = _ATTRIBUTION_LIMIT + _MARKOUT_EVENT_LIMIT
_CONTROLLER_HISTORY_LIMIT = 200
_QUOTE_CONTEXT_LIMIT = 500
_FILL_ROLES = ("entry", "risk_increasing", "passive_exit", "active_exit")
_ENTRY_FEEDBACK_HORIZONS = (5, 15)
_ENTRY_FEEDBACK_SOURCES = frozenset(
    {"websocket_order_update", "reconciliation"}
)


def _markout_stats(values: list[Decimal]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean_bps": None,
            "median_bps": None,
            "min_bps": None,
            "max_bps": None,
        }
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / Decimal("2")
    )
    return {
        "count": len(ordered),
        "mean_bps": sum(ordered, Decimal("0")) / len(ordered),
        "median_bps": median,
        "min_bps": ordered[0],
        "max_bps": ordered[-1],
    }


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
            "markout_telemetry_errors",
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
    quote_controller: dict[str, Any] = field(default_factory=dict)
    controller_decision_history: list[dict[str, Any]] = field(
        default_factory=list
    )
    controller_decision_history_total: int = 0
    controller_error_count: int = 0
    controller_ready_seconds: float = 0.0
    controller_warming_seconds: float = 0.0
    controller_bid_blocked_seconds: float = 0.0
    controller_ask_blocked_seconds: float = 0.0
    controller_both_blocked_seconds: float = 0.0
    controller_bid_extra_ticks: int = 0
    controller_ask_extra_ticks: int = 0
    controller_base_bid: Decimal | None = None
    controller_base_ask: Decimal | None = None
    controller_shadow_bid: Decimal | None = None
    controller_shadow_ask: Decimal | None = None
    controller_applied_bid: Decimal | None = None
    controller_applied_ask: Decimal | None = None
    controller_feature_snapshot: dict[str, Any] = field(default_factory=dict)
    fill_markouts: list[dict[str, Any]] = field(default_factory=list)
    _maker_fill_progress: dict[str, tuple[Decimal, Decimal]] = field(
        default_factory=dict
    )
    _maker_fill_open_ids: set[str] = field(default_factory=set)
    _authenticated_fill_attributions: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )
    _attribution_conflict_order_ids: dict[str, None] = field(
        default_factory=dict
    )
    _controller_last_monotonic: float | None = None
    _controller_history_key: tuple[Any, ...] | None = None
    _controller_last_decision: dict[str, Any] | None = None
    _quote_context_by_order: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )

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

    def record_controller_decision(
        self,
        decision: Any,
        *,
        now: float,
        base_bid: Decimal | None,
        base_ask: Decimal | None,
        shadow_bid: Decimal | None,
        shadow_ask: Decimal | None,
        applied_bid: Decimal | None,
        applied_ask: Decimal | None,
        feature_snapshot: dict[str, Any] | None,
        entry_applicable: bool = True,
        error: str | None = None,
    ) -> None:
        """Record bounded controller telemetry without affecting quote behavior."""
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            return
        now_value = float(now)
        if not math.isfinite(now_value):
            return
        self._accrue_controller_seconds(now_value)

        bid = getattr(decision, "bid", None)
        ask = getattr(decision, "ask", None)
        bid_extra = getattr(bid, "extra_spread_ticks", 0)
        ask_extra = getattr(ask, "extra_spread_ticks", 0)
        bid_blocked = getattr(bid, "blocked", False)
        ask_blocked = getattr(ask, "blocked", False)
        current = {
            "mode": getattr(decision, "mode", None),
            "controller": getattr(decision, "controller", None),
            "ready": getattr(decision, "ready", False) is True,
            "reason": getattr(decision, "reason", None),
            "decision_id": getattr(decision, "decision_id", None),
            "bid": {
                "base_price": base_bid,
                "shadow_price": shadow_bid,
                "applied_price": applied_bid,
                "extra_spread_ticks": bid_extra,
                "blocked": bid_blocked,
                "toxicity_score_ticks": getattr(
                    bid, "toxicity_score_ticks", None
                ),
                "directional_confirmations": getattr(
                    bid, "directional_confirmations", None
                ),
                "reason": getattr(bid, "reason", None),
            },
            "ask": {
                "base_price": base_ask,
                "shadow_price": shadow_ask,
                "applied_price": applied_ask,
                "extra_spread_ticks": ask_extra,
                "blocked": ask_blocked,
                "toxicity_score_ticks": getattr(
                    ask, "toxicity_score_ticks", None
                ),
                "directional_confirmations": getattr(
                    ask, "directional_confirmations", None
                ),
                "reason": getattr(ask, "reason", None),
            },
            "feature_health": (
                feature_snapshot.get("health")
                if isinstance(feature_snapshot, dict)
                else None
            ),
            "features": (
                dict(feature_snapshot)
                if isinstance(feature_snapshot, dict)
                else {}
            ),
            "entry_applicable": entry_applicable is True,
            "error": error,
            "recorded_monotonic": now_value,
        }
        self.quote_controller = current
        self.controller_bid_extra_ticks = (
            bid_extra if type(bid_extra) is int and bid_extra >= 0 else 0
        )
        self.controller_ask_extra_ticks = (
            ask_extra if type(ask_extra) is int and ask_extra >= 0 else 0
        )
        self.controller_base_bid = base_bid
        self.controller_base_ask = base_ask
        self.controller_shadow_bid = shadow_bid
        self.controller_shadow_ask = shadow_ask
        self.controller_applied_bid = applied_bid
        self.controller_applied_ask = applied_ask
        self.controller_feature_snapshot = (
            dict(feature_snapshot) if isinstance(feature_snapshot, dict) else {}
        )
        if error is not None:
            self.controller_error_count += 1

        history_key = (
            current["ready"],
            bid_blocked,
            ask_blocked,
            self.controller_bid_extra_ticks,
            self.controller_ask_extra_ticks,
            current["entry_applicable"],
            shadow_bid,
            shadow_ask,
            error,
        )
        if history_key != self._controller_history_key or error is not None:
            self.controller_decision_history_total += 1
            self.controller_decision_history.append(dict(current))
            del self.controller_decision_history[:-_CONTROLLER_HISTORY_LIMIT]
            self._controller_history_key = history_key
        self._controller_last_decision = dict(current)

    def record_controller_fill_snapshot(self, order_id: str, now: float) -> None:
        context = self._quote_context_by_order.get(order_id)
        if not order_id or self._controller_last_decision is None:
            return
        snapshot = dict(self._controller_last_decision)
        snapshot.update(
            event="maker_fill",
            fill_order_id=order_id,
            placement_quote_context=(dict(context) if context is not None else None),
            recorded_monotonic=float(now),
        )
        self.controller_decision_history_total += 1
        self.controller_decision_history.append(snapshot)
        del self.controller_decision_history[:-_CONTROLLER_HISTORY_LIMIT]

    def record_quote_context(
        self, order_id: str, context: dict[str, Any]
    ) -> None:
        if not order_id or not isinstance(context, dict):
            return
        self._quote_context_by_order.pop(order_id, None)
        self._quote_context_by_order[order_id] = dict(context)
        while len(self._quote_context_by_order) > _QUOTE_CONTEXT_LIMIT:
            self._quote_context_by_order.pop(next(iter(self._quote_context_by_order)))
        for event in self.fill_markouts:
            if event.get("order_id") == order_id:
                event["quote_context"] = dict(context)

    def _accrue_controller_seconds(self, now: float) -> None:
        previous = self._controller_last_monotonic
        if previous is not None and now < previous:
            return
        self._controller_last_monotonic = now
        if (
            previous is None
            or not self.quote_controller
            or self.runtime_state is not RuntimeState.ACTIVE
            or self.quote_controller.get("entry_applicable") is not True
        ):
            return
        elapsed = now - previous
        if self.quote_controller.get("ready") is True:
            self.controller_ready_seconds += elapsed
        else:
            self.controller_warming_seconds += elapsed
            return
        bid_blocked = self.quote_controller.get("bid", {}).get("blocked") is True
        ask_blocked = self.quote_controller.get("ask", {}).get("blocked") is True
        if bid_blocked:
            self.controller_bid_blocked_seconds += elapsed
        if ask_blocked:
            self.controller_ask_blocked_seconds += elapsed
        if bid_blocked and ask_blocked:
            self.controller_both_blocked_seconds += elapsed

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
        external_mid: Decimal | None = None,
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
        if (
            order_id not in self._maker_fill_progress
            and len(self._maker_fill_progress) >= _MARKOUT_PROGRESS_LIMIT
        ):
            evicted = next(
                (
                    candidate
                    for candidate in self._maker_fill_progress
                    if candidate not in self._maker_fill_open_ids
                ),
                None,
            )
            if evicted is None:
                return False
            self._maker_fill_progress.pop(evicted)

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
            "raw_mid_at_start": (
                mid
                if isinstance(mid, Decimal) and mid.is_finite() and mid > 0
                else None
            ),
            "external_mid_at_start": (
                external_mid
                if isinstance(external_mid, Decimal)
                and external_mid.is_finite()
                and external_mid > 0
                else None
            ),
            "mae_bps": None,
            "mfe_bps": None,
            "attribution_state": "pending",
            "fill_role": None,
            "episode_sequence": None,
            "active_unwind": None,
            "attribution_signature": None,
            "attribution_conflict": False,
            "quote_context": (
                dict(self._quote_context_by_order[order_id])
                if order_id in self._quote_context_by_order
                else None
            ),
        }
        for horizon in _MARKOUT_HORIZONS:
            event[f"markout_{horizon}s_bps"] = None
            event[f"raw_mid_{horizon}s"] = None
            event[f"raw_mid_markout_{horizon}s_bps"] = None
            event[f"external_mid_{horizon}s"] = None
            event[f"external_mid_markout_{horizon}s_bps"] = None
        self._annotate_markout_event(event)
        self.fill_markouts.append(event)
        del self.fill_markouts[:-_MARKOUT_EVENT_LIMIT]
        self.update_fill_markouts(
            now=now,
            mid=mid,
            external_mid=external_mid,
        )
        return True

    def apply_authenticated_fill_attributions(
        self, attributions: Iterable[dict[str, Any]]
    ) -> None:
        for attribution in attributions:
            if not isinstance(attribution, dict):
                continue
            trade_id = str(attribution.get("trade_id", "") or "")
            order_id = str(attribution.get("order_id", "") or "")
            side = attribution.get("side")
            role = attribution.get("role")
            episode_sequence = attribution.get("episode_sequence")
            prior_position = attribution.get("prior_position")
            next_position = attribution.get("next_position")
            exchange_timestamp = attribution.get("exchange_timestamp")
            active_unwind = attribution.get("active_unwind")
            position_flip = attribution.get("position_flip", False)
            if (
                not trade_id
                or not order_id
                or side not in {"buy", "sell"}
                or role not in _FILL_ROLES
                or type(episode_sequence) is not int
                or episode_sequence <= 0
                or not isinstance(prior_position, Decimal)
                or not prior_position.is_finite()
                or not isinstance(next_position, Decimal)
                or not next_position.is_finite()
                or type(exchange_timestamp) is not int
                or exchange_timestamp < 0
                or type(active_unwind) is not bool
                or type(position_flip) is not bool
            ):
                continue
            normalized = {
                "trade_id": trade_id,
                "order_id": order_id,
                "side": side,
                "role": role,
                "episode_sequence": episode_sequence,
                "prior_position": prior_position,
                "next_position": next_position,
                "exchange_timestamp": exchange_timestamp,
                "active_unwind": active_unwind,
                "position_flip": position_flip,
            }
            existing = self._authenticated_fill_attributions.get(trade_id)
            if existing is None:
                self._authenticated_fill_attributions[trade_id] = normalized
            elif existing != normalized:
                self._remember_attribution_conflict(existing["order_id"])
                self._remember_attribution_conflict(order_id)
                continue
        while len(self._authenticated_fill_attributions) > _ATTRIBUTION_LIMIT:
            self._authenticated_fill_attributions.pop(
                next(iter(self._authenticated_fill_attributions))
            )
        for event in self.fill_markouts:
            self._annotate_markout_event(event)

    def _annotate_markout_event(self, event: dict[str, Any]) -> None:
        order_id = event["order_id"]
        if order_id in self._attribution_conflict_order_ids:
            self._set_pending_attribution(event, conflict=True)
            return
        matches = [
            attribution
            for attribution in self._authenticated_fill_attributions.values()
            if attribution["order_id"] == order_id
        ]
        if not matches:
            return
        signatures = {
            (
                attribution["side"],
                attribution["role"],
                attribution["episode_sequence"],
                attribution["active_unwind"],
            )
            for attribution in matches
        }
        if len(signatures) != 1 or any(
            attribution["position_flip"] for attribution in matches
        ):
            self._remember_attribution_conflict(order_id)
            self._set_pending_attribution(event, conflict=True)
            return
        side, role, episode_sequence, active_unwind = next(iter(signatures))
        if side != event["side"]:
            self._remember_attribution_conflict(order_id)
            self._set_pending_attribution(event, conflict=True)
            return
        signature = {
            "side": side,
            "role": role,
            "episode_sequence": episode_sequence,
            "active_unwind": active_unwind,
        }
        prior_signature = event.get("attribution_signature")
        if prior_signature is not None and prior_signature != signature:
            self._remember_attribution_conflict(order_id)
            self._set_pending_attribution(event, conflict=True)
            return
        event.update(
            attribution_state="authenticated",
            fill_role=role,
            episode_sequence=episode_sequence,
            active_unwind=active_unwind,
            attribution_signature=signature,
            attribution_conflict=False,
        )

    @staticmethod
    def _set_pending_attribution(
        event: dict[str, Any], *, conflict: bool
    ) -> None:
        event.update(
            attribution_state="pending",
            fill_role=None,
            episode_sequence=None,
            active_unwind=None,
            attribution_conflict=conflict,
        )

    def _remember_attribution_conflict(self, order_id: str) -> None:
        if not order_id:
            return
        self._attribution_conflict_order_ids.pop(order_id, None)
        self._attribution_conflict_order_ids[order_id] = None
        while (
            len(self._attribution_conflict_order_ids)
            > _ATTRIBUTION_CONFLICT_LIMIT
        ):
            pinned = {
                event["order_id"] for event in self.fill_markouts
            } | self._maker_fill_open_ids
            evicted = next(
                (
                    candidate
                    for candidate in self._attribution_conflict_order_ids
                    if candidate not in pinned
                ),
                None,
            )
            if evicted is None:
                break
            self._attribution_conflict_order_ids.pop(evicted)

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

    def update_fill_markouts(
        self,
        *,
        now: float,
        mid: Decimal,
        external_mid: Decimal | None = None,
    ) -> None:
        if not isinstance(mid, Decimal) or not mid.is_finite() or mid <= 0:
            return
        valid_external_mid = (
            isinstance(external_mid, Decimal)
            and external_mid.is_finite()
            and external_mid > 0
        )
        for event in self.fill_markouts:
            if event["markout_60s_bps"] is not None:
                continue
            age = max(0.0, now - event["started_monotonic"])
            fill_price = event["fill_price"]
            raw_markout = (
                (mid - fill_price) / fill_price * _TEN_THOUSAND
                if event["side"] == "buy"
                else (fill_price - mid) / fill_price * _TEN_THOUSAND
            )
            event["mae_bps"] = (
                raw_markout
                if event["mae_bps"] is None
                else min(event["mae_bps"], raw_markout)
            )
            event["mfe_bps"] = (
                raw_markout
                if event["mfe_bps"] is None
                else max(event["mfe_bps"], raw_markout)
            )
            for horizon in _MARKOUT_HORIZONS:
                key = f"markout_{horizon}s_bps"
                if event[key] is None and age >= horizon:
                    event[key] = raw_markout
                    event[f"raw_mid_{horizon}s"] = mid
                    event[f"raw_mid_markout_{horizon}s_bps"] = raw_markout
                    if valid_external_mid:
                        external_markout = (
                            (external_mid - fill_price)
                            / fill_price
                            * _TEN_THOUSAND
                            if event["side"] == "buy"
                            else (fill_price - external_mid)
                            / fill_price
                            * _TEN_THOUSAND
                        )
                        event[f"external_mid_{horizon}s"] = external_mid
                        event[
                            f"external_mid_markout_{horizon}s_bps"
                        ] = external_markout

    def _side_markout_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for side in ("buy", "sell"):
            side_summary: dict[str, Any] = {}
            side_events = [
                event for event in self.fill_markouts if event["side"] == side
            ]
            for horizon in _MARKOUT_HORIZONS:
                values = [
                    event.get(f"external_mid_markout_{horizon}s_bps")
                    for event in side_events
                    if event.get(f"external_mid_markout_{horizon}s_bps")
                    is not None
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

    def _authenticated_markout_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for side in ("buy", "sell"):
            side_events = [
                event for event in self.fill_markouts if event["side"] == side
            ]
            side_summary: dict[str, Any] = {
                "pending_count": sum(
                    event.get("attribution_state") != "authenticated"
                    for event in side_events
                )
            }
            for role in _FILL_ROLES:
                role_events = [
                    event
                    for event in side_events
                    if event.get("attribution_state") == "authenticated"
                    and event.get("fill_role") == role
                ]
                side_summary[role] = {
                    f"{horizon}s": _markout_stats(
                        [
                            value
                            for event in role_events
                            if (
                                value := event.get(
                                    f"external_mid_markout_{horizon}s_bps"
                                )
                            )
                            is not None
                        ]
                    )
                    for horizon in _MARKOUT_HORIZONS
                }
            summary[side] = side_summary
        return summary

    def authenticated_entry_markout_feedback(
        self,
        *,
        now_monotonic: float | None = None,
        half_life_seconds: int | None = None,
    ) -> dict[str, Any]:
        use_decay = (
            isinstance(now_monotonic, (int, float))
            and not isinstance(now_monotonic, bool)
            and math.isfinite(float(now_monotonic))
            and type(half_life_seconds) is int
            and half_life_seconds > 0
        )
        summary: dict[str, Any] = {}
        for side in ("buy", "sell"):
            events = [
                event
                for event in self.fill_markouts
                if event["side"] == side
                and event.get("attribution_state") == "authenticated"
                and event.get("fill_role") == "entry"
                and event.get("active_unwind") is False
                and event.get("observation_source") in _ENTRY_FEEDBACK_SOURCES
            ]
            side_summary: dict[str, Any] = {}
            for horizon in _ENTRY_FEEDBACK_HORIZONS:
                observed = [
                    event
                    for event in events
                    if event.get(f"external_mid_markout_{horizon}s_bps")
                    is not None
                ]
                stats = _markout_stats(
                    [
                        event.get(f"external_mid_markout_{horizon}s_bps")
                        for event in observed
                    ]
                )
                stats["ewma_bps"] = None
                if use_decay and observed:
                    half_life = Decimal(half_life_seconds)
                    weighted_sum = Decimal("0")
                    total_weight = Decimal("0")
                    for event in observed:
                        age = max(
                            Decimal("0"),
                            Decimal(
                                str(
                                    float(now_monotonic)
                                    - event["started_monotonic"]
                                )
                            ),
                        )
                        weight = (
                            -Decimal("2").ln() * age / half_life
                        ).exp()
                        weighted_sum += (
                            event.get(
                                f"external_mid_markout_{horizon}s_bps"
                            )
                            * weight
                        )
                        total_weight += weight
                    if total_weight > 0:
                        stats["ewma_bps"] = weighted_sum / total_weight
                side_summary[f"{horizon}s"] = stats
            summary[side] = side_summary
        return summary

    def snapshot(self, now: float) -> dict[str, Any]:
        self._accrue_controller_seconds(now)
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
            "quote_controller": dict(self.quote_controller),
            "controller_decision_history": [
                dict(item) for item in self.controller_decision_history
            ],
            "controller_decision_history_total": (
                self.controller_decision_history_total
            ),
            "controller_error_count": self.controller_error_count,
            "controller_ready_seconds": self.controller_ready_seconds,
            "controller_warming_seconds": self.controller_warming_seconds,
            "controller_bid_blocked_seconds": (
                self.controller_bid_blocked_seconds
            ),
            "controller_ask_blocked_seconds": (
                self.controller_ask_blocked_seconds
            ),
            "controller_both_blocked_seconds": (
                self.controller_both_blocked_seconds
            ),
            "controller_bid_extra_ticks": self.controller_bid_extra_ticks,
            "controller_ask_extra_ticks": self.controller_ask_extra_ticks,
            "controller_base_bid": self.controller_base_bid,
            "controller_base_ask": self.controller_base_ask,
            "controller_shadow_bid": self.controller_shadow_bid,
            "controller_shadow_ask": self.controller_shadow_ask,
            "controller_applied_bid": self.controller_applied_bid,
            "controller_applied_ask": self.controller_applied_ask,
            "controller_feature_snapshot": dict(
                self.controller_feature_snapshot
            ),
            "quote_contexts": [
                dict(context)
                for context in self._quote_context_by_order.values()
            ],
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
                "authenticated_events": sum(
                    event.get("attribution_state") == "authenticated"
                    for event in self.fill_markouts
                ),
                "pending_events": sum(
                    event.get("attribution_state") != "authenticated"
                    for event in self.fill_markouts
                ),
                "retained_authenticated_attributions": len(
                    self._authenticated_fill_attributions
                ),
            },
            "side_markout_summary": self._side_markout_summary(),
            "authenticated_markout_summary": (
                self._authenticated_markout_summary()
            ),
            "authenticated_entry_markout_feedback": (
                self.authenticated_entry_markout_feedback()
            ),
        }
