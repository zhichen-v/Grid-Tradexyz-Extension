from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Iterable, Mapping

from ...adapters.exchanges.exceptions import (
    OrderSubmissionNotSentError,
    OrderSubmissionRejectedError,
)
from ...adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from .config import MarketMakerConfig, is_step_aligned
from .models import (
    DesiredOrder,
    DesiredQuotes,
    ManagedOrder,
    MarketMetadata,
    OrderIntentKind,
    OrderIntentMetadata,
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
_ACTIVE_TERMINAL_MAX_POLLS = 10
_ACTIVE_TERMINAL_POLL_SECONDS = 0.5
_TERMINAL_INTENT_CONTEXT_LIMIT = 512


class ReconcileActionCause(Enum):
    NORMAL = "normal"
    SAFETY = "safety"
    CONTROLLER_PROTECTIVE = "controller_protective"
    CONTROLLER_BLOCK = "controller_block"
    CONTROLLER_RESUME = "controller_resume"


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
    try:
        remaining = Decimal(str(order.remaining))
    except (ArithmeticError, TypeError, ValueError):
        remaining = None

    try:
        previous = (
            Decimal(str(previous_remaining))
            if previous_remaining is not None
            else None
        )
    except (ArithmeticError, TypeError, ValueError):
        previous = None
    if previous is not None and previous.is_finite() and previous >= 0:
        return _valid_limit_order_values(order) and (
            (order.status is OrderStatus.FILLED and previous > 0)
            or (
                remaining is not None
                and remaining.is_finite()
                and 0 <= remaining < previous
            )
        )

    if order.status is OrderStatus.FILLED:
        return _valid_limit_order_values(order)
    if remaining is None or not remaining.is_finite() or remaining < 0:
        return False

    try:
        filled = Decimal(str(order.filled))
    except (ArithmeticError, TypeError, ValueError):
        filled = None
    if (
        filled is not None
        and filled.is_finite()
        and filled > 0
        and _valid_limit_order_values(order)
    ):
        return True

    try:
        amount = Decimal(str(order.amount))
    except (ArithmeticError, TypeError, ValueError):
        amount = None
    if (
        amount is not None
        and amount.is_finite()
        and amount > remaining
        and _valid_limit_order_values(order)
    ):
        return True

    return False


def _valid_limit_order_values(order: OrderData) -> bool:
    try:
        amount = Decimal(str(order.amount))
        remaining = Decimal(str(order.remaining))
        price = Decimal(str(order.price))
    except (ArithmeticError, TypeError, ValueError):
        return False
    return (
        amount.is_finite()
        and remaining.is_finite()
        and price.is_finite()
        and amount > 0
        and 0 <= remaining <= amount
        and price > 0
        and (order.status is not OrderStatus.FILLED or remaining == 0)
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
    order_id: str | None = None
    cause: ReconcileActionCause = ReconcileActionCause.NORMAL
    intent: OrderIntentMetadata | None = None


@dataclass(frozen=True)
class ReconcileResult:
    actions: tuple[ReconcileAction, ...]
    runtime_state: RuntimeState
    errors: tuple[str, ...] = ()
    position_refresh_required: bool = False
    fill_observed: bool = False
    observed_fill_orders: tuple[OrderData, ...] = ()


@dataclass
class _OrderEffect:
    position_refresh_required: bool = False
    fill_observed: bool = False
    observed_fill_orders: list[OrderData] = field(default_factory=list)

    def include(self, other: "_OrderEffect") -> None:
        self.position_refresh_required |= other.position_refresh_required
        self.fill_observed |= other.fill_observed
        self.observed_fill_orders.extend(other.observed_fill_orders)


class MarketMakerOrderManager:
    """Own exactly one managed order per side and serialize all mutations."""

    def __init__(
        self,
        adapter: Any,
        config: MarketMakerConfig,
        metadata: MarketMetadata,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.metadata = metadata
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._slots: dict[OrderSide, ManagedOrder | None] = {
            OrderSide.BUY: None,
            OrderSide.SELL: None,
        }
        self._mutation_timestamps: deque[float] = deque()
        self._dry_run_mutation_timestamps: deque[float] = deque()
        self._dry_run_order_sequence = 0
        self._submission_ambiguity_latched = False
        self._resolved_ambiguous_cancellations = 0
        self._post_only_event_generation = 0
        self._post_only_refreshed_generation = 0
        self._pending_post_only_cancellations = 0
        self._post_only_create_not_before = 0.0
        self._risk_reducing_create_not_before = 0.0
        self._mutation_generation = 0
        self._known_order_ids: set[str] = set()
        self._order_intent_contexts: dict[str, OrderIntentMetadata] = {}
        self._terminal_order_intent_contexts: dict[
            str, OrderIntentMetadata
        ] = {}
        self._controller_protective_cancel_monotonic: dict[
            OrderSide, float
        ] = {}
        self._pending_controller_protective_target: dict[
            OrderSide, DesiredOrder
        ] = {}
        self._controller_blocked_sides: set[OrderSide] = set()
        self._active_unwind_order_ids: set[str] = set()
        self._active_unwind_side: OrderSide | None = None
        self._active_unwind_prepare_sequence = 0
        self._active_unwind_prepared_generation: int | None = None
        self._active_unwind_not_sent_failures = 0
        self._active_unwind_not_sent_until = 0.0
        self._terminal_orders: dict[
            tuple[OrderSide, str, str], ManagedOrder
        ] = {}
        self.runtime_state = RuntimeState.SYNCING
        self.pause_reason: str | None = None
        self._shutting_down = False
        self.last_result = ReconcileResult((), RuntimeState.SYNCING)
        self.last_sync_result = ReconcileResult((), RuntimeState.SYNCING)

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
    def known_order_ids(self) -> frozenset[str]:
        """Confirmed order ids attributable to this manager runtime."""
        return frozenset(self._known_order_ids)

    @property
    def terminal_order_ids(self) -> frozenset[str]:
        """Order IDs backed by exact terminal exchange evidence."""
        return frozenset(
            proof.order_id
            for proof in self._terminal_orders.values()
            if proof.order_id
        )

    @property
    def order_intent_contexts(self) -> Mapping[str, OrderIntentMetadata]:
        """Confirmed live order intent, detached from mutable manager state."""
        return MappingProxyType(dict(self._order_intent_contexts))

    @property
    def terminal_order_intent_contexts(
        self,
    ) -> Mapping[str, OrderIntentMetadata]:
        """Bounded intent evidence backed by exact terminal order proof."""
        return MappingProxyType(dict(self._terminal_order_intent_contexts))

    @property
    def active_unwind_order_ids(self) -> frozenset[str]:
        """Confirmed order ids attributable to the isolated taker lane."""
        return frozenset(self._active_unwind_order_ids)

    @property
    def active_unwind_pending(self) -> bool:
        return self._active_unwind_side is not None

    @property
    def mutation_generation(self) -> int:
        return self._mutation_generation

    @property
    def active_unwind_prepared_generation(self) -> int | None:
        """One-shot zero-open-order proof awaiting fresh external truth."""
        return self._active_unwind_prepared_generation

    @property
    def has_uncertain_state(self) -> bool:
        return (
            self._submission_ambiguity_latched
            or bool(self.get_unresolved_submissions())
            or bool(self.get_unresolved_cancellations())
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
    def unresolved_cancellation_count(self) -> int:
        unresolved = {
            (self.config.symbol, str(order.order_id))
            for order in self._slots.values()
            if order is not None
            and order.order_id is not None
            and (
                order.state is OrderSlotState.UNCERTAIN_CANCELLATION
                or order.cancellation_uncertain
            )
        }
        confirming = {
            (self.config.symbol, str(order.order_id))
            for order in self._slots.values()
            if order is not None
            and order.order_id is not None
            and order.state is OrderSlotState.CANCELING
            and not order.cancellation_uncertain
        }
        unresolved.update(self.get_unresolved_cancellations() - confirming)
        return len(unresolved)

    @property
    def resolved_ambiguous_cancellations(self) -> int:
        return self._resolved_ambiguous_cancellations

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

    def get_unresolved_cancellations(self) -> set[tuple[str, str]]:
        getter = getattr(self.adapter, "get_unresolved_cancellations", None)
        if not callable(getter):
            return set()
        return {
            (str(symbol), str(order_id))
            for symbol, order_id in getter()
            if str(symbol) == self.config.symbol
        }

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
            self._invalidate_active_unwind_preparation()
            self._active_unwind_not_sent_failures = 0
            self._active_unwind_not_sent_until = 0.0
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
            if self.get_unresolved_cancellations():
                self._pause(
                    "startup unresolved cancellations require terminal proof"
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
            self._invalidate_active_unwind_preparation()
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            effect = _OrderEffect()
            if self._shutting_down:
                return self._result(actions, ("order manager is stopping",))

            resolved = await self._resolve_uncertain_locked()
            effect.observed_fill_orders.extend(
                order for order in resolved if _has_visible_fill(order)
            )
            effect.fill_observed = bool(effect.observed_fill_orders)
            effect.position_refresh_required = effect.fill_observed
            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                effect.include(
                    await self._cancel_confirmable_locked(
                        "uncertain order state", actions, errors
                    )
                )
                return self._result(actions, errors, effect)
            if (
                desired.bid is not None
                and desired.bid.side is not OrderSide.BUY
            ) or (
                desired.ask is not None
                and desired.ask.side is not OrderSide.SELL
            ):
                self._pause("desired quote side does not match its slot")
                errors.append(self.pause_reason)
                effect.include(
                    await self._cancel_confirmable_locked(
                        "invalid desired quote", actions, errors
                    )
                )
                return self._result(actions, errors, effect)

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
                    controller_block = (
                        quote_allowed
                        and side in desired.controller_blocked_sides
                    )
                    action_start = len(actions)
                    effect.include(
                        await self._cancel_locked(
                            side,
                            desired.reason or "side disabled",
                            actions,
                            errors,
                            safety=True,
                            cause=(
                                ReconcileActionCause.CONTROLLER_BLOCK
                                if controller_block
                                else ReconcileActionCause.SAFETY
                            ),
                        )
                    )
                    if controller_block and any(
                        (
                            action.operation == "cancel"
                            and action.success is True
                        )
                        or (
                            self.config.dry_run
                            and action.operation == "would_cancel"
                        )
                        for action in actions[action_start:]
                    ):
                        self._controller_blocked_sides.add(side)
                    if self.unresolved_cancellation_count:
                        break
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
                protective = self.is_controller_protective_outward_revision(
                    live, target
                )
                now = self._monotonic()
                if protective:
                    last = self._controller_protective_cancel_monotonic.get(
                        side
                    )
                    interval_seconds = (
                        self.config.toxicity_outward_reprice_min_interval_ms
                        / 1000
                    )
                    if last is not None and (
                        now < last or now - last < interval_seconds
                    ):
                        actions.append(
                            ReconcileAction(
                                side,
                                "deferred",
                                "controller protective reprice interval",
                                target.price,
                                target.amount,
                                target.reduce_only,
                                cause=(
                                    ReconcileActionCause.CONTROLLER_PROTECTIVE
                                ),
                                intent=target.intent,
                            )
                        )
                        continue
                age_ms = (now - live.created_monotonic) * 1000
                if (
                    not safety
                    and not protective
                    and age_ms < self.config.min_order_lifetime_ms
                ):
                    continue
                action_start = len(actions)
                effect.include(
                    await self._cancel_locked(
                        side,
                        replace_reason,
                        actions,
                        errors,
                        safety=safety,
                        cause=(
                            ReconcileActionCause.CONTROLLER_PROTECTIVE
                            if protective
                            else ReconcileActionCause.SAFETY
                            if safety
                            else ReconcileActionCause.NORMAL
                        ),
                    )
                )
                new_actions = actions[action_start:]
                if protective and any(
                    action.operation in {"cancel", "would_cancel"}
                    for action in new_actions
                ):
                    self._controller_protective_cancel_monotonic[side] = now
                if protective and any(
                    (
                        action.operation == "cancel"
                        and action.success is True
                    )
                    or (
                        self.config.dry_run
                        and action.operation == "would_cancel"
                    )
                    for action in new_actions
                ):
                    assert target.intent is not None
                    self._pending_controller_protective_target[side] = target
                if self.unresolved_cancellation_count:
                    break

            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                effect.include(
                    await self._cancel_confirmable_locked(
                        "uncertain order state", actions, errors
                    )
                )
                return self._result(actions, errors, effect)
            if not quote_allowed:
                if risk.runtime_state not in quoting_states:
                    self.runtime_state = risk.runtime_state
                elif desired.runtime_state not in quoting_states:
                    self.runtime_state = desired.runtime_state
                else:
                    self.runtime_state = RuntimeState.PAUSED_ERROR
                return self._result(actions, errors, effect)
            if errors:
                return self._result(actions, errors, effect)
            if effect.position_refresh_required:
                return self._result(actions, errors, effect)
            if self._post_only_create_blocked():
                self.runtime_state = RuntimeState.SYNCING
                return self._result(actions, errors, effect)
            if self.config.dry_run and any(
                action.operation == "would_cancel" for action in actions
            ):
                if self.pause_reason is None:
                    self.runtime_state = desired.runtime_state
                return self._result(actions, errors, effect)

            # Risk-reducing creates have priority over risk-increasing creates.
            # For ordinary two-sided quotes, spend scarce mutation budget on
            # the inventory-reducing side first. Flat inventory keeps BUY-first.
            preferred_side = (
                OrderSide.SELL
                if risk.inventory_ratio > 0
                else OrderSide.BUY
            )
            placements = sorted(
                desired_by_side.items(),
                key=lambda item: (
                    item[1] is None or not item[1].reduce_only,
                    item[0] is not preferred_side,
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
                effect.include(
                    await self._place_locked(
                        target,
                        actions,
                        errors,
                        risk_reduction=(
                            risk.runtime_state is RuntimeState.RISK_REDUCTION
                        ),
                    )
                )
                if self._post_only_create_blocked():
                    self.runtime_state = RuntimeState.SYNCING
                    return self._result(actions, errors, effect)
                blocking = self._blocking_reason()
                if blocking:
                    self._pause(blocking)
                    effect.include(
                        await self._cancel_confirmable_locked(
                            "uncertain order state", actions, errors
                        )
                    )
                    return self._result(actions, errors, effect)
                if errors:
                    return self._result(actions, errors, effect)

                if any(
                    action.operation in {"place", "would_place"}
                    for action in actions[action_start:]
                ):
                    # Keep live and dry-run create cadence aligned. Live order
                    # updates queued during the adapter call are applied first;
                    # dry-run consumes the same one-create-per-cycle boundary.
                    if self.pause_reason is None:
                        self.runtime_state = desired.runtime_state
                    return self._result(actions, errors, effect)

            if self.pause_reason is None:
                self.runtime_state = desired.runtime_state
            return self._result(actions, errors, effect)

    async def execute_active_unwind(
        self,
        desired: DesiredOrder,
        *,
        prepared_generation: int | None = None,
    ) -> ReconcileResult:
        """Execute one bounded reduce-only IOC through an isolated lane."""
        async with self._lock:
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            effect = _OrderEffect()
            if self._shutting_down:
                return self._result(actions, ("order manager is stopping",))
            if not self.config.active_unwind_enabled:
                self._pause("active unwind is disabled")
                return self._result(actions, (self.pause_reason or "disabled",))

            resolved = await self._resolve_uncertain_locked()
            effect.observed_fill_orders.extend(
                order for order in resolved if _has_visible_fill(order)
            )
            effect.fill_observed = bool(effect.observed_fill_orders)
            effect.position_refresh_required = bool(resolved)
            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                errors.append(blocking)
                effect.include(
                    await self._cancel_confirmable_locked(
                        "active unwind blocked by uncertain state",
                        actions,
                        errors,
                    )
                )
                return self._result(actions, errors, effect)
            if effect.position_refresh_required:
                return self._result(actions, errors, effect)

            validation_error = self._validate_active_unwind(desired)
            if validation_error:
                self._pause(validation_error)
                return self._result(
                    actions, (self.pause_reason or validation_error,), effect
                )

            # The first call is always prepare-only.  It proves managed and
            # authenticated symbol orders are empty; the caller must then
            # refresh BBO, position, and account truth before consuming the
            # returned one-shot generation on a later call.
            if prepared_generation is None:
                self._invalidate_active_unwind_preparation()
                if any(slot is not None for slot in self._slots.values()):
                    effect.include(
                        await self._cancel_confirmable_locked(
                            "prepare active unwind", actions, errors
                        )
                    )
                blocking = self._blocking_reason()
                if errors or blocking:
                    if blocking:
                        self._pause(blocking)
                        errors.append(blocking)
                    return self._result(actions, errors, effect)
                proof_error = await self._active_unwind_zero_order_proof()
                if proof_error:
                    self._pause(proof_error)
                    errors.append(proof_error)
                    return self._result(actions, errors, effect)
                self._active_unwind_prepare_sequence += 1
                self._active_unwind_prepared_generation = (
                    self._active_unwind_prepare_sequence
                )
                actions.append(
                    ReconcileAction(
                        desired.side,
                        "prepare_active_unwind",
                        "zero symbol orders proved; fresh truth required",
                        desired.price,
                        desired.amount,
                        True,
                        True,
                    )
                )
                effect.position_refresh_required = True
                return self._result(actions, errors, effect)

            if (
                type(prepared_generation) is not int
                or prepared_generation
                != self._active_unwind_prepared_generation
            ):
                reason = "active unwind requires a fresh one-shot preparation"
                self._pause(reason)
                return self._result(actions, (reason,), effect)
            self._active_unwind_prepared_generation = None
            if any(slot is not None for slot in self._slots.values()):
                reason = "active unwind order state changed after preparation"
                self._pause(reason)
                return self._result(actions, (reason,), effect)

            action = ReconcileAction(
                desired.side,
                "would_active_unwind" if self.config.dry_run else "active_unwind",
                desired.reason,
                desired.price,
                desired.amount,
                True,
            )
            if self.config.dry_run:
                actions.append(replace(action, success=True))
                return self._result(actions, errors, effect)

            proof_error = await self._active_unwind_zero_order_proof()
            if proof_error:
                self._pause(proof_error)
                errors.append(proof_error)
                return self._result(actions, errors, effect)

            now = self._monotonic()
            if self._active_unwind_not_sent_failures >= (
                self.config.active_unwind_max_attempts
            ):
                reason = "active unwind pre-send failure limit exhausted"
                self._pause(reason)
                return self._result(actions, (reason,), effect)
            if now < self._active_unwind_not_sent_until:
                actions.append(
                    replace(
                        action,
                        operation="blocked",
                        reason="active unwind pre-send cooldown is active",
                        success=False,
                    )
                )
                return self._result(actions, errors, effect)
            normal_budget_available = self._create_budget_available()
            emergency_budget_available = (
                not normal_budget_available
                and now >= self._risk_reducing_create_not_before
            )
            if not normal_budget_available and not emergency_budget_available:
                actions.append(
                    replace(
                        action,
                        operation="blocked",
                        reason="mutation budget exhausted",
                        success=False,
                    )
                )
                return self._result(actions, errors, effect)
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
                reduce_only=True,
                created_monotonic=now,
                updated_monotonic=now,
                intent=desired.intent,
            )
            self._active_unwind_side = desired.side
            mutation_timestamp = self._record_mutation()
            actions.append(action)
            action_index = len(actions) - 1
            order_params = {"time_in_force": "IOC", "reduce_only": True}
            if getattr(
                self.adapter, "supports_definitive_pre_send_failure", False
            ) is True:
                order_params["_raise_on_definitive_pre_send_failure"] = True
            if getattr(
                self.adapter,
                "supports_definitive_submission_rejection",
                False,
            ) is True:
                order_params["_raise_on_definitive_submission_rejection"] = True
            try:
                order = await self.adapter.create_order(
                    self.config.symbol,
                    desired.side,
                    OrderType.LIMIT,
                    desired.amount,
                    desired.price,
                    params=order_params,
                )
            except asyncio.CancelledError:
                self._mark_submission_uncertain(
                    desired.side,
                    "active unwind create task was cancelled before confirmation",
                )
                raise
            except OrderSubmissionNotSentError:
                self._rollback_mutation(mutation_timestamp)
                self._active_unwind_not_sent_failures += 1
                self._active_unwind_not_sent_until = (
                    self._monotonic() + self.config.error_cooldown_seconds
                )
                actions[action_index] = replace(
                    action,
                    reason="active unwind submission was not sent",
                    success=False,
                )
                self._slots[desired.side] = None
                self._active_unwind_side = None
                if self._active_unwind_not_sent_failures >= (
                    self.config.active_unwind_max_attempts
                ):
                    reason = "active unwind pre-send failure limit exhausted"
                    self._pause(reason)
                    errors.append(reason)
                return self._result(actions, errors, effect)
            except OrderSubmissionRejectedError:
                self._active_unwind_not_sent_failures = 0
                actions[action_index] = replace(
                    action,
                    reason="active unwind submission was rejected",
                    success=False,
                )
                self._slots[desired.side] = None
                self._active_unwind_side = None
                effect.position_refresh_required = True
                return self._result(actions, errors, effect)
            except Exception as exc:
                category = _error_category(exc)
                self._mark_submission_uncertain(desired.side, category)
                errors.append(
                    f"active unwind terminal proof is unavailable: {category}"
                )
                return self._result(actions, errors, effect)

            if self._valid_active_uncertain_placeholder(order, desired):
                slot = self._slots[desired.side]
                if slot is not None:
                    slot.client_id = str(order.client_id)
                    slot.updated_monotonic = self._monotonic()
                self._mark_submission_uncertain(
                    desired.side,
                    "active unwind submission awaits exact client-id terminal proof",
                )
                errors.append(
                    "active unwind terminal proof is unavailable: "
                    "submission outcome is uncertain"
                )
                return self._result(actions, errors, effect)
            if not self._valid_active_confirmation(order, desired):
                self._mark_submission_uncertain(
                    desired.side, "invalid active unwind confirmation"
                )
                errors.append(
                    "active unwind terminal proof is unavailable: invalid confirmation"
                )
                return self._result(actions, errors, effect)

            self._active_unwind_order_ids.add(str(order.id))
            slot = self._slots[desired.side]
            if slot is None:
                self._mark_submission_uncertain(
                    desired.side, "active unwind slot disappeared"
                )
                errors.append("active unwind terminal proof is unavailable")
                return self._result(actions, errors, effect)
            slot.order_id = str(order.id)
            self._bind_order_intent(str(order.id), slot.intent)
            slot.client_id = (
                str(order.client_id) if order.client_id not in (None, "") else None
            )
            slot.remaining = Decimal(str(order.remaining))
            slot.updated_monotonic = self._monotonic()

            terminal = order if order.status in _TERMINAL_STATUSES else None
            if terminal is None:
                try:
                    terminal = await asyncio.wait_for(
                        self._poll_active_terminal(slot),
                        timeout=float(
                            self.config.active_unwind_confirmation_timeout_seconds
                        ),
                    )
                except asyncio.CancelledError:
                    self._mark_submission_uncertain(
                        desired.side,
                        "active unwind terminal proof task was cancelled",
                    )
                    raise
                except TimeoutError:
                    self._mark_submission_uncertain(
                        desired.side,
                        "active unwind exact terminal proof timed out",
                    )
                    errors.append("active unwind exact terminal proof timed out")
                    return self._result(actions, errors, effect)
                except Exception as exc:
                    category = _error_category(exc)
                    self._mark_submission_uncertain(
                        desired.side,
                        f"active unwind terminal proof failed: {category}",
                    )
                    errors.append(
                        f"active unwind terminal proof is unavailable: {category}"
                    )
                    return self._result(actions, errors, effect)

            if terminal is None:
                self._mark_submission_uncertain(
                    desired.side, "active unwind exact terminal proof is missing"
                )
                errors.append("active unwind exact terminal proof is missing")
                return self._result(actions, errors, effect)

            fill_observed = _has_visible_fill(
                terminal, previous_remaining=desired.amount
            )
            self._active_unwind_not_sent_failures = 0
            self._apply_order_update(desired.side, terminal)
            actions[action_index] = replace(
                action,
                reason=(
                    "active unwind filled"
                    if terminal.status is OrderStatus.FILLED
                    else "active unwind terminal partial fill"
                    if fill_observed
                    else f"active unwind terminal {terminal.status.value}"
                ),
                success=terminal.status is not OrderStatus.REJECTED,
            )
            effect.position_refresh_required = True
            effect.fill_observed |= fill_observed
            if fill_observed:
                effect.observed_fill_orders.append(terminal)
            return self._result(actions, errors, effect)

    async def handle_order_update(self, order: OrderData) -> bool:
        async with self._lock:
            self._invalidate_active_unwind_preparation()
            if order.symbol != self.config.symbol:
                return False
            side = order.side
            slot = self._slots.get(side)
            if slot is None or not self._order_matches(slot, order):
                if order.status not in _TERMINAL_STATUSES:
                    proof = self._terminal_order_replay(order)
                    if proof is not None:
                        fill_observed = _has_visible_fill(
                            order, previous_remaining=proof.remaining
                        )
                        if fill_observed:
                            self._record_terminal_order(proof, order)
                        return fill_observed
                    self._pause(f"unknown open order update: {order.id}")
                return False
            if (
                self._active_unwind_side is side
                and not self._valid_active_resolution(slot, order)
            ):
                self._mark_submission_uncertain(
                    side, "invalid active unwind order update proof"
                )
                return False
            if self._active_unwind_side is side and order.id not in (None, ""):
                self._active_unwind_order_ids.add(str(order.id))
            fill_observed = _has_visible_fill(
                order, previous_remaining=slot.remaining
            )
            self._apply_order_update(side, order)
            return fill_observed

    async def resolve_unresolved_submissions(self) -> list[OrderData]:
        async with self._lock:
            self._invalidate_active_unwind_preparation()
            return await self._resolve_uncertain_locked()

    async def sync_open_orders(self) -> bool:
        async with self._lock:
            self._invalidate_active_unwind_preparation()
            effect = _OrderEffect()
            resolved = await self._resolve_uncertain_locked()
            effect.observed_fill_orders.extend(
                order
                for order in resolved
                if order.symbol == self.config.symbol and _has_visible_fill(order)
            )
            effect.fill_observed = bool(effect.observed_fill_orders)
            effect.position_refresh_required = effect.fill_observed
            open_orders = self._active_symbol_orders(
                await self.adapter.get_open_orders(self.config.symbol)
            )
            if self.config.dry_run:
                if open_orders:
                    self._pause(
                        "unknown open orders: "
                        + ", ".join(str(order.id) for order in open_orders)
                    )
                elif (
                    self.pause_reason
                    and self.pause_reason.startswith(
                        ("unknown open order", "unknown open orders")
                    )
                ):
                    self.pause_reason = None
                    self.runtime_state = RuntimeState.SYNCING
                self.last_sync_result = ReconcileResult(
                    (),
                    self.runtime_state,
                    (),
                    effect.position_refresh_required,
                    effect.fill_observed,
                    tuple(effect.observed_fill_orders),
                )
                return effect.position_refresh_required
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
                    if (
                        self._active_unwind_side is side
                        and not self._valid_active_resolution(
                            slot, matches[0]
                        )
                    ):
                        self._mark_submission_uncertain(
                            side,
                            "invalid active unwind open-order proof",
                        )
                        continue
                    matched = matches[0]
                    if (
                        self._active_unwind_side is side
                        and matched.id not in (None, "")
                    ):
                        self._active_unwind_order_ids.add(str(matched.id))
                    fill_observed = _has_visible_fill(
                        matched, previous_remaining=slot.remaining
                    )
                    effect.position_refresh_required |= fill_observed
                    effect.fill_observed |= fill_observed
                    if fill_observed:
                        effect.observed_fill_orders.append(matched)
                    self._apply_order_update(side, matched)
                    continue
                effect.include(
                    await self._resolve_missing_slot_locked(side, slot)
                )

            unknown = [
                order
                for order in open_orders
                if id(order) not in matched_ids
            ]
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
            self.last_sync_result = ReconcileResult(
                (),
                self.runtime_state,
                (),
                effect.position_refresh_required,
                effect.fill_observed,
                tuple(effect.observed_fill_orders),
            )
            return effect.position_refresh_required

    async def cancel_managed_orders(self, reason: str) -> ReconcileResult:
        self._begin_safety_requests()
        try:
            return await self._cancel_managed_orders(reason)
        finally:
            self._end_safety_requests()

    async def _cancel_managed_orders(self, reason: str) -> ReconcileResult:
        async with self._lock:
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            effect = await self._cancel_confirmable_locked(
                reason, actions, errors
            )
            return self._result(actions, errors, effect)

    async def shutdown(self) -> None:
        self._begin_safety_requests()
        try:
            await self._shutdown()
        finally:
            self._end_safety_requests()

    async def _shutdown(self) -> None:
        async with self._lock:
            self._invalidate_active_unwind_preparation()
            if self.runtime_state is RuntimeState.STOPPED:
                return
            self._shutting_down = True
            self.runtime_state = RuntimeState.STOPPING
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            open_orders: list[OrderData] = []
            effect = _OrderEffect()
            resolved = await self._resolve_uncertain_locked()
            effect.observed_fill_orders.extend(
                order for order in resolved if _has_visible_fill(order)
            )
            effect.fill_observed = bool(effect.observed_fill_orders)
            effect.position_refresh_required = effect.fill_observed
            if self.config.cancel_on_shutdown:
                effect.include(
                    await self._cancel_confirmable_locked(
                        "shutdown", actions, errors
                    )
                )
            if not self.config.dry_run:
                effect.include(await self._sync_for_shutdown_locked())
                open_orders = self._active_symbol_orders(
                    await self.adapter.get_open_orders(self.config.symbol)
                )
                if open_orders:
                    errors.append(
                        "target symbol open orders remain active after shutdown"
                    )
            remaining = [order for order in self._slots.values() if order is not None]
            unresolved_submissions = self.get_unresolved_submissions()
            unresolved_cancellations = self.get_unresolved_cancellations()
            if unresolved_submissions:
                errors.append("adapter submissions remain unresolved after shutdown")
            if unresolved_cancellations:
                errors.append("adapter cancellations remain unresolved after shutdown")
            if (
                not unresolved_submissions
                and not unresolved_cancellations
                and not remaining
                and not open_orders
            ):
                # Earlier cancel errors are provisional once exact REST state
                # proves the target symbol empty and no mutation is unresolved.
                errors.clear()
            if errors or remaining:
                self.runtime_state = RuntimeState.PAUSED_ORDER_STATE
                details = errors or ["managed orders remain after shutdown"]
                raise RuntimeError("; ".join(details))
            self.runtime_state = RuntimeState.STOPPED
            self.pause_reason = None
            self._result(actions, (), effect)

    def _begin_safety_requests(self) -> None:
        begin = getattr(self.adapter, "begin_safety_requests", None)
        if callable(begin):
            begin()

    def _end_safety_requests(self) -> None:
        end = getattr(self.adapter, "end_safety_requests", None)
        if callable(end):
            end()

    async def _place_locked(
        self,
        desired: DesiredOrder,
        actions: list[ReconcileAction],
        errors: list[str],
        *,
        risk_reduction: bool,
    ) -> _OrderEffect:
        operation = "would_place" if self.config.dry_run else "place"
        pending_target = self._pending_controller_protective_target.get(
            desired.side
        )
        if pending_target is not None and desired.reduce_only:
            self._pending_controller_protective_target.pop(
                desired.side, None
            )
            pending_target = None
        controller_revision = (
            desired.intent.revision
            if desired.intent is not None
            and desired.intent.kind is OrderIntentKind.CONTROLLER_ENTRY
            else None
        )
        pending_intent = pending_target.intent if pending_target else None
        protective_target_preserved = (
            pending_target is not None
            and pending_intent is not None
            and desired.intent is not None
            and desired.intent.kind is OrderIntentKind.CONTROLLER_ENTRY
            and controller_revision is not None
            and controller_revision >= pending_intent.revision
            and desired.intent.controller_extra_spread_ticks is not None
            and pending_intent.controller_extra_spread_ticks is not None
            and desired.intent.controller_extra_spread_ticks
            >= pending_intent.controller_extra_spread_ticks
            and (
                desired.price <= pending_target.price
                if desired.side is OrderSide.BUY
                else desired.price >= pending_target.price
            )
        )
        if pending_target is not None and not protective_target_preserved:
            cancelled_at = self._controller_protective_cancel_monotonic.get(
                desired.side
            )
            now = self._monotonic()
            normal_wait_seconds = self.config.min_order_lifetime_ms / 1000
            if cancelled_at is None or (
                now < cancelled_at
                or now - cancelled_at < normal_wait_seconds
            ):
                actions.append(
                    ReconcileAction(
                        desired.side,
                        "deferred",
                        "controller protective target reverted before replacement",
                        desired.price,
                        desired.amount,
                        desired.reduce_only,
                        cause=ReconcileActionCause.CONTROLLER_PROTECTIVE,
                        intent=desired.intent,
                    )
                )
                return _OrderEffect()
            self._pending_controller_protective_target.pop(
                desired.side, None
            )

        action_cause = (
            ReconcileActionCause.CONTROLLER_PROTECTIVE
            if protective_target_preserved
            else ReconcileActionCause.CONTROLLER_RESUME
            if desired.side in self._controller_blocked_sides
            and controller_revision is not None
            else ReconcileActionCause.NORMAL
        )
        action = ReconcileAction(
            desired.side,
            operation,
            desired.reason,
            desired.price,
            desired.amount,
            desired.reduce_only,
            cause=action_cause,
            intent=desired.intent,
        )
        now = self._monotonic()
        if self.config.dry_run:
            normal_budget_available = self._dry_run_mutation_budget_available()
            emergency_budget_available = (
                not normal_budget_available
                and risk_reduction
                and desired.reduce_only
                and now >= self._risk_reducing_create_not_before
            )
            if not normal_budget_available and not emergency_budget_available:
                actions.append(
                    replace(
                        action,
                        operation="would_defer",
                        reason="dry-run mutation budget exhausted",
                    )
                )
                return _OrderEffect()
            if emergency_budget_available:
                self._risk_reducing_create_not_before = now + 60
            self._dry_run_order_sequence += 1
            order_id = f"dry-run-{self._dry_run_order_sequence}"
            self._slots[desired.side] = ManagedOrder(
                side=desired.side,
                state=OrderSlotState.LIVE,
                order_id=order_id,
                client_id=None,
                price=desired.price,
                amount=desired.amount,
                remaining=desired.amount,
                reduce_only=desired.reduce_only,
                created_monotonic=now,
                updated_monotonic=now,
                intent=desired.intent,
                simulated=True,
            )
            self._record_dry_run_mutation()
            actions.append(replace(action, order_id=order_id))
            self._complete_controller_create(desired.side)
            return _OrderEffect()
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
                    cause=action_cause,
                    intent=desired.intent,
                )
            )
            return _OrderEffect()
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
            intent=desired.intent,
        )
        self._record_mutation()
        actions.append(action)
        action_index = len(actions) - 1
        order_params = {
            "time_in_force": "POST_ONLY",
            "reduce_only": desired.reduce_only,
        }
        if (
            getattr(
                self.adapter,
                "supports_definitive_pre_send_failure",
                False,
            )
            is True
        ):
            order_params["_raise_on_definitive_pre_send_failure"] = True
        if (
            getattr(
                self.adapter,
                "supports_definitive_submission_rejection",
                False,
            )
            is True
        ):
            order_params["_raise_on_definitive_submission_rejection"] = True
        try:
            order = await self.adapter.create_order(
                self.config.symbol,
                desired.side,
                OrderType.LIMIT,
                desired.amount,
                desired.price,
                params=order_params,
            )
        except asyncio.CancelledError:
            self._mark_submission_uncertain(
                desired.side, "create task was cancelled before confirmation"
            )
            raise
        except OrderSubmissionNotSentError:
            actions[action_index] = replace(
                action,
                reason="order submission was not sent",
                success=False,
            )
            self._slots[desired.side] = None
            return _OrderEffect()
        except OrderSubmissionRejectedError:
            actions[action_index] = replace(
                action,
                reason="order submission was rejected",
                success=False,
            )
            self._slots[desired.side] = None
            return _OrderEffect()
        except Exception as exc:
            category = _error_category(exc)
            if category == "http_429":
                actions[action_index] = replace(action, success=False)
                self._slots[desired.side] = None
                errors.append("create rejected: http_429")
                return _OrderEffect()
            self._mark_submission_uncertain(desired.side, category)
            errors.append(f"create outcome uncertain: {category}")
            return _OrderEffect()
        if order is None:
            self._mark_submission_uncertain(
                desired.side, "adapter returned no order confirmation"
            )
            errors.append("create outcome uncertain: no order confirmation")
            return _OrderEffect()
        fill_observed = _has_visible_fill(order)
        effect = _OrderEffect(fill_observed, fill_observed)
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
            return effect
        if not _valid_limit_order_values(order):
            self._mark_submission_uncertain(
                desired.side, "adapter returned invalid order financials"
            )
            errors.append("create outcome uncertain: invalid confirmation")
            return effect
        action = replace(
            action,
            order_id=str(order.id) if order.id is not None else None,
        )
        actions[action_index] = action
        if order.id is not None:
            self._known_order_ids.add(str(order.id))
            self._bind_order_intent(str(order.id), desired.intent)
        if fill_observed:
            effect.observed_fill_orders.append(order)
        if order.status in _TERMINAL_STATUSES:
            terminal_slot = self._slots[desired.side]
            if terminal_slot is not None:
                self._record_terminal_order(terminal_slot, order)
            if order.status is OrderStatus.FILLED:
                actions[action_index] = replace(
                    action,
                    reason="filled during submission",
                    success=True,
                )
                self._complete_controller_create(desired.side)
                self._slots[desired.side] = None
                return effect
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
                return effect
            errors.append(
                f"create returned terminal status: {order.status.value}"
            )
            return effect

        self._apply_order_update(desired.side, order)
        if self._submission_uncertain(order):
            self._submission_ambiguity_latched = True
            slot = self._slots[desired.side]
            if slot is not None:
                slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
                slot.submission_uncertain = True
            self._pause("order submission outcome is uncertain")
            return effect
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
            return effect
        actions[action_index] = replace(action, success=True)
        self._complete_controller_create(desired.side)
        return effect

    async def _cancel_locked(
        self,
        side: OrderSide,
        reason: str,
        actions: list[ReconcileAction],
        errors: list[str],
        *,
        safety: bool,
        cause: ReconcileActionCause = ReconcileActionCause.NORMAL,
    ) -> _OrderEffect:
        slot = self._slots[side]
        if slot is None:
            return _OrderEffect()
        operation = "would_cancel" if self.config.dry_run else "cancel"
        action = ReconcileAction(
            side,
            operation,
            reason,
            slot.price,
            slot.remaining,
            slot.reduce_only,
            order_id=slot.order_id,
            cause=cause,
            intent=slot.intent,
        )
        if self.config.dry_run:
            if not slot.simulated:
                self._pause("dry-run cannot mutate a non-simulated order slot")
                errors.append(
                    self.pause_reason
                    or "dry-run cannot mutate a non-simulated order slot"
                )
                return _OrderEffect()
            if not safety and not self._dry_run_mutation_budget_available():
                actions.append(
                    replace(
                        action,
                        operation="would_defer",
                        reason="dry-run mutation budget exhausted",
                    )
                )
                return _OrderEffect()
            actions.append(action)
            self._slots[side] = None
            self._record_dry_run_mutation()
            return _OrderEffect()
        if slot.order_id is None:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            self._pause("cannot cancel an order without a confirmed order id")
            errors.append(self.pause_reason or "missing order id")
            return _OrderEffect()
        if not safety and not self._mutation_budget_available():
            actions.append(
                ReconcileAction(
                    side,
                    "blocked",
                    "mutation budget exhausted",
                    cause=cause,
                    intent=slot.intent,
                )
            )
            return _OrderEffect()

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
                return _OrderEffect()
            self._mark_cancellation_uncertain(slot)
            errors.append(f"cancel outcome uncertain: {category}")
            return _OrderEffect()

        terminal_outcome_getter = getattr(
            self.adapter, "get_terminal_cancellation_outcome", None
        )
        cached_terminal_used = False
        if callable(terminal_outcome_getter) and not self._cancellation_terminal(
            result
        ):
            terminal_outcome = terminal_outcome_getter(
                slot.order_id, self.config.symbol
            )
            if (
                terminal_outcome is not None
                and self._cancellation_terminal(terminal_outcome)
                and self._order_matches(slot, terminal_outcome)
                and terminal_outcome.side is side
                and (
                    terminal_outcome.status is not OrderStatus.FILLED
                    or _has_visible_fill(
                        terminal_outcome,
                        previous_remaining=slot.remaining,
                    )
                )
            ):
                result = terminal_outcome
                cached_terminal_used = True
        confirmed_match = (
            result is not None
            and result.symbol == self.config.symbol
            and self._order_matches(slot, result)
        )
        fill_observed = confirmed_match and _has_visible_fill(
            result, previous_remaining=slot.remaining
        )
        if self._cancellation_terminal(result) and confirmed_match:
            confirmer = getattr(
                self.adapter, "confirm_terminal_cancellation_outcome", None
            )
            confirmed_cached_terminal = (
                callable(confirmer) and confirmer(result)
                if cached_terminal_used
                else True
            )
            if not confirmed_cached_terminal:
                self._mark_cancellation_uncertain(slot)
                errors.append(
                    "exact cancellation terminal proof could not be confirmed"
                )
                return _OrderEffect(
                    fill_observed,
                    fill_observed,
                )
            if callable(confirmer) and not cached_terminal_used:
                confirmer(result)
            actions[action_index] = replace(
                action,
                success=result.status is not OrderStatus.FILLED,
            )
            self._record_terminal_order(slot, result)
            self._slots[side] = None
            return _OrderEffect(
                True,
                fill_observed,
                [result] if fill_observed else [],
            )
        self._mark_cancellation_uncertain(slot)
        errors.append("cancel outcome is not terminal")
        return _OrderEffect(
            fill_observed,
            fill_observed,
            [result] if fill_observed else [],
        )

    async def _cancel_confirmable_locked(
        self,
        reason: str,
        actions: list[ReconcileAction],
        errors: list[str],
    ) -> _OrderEffect:
        effect = _OrderEffect()
        for side in (OrderSide.BUY, OrderSide.SELL):
            slot = self._slots[side]
            if slot is None:
                continue
            if (
                slot.state is OrderSlotState.UNCERTAIN_CANCELLATION
                or slot.cancellation_uncertain
            ):
                errors.append(f"{side.value} order state is uncertain")
                break
            if slot.state is OrderSlotState.UNCERTAIN_SUBMISSION:
                errors.append(f"{side.value} order state is uncertain")
                continue
            effect.include(
                await self._cancel_locked(
                    side, reason, actions, errors, safety=True
                )
            )
            if self.unresolved_cancellation_count:
                break
        return effect

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
            candidates = list(await resolver())
            for order in candidates:
                slot = self._slots.get(order.side)
                if slot is None or not self._order_matches(slot, order):
                    continue
                if self._active_unwind_side is order.side:
                    if not self._valid_active_resolution(slot, order):
                        continue
                    if order.id not in (None, ""):
                        active_id = str(order.id)
                        self._active_unwind_order_ids.add(active_id)
                        self._known_order_ids.add(active_id)
                        self._bind_order_intent(active_id, slot.intent)
                    if order.status in _TERMINAL_STATUSES:
                        self._apply_order_update(order.side, order)
                    else:
                        slot.order_id = (
                            str(order.id) if order.id not in (None, "") else None
                        )
                        slot.client_id = str(order.client_id)
                        slot.updated_monotonic = self._monotonic()
                    resolved.append(order)
                    continue
                self._apply_order_update(order.side, order)
                resolved.append(order)
        self._clear_resolved_uncertainty_pause()
        return resolved

    async def _resolve_missing_slot_locked(
        self, side: OrderSide, slot: ManagedOrder
    ) -> _OrderEffect:
        effect = _OrderEffect()
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
                and (
                    self._active_unwind_side is not side
                    or self._valid_active_resolution(slot, order)
                )
            ),
            None,
        )
        if terminal is not None:
            visible_fill = _has_visible_fill(
                terminal, previous_remaining=slot.remaining
            )
            self._apply_order_update(side, terminal)
            if visible_fill and self._slots[side] is None:
                effect.position_refresh_required = True
                effect.fill_observed = True
                effect.observed_fill_orders.append(terminal)
            return effect
        if slot.state in {
            OrderSlotState.CANCELING,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        }:
            self._mark_cancellation_uncertain(slot)
        else:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            self._pause(f"{side.value} order disappeared without terminal proof")
        return effect

    async def _sync_for_shutdown_locked(self) -> _OrderEffect:
        effect = _OrderEffect()
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
            effect.include(await self._resolve_missing_slot_locked(side, slot))
        return effect

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
            if not _valid_limit_order_values(order):
                slot = self._slots[side]
                if slot is not None:
                    slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
                    slot.submission_uncertain = True
                    slot.updated_monotonic = self._monotonic()
                self._pause(f"invalid {side.value} order update")
                return
            slot = self._slots[side]
            if (
                slot is not None
                and self._active_unwind_side is side
                and self._order_matches(slot, order)
            ):
                active_id = str(order.id or "").strip()
                if not active_id:
                    self._mark_submission_uncertain(
                        side, "active unwind terminal order id is missing"
                    )
                    return
                self._active_unwind_order_ids.add(active_id)
                self._active_unwind_side = None
            if slot is not None and (
                slot.state is OrderSlotState.UNCERTAIN_CANCELLATION
                or slot.cancellation_uncertain
            ):
                confirmer = getattr(
                    self.adapter,
                    "confirm_terminal_cancellation_outcome",
                    None,
                )
                if callable(confirmer):
                    confirmer(order)
                self._resolved_ambiguous_cancellations += 1
            if slot is not None:
                self._record_terminal_order(slot, order)
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
        if order.id is not None:
            confirmed_order_id = str(order.id)
            self._known_order_ids.add(confirmed_order_id)
            self._bind_order_intent(
                confirmed_order_id,
                previous.intent if previous is not None else None,
            )
            if self._active_unwind_side is side:
                self._active_unwind_order_ids.add(confirmed_order_id)
        cancellation_uncertain = bool(
            previous is not None
            and (
                previous.state is OrderSlotState.UNCERTAIN_CANCELLATION
                or previous.cancellation_uncertain
            )
        )
        state = (
            OrderSlotState.UNCERTAIN_CANCELLATION
            if cancellation_uncertain
            else (
                OrderSlotState.PARTIALLY_FILLED
                if remaining < amount
                else (
                    OrderSlotState.LIVE
                    if order.status is OrderStatus.OPEN
                    else OrderSlotState.SUBMITTING
                )
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
            cancellation_uncertain=cancellation_uncertain,
            intent=previous.intent if previous is not None else None,
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

    def _validate_active_unwind(self, desired: DesiredOrder) -> str | None:
        if not desired.reduce_only:
            return "active unwind must be reduce-only"
        if (
            not isinstance(desired.amount, Decimal)
            or not isinstance(desired.price, Decimal)
            or not desired.amount.is_finite()
            or not desired.price.is_finite()
            or desired.amount <= 0
            or desired.amount > self.config.max_position
            or desired.price <= 0
            or not is_step_aligned(
                desired.amount, self.metadata.quantity_step
            )
            or not is_step_aligned(desired.price, self.metadata.price_tick)
            or desired.amount < self.metadata.min_base_amount
            or desired.amount * desired.price < self.metadata.min_quote_amount
        ):
            return "active unwind desired price/amount is invalid"
        return None

    def _valid_active_confirmation(
        self, order: OrderData | None, desired: DesiredOrder
    ) -> bool:
        if order is None:
            return False
        try:
            amount = Decimal(str(order.amount))
            price = Decimal(str(order.price))
        except (ArithmeticError, TypeError, ValueError):
            return False
        return (
            order.symbol == self.config.symbol
            and order.side is desired.side
            and order.type is OrderType.LIMIT
            and order.status is not OrderStatus.UNKNOWN
            and bool(str(order.id or "").strip())
            and amount == desired.amount
            and price == desired.price
            and _valid_limit_order_values(order)
        )

    def _valid_active_uncertain_placeholder(
        self, order: OrderData | None, desired: DesiredOrder
    ) -> bool:
        if order is None:
            return False
        params = order.params if isinstance(order.params, dict) else {}
        raw = order.raw_data if isinstance(order.raw_data, dict) else {}
        client_id = str(order.client_id or "").strip()
        try:
            amount = Decimal(str(order.amount))
            price = Decimal(str(order.price))
            remaining = Decimal(str(order.remaining))
        except (ArithmeticError, TypeError, ValueError):
            return False
        return (
            order.id in (None, "")
            and bool(client_id)
            and order.symbol == self.config.symbol
            and order.side is desired.side
            and order.type is OrderType.LIMIT
            and order.status is OrderStatus.PENDING
            and amount == desired.amount
            and price == desired.price
            and remaining == desired.amount
            and params.get("submission_uncertain") is True
            and raw.get("submission_uncertain") is True
            and str(params.get("client_order_id", "")) == client_id
            and str(raw.get("client_order_id", "")) == client_id
            and params.get("reduce_only") is True
            and str(params.get("time_in_force", "")).upper() == "IOC"
        )

    def _valid_active_resolution(
        self, slot: ManagedOrder, order: OrderData
    ) -> bool:
        try:
            amount = Decimal(str(order.amount))
            price = Decimal(str(order.price))
        except (ArithmeticError, TypeError, ValueError):
            return False
        if (
            order.symbol != self.config.symbol
            or order.side is not slot.side
            or order.type is not OrderType.LIMIT
            or order.status is OrderStatus.UNKNOWN
            or not self._order_matches(slot, order)
            or amount != slot.amount
            or price != slot.price
            or not _valid_limit_order_values(order)
        ):
            return False
        if order.status in _TERMINAL_STATUSES:
            return bool(str(order.id or "").strip())
        return order.status in {OrderStatus.OPEN, OrderStatus.PENDING}

    async def _active_unwind_zero_order_proof(self) -> str | None:
        try:
            open_orders = self._active_symbol_orders(
                await self.adapter.get_open_orders(self.config.symbol)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return (
                "active unwind open-order proof failed: "
                f"{_error_category(exc)}"
            )
        if open_orders:
            return "active unwind requires zero authenticated symbol open orders"
        return None

    def _matching_active_terminal(
        self,
        slot: ManagedOrder,
        orders: Iterable[OrderData] | None,
    ) -> OrderData | None:
        for order in orders or ():
            if (
                order.status in _TERMINAL_STATUSES
                and self._valid_active_resolution(slot, order)
            ):
                return order
        return None

    async def _poll_active_terminal(
        self, slot: ManagedOrder
    ) -> OrderData | None:
        timeout = self.config.active_unwind_confirmation_timeout_seconds
        attempts = min(
            _ACTIVE_TERMINAL_MAX_POLLS,
            max(1, int(timeout / _ACTIVE_TERMINAL_POLL_SECONDS)),
        )
        for attempt in range(attempts):
            history = await self.adapter.get_order_history(
                self.config.symbol, limit=100
            )
            terminal = self._matching_active_terminal(slot, history)
            if terminal is not None:
                return terminal
            if attempt + 1 < attempts:
                await self._sleep(_ACTIVE_TERMINAL_POLL_SECONDS)
        return None

    def _validate_desired(
        self, desired: DesiredOrder, risk: RiskDecision
    ) -> str | None:
        if (
            not desired.amount.is_finite()
            or not desired.price.is_finite()
            or desired.amount <= 0
            or desired.price <= 0
            or not is_step_aligned(
                desired.amount, self.metadata.quantity_step
            )
            or not is_step_aligned(desired.price, self.metadata.price_tick)
        ):
            return f"{desired.side.value} desired price/amount is invalid"
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
        if desired.reduce_only is not expected_reduce_only:
            return f"{desired.side.value} reduce_only conflicts with risk decision"
        if (
            desired.amount < self.metadata.min_base_amount
            or desired.amount * desired.price < self.metadata.min_quote_amount
        ):
            return f"{desired.side.value} desired price/amount is invalid"
        return None

    def is_controller_protective_outward_revision(
        self,
        live: ManagedOrder,
        desired: DesiredOrder,
    ) -> bool:
        live_intent = live.intent
        target_intent = desired.intent
        if (
            live.state is not OrderSlotState.LIVE
            or live.reduce_only
            or desired.reduce_only
            or live.side is not desired.side
            or live.remaining != live.amount
            or desired.amount != live.amount
            or live_intent is None
            or target_intent is None
            or live_intent.kind is not OrderIntentKind.CONTROLLER_ENTRY
            or target_intent.kind is not OrderIntentKind.CONTROLLER_ENTRY
            or live_intent.controller_outward_only is not True
            or target_intent.controller_outward_only is not True
            or type(live_intent.revision) is not int
            or type(target_intent.revision) is not int
            or target_intent.revision <= live_intent.revision
            or type(live_intent.controller_extra_spread_ticks) is not int
            or type(target_intent.controller_extra_spread_ticks) is not int
            or target_intent.controller_extra_spread_ticks
            <= live_intent.controller_extra_spread_ticks
        ):
            return False
        outward = (
            desired.price < live.price
            if live.side is OrderSide.BUY
            else desired.price > live.price
        )
        threshold = self.metadata.price_tick * Decimal(
            self.config.toxicity_outward_reprice_threshold_ticks
        )
        return outward and abs(desired.price - live.price) >= threshold

    def _replacement_reason(
        self,
        live: ManagedOrder,
        desired: DesiredOrder,
        risk: RiskDecision,
    ) -> str | None:
        if self._is_safety_replacement(live, desired, risk):
            return "live order no longer matches risk"
        if self.is_controller_protective_outward_revision(live, desired):
            return "controller protective outward revision"
        if (
            risk.runtime_state is RuntimeState.RISK_REDUCTION
            and live.reduce_only
            and desired.reduce_only
            and (
                (
                    live.side is OrderSide.BUY
                    and desired.price > live.price
                )
                or (
                    live.side is OrderSide.SELL
                    and desired.price < live.price
                )
            )
        ):
            return "risk-reducing quote became more aggressive"
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
        if self.get_unresolved_cancellations():
            return "adapter cancellations remain unresolved"
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

    def _record_terminal_order(
        self, slot: ManagedOrder, order: OrderData
    ) -> None:
        if order.id is not None:
            self._known_order_ids.add(str(order.id))
        order_id = slot.order_id or (
            str(order.id) if order.id is not None else None
        )
        client_id = slot.client_id or (
            str(order.client_id) if order.client_id is not None else None
        )
        remaining = slot.remaining
        if _valid_limit_order_values(order):
            remaining = min(remaining, Decimal(str(order.remaining)))
        proof = replace(
            slot,
            order_id=order_id,
            client_id=client_id,
            remaining=remaining,
        )
        if proof.order_id:
            self._bind_terminal_order_intent(proof.order_id, proof.intent)
        for namespace, value in (
            ("order_id", proof.order_id),
            ("client_id", proof.client_id),
        ):
            if value:
                self._terminal_orders[(proof.side, namespace, value)] = proof

    def _bind_order_intent(
        self, order_id: str, intent: OrderIntentMetadata | None
    ) -> None:
        if intent is None:
            return
        existing = self._order_intent_contexts.get(order_id)
        terminal = self._terminal_order_intent_contexts.get(order_id)
        if (existing is not None and existing != intent) or (
            terminal is not None and terminal != intent
        ):
            self._pause("confirmed order id changed intent context")
            return
        self._order_intent_contexts[order_id] = intent

    def _complete_controller_create(self, side: OrderSide) -> None:
        self._pending_controller_protective_target.pop(side, None)
        self._controller_blocked_sides.discard(side)

    def _bind_terminal_order_intent(
        self, order_id: str, intent: OrderIntentMetadata | None
    ) -> None:
        if intent is None:
            self._order_intent_contexts.pop(order_id, None)
            return
        existing = self._order_intent_contexts.get(order_id)
        terminal = self._terminal_order_intent_contexts.get(order_id)
        if (existing is not None and existing != intent) or (
            terminal is not None and terminal != intent
        ):
            self._pause("confirmed order id changed intent context")
            return
        self._order_intent_contexts.pop(order_id, None)
        self._terminal_order_intent_contexts.pop(order_id, None)
        self._terminal_order_intent_contexts[order_id] = intent
        while (
            len(self._terminal_order_intent_contexts)
            > _TERMINAL_INTENT_CONTEXT_LIMIT
        ):
            oldest = next(iter(self._terminal_order_intent_contexts))
            self._terminal_order_intent_contexts.pop(oldest)

    def _terminal_order_replay(
        self, order: OrderData
    ) -> ManagedOrder | None:
        order_id = str(order.id) if order.id is not None else None
        client_id = (
            str(order.client_id) if order.client_id is not None else None
        )
        proofs: list[ManagedOrder] = []
        for namespace, value in (
            ("order_id", order_id),
            ("client_id", client_id),
        ):
            if value:
                proof = self._terminal_orders.get(
                    (order.side, namespace, value)
                )
                if proof is not None:
                    proofs.append(proof)
        if not proofs or any(proof is not proofs[0] for proof in proofs[1:]):
            return None
        proof = proofs[0]
        if (
            order_id
            and proof.order_id
            and order_id != proof.order_id
        ) or (
            client_id
            and proof.client_id
            and client_id != proof.client_id
        ):
            return None
        if not _valid_limit_order_values(order) or not (
            Decimal(str(order.amount)) == proof.amount
            and Decimal(str(order.price)) == proof.price
        ):
            return None
        return proof

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

    def _dry_run_mutation_budget_available(self) -> bool:
        self._purge_dry_run_mutations()
        return (
            len(self._dry_run_mutation_timestamps)
            < self.config.max_mutations_per_minute
        )

    def _record_dry_run_mutation(self) -> None:
        self._purge_dry_run_mutations()
        self._dry_run_mutation_timestamps.append(self._monotonic())

    def _purge_dry_run_mutations(self) -> None:
        cutoff = self._monotonic() - 60
        while (
            self._dry_run_mutation_timestamps
            and self._dry_run_mutation_timestamps[0] <= cutoff
        ):
            self._dry_run_mutation_timestamps.popleft()

    def _create_budget_available(self) -> bool:
        return self._mutation_budget_available()

    def _record_mutation(self) -> float:
        self._purge_mutations()
        timestamp = self._monotonic()
        self._mutation_timestamps.append(timestamp)
        self._mutation_generation += 1
        self._invalidate_active_unwind_preparation()
        return timestamp

    def _rollback_mutation(self, timestamp: float) -> None:
        if (
            self._mutation_timestamps
            and self._mutation_timestamps[-1] == timestamp
        ):
            self._mutation_timestamps.pop()

    def _invalidate_active_unwind_preparation(self) -> None:
        self._active_unwind_prepared_generation = None

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
        effect: _OrderEffect | None = None,
    ) -> ReconcileResult:
        effect = effect or _OrderEffect()
        result = ReconcileResult(
            tuple(actions),
            self.runtime_state,
            tuple(errors),
            effect.position_refresh_required,
            effect.fill_observed,
            tuple(effect.observed_fill_orders),
        )
        self.last_result = result
        return result
