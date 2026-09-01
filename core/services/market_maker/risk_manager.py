from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .config import MarketMakerConfig, floor_to_step
from .models import (
    ManagedOrder,
    MarketMetadata,
    OrderSide,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_EXPOSURE_STATES = {
    OrderSlotState.SUBMITTING,
    OrderSlotState.LIVE,
    OrderSlotState.PARTIALLY_FILLED,
    OrderSlotState.CANCELING,
    OrderSlotState.UNCERTAIN_SUBMISSION,
    OrderSlotState.UNCERTAIN_CANCELLATION,
}


def _finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


@dataclass(frozen=True)
class RiskDecision:
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    buy_reduce_only: bool
    sell_reduce_only: bool
    buy_capacity: Decimal
    sell_capacity: Decimal
    worst_long: Decimal
    worst_short: Decimal
    inventory_ratio: Decimal
    runtime_state: RuntimeState
    reason: str
    safe: bool
    soft_exit_latched: bool = False

    @property
    def allow_buy(self) -> bool:
        return self.safe and self.buy_amount is not None and self.buy_amount > 0

    @property
    def allow_sell(self) -> bool:
        return self.safe and self.sell_amount is not None and self.sell_amount > 0


class RiskManager:
    def __init__(self, config: MarketMakerConfig):
        self.config = config
        self._inventory_sign = 0
        self._inventory_started_monotonic: float | None = None
        self._soft_exit_latched = False

    def evaluate(
        self,
        position: PositionSnapshot | None,
        live_orders: Iterable[ManagedOrder],
        metadata: MarketMetadata,
        *,
        now_monotonic: float | None = None,
        allow_new_episode: bool = True,
        force_inventory_exit: bool = False,
    ) -> RiskDecision:
        if position is None:
            return self._paused(
                RuntimeState.PAUSED_POSITION, "position snapshot is unknown"
            )
        if (
            position.symbol != self.config.symbol
            or metadata.symbol != self.config.symbol
            or not _finite_decimal(position.signed_size)
        ):
            return self._paused(
                RuntimeState.PAUSED_POSITION, "position snapshot is invalid"
            )

        inventory_ratio = max(
            -_ONE,
            min(_ONE, position.signed_size / self.config.max_position),
        )
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or isinstance(position.received_monotonic, bool)
            or not isinstance(position.received_monotonic, (int, float))
            or not math.isfinite(position.received_monotonic)
        ):
            return self._paused(
                RuntimeState.PAUSED_POSITION,
                "position snapshot is stale",
                position=position.signed_size,
                inventory_ratio=inventory_ratio,
            )
        position_age = now - position.received_monotonic
        if position_age < 0 or position_age > self.config.stale_position_seconds:
            return self._paused(
                RuntimeState.PAUSED_POSITION,
                (
                    "position snapshot timestamp is in the future"
                    if position_age < 0
                    else "position snapshot is stale"
                ),
                position=position.signed_size,
                inventory_ratio=inventory_ratio,
            )
        inventory_age = self._inventory_age_seconds(
            position.signed_size, now
        )
        if (
            not _finite_decimal(metadata.quantity_step)
            or metadata.quantity_step <= 0
            or not _finite_decimal(metadata.min_base_amount)
            or metadata.min_base_amount < 0
        ):
            return self._paused(
                RuntimeState.PAUSED_ERROR,
                "market quantity metadata is invalid",
                position=position.signed_size,
                inventory_ratio=inventory_ratio,
            )

        try:
            orders = tuple(live_orders)
        except TypeError:
            return self._paused(
                RuntimeState.PAUSED_ORDER_STATE,
                "live order state is unavailable",
                position=position.signed_size,
                inventory_ratio=inventory_ratio,
            )

        live_buy, live_sell, invalid_order = self._live_exposure(orders)
        if invalid_order:
            return self._paused(
                RuntimeState.PAUSED_ORDER_STATE,
                invalid_order,
                position=position.signed_size,
                inventory_ratio=inventory_ratio,
            )

        raw_buy_capacity = (
            self.config.max_position - position.signed_size - live_buy
        )
        raw_sell_capacity = (
            self.config.max_position + position.signed_size - live_sell
        )
        buy_capacity = max(_ZERO, raw_buy_capacity)
        sell_capacity = max(_ZERO, raw_sell_capacity)
        # Capacity is reported as room for an additional order.  Desired
        # amounts, however, are targets for the existing one-order-per-side
        # slot, so its current remaining amount is reusable after a confirmed
        # cancel-before-replace.
        buy_target_capacity = buy_capacity + live_buy
        sell_target_capacity = sell_capacity + live_sell
        live_worst_long = position.signed_size + live_buy
        live_worst_short = position.signed_size - live_sell

        if (
            live_buy > 0 and live_worst_long > self.config.max_position
        ) or (
            live_sell > 0 and live_worst_short < -self.config.max_position
        ):
            return RiskDecision(
                buy_amount=None,
                sell_amount=None,
                buy_reduce_only=False,
                sell_reduce_only=False,
                buy_capacity=buy_capacity,
                sell_capacity=sell_capacity,
                worst_long=live_worst_long,
                worst_short=live_worst_short,
                inventory_ratio=inventory_ratio,
                runtime_state=RuntimeState.PAUSED_ORDER_STATE,
                reason="live orders already exceed worst-case exposure limit",
                safe=False,
            )

        absolute_ratio = abs(inventory_ratio)
        buy_reduce_only = False
        sell_reduce_only = False
        reason = "normal inventory"
        state = RuntimeState.ACTIVE

        residual_position = (
            _ZERO < abs(position.signed_size) < self.config.order_size
        )
        ping_pong_reduction = (
            self.config.ping_pong_enabled and position.signed_size != _ZERO
        )
        normal_reduction = (
            force_inventory_exit
            or ping_pong_reduction
            or residual_position
            or absolute_ratio >= self.config.hard_position_ratio
        )
        if (
            not self._soft_exit_latched
            and self.config.soft_exit_after_seconds > 0
            and inventory_age >= self.config.soft_exit_after_seconds
            and position.signed_size != _ZERO
        ):
            self._soft_exit_latched = True
        if force_inventory_exit and position.signed_size == _ZERO:
            state = RuntimeState.SYNCING
            reason = "economic stop flat pending authenticated audit"
            requested_buy = None
            requested_sell = None
        elif (
            self.config.ping_pong_enabled
            and position.signed_size == _ZERO
            and not allow_new_episode
        ):
            state = RuntimeState.SYNCING
            reason = "authenticated flat checkpoint pending"
            requested_buy = None
            requested_sell = None
        elif normal_reduction or self._soft_exit_latched:
            state = RuntimeState.RISK_REDUCTION
            reason = (
                "economic stop pending authenticated flat"
                if force_inventory_exit
                else "soft exit latched until flat"
                if self._soft_exit_latched
                else (
                    "ping-pong inventory exit"
                    if ping_pong_reduction
                    else (
                        "sub-order residual position"
                        if residual_position
                        else (
                            "absolute position limit reached"
                            if abs(position.signed_size)
                            >= self.config.max_position
                            else "hard inventory limit reached"
                        )
                    )
                )
            )
            reduction_amount = max(
                self.config.order_size,
                abs(position.signed_size),
            )
            if position.signed_size > 0:
                requested_buy = None
                requested_sell = reduction_amount
                sell_reduce_only = True
            else:
                requested_buy = reduction_amount
                requested_sell = None
                buy_reduce_only = True
        else:
            requested_buy = self.config.order_size
            requested_sell = self.config.order_size
            if absolute_ratio >= self.config.soft_position_ratio:
                multiplier = (
                    self.config.hard_position_ratio - absolute_ratio
                ) / (
                    self.config.hard_position_ratio
                    - self.config.soft_position_ratio
                )
                reason = "soft inventory limit reached"
                if position.signed_size > 0:
                    requested_buy *= multiplier
                elif position.signed_size < 0:
                    requested_sell *= multiplier

        if not force_inventory_exit:
            if self.config.quote_mode == "bid_only":
                requested_sell = None
            elif self.config.quote_mode == "ask_only":
                requested_buy = None

        buy_amount = self._candidate_amount(
            requested_buy,
            None if buy_reduce_only else buy_target_capacity,
            metadata,
        )
        sell_amount = self._candidate_amount(
            requested_sell,
            None if sell_reduce_only else sell_target_capacity,
            metadata,
        )
        target_buy = (
            buy_amount
            if buy_amount is not None and not buy_reduce_only
            else _ZERO
        )
        target_sell = (
            sell_amount
            if sell_amount is not None and not sell_reduce_only
            else _ZERO
        )
        worst_long = position.signed_size + max(live_buy, target_buy)
        worst_short = position.signed_size - max(live_sell, target_sell)

        return RiskDecision(
            buy_amount=buy_amount,
            sell_amount=sell_amount,
            buy_reduce_only=buy_reduce_only and buy_amount is not None,
            sell_reduce_only=sell_reduce_only and sell_amount is not None,
            buy_capacity=buy_capacity,
            sell_capacity=sell_capacity,
            worst_long=worst_long,
            worst_short=worst_short,
            inventory_ratio=inventory_ratio,
            runtime_state=state,
            reason=reason,
            safe=True,
            soft_exit_latched=self._soft_exit_latched,
        )

    def _inventory_age_seconds(
        self, signed_size: Decimal, now_monotonic: float
    ) -> float:
        sign = 1 if signed_size > 0 else -1 if signed_size < 0 else 0
        if sign == 0:
            self._inventory_sign = 0
            self._inventory_started_monotonic = None
            self._soft_exit_latched = False
            return 0.0
        if (
            self._inventory_started_monotonic is None
            or sign != self._inventory_sign
            or now_monotonic < self._inventory_started_monotonic
        ):
            self._inventory_sign = sign
            self._inventory_started_monotonic = now_monotonic
            return 0.0
        return now_monotonic - self._inventory_started_monotonic

    @staticmethod
    def _live_exposure(
        orders: tuple[ManagedOrder, ...],
    ) -> tuple[Decimal, Decimal, str | None]:
        buy = _ZERO
        sell = _ZERO
        for order in orders:
            if (
                not isinstance(order, ManagedOrder)
                or not isinstance(order.state, OrderSlotState)
                or order.side not in {OrderSide.BUY, OrderSide.SELL}
            ):
                return _ZERO, _ZERO, "live order state is invalid"
            exposure_live = (
                order.state in _EXPOSURE_STATES
                or order.submission_uncertain
                or order.cancellation_uncertain
            )
            if not exposure_live:
                continue
            if (
                not _finite_decimal(order.remaining)
                or order.remaining < 0
                or type(order.reduce_only) is not bool
            ):
                return _ZERO, _ZERO, "live order exposure is invalid"
            if order.reduce_only:
                continue
            if order.side is OrderSide.BUY:
                buy += order.remaining
            else:
                sell += order.remaining
        return buy, sell, None

    @staticmethod
    def _candidate_amount(
        requested: Decimal | None,
        capacity: Decimal | None,
        metadata: MarketMetadata,
    ) -> Decimal | None:
        if requested is None or requested <= 0:
            return None
        allowed = requested if capacity is None else min(requested, capacity)
        if allowed <= 0:
            return None
        amount = floor_to_step(allowed, metadata.quantity_step)
        if amount <= 0 or amount < metadata.min_base_amount:
            return None
        return amount

    @staticmethod
    def _paused(
        state: RuntimeState,
        reason: str,
        *,
        position: Decimal = _ZERO,
        inventory_ratio: Decimal = _ZERO,
    ) -> RiskDecision:
        return RiskDecision(
            buy_amount=None,
            sell_amount=None,
            buy_reduce_only=False,
            sell_reduce_only=False,
            buy_capacity=_ZERO,
            sell_capacity=_ZERO,
            worst_long=position,
            worst_short=position,
            inventory_ratio=inventory_ratio,
            runtime_state=state,
            reason=reason,
            safe=False,
        )
