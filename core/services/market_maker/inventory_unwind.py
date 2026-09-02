from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ...adapters.exchanges.models import OrderSide
from .config import MarketMakerConfig, ceil_to_step, floor_to_step
from .models import (
    DesiredOrder,
    EpisodePolicyObservation,
    ExitBindingConstraint,
    InventoryExitStage,
    MarketMetadata,
    MarketSnapshot,
    OrderIntentKind,
    OrderIntentMetadata,
    PositionSnapshot,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_COMPLETED_EPISODE_HISTORY_LIMIT = 100
_STAGE_TRANSITION_HISTORY_LIMIT = 100


@dataclass(frozen=True)
class InventoryUnwindDecision:
    episode_id: int | None
    state: str
    reason: str
    age_seconds: float
    policy_decision_id: int = 0
    authenticated_episode_sequence: int | None = None
    exit_stage: InventoryExitStage | None = None
    normal_passive_price: Decimal | None = None
    selected_exit_price: Decimal | None = None
    episode_boundary_price: Decimal | None = None
    session_boundary_price: Decimal | None = None
    drawdown_boundary_price: Decimal | None = None
    active_slippage_boundary_price: Decimal | None = None
    available_completed_surplus: Decimal = _ZERO
    surplus_reserve: Decimal = _ZERO
    trigger: str | None = None
    passive_order: DesiredOrder | None = None
    active_order: DesiredOrder | None = None
    suppress_passive: bool = False
    blocked: bool = False
    unlocked_episode_loss: Decimal = _ZERO
    allowed_passive_loss: Decimal = _ZERO
    projected_episode_net: Decimal | None = None
    projected_session_net: Decimal | None = None
    projected_drawdown: Decimal | None = None
    binding_constraint: ExitBindingConstraint | None = None
    reachable_now: bool | None = None
    entered_inventory_hold: bool = False
    projected_episode_loss: Decimal | None = None
    projected_session_loss: Decimal | None = None
    remaining_drawdown: Decimal | None = None
    active_attempts: int = 0
    last_active_trigger: str | None = None
    budget_blocked: bool = False
    completed_episode_execution_history: tuple[dict[str, Any], ...] = ()
    stage_transition_history: tuple[dict[str, Any], ...] = ()

    def snapshot(self) -> dict[str, Any]:
        order = self.active_order or self.passive_order
        lane = (
            "active_ioc"
            if self.active_order is not None
            else "passive_post_only"
            if self.passive_order is not None
            else None
        )
        return {
            "episode_id": self.episode_id,
            "state": self.state,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
            "policy_decision_id": self.policy_decision_id,
            "authenticated_episode_sequence": (
                self.authenticated_episode_sequence
            ),
            "exit_stage": (
                self.exit_stage.value if self.exit_stage is not None else None
            ),
            "normal_passive_price": self.normal_passive_price,
            "selected_exit_price": self.selected_exit_price,
            "episode_boundary_price": self.episode_boundary_price,
            "session_boundary_price": self.session_boundary_price,
            "drawdown_boundary_price": self.drawdown_boundary_price,
            "active_slippage_boundary_price": (
                self.active_slippage_boundary_price
            ),
            "available_completed_surplus": self.available_completed_surplus,
            "surplus_reserve": self.surplus_reserve,
            "trigger": self.trigger,
            "suppress_passive": self.suppress_passive,
            "blocked": self.blocked,
            "unlocked_episode_loss": self.unlocked_episode_loss,
            "allowed_passive_loss": self.allowed_passive_loss,
            "projected_episode_net": self.projected_episode_net,
            "projected_session_net": self.projected_session_net,
            "projected_drawdown": self.projected_drawdown,
            "binding_constraint": (
                self.binding_constraint.value
                if self.binding_constraint is not None
                else None
            ),
            "reachable_now": self.reachable_now,
            "entered_inventory_hold": self.entered_inventory_hold,
            "projected_episode_loss": self.projected_episode_loss,
            "projected_session_loss": self.projected_session_loss,
            "remaining_drawdown": self.remaining_drawdown,
            "active_attempts": self.active_attempts,
            "episode_active_attempts": self.active_attempts,
            "last_active_trigger": self.last_active_trigger,
            "budget_blocked": self.budget_blocked,
            "completed_episode_execution_history": [
                dict(episode)
                for episode in self.completed_episode_execution_history
            ],
            "stage_transition_history": [
                dict(transition)
                for transition in self.stage_transition_history
            ],
            "order_lane": lane,
            "order_side": order.side.value if order is not None else None,
            "order_price": order.price if order is not None else None,
            "order_amount": order.amount if order is not None else None,
            "order_reduce_only": (
                order.reduce_only if order is not None else None
            ),
            "order_time_in_force": (
                "IOC" if self.active_order is not None else "POST_ONLY"
                if self.passive_order is not None else None
            ),
        }


class InventoryEpisodeExecutor:
    """Pure inventory policy; the order manager remains the mutation authority."""

    def __init__(self, config: MarketMakerConfig) -> None:
        self.config = config
        self._next_episode_id = 1
        self._episode_id: int | None = None
        self._episode_sign = 0
        self._opened_monotonic: float | None = None
        self._active_attempts = 0
        self._active_ready_trigger: str | None = None
        self._last_active_trigger: str | None = None
        self._authenticated_episode_sequence: int | None = None
        self._entered_inventory_hold = False
        self._max_unlocked_episode_loss = _ZERO
        self._policy_decision_id = 0
        self._last_exit_stage: InventoryExitStage | None = None
        self._stage_transition_sequence = 0
        self._stage_transition_history: list[dict[str, Any]] = []
        self._completed_episode_execution_history: list[dict[str, Any]] = []

    def reset_session(self) -> None:
        self._next_episode_id = 1
        self._episode_id = None
        self._episode_sign = 0
        self._opened_monotonic = None
        self._active_attempts = 0
        self._active_ready_trigger = None
        self._last_active_trigger = None
        self._authenticated_episode_sequence = None
        self._entered_inventory_hold = False
        self._max_unlocked_episode_loss = _ZERO
        self._last_exit_stage = None
        self._stage_transition_sequence = 0
        self._stage_transition_history.clear()
        self._completed_episode_execution_history.clear()

    def record_active_attempt(self) -> None:
        if self._episode_id is not None:
            self._active_attempts += 1
            if self._active_ready_trigger in {"loss", "time"}:
                self._last_active_trigger = self._active_ready_trigger
            self._active_ready_trigger = None

    def record_authenticated_flat(self) -> bool:
        if self._episode_id is None:
            return False
        self._record_stage_transition(InventoryExitStage.COMPLETED, None)
        self._completed_episode_execution_history.append(
            {
                "episode_id": self._episode_id,
                "active_attempts": self._active_attempts,
                "last_active_trigger": self._last_active_trigger,
            }
        )
        del self._completed_episode_execution_history[
            :-_COMPLETED_EPISODE_HISTORY_LIMIT
        ]
        self._episode_id = None
        self._episode_sign = 0
        self._opened_monotonic = None
        self._active_attempts = 0
        self._active_ready_trigger = None
        self._last_active_trigger = None
        self._authenticated_episode_sequence = None
        self._entered_inventory_hold = False
        self._max_unlocked_episode_loss = _ZERO
        return True

    def policy_observation(self) -> EpisodePolicyObservation | None:
        sequence = self._authenticated_episode_sequence
        if self._episode_id is None or sequence is None:
            return None
        return EpisodePolicyObservation(
            authenticated_episode_sequence=sequence,
            entered_inventory_hold=self._entered_inventory_hold,
            active_attempts=self._active_attempts,
            max_unlocked_episode_loss=self._max_unlocked_episode_loss,
        )

    def execution_snapshot(self) -> dict[str, Any]:
        return self._decision(
            "flat", "authenticated flat checkpoint", 0.0
        ).snapshot()

    def evaluate(
        self,
        *,
        position: PositionSnapshot,
        market: MarketSnapshot,
        metadata: MarketMetadata,
        account_snapshot: dict[str, Any] | None,
        now_monotonic: float,
        soft_exit_latched: bool,
        stranded_soft_exit: bool,
        authenticated_flat: bool,
        active_unwind_pending: bool,
        normal_passive_price: Decimal | None,
    ) -> InventoryUnwindDecision:
        self._policy_decision_id += 1
        invalid = self._validate_inputs(position, market, metadata, now_monotonic)
        if invalid:
            return self._decision("blocked", invalid, 0.0, blocked=True)

        signed = position.signed_size
        sign = 1 if signed > 0 else -1 if signed < 0 else 0
        if sign == 0:
            if self._episode_id is None:
                return self._decision("flat", "no inventory episode", 0.0)
            age = self._age(now_monotonic)
            if not authenticated_flat:
                return self._decision(
                    "flat_pending_audit",
                    "authenticated flat checkpoint pending",
                    age,
                    exit_stage=InventoryExitStage.FLAT_PENDING_AUDIT,
                )
            self._record_stage_transition(InventoryExitStage.COMPLETED, None)
            self.record_authenticated_flat()
            return self._decision(
                "flat",
                "authenticated flat checkpoint",
                0.0,
                exit_stage=InventoryExitStage.COMPLETED,
            )

        if self._episode_id is None:
            self._episode_id = self._next_episode_id
            self._next_episode_id += 1
            self._episode_sign = sign
            self._opened_monotonic = now_monotonic
            self._active_attempts = 0
            self._active_ready_trigger = None
            self._last_active_trigger = None
            self._authenticated_episode_sequence = None
            self._entered_inventory_hold = False
            self._max_unlocked_episode_loss = _ZERO
            self._last_exit_stage = None
            self._stage_transition_history.clear()
        elif sign != self._episode_sign:
            return self._decision(
                "blocked",
                "inventory direction changed before authenticated flat",
                self._age(now_monotonic),
                blocked=True,
                binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
            )

        age = self._age(now_monotonic)
        economics = self._trusted_economics(account_snapshot, signed)
        if economics is None:
            if (
                self.config.active_unwind_enabled
                and age >= self.config.active_unwind_after_seconds
            ):
                return self._decision(
                    "blocked",
                    "active unwind requires fresh authenticated economics",
                    age,
                    blocked=True,
                    exit_stage=InventoryExitStage.INVENTORY_HOLD,
                    binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
                    entered_inventory_hold=True,
                )
            return self._decision(
                "passive_wait",
                "passive unwind budget is unavailable",
                age,
                suppress_passive=True,
                exit_stage=InventoryExitStage.INVENTORY_HOLD,
                binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
                entered_inventory_hold=True,
            )

        authenticated_sequence = account_snapshot.get("episode_sequence")
        if (
            type(authenticated_sequence) is not int
            or authenticated_sequence <= 0
        ):
            return self._decision(
                "passive_wait",
                "authenticated episode sequence is unavailable",
                age,
                suppress_passive=True,
                authenticated_episode_sequence=None,
                exit_stage=InventoryExitStage.INVENTORY_HOLD,
                binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
                entered_inventory_hold=True,
            )
        if self._authenticated_episode_sequence is None:
            self._authenticated_episode_sequence = authenticated_sequence
        elif self._authenticated_episode_sequence != authenticated_sequence:
            return self._decision(
                "blocked",
                "authenticated episode sequence changed before flat",
                age,
                blocked=True,
                suppress_passive=True,
                authenticated_episode_sequence=None,
                exit_stage=InventoryExitStage.INVENTORY_HOLD,
                binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
                entered_inventory_hold=True,
            )

        entry = position.entry_price
        if not isinstance(entry, Decimal) or not entry.is_finite() or entry <= 0:
            return self._decision(
                "blocked",
                "inventory entry price is invalid",
                age,
                blocked=True,
                binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
            )
        quantity = abs(signed)
        reduce_only_amount = max(self.config.order_size, quantity)
        open_net = economics["open_net"]
        completed_base = min(
            economics["completed_net"], economics["last_flat_equity_change"]
        )
        available_surplus, surplus_reserve = self._completed_surplus(
            completed_base, economics["completed_turnover"]
        )
        remaining_drawdown = max(
            _ZERO,
            self.config.max_session_drawdown - economics["current_drawdown"],
        )

        slippage_price = None
        trigger_episode_loss = _ZERO
        trigger_session_loss = _ZERO
        trigger_drawdown = _ZERO
        trigger = None
        if self.config.active_unwind_enabled:
            slippage_price = self._slippage_limit(sign, market, metadata)
            if slippage_price is None:
                return self._decision(
                    "blocked",
                    "active unwind slippage price is invalid",
                    age,
                    blocked=True,
                    suppress_passive=True,
                    remaining_drawdown=remaining_drawdown,
                    binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
                )
            trigger_net = self._projected_episode_net(
                sign,
                quantity,
                entry,
                slippage_price,
                self.config.taker_fee_rate,
                open_net,
            )
            trigger_episode_loss = max(_ZERO, -trigger_net)
            trigger_session_loss = max(
                _ZERO, -(completed_base + trigger_net)
            )
            trigger_drawdown = self._projected_drawdown(
                sign,
                quantity,
                entry,
                slippage_price,
                economics,
            )
            loss_triggered = (
                trigger_episode_loss
                >= self.config.active_unwind_loss_trigger
            )
            time_triggered = age >= self.config.active_unwind_after_seconds
            trigger = (
                "loss" if loss_triggered else "time" if time_triggered else None
            )
        if trigger is None and not soft_exit_latched:
            return self._decision(
                "profit_exit",
                "strict fee-aware profit exit",
                age,
                normal_passive_price=normal_passive_price,
                selected_exit_price=normal_passive_price,
                active_slippage_boundary_price=slippage_price,
                available_completed_surplus=available_surplus,
                surplus_reserve=surplus_reserve,
                binding_constraint=ExitBindingConstraint.NORMAL_PASSIVE,
                reachable_now=True,
            )

        if trigger is not None:
            active_episode_boundary = self._loss_price_boundary(
                sign,
                quantity,
                entry,
                open_net,
                self.config.max_episode_loss_for_unwind,
                self.config.taker_fee_rate,
                metadata,
            )
            active_session_boundary = self._loss_price_boundary(
                sign,
                quantity,
                entry,
                open_net,
                max(
                    _ZERO,
                    self.config.max_session_loss_for_unwind
                    + completed_base,
                ),
                self.config.taker_fee_rate,
                metadata,
            )
            active_drawdown_boundary = self._drawdown_price_boundary(
                sign,
                quantity,
                entry,
                economics,
                metadata,
                self.config.taker_fee_rate,
            )
            active_price = self._active_limit(
                sign,
                quantity,
                entry,
                open_net,
                completed_base,
                economics,
                market,
                metadata,
            )
            if active_price is None:
                blocked_constraint = self._active_block_constraint(
                    trigger_episode_loss,
                    trigger_session_loss,
                    trigger_drawdown,
                )
                return self._decision(
                    "blocked",
                    self._active_marketability_block_reason(
                        trigger_episode_loss,
                        trigger_session_loss,
                        trigger_drawdown,
                    ),
                    age,
                    trigger=trigger,
                    blocked=True,
                    budget_blocked=True,
                    suppress_passive=True,
                    remaining_drawdown=remaining_drawdown,
                    exit_stage=InventoryExitStage.INVENTORY_HOLD,
                    episode_boundary_price=active_episode_boundary,
                    session_boundary_price=active_session_boundary,
                    drawdown_boundary_price=active_drawdown_boundary,
                    active_slippage_boundary_price=slippage_price,
                    available_completed_surplus=available_surplus,
                    surplus_reserve=surplus_reserve,
                    projected_episode_net=trigger_net,
                    projected_session_net=completed_base + trigger_net,
                    projected_drawdown=trigger_drawdown,
                    binding_constraint=blocked_constraint,
                    reachable_now=False,
                )
            active_binding = self._selected_binding_constraint(
                active_price,
                active_episode_boundary,
                active_session_boundary,
                active_drawdown_boundary,
                slippage_price,
                ExitBindingConstraint.SESSION_LOSS_CAP,
                ExitBindingConstraint.ACTIVE_SLIPPAGE,
            )
            projected_net = self._projected_episode_net(
                sign,
                quantity,
                entry,
                active_price,
                self.config.taker_fee_rate,
                open_net,
            )
            projected_episode_loss = max(_ZERO, -projected_net)
            projected_session_loss = max(
                _ZERO, -(completed_base + projected_net)
            )
            projected_drawdown = self._projected_drawdown(
                sign,
                quantity,
                entry,
                active_price,
                economics,
            )
            active_values = {
                "exit_stage": InventoryExitStage.ACTIVE_IOC,
                "normal_passive_price": normal_passive_price,
                "selected_exit_price": active_price,
                "episode_boundary_price": active_episode_boundary,
                "session_boundary_price": active_session_boundary,
                "drawdown_boundary_price": active_drawdown_boundary,
                "active_slippage_boundary_price": slippage_price,
                "available_completed_surplus": available_surplus,
                "surplus_reserve": surplus_reserve,
                "projected_episode_net": projected_net,
                "projected_session_net": completed_base + projected_net,
                "projected_drawdown": projected_drawdown,
                "binding_constraint": active_binding,
                "reachable_now": True,
            }
            if active_unwind_pending:
                return self._decision(
                    "active_pending",
                    "active unwind terminal proof pending",
                    age,
                    trigger=trigger,
                    suppress_passive=True,
                    projected_episode_loss=projected_episode_loss,
                    projected_session_loss=projected_session_loss,
                    remaining_drawdown=remaining_drawdown,
                    **active_values,
                )
            if self._active_attempts >= self.config.active_unwind_max_attempts:
                return self._decision(
                    "blocked",
                    "active unwind attempt limit exhausted",
                    age,
                    trigger=trigger,
                    blocked=True,
                    suppress_passive=True,
                    projected_episode_loss=projected_episode_loss,
                    projected_session_loss=projected_session_loss,
                    remaining_drawdown=remaining_drawdown,
                    **{
                        **active_values,
                        "binding_constraint": ExitBindingConstraint.ATTEMPT_CAP,
                    },
                )
            blocked_reason = self._active_budget_block(
                projected_episode_loss,
                projected_session_loss,
                projected_drawdown,
            )
            if blocked_reason:
                return self._decision(
                    "blocked",
                    blocked_reason,
                    age,
                    trigger=trigger,
                    blocked=True,
                    budget_blocked=True,
                    suppress_passive=True,
                    projected_episode_loss=projected_episode_loss,
                    projected_session_loss=projected_session_loss,
                    remaining_drawdown=remaining_drawdown,
                    **active_values,
                )
            side = OrderSide.SELL if sign > 0 else OrderSide.BUY
            order = DesiredOrder(
                side=side,
                price=active_price,
                amount=reduce_only_amount,
                reduce_only=True,
                reason=f"inventory unwind {trigger} barrier",
                intent=self._order_intent(
                    OrderIntentKind.ACTIVE_EXIT,
                    InventoryExitStage.ACTIVE_IOC,
                    active_binding,
                    available_completed_surplus=available_surplus,
                    surplus_reserve=surplus_reserve,
                ),
            )
            self._active_ready_trigger = trigger
            return self._decision(
                "active_ready",
                order.reason,
                age,
                trigger=trigger,
                active_order=order,
                suppress_passive=True,
                projected_episode_loss=projected_episode_loss,
                projected_session_loss=projected_session_loss,
                remaining_drawdown=remaining_drawdown,
                **active_values,
            )

        progress = self._passive_unlock_progress(age)
        unlocked = self.config.max_episode_loss_for_unwind * progress
        episode_boundary = self._passive_limit(
            sign,
            quantity,
            entry,
            open_net,
            unlocked,
            metadata,
        )
        if self.config.max_session_loss_for_maker_exit > 0:
            session_allowance = max(
                _ZERO,
                self.config.max_session_loss_for_maker_exit
                + completed_base,
            )
            session_boundary = self._passive_limit(
                sign,
                quantity,
                entry,
                open_net,
                session_allowance,
                metadata,
            )
        else:
            session_allowance = self._authenticated_surplus_allowance(
                completed_base,
                economics["completed_turnover"],
            )
            session_boundary = self._surplus_price_boundary(
                sign,
                quantity,
                entry,
                open_net,
                completed_base,
                economics["completed_turnover"],
                economics["open_turnover"],
                metadata,
            )
            if session_boundary is None:
                return self._decision(
                    "blocked",
                    "passive surplus exit denominator must be positive",
                    age,
                    blocked=True,
                    suppress_passive=True,
                    unlocked_episode_loss=unlocked,
                    allowed_passive_loss=session_allowance,
                    remaining_drawdown=remaining_drawdown,
                    exit_stage=InventoryExitStage.INVENTORY_HOLD,
                    available_completed_surplus=available_surplus,
                    surplus_reserve=surplus_reserve,
                    binding_constraint=ExitBindingConstraint.SESSION_SURPLUS,
                    reachable_now=False,
                    entered_inventory_hold=True,
                )
        drawdown_boundary = self._drawdown_price_boundary(
            sign,
            quantity,
            entry,
            economics,
            metadata,
            self.config.maker_fee_rate,
        )
        allowed = min(unlocked, session_allowance, remaining_drawdown)
        normal_price = normal_passive_price
        if (
            not isinstance(normal_price, Decimal)
            or not normal_price.is_finite()
            or normal_price <= 0
        ):
            return self._decision(
                "blocked",
                "normal passive unwind price is unavailable",
                age,
                blocked=True,
                suppress_passive=True,
                unlocked_episode_loss=unlocked,
                allowed_passive_loss=allowed,
                remaining_drawdown=remaining_drawdown,
                exit_stage=InventoryExitStage.INVENTORY_HOLD,
                normal_passive_price=normal_price,
                episode_boundary_price=episode_boundary,
                session_boundary_price=session_boundary,
                drawdown_boundary_price=drawdown_boundary,
                active_slippage_boundary_price=slippage_price,
                available_completed_surplus=available_surplus,
                surplus_reserve=surplus_reserve,
                binding_constraint=ExitBindingConstraint.DATA_UNTRUSTED,
                reachable_now=False,
                entered_inventory_hold=True,
            )
        if sign > 0:
            passive_boundary = max(
                episode_boundary, session_boundary, drawdown_boundary
            )
            reachable = normal_price >= passive_boundary
            passive_price = max(normal_price, passive_boundary)
        else:
            passive_boundary = min(
                episode_boundary, session_boundary, drawdown_boundary
            )
            reachable = normal_price <= passive_boundary
            passive_price = min(normal_price, passive_boundary)
        session_constraint = (
            ExitBindingConstraint.SESSION_LOSS_CAP
            if self.config.max_session_loss_for_maker_exit > 0
            else ExitBindingConstraint.SESSION_SURPLUS
        )
        binding_constraint = (
            ExitBindingConstraint.NORMAL_PASSIVE
            if reachable
            else self._selected_binding_constraint(
                passive_price,
                episode_boundary,
                session_boundary,
                drawdown_boundary,
                None,
                session_constraint,
                ExitBindingConstraint.NORMAL_PASSIVE,
            )
        )
        exit_stage = (
            InventoryExitStage.SURPLUS_FUNDED_PASSIVE
            if reachable
            and self.config.max_session_loss_for_maker_exit == 0
            else InventoryExitStage.BOUNDED_PASSIVE_LOSS
            if reachable
            else InventoryExitStage.INVENTORY_HOLD
        )
        passive_order = DesiredOrder(
            side=OrderSide.SELL if sign > 0 else OrderSide.BUY,
            price=passive_price,
            amount=reduce_only_amount,
            reduce_only=True,
            reason="inventory unwind passive budget chase",
            intent=self._order_intent(
                OrderIntentKind.PASSIVE_EXIT,
                exit_stage,
                binding_constraint,
                available_completed_surplus=available_surplus,
                surplus_reserve=surplus_reserve,
                unlocked_episode_loss=unlocked,
                allowed_passive_loss=allowed,
                entered_inventory_hold=not reachable,
            ),
        )
        projected_net = self._projected_episode_net(
            sign,
            quantity,
            entry,
            passive_price,
            self.config.maker_fee_rate,
            open_net,
        )
        projected_episode_loss = max(_ZERO, -projected_net)
        projected_session_loss = max(
            _ZERO, -(completed_base + projected_net)
        )
        projected_drawdown = self._projected_drawdown(
            sign,
            quantity,
            entry,
            passive_price,
            economics,
            self.config.maker_fee_rate,
        )
        return self._decision(
            "passive_chase" if reachable else "unwind_blocked",
            (
                passive_order.reason
                if reachable
                else "passive quote held at the authenticated economic boundary"
            ),
            age,
            passive_order=passive_order,
            unlocked_episode_loss=unlocked,
            allowed_passive_loss=allowed,
            projected_episode_loss=projected_episode_loss,
            projected_session_loss=projected_session_loss,
            remaining_drawdown=remaining_drawdown,
            exit_stage=exit_stage,
            normal_passive_price=normal_price,
            selected_exit_price=passive_price,
            episode_boundary_price=episode_boundary,
            session_boundary_price=session_boundary,
            drawdown_boundary_price=drawdown_boundary,
            active_slippage_boundary_price=slippage_price,
            available_completed_surplus=available_surplus,
            surplus_reserve=surplus_reserve,
            projected_episode_net=projected_net,
            projected_session_net=completed_base + projected_net,
            projected_drawdown=projected_drawdown,
            binding_constraint=binding_constraint,
            reachable_now=reachable,
            entered_inventory_hold=not reachable,
        )

    def _decision(
        self, state: str, reason: str, age: float, **values: Any
    ) -> InventoryUnwindDecision:
        exit_stage = values.setdefault(
            "exit_stage", self._default_exit_stage(state)
        )
        if exit_stage is InventoryExitStage.INVENTORY_HOLD or (
            state in {"blocked", "passive_wait"}
            and values.get("suppress_passive") is True
        ):
            self._entered_inventory_hold = True
        unlocked = values.get("unlocked_episode_loss")
        if (
            isinstance(unlocked, Decimal)
            and unlocked.is_finite()
            and unlocked >= 0
        ):
            self._max_unlocked_episode_loss = max(
                self._max_unlocked_episode_loss, unlocked
            )
        values["entered_inventory_hold"] = bool(
            values.get("entered_inventory_hold")
            or self._entered_inventory_hold
        )
        self._record_stage_transition(
            exit_stage, values.get("binding_constraint")
        )
        values.setdefault(
            "authenticated_episode_sequence",
            self._authenticated_episode_sequence
            if self._episode_id is not None
            else None,
        )
        return InventoryUnwindDecision(
            episode_id=self._episode_id,
            state=state,
            reason=reason,
            age_seconds=age,
            policy_decision_id=self._policy_decision_id,
            active_attempts=self._active_attempts,
            last_active_trigger=self._last_active_trigger,
            completed_episode_execution_history=tuple(
                dict(episode)
                for episode in self._completed_episode_execution_history
            ),
            stage_transition_history=tuple(
                dict(transition)
                for transition in self._stage_transition_history
            ),
            **values,
        )

    def _default_exit_stage(
        self, state: str
    ) -> InventoryExitStage | None:
        if state == "profit_exit":
            return InventoryExitStage.STRICT_PROFIT
        if state == "passive_chase":
            return (
                InventoryExitStage.SURPLUS_FUNDED_PASSIVE
                if self.config.max_session_loss_for_maker_exit == 0
                else InventoryExitStage.BOUNDED_PASSIVE_LOSS
            )
        if state in {"unwind_blocked", "passive_wait"}:
            return InventoryExitStage.INVENTORY_HOLD
        if state in {"active_ready", "active_pending"}:
            return InventoryExitStage.ACTIVE_IOC
        if state == "flat_pending_audit":
            return InventoryExitStage.FLAT_PENDING_AUDIT
        return None

    def _record_stage_transition(
        self,
        exit_stage: InventoryExitStage | None,
        binding_constraint: ExitBindingConstraint | None,
    ) -> None:
        if (
            self._episode_id is None
            or exit_stage is None
            or exit_stage is self._last_exit_stage
        ):
            return
        self._stage_transition_sequence += 1
        self._stage_transition_history.append(
            {
                "transition_sequence": self._stage_transition_sequence,
                "episode_id": self._episode_id,
                "policy_decision_id": self._policy_decision_id,
                "exit_stage": exit_stage.value,
                "binding_constraint": (
                    binding_constraint.value
                    if binding_constraint is not None
                    else None
                ),
            }
        )
        del self._stage_transition_history[:-_STAGE_TRANSITION_HISTORY_LIMIT]
        self._last_exit_stage = exit_stage

    def _order_intent(
        self,
        kind: OrderIntentKind,
        exit_stage: InventoryExitStage,
        binding_constraint: ExitBindingConstraint,
        *,
        available_completed_surplus: Decimal | None = None,
        surplus_reserve: Decimal | None = None,
        unlocked_episode_loss: Decimal | None = None,
        allowed_passive_loss: Decimal | None = None,
        entered_inventory_hold: bool = False,
    ) -> OrderIntentMetadata:
        return OrderIntentMetadata(
            kind=kind,
            revision=self._policy_decision_id,
            inventory_episode_id=self._episode_id,
            authenticated_episode_sequence=(
                self._authenticated_episode_sequence
            ),
            exit_stage=exit_stage,
            policy_decision_id=self._policy_decision_id,
            binding_constraint=binding_constraint,
            available_completed_surplus=available_completed_surplus,
            surplus_reserve=surplus_reserve,
            unlocked_episode_loss=unlocked_episode_loss,
            allowed_passive_loss=allowed_passive_loss,
            entered_inventory_hold=(
                entered_inventory_hold or self._entered_inventory_hold
            ),
            active_attempts=self._active_attempts,
        )

    def _age(self, now: float) -> float:
        return max(
            0.0,
            now - self._opened_monotonic
            if self._opened_monotonic is not None
            else 0.0,
        )

    def _passive_unlock_progress(self, age: float) -> Decimal:
        start = self.config.soft_exit_after_seconds
        end = self.config.active_unwind_after_seconds
        if age <= start:
            return _ZERO
        if end <= start:
            return _ZERO
        return min(
            _ONE,
            Decimal(str(age - start)) / Decimal(end - start),
        )

    def _active_limit(
        self,
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        open_net: Decimal,
        completed_base: Decimal,
        economics: dict[str, Decimal],
        market: MarketSnapshot,
        metadata: MarketMetadata,
    ) -> Decimal | None:
        distance = (
            metadata.price_tick * self.config.active_unwind_max_slippage_ticks
        )
        episode_boundary = self._loss_price_boundary(
            sign,
            quantity,
            entry,
            open_net,
            self.config.max_episode_loss_for_unwind,
            self.config.taker_fee_rate,
            metadata,
        )
        session_allowance = max(
            _ZERO, self.config.max_session_loss_for_unwind + completed_base
        )
        session_boundary = self._loss_price_boundary(
            sign,
            quantity,
            entry,
            open_net,
            session_allowance,
            self.config.taker_fee_rate,
            metadata,
        )
        drawdown_boundary = self._drawdown_price_boundary(
            sign,
            quantity,
            entry,
            economics,
            metadata,
            self.config.taker_fee_rate,
        )
        if sign > 0:
            price = max(
                floor_to_step(
                    market.best_bid - distance, metadata.price_tick
                ),
                episode_boundary,
                session_boundary,
                drawdown_boundary,
            )
            return price if 0 < price <= market.best_bid else None
        price = min(
            ceil_to_step(market.best_ask + distance, metadata.price_tick),
            episode_boundary,
            session_boundary,
            drawdown_boundary,
        )
        return price if price >= market.best_ask and price > 0 else None

    def _slippage_limit(
        self,
        sign: int,
        market: MarketSnapshot,
        metadata: MarketMetadata,
    ) -> Decimal | None:
        distance = metadata.price_tick * self.config.active_unwind_max_slippage_ticks
        if sign > 0:
            price = floor_to_step(market.best_bid - distance, metadata.price_tick)
        else:
            price = ceil_to_step(market.best_ask + distance, metadata.price_tick)
        return price if price > 0 else None

    def _passive_limit(
        self,
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        open_net: Decimal,
        allowed_loss: Decimal,
        metadata: MarketMetadata,
    ) -> Decimal:
        return self._loss_price_boundary(
            sign,
            quantity,
            entry,
            open_net,
            allowed_loss,
            self.config.maker_fee_rate,
            metadata,
        )

    def _authenticated_surplus_allowance(
        self,
        completed_base: Decimal,
        completed_turnover: Decimal,
    ) -> Decimal:
        available, reserve = self._completed_surplus(
            completed_base, completed_turnover
        )
        return max(_ZERO, available - reserve)

    def _completed_surplus(
        self,
        completed_base: Decimal,
        completed_turnover: Decimal,
    ) -> tuple[Decimal, Decimal]:
        minimum_rate = (
            self.config.min_completed_net_turnover_bps
            / Decimal("10000")
        )
        completed_target = minimum_rate * completed_turnover
        available = max(_ZERO, completed_base - completed_target)
        reserve = min(
            available,
            self.config.soft_exit_surplus_reserve_bps
            / Decimal("10000")
            * completed_turnover,
        )
        return available, reserve

    def _surplus_price_boundary(
        self,
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        open_net: Decimal,
        completed_base: Decimal,
        completed_turnover: Decimal,
        open_turnover: Decimal,
        metadata: MarketMetadata,
    ) -> Decimal | None:
        minimum_rate = (
            self.config.min_completed_net_turnover_bps
            / Decimal("10000")
        )
        reserve = min(
            max(
                _ZERO,
                completed_base - minimum_rate * completed_turnover,
            ),
            self.config.soft_exit_surplus_reserve_bps
            / Decimal("10000")
            * completed_turnover,
        )
        required_net = (
            minimum_rate * (completed_turnover + open_turnover)
            + reserve
        )
        fee = self.config.maker_fee_rate
        if sign > 0:
            denominator = quantity * (_ONE - fee - minimum_rate)
            if denominator <= 0:
                return None
            raw = (
                required_net
                - completed_base
                - open_net
                + quantity * entry
            ) / denominator
            return ceil_to_step(raw, metadata.price_tick)
        raw = (
            completed_base
            + open_net
            + quantity * entry
            - required_net
        ) / (quantity * (_ONE + fee + minimum_rate))
        return floor_to_step(raw, metadata.price_tick)

    @staticmethod
    def _loss_price_boundary(
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        open_net: Decimal,
        allowed_loss: Decimal,
        fee: Decimal,
        metadata: MarketMetadata,
    ) -> Decimal:
        if sign > 0:
            raw = (
                quantity * entry - allowed_loss - open_net
            ) / (quantity * (_ONE - fee))
            return ceil_to_step(raw, metadata.price_tick)
        raw = (
            open_net + quantity * entry + allowed_loss
        ) / (quantity * (_ONE + fee))
        return floor_to_step(raw, metadata.price_tick)

    @staticmethod
    def _projected_episode_net(
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        exit_price: Decimal,
        fee_rate: Decimal,
        open_net: Decimal,
    ) -> Decimal:
        gross = (
            quantity * (exit_price - entry)
            if sign > 0
            else quantity * (entry - exit_price)
        )
        return open_net + gross - quantity * exit_price * fee_rate

    def _projected_drawdown(
        self,
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        exit_price: Decimal,
        economics: dict[str, Decimal],
        fee_rate: Decimal | None = None,
    ) -> Decimal:
        gross = (
            quantity * (exit_price - entry)
            if sign > 0
            else quantity * (entry - exit_price)
        )
        projected_equity = (
            economics["current_equity"]
            - economics["audited_unrealized_pnl"]
            + gross
            - quantity
            * exit_price
            * (
                self.config.taker_fee_rate
                if fee_rate is None
                else fee_rate
            )
        )
        return max(_ZERO, economics["baseline_equity"] - projected_equity)

    def _drawdown_price_boundary(
        self,
        sign: int,
        quantity: Decimal,
        entry: Decimal,
        economics: dict[str, Decimal],
        metadata: MarketMetadata,
        fee: Decimal,
    ) -> Decimal:
        required_equity = (
            economics["baseline_equity"] - self.config.max_session_drawdown
        )
        equity_before_close = (
            economics["current_equity"]
            - economics["audited_unrealized_pnl"]
        )
        if sign > 0:
            raw = (
                required_equity - equity_before_close + quantity * entry
            ) / (quantity * (_ONE - fee))
            return ceil_to_step(raw, metadata.price_tick)
        raw = (
            equity_before_close + quantity * entry - required_equity
        ) / (quantity * (_ONE + fee))
        return floor_to_step(raw, metadata.price_tick)

    def _active_budget_block(
        self,
        episode_loss: Decimal,
        session_loss: Decimal,
        projected_drawdown: Decimal,
    ) -> str | None:
        if episode_loss > self.config.max_episode_loss_for_unwind:
            return "projected active unwind episode loss exceeds cap"
        if session_loss > self.config.max_session_loss_for_unwind:
            return "projected active unwind session loss exceeds cap"
        if projected_drawdown >= self.config.max_session_drawdown:
            return "projected active unwind drawdown reaches cap"
        return None

    @staticmethod
    def _selected_binding_constraint(
        selected_price: Decimal,
        episode_boundary: Decimal,
        session_boundary: Decimal,
        drawdown_boundary: Decimal,
        lane_boundary: Decimal | None,
        session_constraint: ExitBindingConstraint,
        lane_constraint: ExitBindingConstraint,
    ) -> ExitBindingConstraint:
        ordered = (
            (episode_boundary, ExitBindingConstraint.EPISODE_CAP),
            (session_boundary, session_constraint),
            (drawdown_boundary, ExitBindingConstraint.DRAWDOWN_CAP),
            (lane_boundary, lane_constraint),
        )
        for boundary, constraint in ordered:
            if boundary is not None and selected_price == boundary:
                return constraint
        return lane_constraint

    def _active_block_constraint(
        self,
        episode_loss: Decimal,
        session_loss: Decimal,
        projected_drawdown: Decimal,
    ) -> ExitBindingConstraint:
        if episode_loss > self.config.max_episode_loss_for_unwind:
            return ExitBindingConstraint.EPISODE_CAP
        if session_loss > self.config.max_session_loss_for_unwind:
            return ExitBindingConstraint.SESSION_LOSS_CAP
        if projected_drawdown >= self.config.max_session_drawdown:
            return ExitBindingConstraint.DRAWDOWN_CAP
        return ExitBindingConstraint.ACTIVE_SLIPPAGE

    def _active_marketability_block_reason(
        self,
        episode_loss: Decimal,
        session_loss: Decimal,
        projected_drawdown: Decimal,
    ) -> str:
        if episode_loss > self.config.max_episode_loss_for_unwind:
            return "marketable active unwind exceeds episode loss cap"
        if session_loss > self.config.max_session_loss_for_unwind:
            return "marketable active unwind exceeds session loss cap"
        if projected_drawdown >= self.config.max_session_drawdown:
            return "marketable active unwind reaches drawdown cap"
        return "active unwind has no marketable price inside all caps"

    def _trusted_economics(
        self, snapshot: dict[str, Any] | None, signed: Decimal
    ) -> dict[str, Decimal] | None:
        if not isinstance(snapshot, dict) or snapshot.get("state") != "healthy":
            return None
        values = {
            "ledger_position": snapshot.get("ledger_position"),
            "completed_net": snapshot.get("completed_net_ex_funding"),
            "last_flat_equity_change": snapshot.get("last_flat_equity_change"),
            "open_net": snapshot.get("open_episode_net_ex_funding"),
            "current_drawdown": snapshot.get("current_drawdown"),
            "baseline_equity": snapshot.get("baseline_equity"),
            "current_equity": snapshot.get("current_equity"),
            "audited_position": snapshot.get("audited_position"),
            "audited_unrealized_pnl": snapshot.get(
                "audited_unrealized_pnl"
            ),
        }
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in values.values()
        ):
            return None
        turnover_values = {
            "completed_turnover": snapshot.get("completed_turnover"),
            "open_turnover": snapshot.get("open_episode_turnover"),
        }
        trusted_turnover = all(
            isinstance(value, Decimal)
            and value.is_finite()
            and value >= 0
            for value in turnover_values.values()
        )
        if (
            self.config.max_session_loss_for_maker_exit == 0
            and not trusted_turnover
        ):
            return None
        values.update(
            turnover_values
            if trusted_turnover
            else {
                "completed_turnover": _ZERO,
                "open_turnover": _ZERO,
            }
        )
        if (
            values["ledger_position"] != signed
            or values["audited_position"] != signed
            or values["current_drawdown"] < 0
        ):
            return None
        age = snapshot.get("age_seconds")
        if (
            isinstance(age, bool)
            or not isinstance(age, (int, float, Decimal))
            or not math.isfinite(age)
            or age < 0
            or Decimal(str(age))
            > Decimal(
                self.config.account_audit_interval_seconds
                + self.config.account_audit_timeout_seconds
            )
        ):
            return None
        return values

    def _validate_inputs(
        self,
        position: PositionSnapshot,
        market: MarketSnapshot,
        metadata: MarketMetadata,
        now: float,
    ) -> str | None:
        if (
            position.symbol != self.config.symbol
            or market.symbol != self.config.symbol
            or metadata.symbol != self.config.symbol
        ):
            return "inventory unwind symbol mismatch"
        if (
            not isinstance(position.signed_size, Decimal)
            or not position.signed_size.is_finite()
            or abs(position.signed_size) > self.config.max_position
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            return "inventory unwind inputs are invalid"
        unrealized_pnl = position.unrealized_pnl
        if (
            (unrealized_pnl is None and position.signed_size != 0)
            or (
                unrealized_pnl is not None
                and (
                    not isinstance(unrealized_pnl, Decimal)
                    or not unrealized_pnl.is_finite()
                )
            )
        ):
            return "inventory unrealized pnl is invalid"
        if (
            not isinstance(market.best_bid, Decimal)
            or not market.best_bid.is_finite()
            or market.best_bid <= 0
            or not isinstance(market.best_ask, Decimal)
            or not market.best_ask.is_finite()
            or market.best_ask <= 0
            or market.best_bid >= market.best_ask
        ):
            return "inventory unwind market is invalid"
        if (
            not isinstance(metadata.price_tick, Decimal)
            or not metadata.price_tick.is_finite()
            or metadata.price_tick <= 0
            or not isinstance(metadata.quantity_step, Decimal)
            or not metadata.quantity_step.is_finite()
            or metadata.quantity_step <= 0
        ):
            return "inventory unwind metadata is invalid"
        return None
