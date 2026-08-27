from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable

from .config import MarketMakerConfig, ceil_to_step, floor_to_step, is_step_aligned
from .models import (
    DesiredOrder,
    DesiredQuotes,
    ManagedOrder,
    MarketMetadata,
    MarketSnapshot,
    OrderSide,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)

if TYPE_CHECKING:
    from .risk_manager import RiskDecision


_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_TEN_THOUSAND = Decimal("10000")
_OWN_QUOTE_STATES = {
    OrderSlotState.LIVE,
    OrderSlotState.PARTIALLY_FILLED,
    OrderSlotState.CANCELING,
    OrderSlotState.UNCERTAIN_CANCELLATION,
}


@dataclass(frozen=True)
class SoftExitEconomics:
    completed_turnover: Decimal
    completed_net: Decimal
    open_turnover: Decimal
    open_net: Decimal


def _finite_decimal(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (not positive or value > 0)
    )


def _live_order_violates_exit_limit(
    orders: Iterable[ManagedOrder],
    side: OrderSide,
    limit: Decimal,
) -> bool:
    for order in orders:
        if (
            order.side is not side
            or order.state
            not in {OrderSlotState.LIVE, OrderSlotState.PARTIALLY_FILLED}
            or order.submission_uncertain
            or not _finite_decimal(order.price, positive=True)
            or not _finite_decimal(order.remaining, positive=True)
        ):
            continue
        if side is OrderSide.SELL and order.price < limit:
            return True
        if side is OrderSide.BUY and order.price > limit:
            return True
    return False


def validate_market_snapshot(
    market: MarketSnapshot,
    *,
    now_monotonic: float | None = None,
    stale_after_seconds: int | None = None,
) -> None:
    if not market.bids or not market.asks:
        raise ValueError("order book must contain bids and asks")
    if not _finite_decimal(market.best_bid, positive=True):
        raise ValueError("best bid must be a finite positive Decimal")
    if not _finite_decimal(market.best_ask, positive=True):
        raise ValueError("best ask must be a finite positive Decimal")
    if market.best_bid >= market.best_ask:
        raise ValueError("order book must not be crossed")
    if now_monotonic is not None:
        if stale_after_seconds is None or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if (
            isinstance(now_monotonic, bool)
            or not isinstance(now_monotonic, (int, float))
            or not math.isfinite(now_monotonic)
            or not math.isfinite(market.received_monotonic)
        ):
            raise ValueError("monotonic timestamps must be finite")
        age = now_monotonic - market.received_monotonic
        if age < 0:
            raise ValueError("order book timestamp is in the future")
        if age > stale_after_seconds:
            raise ValueError("order book is stale")


def _external_best_for_side(
    best: Decimal,
    levels: tuple[object, ...],
    own_orders: tuple[ManagedOrder, ...],
    side: OrderSide,
) -> Decimal:
    own_remaining = sum(
        (
            order.remaining
            for order in own_orders
            if order.side is side
            and order.state in _OWN_QUOTE_STATES
            and not order.submission_uncertain
            and _finite_decimal(order.price, positive=True)
            and order.price == best
            and _finite_decimal(order.remaining)
            and order.remaining > 0
        ),
        _ZERO,
    )
    if own_remaining <= 0:
        return best

    top_size = sum(
        (
            level.size
            for level in levels
            if _finite_decimal(getattr(level, "price", None), positive=True)
            and level.price == best
            and _finite_decimal(getattr(level, "size", None), positive=True)
        ),
        _ZERO,
    )
    if top_size <= 0 or top_size > own_remaining:
        return best

    candidates = [
        level.price
        for level in levels
        if _finite_decimal(getattr(level, "price", None), positive=True)
        and _finite_decimal(getattr(level, "size", None), positive=True)
        and (
            level.price < best if side is OrderSide.BUY else level.price > best
        )
    ]
    if not candidates:
        return best
    return max(candidates) if side is OrderSide.BUY else min(candidates)


def calculate_external_bbo(
    market: MarketSnapshot,
    live_orders: Iterable[ManagedOrder] = (),
) -> tuple[Decimal, Decimal]:
    """Return a conservative BBO after subtracting identifiable own quotes."""
    validate_market_snapshot(market)
    orders = tuple(live_orders)
    bid = _external_best_for_side(
        market.best_bid, tuple(market.bids), orders, OrderSide.BUY
    )
    ask = _external_best_for_side(
        market.best_ask, tuple(market.asks), orders, OrderSide.SELL
    )
    if bid >= ask:
        raise ValueError("external order book must not be crossed")
    return bid, ask


class MarketMakerStrategy:
    def __init__(self, config: MarketMakerConfig):
        self.config = config
        self._trend_samples: deque[tuple[float, Decimal]] = deque()
        self._trend_direction = 0

    def calculate_quotes(
        self,
        market: MarketSnapshot,
        position: PositionSnapshot,
        metadata: MarketMetadata,
        risk: RiskDecision,
        live_orders: Iterable[ManagedOrder] = (),
        *,
        now_monotonic: float | None = None,
        soft_exit_economics: SoftExitEconomics | None = None,
    ) -> DesiredQuotes:
        if now_monotonic is None:
            return self._no_quotes(
                RuntimeState.PAUSED_DATA,
                "current monotonic time is required",
            )
        try:
            validate_market_snapshot(
                market,
                now_monotonic=now_monotonic,
                stale_after_seconds=self.config.stale_book_seconds,
            )
            self._validate_metadata(market, metadata)
            orders = tuple(live_orders)
            external_bid, external_ask = calculate_external_bbo(market, orders)
        except (AttributeError, TypeError, ValueError) as exc:
            return self._no_quotes(RuntimeState.PAUSED_DATA, str(exc))

        if (
            position.symbol != self.config.symbol
            or not _finite_decimal(position.signed_size)
        ):
            return self._no_quotes(
                RuntimeState.PAUSED_POSITION, "position snapshot is invalid"
            )
        hard_exit_rate = (
            self.config.maker_fee_rate
            + self.config.min_completed_net_turnover_bps / _TEN_THOUSAND
        )
        soft_exit_active = self._soft_exit_active(position.signed_size, risk)
        enforce_exit_limit = soft_exit_active or hard_exit_rate > 0
        if enforce_exit_limit and not -_ONE < hard_exit_rate < _ONE:
            return self._no_quotes(
                RuntimeState.PAUSED_ERROR,
                "fee-aware exit rate must be between -1 and 1",
            )
        if (
            position.signed_size != 0
            and enforce_exit_limit
            and not _finite_decimal(position.entry_price, positive=True)
        ):
            return self._no_quotes(
                RuntimeState.PAUSED_POSITION,
                "non-flat position entry price is invalid",
            )

        reference = (external_bid + external_ask) / _TWO
        raw_spread_bps = (
            (external_ask - external_bid) / reference * _TEN_THOUSAND
        )
        if raw_spread_bps > self.config.max_raw_spread_bps:
            return self._no_quotes(
                RuntimeState.PAUSED_MARKET,
                "external spread "
                f"{raw_spread_bps} bps exceeds "
                f"max_raw_spread_bps={self.config.max_raw_spread_bps}",
            )
        inventory_ratio = max(
            -_ONE,
            min(_ONE, position.signed_size / self.config.max_position),
        )
        skew = (
            inventory_ratio
            * Decimal(self.config.max_inventory_skew_ticks)
            * metadata.price_tick
        )
        reservation = reference - skew
        configured_half = (
            metadata.price_tick * Decimal(self.config.base_half_spread_ticks)
        )
        required_full_rate = (
            _TWO * self.config.maker_fee_rate
            + self.config.min_profit_buffer_bps / _TEN_THOUSAND
        )
        fee_floor_half = reference * required_full_rate / _TWO
        half_spread = ceil_to_step(
            max(configured_half, fee_floor_half), metadata.price_tick
        )

        runtime_state = getattr(risk, "runtime_state", RuntimeState.PAUSED_ERROR)
        reason = getattr(risk, "reason", "risk decision unavailable")
        if not getattr(risk, "safe", False):
            return DesiredQuotes(
                bid=None,
                ask=None,
                reference_price=reference,
                reservation_price=reservation,
                half_spread=half_spread,
                inventory_ratio=inventory_ratio,
                runtime_state=runtime_state,
                reason=reason,
            )

        block_bid, block_ask, trend_reason = self._trend_guard(
            reference,
            metadata.price_tick,
            position.signed_size,
            market.received_monotonic,
            risk,
        )

        bid_price = min(
            floor_to_step(reservation - half_spread, metadata.price_tick),
            floor_to_step(
                market.best_ask - metadata.price_tick, metadata.price_tick
            ),
        )
        ask_price = max(
            ceil_to_step(reservation + half_spread, metadata.price_tick),
            ceil_to_step(
                market.best_bid + metadata.price_tick, metadata.price_tick
            ),
        )
        cancel_unsafe_bid = False
        cancel_unsafe_ask = False
        quote_reason = reason
        if position.signed_size != 0 and enforce_exit_limit:
            entry_price = position.entry_price
            if position.signed_size > 0:
                hard_exit_limit = ceil_to_step(
                    entry_price
                    * (_ONE + hard_exit_rate)
                    / (_ONE - hard_exit_rate),
                    metadata.price_tick,
                )
                exit_limit = hard_exit_limit
                if soft_exit_active:
                    exit_limit, quote_reason = self._soft_exit_limit(
                        position,
                        metadata,
                        soft_exit_economics,
                        hard_exit_limit,
                    )
                ask_price = max(ask_price, exit_limit)
                cancel_unsafe_ask = _live_order_violates_exit_limit(
                    orders, OrderSide.SELL, exit_limit
                )
            else:
                hard_exit_limit = floor_to_step(
                    entry_price
                    * (_ONE - hard_exit_rate)
                    / (_ONE + hard_exit_rate),
                    metadata.price_tick,
                )
                exit_limit = hard_exit_limit
                if soft_exit_active:
                    exit_limit, quote_reason = self._soft_exit_limit(
                        position,
                        metadata,
                        soft_exit_economics,
                        hard_exit_limit,
                    )
                bid_price = min(bid_price, exit_limit)
                cancel_unsafe_bid = _live_order_violates_exit_limit(
                    orders, OrderSide.BUY, exit_limit
                )
            if cancel_unsafe_bid or cancel_unsafe_ask:
                quote_reason = (
                    f"{quote_reason}; existing quote violates fee-aware exit limit"
                )
        if trend_reason is not None:
            quote_reason = (
                trend_reason
                if quote_reason == reason
                else f"{quote_reason}; {trend_reason}"
            )
        if bid_price >= ask_price:
            return DesiredQuotes(
                bid=None,
                ask=None,
                reference_price=reference,
                reservation_price=reservation,
                half_spread=half_spread,
                inventory_ratio=inventory_ratio,
                runtime_state=runtime_state,
                reason="invalid post-only boundary",
            )

        bid = None
        if not cancel_unsafe_bid and not block_bid:
            bid = self._desired_order(
                OrderSide.BUY,
                bid_price,
                getattr(risk, "buy_amount", None),
                getattr(risk, "buy_reduce_only", False),
                metadata,
                reason,
            )
        ask = None
        if not cancel_unsafe_ask and not block_ask:
            ask = self._desired_order(
                OrderSide.SELL,
                ask_price,
                getattr(risk, "sell_amount", None),
                getattr(risk, "sell_reduce_only", False),
                metadata,
                reason,
            )
        return DesiredQuotes(
            bid=bid,
            ask=ask,
            reference_price=reference,
            reservation_price=reservation,
            half_spread=half_spread,
            inventory_ratio=inventory_ratio,
            runtime_state=runtime_state,
            reason=quote_reason,
        )

    def _soft_exit_limit(
        self,
        position: PositionSnapshot,
        metadata: MarketMetadata,
        economics: SoftExitEconomics | None,
        hard_exit_limit: Decimal,
    ) -> tuple[Decimal, str]:
        if not self._valid_soft_exit_economics(economics):
            return hard_exit_limit, "soft_exit_hard_fallback"

        if self.config.max_session_loss_for_maker_exit > 0:
            return self._session_loss_exit_limit(
                position,
                metadata,
                economics,
                hard_exit_limit,
            )
        if economics.completed_turnover <= 0:
            return hard_exit_limit, "soft_exit_hard_fallback"

        minimum_rate = (
            self.config.min_completed_net_turnover_bps / _TEN_THOUSAND
        )
        completed_surplus = max(
            _ZERO,
            economics.completed_net
            - minimum_rate * economics.completed_turnover,
        )
        reserve = min(
            completed_surplus,
            self.config.soft_exit_surplus_reserve_bps
            / _TEN_THOUSAND
            * economics.completed_turnover,
        )
        if completed_surplus - reserve <= 0:
            return hard_exit_limit, "soft_exit_no_surplus"

        amount = abs(position.signed_size)
        entry_price = position.entry_price
        fee_rate = self.config.maker_fee_rate
        soft_rate = (
            fee_rate
            + self.config.soft_exit_net_turnover_bps / _TEN_THOUSAND
        )
        if not -_ONE < soft_rate < _ONE:
            return hard_exit_limit, "soft_exit_hard_fallback"

        prior_turnover = (
            economics.completed_turnover + economics.open_turnover
        )
        prior_net = economics.completed_net + economics.open_net
        if position.signed_size > 0:
            denominator = amount * (_ONE - fee_rate - minimum_rate)
            if denominator <= 0:
                return hard_exit_limit, "soft_exit_hard_fallback"
            budget_limit = ceil_to_step(
                (
                    minimum_rate * prior_turnover
                    + reserve
                    - prior_net
                    + amount * entry_price
                )
                / denominator,
                metadata.price_tick,
            )
            soft_limit = ceil_to_step(
                entry_price * (_ONE + soft_rate) / (_ONE - soft_rate),
                metadata.price_tick,
            )
            exit_limit = max(soft_limit, budget_limit)
        else:
            denominator = amount * (_ONE + fee_rate + minimum_rate)
            if denominator <= 0:
                return hard_exit_limit, "soft_exit_hard_fallback"
            budget_limit = floor_to_step(
                (
                    prior_net
                    + amount * entry_price
                    - minimum_rate * prior_turnover
                    - reserve
                )
                / denominator,
                metadata.price_tick,
            )
            soft_limit = floor_to_step(
                entry_price * (_ONE - soft_rate) / (_ONE + soft_rate),
                metadata.price_tick,
            )
            exit_limit = min(soft_limit, budget_limit)

        if not _finite_decimal(exit_limit, positive=True):
            return hard_exit_limit, "soft_exit_hard_fallback"
        return exit_limit, "soft_exit_active"

    def _session_loss_exit_limit(
        self,
        position: PositionSnapshot,
        metadata: MarketMetadata,
        economics: SoftExitEconomics,
        hard_exit_limit: Decimal,
    ) -> tuple[Decimal, str]:
        amount = abs(position.signed_size)
        entry_price = position.entry_price
        fee_rate = self.config.maker_fee_rate
        prior_net = economics.completed_net + economics.open_net
        loss_budget = self.config.max_session_loss_for_maker_exit

        if position.signed_size > 0:
            denominator = amount * (_ONE - fee_rate)
            if denominator <= 0:
                return hard_exit_limit, "soft_exit_hard_fallback"
            exit_limit = ceil_to_step(
                (amount * entry_price - prior_net - loss_budget)
                / denominator,
                metadata.price_tick,
            )
        else:
            denominator = amount * (_ONE + fee_rate)
            if denominator <= 0:
                return hard_exit_limit, "soft_exit_hard_fallback"
            exit_limit = floor_to_step(
                (prior_net + amount * entry_price + loss_budget)
                / denominator,
                metadata.price_tick,
            )

        if not _finite_decimal(exit_limit, positive=True):
            return hard_exit_limit, "soft_exit_hard_fallback"
        return exit_limit, "session_loss_maker_exit_active"

    @staticmethod
    def _valid_soft_exit_economics(
        economics: SoftExitEconomics | None,
    ) -> bool:
        return (
            isinstance(economics, SoftExitEconomics)
            and _finite_decimal(economics.completed_turnover)
            and economics.completed_turnover >= 0
            and _finite_decimal(economics.completed_net)
            and _finite_decimal(economics.open_turnover, positive=True)
            and _finite_decimal(economics.open_net)
        )

    def _trend_guard(
        self,
        reference: Decimal,
        price_tick: Decimal,
        signed_size: Decimal,
        sample_monotonic: float,
        risk: RiskDecision,
    ) -> tuple[bool, bool, str | None]:
        window = self.config.trend_guard_window_seconds
        if window <= 0:
            return False, False, None

        if self._trend_samples:
            gap = sample_monotonic - self._trend_samples[-1][0]
            if gap < 0 or gap > window:
                self._trend_samples.clear()
                self._trend_direction = 0
        if (
            self._trend_samples
            and sample_monotonic == self._trend_samples[-1][0]
        ):
            self._trend_samples[-1] = (sample_monotonic, reference)
        else:
            self._trend_samples.append((sample_monotonic, reference))

        cutoff = sample_monotonic - window
        while (
            len(self._trend_samples) > 1
            and self._trend_samples[1][0] <= cutoff
        ):
            self._trend_samples.popleft()
        if sample_monotonic - self._trend_samples[0][0] < window:
            self._trend_direction = 0
            return False, False, None

        delta_ticks = (
            reference - self._trend_samples[0][1]
        ) / price_tick
        threshold = Decimal(self.config.trend_guard_threshold_ticks)
        if delta_ticks >= threshold:
            self._trend_direction = 1
        elif delta_ticks <= -threshold:
            self._trend_direction = -1
        elif (
            self._trend_direction > 0
            and delta_ticks <= threshold / _TWO
        ) or (
            self._trend_direction < 0
            and delta_ticks >= -threshold / _TWO
        ):
            self._trend_direction = 0

        if (
            self._trend_direction > 0
            and signed_size <= 0
            and getattr(risk, "sell_reduce_only", False) is not True
        ):
            return False, True, "trend_guard_rising_block_ask"
        if (
            self._trend_direction < 0
            and signed_size >= 0
            and getattr(risk, "buy_reduce_only", False) is not True
        ):
            return True, False, "trend_guard_falling_block_bid"
        return False, False, None

    def _soft_exit_active(
        self,
        signed_size: Decimal,
        risk: RiskDecision,
    ) -> bool:
        if (
            self.config.soft_exit_after_seconds <= 0
            or getattr(risk, "soft_exit_latched", False) is not True
            or getattr(risk, "safe", False) is not True
            or getattr(risk, "runtime_state", None)
            is not RuntimeState.RISK_REDUCTION
        ):
            return False
        if signed_size > 0:
            return getattr(risk, "sell_reduce_only", False) is True
        if signed_size < 0:
            return getattr(risk, "buy_reduce_only", False) is True
        return False

    def _validate_metadata(
        self, market: MarketSnapshot, metadata: MarketMetadata
    ) -> None:
        if market.symbol != self.config.symbol or metadata.symbol != market.symbol:
            raise ValueError("market symbols do not match config")
        if not _finite_decimal(metadata.price_tick, positive=True):
            raise ValueError("price_tick must be a finite positive Decimal")
        if not _finite_decimal(metadata.quantity_step, positive=True):
            raise ValueError("quantity_step must be a finite positive Decimal")
        if not _finite_decimal(metadata.min_base_amount) or metadata.min_base_amount < 0:
            raise ValueError("min_base_amount must be a finite non-negative Decimal")
        if not _finite_decimal(metadata.min_quote_amount) or metadata.min_quote_amount < 0:
            raise ValueError("min_quote_amount must be a finite non-negative Decimal")

    def _desired_order(
        self,
        side: OrderSide,
        price: Decimal,
        amount: object,
        reduce_only: object,
        metadata: MarketMetadata,
        reason: str,
    ) -> DesiredOrder | None:
        if (
            not _finite_decimal(price, positive=True)
            or not _finite_decimal(amount, positive=True)
            or type(reduce_only) is not bool
            or amount < metadata.min_base_amount
            or not is_step_aligned(amount, metadata.quantity_step)
            or amount * price < metadata.min_quote_amount
        ):
            return None
        return DesiredOrder(
            side=side,
            price=price,
            amount=amount,
            reduce_only=reduce_only,
            reason=reason,
        )

    @staticmethod
    def _no_quotes(state: RuntimeState, reason: str) -> DesiredQuotes:
        return DesiredQuotes(
            bid=None,
            ask=None,
            reference_price=_ZERO,
            reservation_price=_ZERO,
            half_spread=_ZERO,
            inventory_ratio=_ZERO,
            runtime_state=state,
            reason=reason,
        )
