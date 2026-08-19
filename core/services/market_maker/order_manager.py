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


@dataclass(frozen=True)
class ReconcileAction:
    side: OrderSide | None
    operation: str
    reason: str
    price: Decimal | None = None
    amount: Decimal | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class ReconcileResult:
    actions: tuple[ReconcileAction, ...]
    runtime_state: RuntimeState
    errors: tuple[str, ...] = ()


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
        return any(
            order is not None
            and (
                order.state in _UNCERTAIN_STATES
                or order.submission_uncertain
                or order.cancellation_uncertain
            )
            for order in self._slots.values()
        )

    def get_unresolved_submissions(self) -> list[dict[str, Any]]:
        getter = getattr(self.adapter, "get_unresolved_submissions", None)
        if not callable(getter):
            return []
        return list(getter())

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
            if self._shutting_down:
                return self._result(actions, ("order manager is stopping",))

            await self._resolve_uncertain_locked()
            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                await self._cancel_confirmable_locked(
                    "uncertain order state", actions, errors
                )
                return self._result(actions, errors)

            if (
                desired.bid is not None
                and desired.bid.side is not OrderSide.BUY
            ) or (
                desired.ask is not None
                and desired.ask.side is not OrderSide.SELL
            ):
                self._pause("desired quote side does not match its slot")
                errors.append(self.pause_reason)
                await self._cancel_confirmable_locked(
                    "invalid desired quote", actions, errors
                )
                return self._result(actions, errors)

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
                    await self._cancel_locked(
                        side,
                        desired.reason or "side disabled",
                        actions,
                        errors,
                        safety=True,
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
                await self._cancel_locked(
                    side,
                    replace_reason,
                    actions,
                    errors,
                    safety=safety,
                )

            blocking = self._blocking_reason()
            if blocking:
                self._pause(blocking)
                await self._cancel_confirmable_locked(
                    "uncertain order state", actions, errors
                )
                return self._result(actions, errors)

            if not quote_allowed:
                if risk.runtime_state not in quoting_states:
                    self.runtime_state = risk.runtime_state
                elif desired.runtime_state not in quoting_states:
                    self.runtime_state = desired.runtime_state
                else:
                    self.runtime_state = RuntimeState.PAUSED_ERROR
                return self._result(actions, errors)

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
                await self._place_locked(target, actions, errors)
                blocking = self._blocking_reason()
                if blocking:
                    self._pause(blocking)
                    await self._cancel_confirmable_locked(
                        "uncertain order state", actions, errors
                    )
                    return self._result(actions, errors)

            if self.pause_reason is None:
                self.runtime_state = desired.runtime_state
            return self._result(actions, errors)

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

    async def sync_open_orders(self) -> None:
        async with self._lock:
            await self._resolve_uncertain_locked()
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
                    if self._order_matches(slot, order)
                ]
                if len(matches) > 1:
                    self._pause(f"multiple open orders match {side.value} slot")
                    continue
                if matches:
                    matched_ids.add(id(matches[0]))
                    self._apply_order_update(side, matches[0])
                    continue
                await self._resolve_missing_slot_locked(side, slot)

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

    async def cancel_managed_orders(self, reason: str) -> ReconcileResult:
        async with self._lock:
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            await self._cancel_confirmable_locked(reason, actions, errors)
            return self._result(actions, errors)

    async def shutdown(self) -> None:
        async with self._lock:
            if self.runtime_state is RuntimeState.STOPPED:
                return
            self._shutting_down = True
            self.runtime_state = RuntimeState.STOPPING
            actions: list[ReconcileAction] = []
            errors: list[str] = []
            managed_before = self.snapshot()
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
                if any(
                    self._order_matches(managed, order)
                    for managed in managed_before
                    for order in open_orders
                ):
                    errors.append("managed orders remain active after shutdown")
            remaining = [order for order in self._slots.values() if order is not None]
            unresolved = self.get_unresolved_submissions()
            if unresolved:
                errors.append("adapter submissions remain unresolved after shutdown")
            elif not remaining and not any(
                self._order_matches(managed, order)
                for managed in managed_before
                for order in (
                    open_orders if not self.config.dry_run else ()
                )
            ):
                # Earlier cancel errors are provisional once exact REST state
                # proves every managed order terminal and no mutation is unresolved.
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
    ) -> None:
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
            return
        if not self._create_budget_available():
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
            return

        now = self._monotonic()
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
        except Exception as exc:
            self._mark_submission_uncertain(desired.side, str(exc))
            errors.append(f"create outcome uncertain: {type(exc).__name__}")
            return
        if order is None:
            self._mark_submission_uncertain(
                desired.side, "adapter returned no order confirmation"
            )
            errors.append("create outcome uncertain: no order confirmation")
            return
        if (
            order.symbol != self.config.symbol
            or order.side is not desired.side
            or order.status is OrderStatus.UNKNOWN
        ):
            self._mark_submission_uncertain(
                desired.side, "adapter returned an invalid order confirmation"
            )
            errors.append("create outcome uncertain: invalid confirmation")
            return
        if order.status in _TERMINAL_STATUSES:
            self._slots[desired.side] = None
            errors.append(
                f"create returned terminal status: {order.status.value}"
            )
            return

        self._apply_order_update(desired.side, order)
        if self._submission_uncertain(order):
            slot = self._slots[desired.side]
            if slot is not None:
                slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
                slot.submission_uncertain = True
            self._pause("order submission outcome is uncertain")

    async def _cancel_locked(
        self,
        side: OrderSide,
        reason: str,
        actions: list[ReconcileAction],
        errors: list[str],
        *,
        safety: bool,
    ) -> None:
        slot = self._slots[side]
        if slot is None:
            return
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
            return
        if slot.order_id is None:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            self._pause("cannot cancel an order without a confirmed order id")
            errors.append(self.pause_reason or "missing order id")
            return
        if not safety and not self._mutation_budget_available():
            actions.append(
                ReconcileAction(side, "blocked", "mutation budget exhausted")
            )
            return

        actions.append(action)
        slot.state = OrderSlotState.CANCELING
        slot.updated_monotonic = self._monotonic()
        self._record_mutation()
        try:
            result = await self.adapter.cancel_order(
                slot.order_id, self.config.symbol
            )
        except Exception as exc:
            self._mark_cancellation_uncertain(slot)
            errors.append(f"cancel outcome uncertain: {type(exc).__name__}")
            return
        if (
            self._cancellation_terminal(result)
            and result is not None
            and result.symbol == self.config.symbol
            and self._order_matches(slot, result)
        ):
            self._slots[side] = None
            return
        self._mark_cancellation_uncertain(slot)
        errors.append("cancel outcome is not terminal")

    async def _cancel_confirmable_locked(
        self,
        reason: str,
        actions: list[ReconcileAction],
        errors: list[str],
    ) -> None:
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
            await self._cancel_locked(
                side, reason, actions, errors, safety=True
            )

    async def _resolve_uncertain_locked(self) -> list[OrderData]:
        resolver = getattr(self.adapter, "resolve_unresolved_submissions", None)
        resolved: list[OrderData] = []
        if callable(resolver) and any(
            order is not None
            and (
                order.state is OrderSlotState.UNCERTAIN_SUBMISSION
                or order.submission_uncertain
            )
            for order in self._slots.values()
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
    ) -> None:
        history_getter = getattr(self.adapter, "get_order_history", None)
        history: Iterable[OrderData] = ()
        if callable(history_getter):
            history = await history_getter(self.config.symbol)
        terminal = next(
            (
                order
                for order in history
                if self._order_matches(slot, order)
                and order.status in _TERMINAL_STATUSES
            ),
            None,
        )
        if terminal is not None:
            self._slots[side] = None
            return
        if slot.state in {
            OrderSlotState.CANCELING,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        }:
            self._mark_cancellation_uncertain(slot)
        else:
            slot.state = OrderSlotState.UNCERTAIN_SUBMISSION
            slot.submission_uncertain = True
            self._pause(f"{side.value} order disappeared without terminal proof")

    async def _sync_for_shutdown_locked(self) -> None:
        open_orders = self._active_symbol_orders(
            await self.adapter.get_open_orders(self.config.symbol)
        )
        for side, slot in tuple(self._slots.items()):
            if slot is None:
                continue
            if any(self._order_matches(slot, order) for order in open_orders):
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
        for side, order in self._slots.items():
            if order is None:
                continue
            if order.state in _UNCERTAIN_STATES:
                return f"{side.value} slot is {order.state.value}"
            if order.submission_uncertain or order.cancellation_uncertain:
                return f"{side.value} order outcome is uncertain"
        return None

    def _order_matches(self, slot: ManagedOrder, order: OrderData) -> bool:
        identifiers = {value for value in (slot.order_id, slot.client_id) if value}
        order_identifiers = {
            str(value)
            for value in (order.id, order.client_id)
            if value is not None and str(value)
        }
        return bool(identifiers & order_identifiers)

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
    ) -> ReconcileResult:
        result = ReconcileResult(
            tuple(actions), self.runtime_state, tuple(errors)
        )
        self.last_result = result
        return result
