from __future__ import annotations

import math
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


def _finite_decimal(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (not positive or value > 0)
    )


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

    def calculate_quotes(
        self,
        market: MarketSnapshot,
        position: PositionSnapshot,
        metadata: MarketMetadata,
        risk: RiskDecision,
        live_orders: Iterable[ManagedOrder] = (),
        *,
        now_monotonic: float | None = None,
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
            external_bid, external_ask = calculate_external_bbo(
                market, live_orders
            )
        except (AttributeError, TypeError, ValueError) as exc:
            return self._no_quotes(RuntimeState.PAUSED_DATA, str(exc))

        if (
            position.symbol != self.config.symbol
            or not _finite_decimal(position.signed_size)
        ):
            return self._no_quotes(
                RuntimeState.PAUSED_POSITION, "position snapshot is invalid"
            )

        reference = (external_bid + external_ask) / _TWO
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

        bid = self._desired_order(
            OrderSide.BUY,
            bid_price,
            getattr(risk, "buy_amount", None),
            getattr(risk, "buy_reduce_only", False),
            metadata,
            reason,
        )
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
            reason=reason,
        )

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
