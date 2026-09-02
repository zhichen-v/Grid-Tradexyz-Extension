from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from ...adapters.exchanges.models import OrderSide
from .config import is_step_aligned
from .controllers.base import QuoteControllerDecision, SideQuoteAdjustment
from .models import (
    DesiredOrder,
    DesiredQuotes,
    MarketMetadata,
    OrderIntentKind,
    OrderIntentMetadata,
    PositionSnapshot,
)
from .risk_manager import RiskDecision


@dataclass(frozen=True)
class QuoteArbiterContext:
    ordinary_flat_entry: bool = True
    economic_stop_pending: bool = False
    entry_admission_allowed: bool = True
    risk_reduction_active: bool = False
    inventory_unwind_active: bool = False
    active_unwind_pending: bool = False
    active_unwind_ready: bool = False
    apply_bid: bool = True
    apply_ask: bool = True


def _finite_decimal(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (not positive or value > 0)
    )


def _reason(base_reason: str, code: str) -> str:
    return f"{base_reason}; {code}" if base_reason else code


def _controller_code(decision: QuoteControllerDecision) -> str:
    return (
        f"controller={decision.controller};decision_id={decision.decision_id};"
        f"bid_extra_ticks={decision.bid.extra_spread_ticks};"
        f"ask_extra_ticks={decision.ask.extra_spread_ticks};"
        f"bid_blocked={str(decision.bid.blocked).lower()};"
        f"ask_blocked={str(decision.ask.blocked).lower()}"
    )


def _fail_closed(base: DesiredQuotes, reason: str) -> DesiredQuotes:
    return replace(
        base,
        bid=None,
        ask=None,
        controller_blocked_sides=frozenset(
            order.side
            for order in (base.bid, base.ask)
            if order is not None and not order.reduce_only
        ),
        reason=_reason(base.reason, f"controller_error={reason}"),
    )


def _valid_adjustment(adjustment: object) -> bool:
    return (
        isinstance(adjustment, SideQuoteAdjustment)
        and type(adjustment.extra_spread_ticks) is int
        and adjustment.extra_spread_ticks >= 0
        and type(adjustment.blocked) is bool
        and _finite_decimal(adjustment.toxicity_score_ticks)
        and adjustment.toxicity_score_ticks >= 0
        and type(adjustment.directional_confirmations) is int
        and adjustment.directional_confirmations >= 0
        and isinstance(adjustment.reason, str)
    )


def controller_decision_error(
    decision: object, *, expected_mode: str
) -> str | None:
    if not isinstance(decision, QuoteControllerDecision):
        return "invalid_controller_decision_type"
    if decision.mode != expected_mode:
        return "controller_mode_mismatch"
    if type(decision.ready) is not bool:
        return "invalid_controller_ready"
    if decision.applies_to_entry_only is not True:
        return "invalid_controller_scope"
    if not isinstance(decision.controller, str) or not decision.controller:
        return "invalid_controller_name"
    if not isinstance(decision.reason, str):
        return "invalid_controller_reason"
    if type(decision.decision_id) is not int or decision.decision_id <= 0:
        return "invalid_controller_decision_id"
    if not _valid_adjustment(decision.bid) or not _valid_adjustment(decision.ask):
        return "invalid_controller_adjustment"
    return None


def _valid_active_decision(decision: object) -> bool:
    return (
        controller_decision_error(decision, expected_mode="active") is None
        and isinstance(decision, QuoteControllerDecision)
        and decision.ready is True
        and decision.features is not None
    )


def _valid_order(
    order: DesiredOrder | None,
    expected_side: OrderSide,
    tick: Decimal,
) -> bool:
    if order is None:
        return True
    return (
        isinstance(order, DesiredOrder)
        and order.side is expected_side
        and _finite_decimal(order.price, positive=True)
        and is_step_aligned(order.price, tick)
        and _finite_decimal(order.amount, positive=True)
        and order.reduce_only is False
        and isinstance(order.reason, str)
    )


def _protective_precedence(
    base: DesiredQuotes,
    risk: RiskDecision,
    context: QuoteArbiterContext,
) -> bool:
    orders = tuple(order for order in (base.bid, base.ask) if order is not None)
    runtime_state = getattr(getattr(risk, "runtime_state", None), "value", None)
    return (
        any(getattr(order, "reduce_only", False) for order in orders)
        or getattr(risk, "safe", False) is not True
        or runtime_state != "active"
        or getattr(risk, "buy_reduce_only", False)
        or getattr(risk, "sell_reduce_only", False)
        or not context.ordinary_flat_entry
        or context.economic_stop_pending
        or not context.entry_admission_allowed
        or context.risk_reduction_active
        or context.inventory_unwind_active
        or context.active_unwind_pending
        or context.active_unwind_ready
    )


def _apply_side(
    order: DesiredOrder | None,
    adjustment: SideQuoteAdjustment,
    tick: Decimal,
    *,
    is_bid: bool,
    decision: QuoteControllerDecision,
) -> DesiredOrder | None:
    if order is None or adjustment.blocked:
        return None
    offset = Decimal(adjustment.extra_spread_ticks) * tick
    price = order.price - offset if is_bid else order.price + offset
    side_name = "bid" if is_bid else "ask"
    code = (
        f"controller={decision.controller};decision_id={decision.decision_id};"
        f"side={side_name};extra_ticks={adjustment.extra_spread_ticks};"
        f"blocked=false;reason={adjustment.reason}"
    )
    return replace(
        order,
        price=price,
        reason=_reason(order.reason, code),
        intent=OrderIntentMetadata(
            kind=OrderIntentKind.CONTROLLER_ENTRY,
            revision=decision.decision_id,
            controller_decision_id=decision.decision_id,
            controller_outward_only=True,
            controller_extra_spread_ticks=adjustment.extra_spread_ticks,
        ),
    )


def apply_entry_controller(
    base: DesiredQuotes,
    decision: QuoteControllerDecision,
    position: PositionSnapshot,
    risk: RiskDecision,
    metadata: MarketMetadata,
    *,
    context: QuoteArbiterContext | None = None,
) -> DesiredQuotes:
    arbiter_context = context or QuoteArbiterContext()
    signed_size = getattr(position, "signed_size", None)
    if _finite_decimal(signed_size) and signed_size != 0:
        return base
    if _protective_precedence(base, risk, arbiter_context):
        return base

    mode = getattr(decision, "mode", None)
    if mode in {"fixed", "shadow"}:
        return base
    if not _finite_decimal(signed_size) or signed_size != 0:
        return _fail_closed(base, "invalid_flat_position")
    if not _valid_active_decision(decision):
        return _fail_closed(base, "invalid_active_decision")
    if type(arbiter_context.apply_bid) is not bool or type(
        arbiter_context.apply_ask
    ) is not bool:
        return _fail_closed(base, "invalid_active_side_gate")
    tick = getattr(metadata, "price_tick", None)
    if not _finite_decimal(tick, positive=True):
        return _fail_closed(base, "invalid_price_tick")
    if not _valid_order(base.bid, OrderSide.BUY, tick) or not _valid_order(
        base.ask, OrderSide.SELL, tick
    ):
        return _fail_closed(base, "invalid_base_quotes")
    if base.bid is not None and base.ask is not None:
        if base.bid.price >= base.ask.price:
            return _fail_closed(base, "crossed_base_quotes")

    bid = (
        _apply_side(
            base.bid,
            decision.bid,
            tick,
            is_bid=True,
            decision=decision,
        )
        if arbiter_context.apply_bid
        else base.bid
    )
    ask = (
        _apply_side(
            base.ask,
            decision.ask,
            tick,
            is_bid=False,
            decision=decision,
        )
        if arbiter_context.apply_ask
        else base.ask
    )
    if bid is not None and (
        not _finite_decimal(bid.price, positive=True)
        or not is_step_aligned(bid.price, tick)
        or (base.bid is not None and bid.price > base.bid.price)
    ):
        return _fail_closed(base, "invalid_applied_bid")
    if ask is not None and (
        not _finite_decimal(ask.price, positive=True)
        or not is_step_aligned(ask.price, tick)
        or (base.ask is not None and ask.price < base.ask.price)
    ):
        return _fail_closed(base, "invalid_applied_ask")
    if bid is not None and ask is not None and bid.price >= ask.price:
        return _fail_closed(base, "crossed_applied_quotes")

    blocked_sides = frozenset(
        side
        for side, enabled, base_order, applied_order, adjustment in (
            (
                OrderSide.BUY,
                arbiter_context.apply_bid,
                base.bid,
                bid,
                decision.bid,
            ),
            (
                OrderSide.SELL,
                arbiter_context.apply_ask,
                base.ask,
                ask,
                decision.ask,
            ),
        )
        if enabled
        and base_order is not None
        and applied_order is None
        and adjustment.blocked
    )

    return replace(
        base,
        bid=bid,
        ask=ask,
        controller_blocked_sides=blocked_sides,
        reason=_reason(base.reason, _controller_code(decision)),
    )
