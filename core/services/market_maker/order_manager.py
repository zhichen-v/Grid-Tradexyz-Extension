from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Iterable

from ...adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from .config import MarketMakerConfig, is_step_aligned
from .models import (
    DesiredOrder,
    DesiredQuotes,
    ManagedOrder,
    MarketMetadata,
    OrderSlotState,
    RuntimeState,
)
from .risk_manager import RiskDecision


_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}
_UNCERTAIN_STATES = {
    OrderSlotState.UNCERTAIN_SUBMISSION,
    OrderSlotState.UNCERTAIN_CANCELLATION,
}


def _error_category(exc: BaseException) -> str:
    marker = (
        f"{type(exc).__name__} {exc}"
        .lower()
        .replace("_", "")
        .replace(" ", "")
    )
    if "429" in marker or "ratelimit" in marker:
        return "http_429"
    return type(exc).__name__


def _has_visible_fill(
    order: OrderData,
    *,
    previous_remaining: Decimal | None = None,
) -> bool:
    """Return whether an exchange read proves any fill occurred."""
    if order.status is OrderStatus.FILLED:
        return True
    try:
        remaining = Decimal(str(order.remaining))
    except (ArithmeticError, TypeError, ValueError):
        return False
    if not remaining.is_finite() or remaining < 0:
        return False

    try:
        filled = Decimal(str(order.filled))
    except (ArithmeticError, TypeError, ValueError):
        filled = None
    if filled is not None and filled.is_finite() and filled > 0:
        return True

    try:
        amount = Decimal(str(order.amount))
    except (ArithmeticError, TypeError, ValueError):
        amount = None
    if amount is not None and amount.is_finite() and amount > remaining:
        return True

    try:
        previous = (
            Decimal(str(previous_remaining))
            if previous_remaining is not None
            else None
        )
    except (ArithmeticError, TypeError, ValueError):
        previous = None
    return (
        previous is not None
        and previous.is_finite()
        and previous >= 0
        and 0 <= remaining < previous
    )


def _is_post_only_cancellation(order: OrderData) -> bool:
    return (
        order.status is OrderStatus.CANCELED
        and (order.raw_data or {}).get("post_only_canceled") is True
    )


@dataclass(frozen=True)
class ReconcileAction:
    side: OrderSide | None
    operation: str
    reason: str
    price: Decimal | None = None
    amount: Decimal | None = None
    reduce_only: bool = False
    success: bool | None = None


@dataclass(frozen=True)
class ReconcileResult:
    actions: tuple[ReconcileAction, ...]
    runtime_state: RuntimeState
    errors: tuple[str, ...] = ()
    position_refresh_required: bool = False


class MarketMakerOrderManager:
    """Own exactly one managed order per side and serialize all mutations."""

    def __init__(
        self,
        adapter: Any,
        config: MarketMakerConfig,
        metadata: MarketMetadata,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.metadata = metadata
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._slots: dict[OrderSide, ManagedOrder | None] = {
            OrderSide.BUY: None,
            OrderSide.SELL: None,
        }
        self._mutation_timestamps: deque[float] = deque()
        self._submission_ambiguity_latched = False
        self._post_only_event_generation = 0
        self._post_only_refreshed_generation = 0
        self._pending_post_only_cancellations = 0
        self._post_only_create_not_before = 0.0
        self._risk_reducing_create_not_before = 0.0
        self.runtime_state = RuntimeState.SYNCING
        self.pause_reason: str | None = None
        self._shutting_down = False
        self.last_result = ReconcileResult((), RuntimeState.SYNCING)

    @property
    def slots(self) -> dict[OrderSide, ManagedOrder | None]:
        return {
            side: replace(order) if order is not None else None
            for side, order in self._slots.items()
        }

    def snapshot(self) -> tuple[ManagedOrder, ...]:
        return tuple(
            replace(order) for order in self._slots.values() if order is not None
        )

    @property
    def has_uncertain_state(self) -> bool:
        return (
            self._submission_ambiguity_latched
            or bool(self.get_unresolved_submissions())
            or any(
                order is not None
                and (
                    order.state in _UNCERTAIN_STATES
                    or order.submission_uncertain
                    or order.cancellation_uncertain
                )
                for order in self._slots.values()
            )
        )

    @property
    def has_unknown_order_state(self) -> bool:
        return bool(
            self.pause_reason
            and self.pause_reason.startswith(
                ("unknown open order", "unknown open orders", "multiple open orders")
            )
        )

    def get_unresolved_submissions(self) -> list[dict[str, Any]]:
        getter = getattr(self.adapter, "get_unresolved_submissions", None)
        if not callable(getter):
            return []
        return list(getter())

    @property
    def post_only_book_refresh_required(self) -> bool:
        return (
            self._post_only_refreshed_generation
            < self._post_only_event_generation
        )

    def consume_post_only_cancellations(self) -> tuple[int, int]:
        count = self._pending_post_only_cancellations
        self._pending_post_only_cancellations = 0
        return count, self._post_only_event_generation

    def acknowledge_post_only_book_refresh(self, generation: int) -> None:
        confirmed_generation = min(
            max(0, int(generation)), self._post_only_event_generation
        )
        self._post_only_refreshed_generation = max(
            self._post_only_refreshed_generation, confirmed_generation
        )

    async def initialize(self) -> None:
        async with self._lock:
            resolver = getattr(
                self.adapter, "resolve_unresolved_submissions", None
            )
            if callable(resolver):
                await resolver()
            unresolved = self.get_unresolved_submissions()
            if unresolved:
                self._pause(
                    "startup unresolved submissions require reconciliation"
                )
                raise RuntimeError(self.pause_reason)

            orders = await self.adapter.get_open_orders(self.config.symbol)
            active = self._active_symbol_orders(orders)
            if not active:
                self.runtime_state = RuntimeState.SYNCING
                self.pause_reason = None
                return

            summary = ", ".join(str(order.id) for order in active)
            if self.config.dry_run:
                self._pause(f"startup open orders require operator review: {summary}")
                self.last_result = ReconcileResult(
                    (
                        ReconcileAction(
                            None,
                            "report",
                            "dry-run did not cancel startup open orders",
                        ),
                    ),
                    self.runtime_state,
                )
                return
            if self.config.startup_open_order_policy == "abort":
                self._pause(f"startup open orders found: {summary}")
                raise RuntimeError(self.pause_reason)

            self._record_mutation()
            await self.adapter.cancel_all_orders(self.config.symbol)
            remaining = self._active_symbol_orders(
                await self.adapter.get_open_orders(self.config.symbol)
            )
            if remaining:
                self._pause("startup cancel_all did not produce an empty order set")
                raise RuntimeError(self.pause_reason)
            self.runtime_state = RuntimeState.SYNCING
            self.pause_reason = None

    async def reconcile(
        self,
        desired: DesiredQuotes,
        risk: RiskDecision,
    ) -> ReconcileResult:
        async with self._lock:
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            position_refresh_required = False
            if self._shutting_down:
                return self._result(actions, ("order manager is stopping",))

            resolved = await self._resolve_uncertain_locked()
            position_refresh_required = any(
                _has_visible_fill(order) for order in resolved
            )
            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                position_refresh_required = (
                    await self._cancel_confirmable_locked(
                        "uncertain order state", actions, errors
                    )
                    or position_refresh_required
                )
                return self._result(
                    actions, errors, position_refresh_required
                )
            if (
                desired.bid is not None
                and desired.bid.side is not OrderSide.BUY
            ) or (
                desired.ask is not None
                and desired.ask.side is not OrderSide.SELL
            ):
                self._pause("desired quote side does not match its slot")
                errors.append(self.pause_reason)
                position_refresh_required = (
                    await self._cancel_confirmable_locked(
                        "invalid desired quote", actions, errors
                    )
                    or position_refresh_required
                )
                return self._result(
                    actions, errors, position_refresh_required
                )

            desired_by_side = {
                OrderSide.BUY: desired.bid,
                OrderSide.SELL: desired.ask,
            }
            quoting_states = {
                RuntimeState.ACTIVE,
                RuntimeState.RISK_REDUCTION,
            }
            quote_allowed = (
                risk.safe
                and risk.runtime_state in quoting_states
                and desired.runtime_state in quoting_states
            )
            if not quote_allowed:
                desired_by_side = {
                    OrderSide.BUY: None,
                    OrderSide.SELL: None,
                }

            # All required cancels happen before any create in this cycle.
            for side in (OrderSide.BUY, OrderSide.SELL):
                live = self._slots[side]
                target = desired_by_side[side]
                if live is None:
                    continue
                if target is None:
                    position_refresh_required = (
                        await self._cancel_locked(
                            side,
                            desired.reason or "side disabled",
                            actions,
                            errors,
                            safety=True,
                        )
                        or position_refresh_required
                    )
                    continue
                if live.state not in {
                    OrderSlotState.LIVE,
                    OrderSlotState.PARTIALLY_FILLED,
                }:
                    continue
                replace_reason = self._replacement_reason(live, target, risk)
                if replace_reason is None:
                    continue
                safety = self._is_safety_replacement(live, target, risk)
                age_ms = (self._monotonic() - live.created_monotonic) * 1000
                if not safety and age_ms < self.config.min_order_lifetime_ms:
                    continue
                position_refresh_required = (
                    await self._cancel_locked(
                        side,
                        replace_reason,
                        actions,
                        errors,
                        safety=safety,
                    )
                    or position_refresh_required
                )

            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                position_refresh_required = (
                    await self._cancel_confirmable_locked(
                        "uncertain order state", actions, errors
                    )
                    or position_refresh_required
                )
                return self._result(
                    actions, errors, position_refresh_required
                )
            if not quote_allowed:
                if risk.runtime_state not in quoting_states:
                    self.runtime_state = risk.runtime_state
                elif desired.runtime_state not in quoting_states:
                    self.runtime_state = desired.runtime_state
                else:
                    self.runtime_state = RuntimeState.PAUSED_ERROR
                return self._result(
                    actions, errors, position_refresh_required
                )
            if errors:
                return self._result(
                    actions, errors, position_refresh_required
                )
            if position_refresh_required:
                return self._result(actions, errors, True)
            if self._post_only_create_blocked():
                self.runtime_state = RuntimeState.SYNCING
                return self._result(
                    actions, errors, position_refresh_required
                )

            # Risk-reducing creates have priority over risk-increasing creates.
            placements = sorted(
                desired_by_side.items(),
                key=lambda item: (
                    item[1] is None or not item[1].reduce_only,
                    item[0] is OrderSide.SELL,
                ),
            )
            for side, target in placements:
                if target is None or self._slots[side] is not None:
                    continue
                validation_error = self._validate_desired(target, risk)
                if validation_error:
                    errors.append(validation_error)
                    continue
                action_start = len(actions)
                position_refresh_required = (
                    await self._place_locked(
                        target,
                        actions,
                        errors,
                        risk_reduction=(
                            risk.runtime_state is RuntimeState.RISK_REDUCTION
                        ),
                    )
                    or position_refresh_required
                )
                if self._post_only_create_blocked():
                    self.runtime_state = RuntimeState.SYNCING
                    return self._result(
                        actions, errors, position_refresh_required
                    )
                blocking = self._blocking_reason()
                if blocking:
                    self._pause(blocking)
                    position_refresh_required = (
                        await self._cancel_confirmable_locked(
                            "uncertain order state", actions, errors
                        )
                        or position_refresh_required
                    )
                    return self._result(
                        actions, errors, position_refresh_required
                    )
                if errors:
                    return self._result(
                        actions, errors, position_refresh_required
                    )

                if any(
                    action.operation == "place"
                    for action in actions[action_start:]
                ):
                    # Return after one live create attempt so order updates
                    # queued while awaiting the adapter are applied first.
                    if self.pause_reason is None:
                        self.runtime_state = desired.runtime_state
                    return self._result(
                        actions, errors, position_refresh_required
                    )

            if self.pause_reason is None:
                self.runtime_state = desired.runtime_state
            return self._result(actions, errors, position_refresh_required)

    async def handle_order_update(self, order: OrderData) -> None:
        async with self._lock:
            if order.symbol != self.config.symbol:
                return
            side = order.side
            slot = self._slots.get(side)
            if slot is None or not self._order_matches(slot, order):
                if order.status not in _TERMINAL_STATUSES:
                    self._pause(f"unknown open order update: {order.id}")
                return
            self._apply_order_update(side, order)

    async def resolve_unresolved_submissions(self) -> list[OrderData]:
        async with self._lock:
            return await self._resolve_uncertain_locked()

    async def sync_open_orders(self) -> bool:
        async with self._lock:
            resolved = await self._resolve_uncertain_locked()
            position_refresh_required = any(
                order.symbol == self.config.symbol
                and _has_visible_fill(order)
                for order in resolved
            )
            open_orders = self._active_symbol_orders(
                await self.adapter.get_open_orders(self.config.symbol)
            )
            matched_ids: set[int] = set()
            for side, slot in tuple(self._slots.items()):
                if slot is None:
                    continue
                matches = [
                    order
                    for order in open_orders
                    if order.side is side and self._order_matches(slot, order)
                ]
                if len(matches) > 1:
                    self._pause(f"multiple open orders match {side.value} slot")
                    continue
                if matches:
                    matched_ids.add(id(matches[0]))
                    position_refresh_required = (
                        _has_visible_fill(
                            matches[0], previous_remaining=slot.remaining
                        )
                        or position_refresh_required
                    )
                    self._apply_order_update(side, matches[0])
                    continue
                position_refresh_required = (
                    await self._resolve_missing_slot_locked(side, slot)
                    or position_refresh_required
                )

            unknown = [order for order in open_orders if id(order) not in matched_ids]
            if unknown:
                self._pause(
                    "unknown open orders: "
                    + ", ".join(str(order.id) for order in unknown)
                )
            elif not self.has_uncertain_state and self.pause_reason and self.pause_reason.startswith(
                ("unknown open order", "unknown open orders", "multiple open orders")
            ):
                self.pause_reason = None
                self.runtime_state = RuntimeState.SYNCING
            self._clear_resolved_uncertainty_pause()
            return position_refresh_required

    async def cancel_managed_orders(self, reason: str) -> ReconcileResult:
        async with self._lock:
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            position_refresh_required = await self._cancel_confirmable_locked(
                reason, actions, errors
            )
            return self._result(
                actions, errors, position_refresh_required
            )

    async def shutdown(self) -> None:
        async with self._lock:
            if self.runtime_state is RuntimeState.STOPPED:
                return
            self._shutting_down = True
            self.runtime_state = RuntimeState.STOPPING
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            open_orders: list[OrderData] = []
            await self._resolve_uncertain_locked()
            if self.config.cancel_on_shutdown:
                await self._cancel_confirmable_locked(
                    "shutdown", actions, errors
                )
            if not self.config.dry_run:
                await self._sync_for_shutdown_locked()
                open_orders = self._active_symbol_orders(
                    await self.adapter.get_open_orders(self.config.symbol)
                )
                if open_orders:
                    errors.append(
                        "target symbol open orders remain active after shutdown"
                    )
            remaining = [order for order in self._slots.values() if order is not None]
            unresolved = self.get_unresolved_submissions()
            if unresolved:
                errors.append("adapter submissions remain unresolved after shutdown")
            elif not remaining and not open_orders:
                # Earlier cancel errors are provisional once exact REST state
                # proves the target symbol empty and no mutation is unresolved.
                errors.clear()
            if errors or remaining:
                self.runtime_state = RuntimeState.PAUSED_ORDER_STATE
                details = errors or ["managed orders remain after shutdown"]
                raise RuntimeError("; ".join(details))
            self.runtime_state = RuntimeState.STOPPED
            self.pause_reason = None
            self.last_result = ReconcileResult(tuple(actions), self.runtime_state)

    async def _place_locked(
        self,
        desired: DesiredOrder,
        actions: list[ReconcileAction],
        errors: list[str],
        *,
        risk_reduction: bool,
    ) -> bool:
        operation = "would_place" if self.config.dry_run else "place"
        action = ReconcileAction(
            desired.side,
            operation,
            desired.reason,
            desired.price,
            desired.amount,
            desired.reduce_only,
        )
        if self.config.dry_run:
            actions.append(action)
            return False
        now = self._monotonic()
        normal_budget_available = self._create_budget_available()
        emergency_budget_available = (
            not normal_budget_available
            and risk_reduction
            and desired.reduce_only
            and now >= self._risk_reducing_create_not_before
        )
        if not normal_budget_available and not emergency_budget_available:
            actions.append(
                ReconcileAction(
                    desired.side,
                    "blocked",
                    "mutation budget exhausted",
                    desired.price,
                    desired.amount,
                    desired.reduce_only,
                )
            )
            return False
        if emergency_budget_available:
            self._risk_reducing_create_not_before = now + 60

        self._slots[desired.side] = ManagedOrder(
            side=desired.side,
            state=OrderSlotState.SUBMITTING,
            order_id=None,
            client_id=None,
            price=desired.price,
            amount=desired.amount,
            remaining=desired.amount,
            reduce_only=desired.reduce_only,
            created_monotonic=now,
            updated_monotonic=now,
        )
        self._record_mutation()
        actions.append(action)
        action_index = len(actions) - 1
        try:
            order = await self.adapter.create_order(
                self.config.symbol,
                desired.side,
                OrderType.LIMIT,
                desired.amount,
                desired.price,
                params={
                    "time_in_force": "POST_ONLY",
                    "reduce_only": desired.reduce_only,
                },
            )
        except asyncio.CancelledError:
            self._mark_submission_uncertain(
                desired.side, "create task was cancelled before confirmation"
            )
            raise
        except Exception as exc:
            category = _error_category(exc)
            if category == "http_429":
                actions[action_index] = replace(action, success=False)
                self._slots[desired.side] = None
                errors.append("create rejected: http_429")
                return False
            self._mark_submission_uncertain(desired.side, category)
            errors.append(f"create outcome uncertain: {category}")
            return False
        if order is None:
            self._mark_submission_uncertain(
                desired.side, "adapter returned no order confirmation"
            )
            errors.append("create outcome uncertain: no order confirmation")
            return False
        position_refresh_required = _has_visible_fill(order)
        if (
            order.symbol != self.config.symbol
            or order.side is not desired.side
            or order.status is OrderStatus.UNKNOWN
            or not any(
                value is not None and str(value).strip()
                for value in (order.id, order.client_id)
            )
        ):
            self._mark_submission_uncertain(
                desired.side, "adapter returned an invalid order confirmation"
            )
            errors.append("create outcome uncertain: invalid confirmation")
            return position_refresh_required
        if order.status in _TERMINAL_STATUSES:
            actions[action_index] = replace(
                action,
                reason=(
                    "post-only canceled"
                    if _is_post_only_cancellation(order)
                    else action.reason
                ),
                success=False,
            )
            self._slots[desired.side] = None
            if _is_post_only_cancellation(order):
                self._record_post_only_cancellation()
                return position_refresh_required
            errors.append(
                f"create returned terminal status: {order.status.value}"
            )
            return position_refresh_required

        self._apply_order_update(desired.side, order)
        if self._submission_uncertain(order):
            self._submission_ambiguity_latched = True
            slot = self._slots[desired.side]
            if slot is not None:
                slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
                slot.submission_uncertain = True
            self._pause("order submission outcome is uncertain")
            return position_refresh_required
        slot = self._slots[desired.side]
        if (
            slot is None
            or slot.state not in {
                OrderSlotState.LIVE,
                OrderSlotState.PARTIALLY_FILLED,
                OrderSlotState.SUBMITTING,
            }
            or slot.submission_uncertain
            or slot.cancellation_uncertain
        ):
            errors.append("create outcome uncertain: invalid confirmation")
            return position_refresh_required
        actions[action_index] = replace(action, success=True)
        return position_refresh_required

    async def _cancel_locked(
        self,
        side: OrderSide,
        reason: str,
        actions: list[ReconcileAction],
        errors: list[str],
        *,
        safety: bool,
    ) -> bool:
        slot = self._slots[side]
        if slot is None:
            return False
        operation = "would_cancel" if self.config.dry_run else "cancel"
        action = ReconcileAction(
            side,
            operation,
            reason,
            slot.price,
            slot.remaining,
            slot.reduce_only,
        )
        if self.config.dry_run:
            actions.append(action)
            return False
        if slot.order_id is None:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            self._pause("cannot cancel an order without a confirmed order id")
            errors.append(self.pause_reason or "missing order id")
            return False
        if not safety and not self._mutation_budget_available():
            actions.append(
                ReconcileAction(side, "blocked", "mutation budget exhausted")
            )
            return False

        actions.append(action)
        action_index = len(actions) - 1
        previous_state = slot.state
        slot.state = OrderSlotState.CANCELING
        slot.updated_monotonic = self._monotonic()
        self._record_mutation()
        try:
            result = await self.adapter.cancel_order(
                slot.order_id, self.config.symbol
            )
        except asyncio.CancelledError:
            self._mark_cancellation_uncertain(slot)
            raise
        except Exception as exc:
            category = _error_category(exc)
            if category == "http_429":
                actions[action_index] = replace(action, success=False)
                slot.state = previous_state
                slot.updated_monotonic = self._monotonic()
                errors.append("cancel rejected: http_429")
                return False
            self._mark_cancellation_uncertain(slot)
            errors.append(f"cancel outcome uncertain: {category}")
            return False
        if (
            self._cancellation_terminal(result)
            and result is not None
            and result.symbol == self.config.symbol
            and self._order_matches(slot, result)
        ):
            actions[action_index] = replace(action, success=True)
            self._slots[side] = None
            return True
        self._mark_cancellation_uncertain(slot)
        errors.append("cancel outcome is not terminal")
        return False

    async def _cancel_confirmable_locked(
        self,
        reason: str,
        actions: list[ReconcileAction],
        errors: list[str],
    ) -> bool:
        position_refresh_required = False
        for side in (OrderSide.BUY, OrderSide.SELL):
            slot = self._slots[side]
            if slot is None:
                continue
            if slot.state in {
                OrderSlotState.UNCERTAIN_SUBMISSION,
                OrderSlotState.UNCERTAIN_CANCELLATION,
            }:
                errors.append(f"{side.value} order state is uncertain")
                continue
            position_refresh_required = (
                await self._cancel_locked(
                    side, reason, actions, errors, safety=True
                )
                or position_refresh_required
            )
        return position_refresh_required

    async def _resolve_uncertain_locked(self) -> list[OrderData]:
        resolver = getattr(self.adapter, "resolve_unresolved_submissions", None)
        resolved: list[OrderData] = []
        if callable(resolver) and (
            self.get_unresolved_submissions()
            or any(
                order is not None
                and (
                    order.state is OrderSlotState.UNCERTAIN_SUBMISSION
                    or order.submission_uncertain
                )
                for order in self._slots.values()
            )
        ):
            resolved = list(await resolver())
            for order in resolved:
                slot = self._slots.get(order.side)
                if slot is not None and self._order_matches(slot, order):
                    self._apply_order_update(order.side, order)
        self._clear_resolved_uncertainty_pause()
        return resolved

    async def _resolve_missing_slot_locked(
        self, side: OrderSide, slot: ManagedOrder
    ) -> bool:
        history_getter = getattr(self.adapter, "get_order_history", None)
        history: Iterable[OrderData] = ()
        if callable(history_getter):
            history = await history_getter(self.config.symbol)
        terminal = next(
            (
                order
                for order in history
                if order.side is side
                and self._order_matches(slot, order)
                and order.status in _TERMINAL_STATUSES
            ),
            None,
        )
        if terminal is not None:
            if _is_post_only_cancellation(terminal):
                self._record_post_only_cancellation()
            self._slots[side] = None
            return _has_visible_fill(
                terminal, previous_remaining=slot.remaining
            )
        if slot.state in {
            OrderSlotState.CANCELING,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        }:
            self._mark_cancellation_uncertain(slot)
        else:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            self._pause(f"{side.value} order disappeared without terminal proof")
        return False

    async def _sync_for_shutdown_locked(self) -> None:
        open_orders = self._active_symbol_orders(
            await self.adapter.get_open_orders(self.config.symbol)
        )
        for side, slot in tuple(self._slots.items()):
            if slot is None:
                continue
            if any(
                order.side is side and self._order_matches(slot, order)
                for order in open_orders
            ):
                continue
            await self._resolve_missing_slot_locked(side, slot)

    def _apply_order_update(self, side: OrderSide, order: OrderData) -> None:
        if order.status is OrderStatus.UNKNOWN:
            slot = self._slots[side]
            if slot is not None:
                slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
                slot.submission_uncertain = True
                slot.updated_monotonic = self._monotonic()
            self._pause(f"{side.value} order status is unknown")
            return
        if order.status in _TERMINAL_STATUSES:
            if _is_post_only_cancellation(order):
                self._record_post_only_cancellation()
            self._slots[side] = None
            self._clear_resolved_uncertainty_pause()
            return
        previous = self._slots[side]
        now = self._monotonic()
        try:
            amount = Decimal(str(order.amount))
            remaining = Decimal(str(order.remaining))
            price = (
                Decimal(str(order.price))
                if order.price is not None
                else previous.price if previous is not None else Decimal("0")
            )
            valid_values = (
                amount.is_finite()
                and remaining.is_finite()
                and price.is_finite()
                and amount > 0
                and remaining >= 0
                and remaining <= amount
                and price > 0
            )
        except (ArithmeticError, TypeError, ValueError):
            valid_values = False
            amount = Decimal("0")
            remaining = Decimal("0")
            price = Decimal("0")
        if not valid_values:
            if previous is not None:
                previous.state = OrderSlotState.UNCERTAIN_SUBMISSION
                previous.submission_uncertain = True
                previous.updated_monotonic = now
            self._pause(f"invalid {side.value} order update")
            return
        state = (
            OrderSlotState.PARTIALLY_FILLED
            if remaining < amount
            else (
                OrderSlotState.LIVE
                if order.status is OrderStatus.OPEN
                else OrderSlotState.SUBMITTING
            )
        )
        self._slots[side] = ManagedOrder(
            side=side,
            state=state,
            order_id=str(order.id) if order.id is not None else None,
            client_id=(
                str(order.client_id) if order.client_id is not None else None
            ),
            price=price,
            amount=amount,
            remaining=remaining,
            reduce_only=(
                previous.reduce_only
                if previous is not None
                else bool((order.params or {}).get("reduce_only", False))
            ),
            created_monotonic=(
                previous.created_monotonic if previous is not None else now
            ),
            updated_monotonic=now,
            submission_uncertain=self._submission_uncertain(order),
            cancellation_uncertain=False,
        )
        if self._submission_uncertain(order):
            current = self._slots[side]
            if current is not None:
                current.state = OrderSlotState.UNCERTAIN_SUBMISSION

    def _clear_resolved_uncertainty_pause(self) -> None:
        if self._submission_ambiguity_latched:
            submission_unresolved = bool(self.get_unresolved_submissions()) or any(
                order is not None
                and (
                    order.state is OrderSlotState.UNCERTAIN_SUBMISSION
                    or order.submission_uncertain
                )
                for order in self._slots.values()
            )
            if not submission_unresolved:
                self._submission_ambiguity_latched = False
        if self.has_uncertain_state or not self.pause_reason:
            return
        if self.pause_reason.startswith(
            (
                "order submission outcome is uncertain",
                "order cancellation outcome is uncertain",
                "cannot cancel an order without",
                "buy order status is unknown",
                "sell order status is unknown",
                "invalid buy order update",
                "invalid sell order update",
            )
        ) or "order disappeared without terminal proof" in self.pause_reason:
            self.pause_reason = None
            self.runtime_state = RuntimeState.SYNCING

    def _validate_desired(
        self, desired: DesiredOrder, risk: RiskDecision
    ) -> str | None:
        expected_amount = (
            risk.buy_amount
            if desired.side is OrderSide.BUY
            else risk.sell_amount
        )
        expected_reduce_only = (
            risk.buy_reduce_only
            if desired.side is OrderSide.BUY
            else risk.sell_reduce_only
        )
        if expected_amount is None or desired.amount > expected_amount:
            return f"{desired.side.value} desired amount exceeds risk decision"
        if desired.reduce_only != expected_reduce_only:
            return f"{desired.side.value} reduce_only conflicts with risk decision"
        if (
            not desired.amount.is_finite()
            or not desired.price.is_finite()
            or desired.amount <= 0
            or desired.price <= 0
            or desired.amount < self.metadata.min_base_amount
            or desired.amount * desired.price < self.metadata.min_quote_amount
            or not is_step_aligned(
                desired.amount, self.metadata.quantity_step
            )
            or not is_step_aligned(desired.price, self.metadata.price_tick)
        ):
            return f"{desired.side.value} desired price/amount is invalid"
        return None

    def _replacement_reason(
        self,
        live: ManagedOrder,
        desired: DesiredOrder,
        risk: RiskDecision,
    ) -> str | None:
        if self._is_safety_replacement(live, desired, risk):
            return "live order no longer matches risk"
        threshold = (
            self.metadata.price_tick
            * Decimal(self.config.reprice_threshold_ticks)
        )
        if abs(live.price - desired.price) >= threshold:
            return "reprice threshold reached"
        if abs(live.remaining - desired.amount) >= self.metadata.quantity_step:
            return "amount changed by at least one quantity step"
        return None

    @staticmethod
    def _is_safety_replacement(
        live: ManagedOrder,
        desired: DesiredOrder,
        risk: RiskDecision,
    ) -> bool:
        if live.reduce_only != desired.reduce_only:
            return True
        allowed = (
            risk.buy_amount if live.side is OrderSide.BUY else risk.sell_amount
        )
        return allowed is None or live.remaining > allowed

    def _blocking_reason(self) -> str | None:
        if self.pause_reason:
            return self.pause_reason
        if self._submission_ambiguity_latched:
            return "order submission ambiguity is latched until restart"
        for side, order in self._slots.items():
            if order is None:
                continue
            if order.state in _UNCERTAIN_STATES:
                return f"{side.value} slot is {order.state.value}"
            if order.submission_uncertain or order.cancellation_uncertain:
                return f"{side.value} order outcome is uncertain"
        if self.get_unresolved_submissions():
            return "adapter submissions remain unresolved"
        return None

    def _order_matches(self, slot: ManagedOrder, order: OrderData) -> bool:
        order_id = str(order.id) if order.id is not None else None
        client_id = (
            str(order.client_id) if order.client_id is not None else None
        )
        order_id_matches = bool(
            slot.order_id and order_id and slot.order_id == order_id
        )
        client_id_matches = bool(
            slot.client_id and client_id and slot.client_id == client_id
        )
        return order.symbol == self.config.symbol and (
            order_id_matches or client_id_matches
        )

    def _active_symbol_orders(
        self, orders: Iterable[OrderData] | None
    ) -> list[OrderData]:
        return [
            order
            for order in (orders or ())
            if order.symbol == self.config.symbol
            and order.status not in _TERMINAL_STATUSES
        ]

    @staticmethod
    def _submission_uncertain(order: OrderData) -> bool:
        return bool(
            (order.params or {}).get("submission_uncertain")
            or (order.raw_data or {}).get("submission_uncertain")
        )

    @staticmethod
    def _cancellation_terminal(order: OrderData | None) -> bool:
        if order is None:
            return False
        params = order.params or {}
        raw_data = order.raw_data or {}
        flag = params.get("cancel_terminal", raw_data.get("cancel_terminal"))
        if flag is False:
            return False
        return flag is True or order.status in _TERMINAL_STATUSES

    def _mark_submission_uncertain(self, side: OrderSide, reason: str) -> None:
        self._submission_ambiguity_latched = True
        slot = self._slots[side]
        if slot is not None:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            slot.updated_monotonic = self._monotonic()
        self._pause(f"order submission outcome is uncertain: {reason}")

    def _mark_cancellation_uncertain(self, slot: ManagedOrder) -> None:
        slot.state = OrderSlotState.UNCERTAIN_CANCELLATION
        slot.cancellation_uncertain = True
        slot.updated_monotonic = self._monotonic()
        self._pause("order cancellation outcome is uncertain")

    def _record_post_only_cancellation(self) -> None:
        self._post_only_event_generation += 1
        self._pending_post_only_cancellations += 1
        cooldown = self.config.refresh_interval_ms / 1000
        self._post_only_create_not_before = max(
            self._post_only_create_not_before,
            self._monotonic() + cooldown,
        )
        self.runtime_state = RuntimeState.SYNCING

    def _post_only_create_blocked(self) -> bool:
        return (
            self.post_only_book_refresh_required
            or self._monotonic() < self._post_only_create_not_before
        )

    def _mutation_budget_available(self) -> bool:
        self._purge_mutations()
        return len(self._mutation_timestamps) < self.config.max_mutations_per_minute

    def _create_budget_available(self) -> bool:
        return self._mutation_budget_available()

    def _record_mutation(self) -> None:
        self._purge_mutations()
        self._mutation_timestamps.append(self._monotonic())

    def _purge_mutations(self) -> None:
        cutoff = self._monotonic() - 60
        while self._mutation_timestamps and self._mutation_timestamps[0] <= cutoff:
            self._mutation_timestamps.popleft()

    def _pause(self, reason: str) -> None:
        self.pause_reason = reason
        self.runtime_state = RuntimeState.PAUSED_ORDER_STATE

    def _result(
        self,
        actions: list[ReconcileAction],
        errors: Iterable[str],
        position_refresh_required: bool = False,
    ) -> ReconcileResult:
        result = ReconcileResult(
            tuple(actions),
            self.runtime_state,
            tuple(errors),
            position_refresh_required,
        )
        self.last_result = result
        return result
