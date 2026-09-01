from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Callable, Iterable

from ...adapters.exchanges.models import OrderBookLevel, OrderSide
from .models import ManagedOrder, MarketSnapshot, OrderSlotState


_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_MOMENTUM_HORIZONS = (1, 5, 15, 60)
_IDENTIFIABLE_OWN_STATES = {
    OrderSlotState.LIVE,
    OrderSlotState.PARTIALLY_FILLED,
    OrderSlotState.CANCELING,
}


class FeatureHealth(Enum):
    WARMING = "warming"
    READY = "ready"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True)
class ExternalBookView:
    valid: bool
    reason: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    external_best_bid: Decimal | None
    external_best_ask: Decimal | None


@dataclass(frozen=True)
class MarketFeatureSnapshot:
    health: FeatureHealth
    reason: str
    received_monotonic: float
    sample_count: int

    external_best_bid: Decimal | None
    external_best_ask: Decimal | None
    mid: Decimal | None
    spread_ticks: Decimal | None

    return_1s_ticks: Decimal | None
    return_5s_ticks: Decimal | None
    return_15s_ticks: Decimal | None
    return_60s_ticks: Decimal | None

    rms_1s_move_15s_ticks: Decimal | None
    rms_1s_move_60s_ticks: Decimal | None

    microprice: Decimal | None
    microprice_shift_ticks: Decimal | None
    depth_imbalance: Decimal | None

    def to_dict(self) -> dict[str, object]:
        values = vars(self).copy()
        values["health"] = self.health.value
        return values


def _finite_decimal(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (not positive or value > _ZERO)
    )


def _valid_timestamp(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _invalid_book(reason: str) -> ExternalBookView:
    return ExternalBookView(False, reason, (), (), None, None)


def _aggregate_levels(
    levels: tuple[OrderBookLevel, ...],
    *,
    descending: bool,
) -> list[tuple[Decimal, Decimal]] | None:
    totals: dict[Decimal, Decimal] = {}
    for level in levels:
        price = getattr(level, "price", None)
        size = getattr(level, "size", None)
        if not _finite_decimal(price, positive=True) or not _finite_decimal(
            size, positive=True
        ):
            return None
        totals[price] = totals.get(price, _ZERO) + size
    return sorted(totals.items(), key=lambda item: item[0], reverse=descending)


def build_external_book_view(
    market: MarketSnapshot,
    live_orders: Iterable[ManagedOrder],
    depth_levels: int,
) -> ExternalBookView:
    """Build a detached top-N book after subtracting identifiable own size."""
    if type(depth_levels) is not int or depth_levels <= 0:
        raise ValueError("depth_levels must be a positive integer")
    if not market.bids or not market.asks:
        return _invalid_book("order book must contain bids and asks")
    if not _finite_decimal(market.best_bid, positive=True) or not _finite_decimal(
        market.best_ask, positive=True
    ):
        return _invalid_book("best prices must be finite positive Decimals")
    if market.best_bid >= market.best_ask:
        return _invalid_book("order book must not be crossed")

    bids = _aggregate_levels(tuple(market.bids), descending=True)
    asks = _aggregate_levels(tuple(market.asks), descending=False)
    if not bids or not asks:
        return _invalid_book("order book levels must be finite and positive")
    if bids[0][0] != market.best_bid or asks[0][0] != market.best_ask:
        return _invalid_book("best prices must match the top book levels")

    own_sizes: dict[tuple[OrderSide, Decimal], Decimal] = {}
    for order in live_orders:
        if (
            order.state not in _IDENTIFIABLE_OWN_STATES
            or order.submission_uncertain
            or order.cancellation_uncertain
            or not _finite_decimal(order.price, positive=True)
            or not _finite_decimal(order.remaining, positive=True)
        ):
            continue
        key = (order.side, order.price)
        own_sizes[key] = own_sizes.get(key, _ZERO) + order.remaining

    def subtract(
        levels: list[tuple[Decimal, Decimal]], side: OrderSide
    ) -> tuple[OrderBookLevel, ...]:
        external = []
        for price, size in levels:
            remaining = max(_ZERO, size - own_sizes.get((side, price), _ZERO))
            if remaining > _ZERO:
                external.append(OrderBookLevel(price, remaining))
            if len(external) == depth_levels:
                break
        return tuple(external)

    external_bids = subtract(bids, OrderSide.BUY)
    external_asks = subtract(asks, OrderSide.SELL)
    if not external_bids or not external_asks:
        return _invalid_book("external book must contain bids and asks")
    external_best_bid = external_bids[0].price
    external_best_ask = external_asks[0].price
    if external_best_bid >= external_best_ask:
        return _invalid_book("external order book must not be crossed")
    return ExternalBookView(
        True,
        "valid",
        external_bids,
        external_asks,
        external_best_bid,
        external_best_ask,
    )


class MarketFeatureStore:
    """Bounded, synchronous feature history for entry quote decisions."""

    def __init__(
        self,
        *,
        price_tick: Decimal,
        depth_levels: int,
        feature_window_seconds: int,
        reset_gap_seconds: int,
        warmup_seconds: int,
        min_samples: int,
        stale_after_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        max_samples: int = 4096,
    ) -> None:
        if not _finite_decimal(price_tick, positive=True):
            raise ValueError("price_tick must be a finite positive Decimal")
        for name, value in (
            ("depth_levels", depth_levels),
            ("feature_window_seconds", feature_window_seconds),
            ("reset_gap_seconds", reset_gap_seconds),
            ("warmup_seconds", warmup_seconds),
            ("min_samples", min_samples),
            ("stale_after_seconds", stale_after_seconds),
            ("max_samples", max_samples),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if feature_window_seconds < max(_MOMENTUM_HORIZONS):
            raise ValueError("feature_window_seconds must cover the 60s horizon")
        if max_samples < min_samples:
            raise ValueError("max_samples cannot be below min_samples")
        if not callable(clock):
            raise ValueError("clock must be callable")

        self.price_tick = price_tick
        self.depth_levels = depth_levels
        self.feature_window_seconds = feature_window_seconds
        self.reset_gap_seconds = reset_gap_seconds
        self.warmup_seconds = warmup_seconds
        self.min_samples = min_samples
        self.stale_after_seconds = stale_after_seconds
        self.max_samples = max_samples
        self._clock = clock
        self._history: deque[tuple[float, Decimal]] = deque(maxlen=max_samples)
        self._warmup_started_monotonic: float | None = None
        self._latest = self._empty_snapshot()

    @property
    def sample_count(self) -> int:
        return len(self._history)

    def update(
        self,
        market: MarketSnapshot,
        live_orders: Iterable[ManagedOrder] = (),
        *,
        now_monotonic: float | None = None,
    ) -> MarketFeatureSnapshot:
        now = self._resolve_now(now_monotonic)
        received = market.received_monotonic
        if not _valid_timestamp(received):
            return self._set_invalid(now, "market timestamp must be finite")
        age = now - received
        if age < 0:
            return self._set_invalid(received, "market timestamp is in the future")

        book = build_external_book_view(market, live_orders, self.depth_levels)
        if not book.valid:
            return self._set_invalid(received, book.reason)
        if age > self.stale_after_seconds:
            self._latest = self._features(book, received, FeatureHealth.STALE, "stale")
            return self._latest

        assert book.external_best_bid is not None
        assert book.external_best_ask is not None
        mid = (book.external_best_bid + book.external_best_ask) / _TWO
        if self._history:
            previous_timestamp = self._history[-1][0]
            if (
                received < previous_timestamp
                or received - previous_timestamp > self.reset_gap_seconds
            ):
                self._history.clear()
                self._warmup_started_monotonic = None

        if self._history and received == self._history[-1][0]:
            self._history[-1] = (received, mid)
        else:
            self._history.append((received, mid))
        if self._warmup_started_monotonic is None:
            self._warmup_started_monotonic = received
        self._prune(received)

        self._latest = self._features(book, received)
        return self._latest

    def snapshot(
        self, *, now_monotonic: float | None = None
    ) -> MarketFeatureSnapshot:
        now = self._resolve_now(now_monotonic)
        if not self._history or self._latest.health is FeatureHealth.INVALID:
            return self._latest
        age = now - self._latest.received_monotonic
        if age < 0:
            return replace(
                self._latest,
                health=FeatureHealth.INVALID,
                reason="market timestamp is in the future",
            )
        if age > self.stale_after_seconds:
            return replace(
                self._latest,
                health=FeatureHealth.STALE,
                reason="market snapshot is stale",
            )
        return self._latest

    def _features(
        self,
        book: ExternalBookView,
        received: float,
        forced_health: FeatureHealth | None = None,
        forced_reason: str | None = None,
    ) -> MarketFeatureSnapshot:
        bid = book.external_best_bid
        ask = book.external_best_ask
        assert bid is not None and ask is not None
        mid = (bid + ask) / _TWO
        bid_size = book.bids[0].size
        ask_size = book.asks[0].size
        microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
        bid_depth = sum((level.size for level in book.bids), _ZERO)
        ask_depth = sum((level.size for level in book.asks), _ZERO)
        depth_total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / depth_total
        imbalance = min(_ONE, max(-_ONE, imbalance))

        returns = {
            horizon: self._return_ticks(received, mid, horizon)
            for horizon in _MOMENTUM_HORIZONS
        }
        rms_15 = self._rms_ticks(received, 15)
        rms_60 = self._rms_ticks(received, 60)

        if forced_health is not None:
            health = forced_health
            reason = (
                "market snapshot is stale"
                if forced_reason == "stale"
                else forced_reason or forced_health.value
            )
        elif len(self._history) < self.min_samples:
            health = FeatureHealth.WARMING
            reason = "minimum sample count not reached"
        elif (
            self._warmup_started_monotonic is None
            or received - self._warmup_started_monotonic < self.warmup_seconds
        ):
            health = FeatureHealth.WARMING
            reason = "warmup interval not reached"
        elif returns[5] is None or returns[15] is None or rms_15 is None:
            health = FeatureHealth.WARMING
            reason = "required momentum or volatility is unavailable"
        else:
            health = FeatureHealth.READY
            reason = "ready"

        return MarketFeatureSnapshot(
            health=health,
            reason=reason,
            received_monotonic=received,
            sample_count=len(self._history),
            external_best_bid=bid,
            external_best_ask=ask,
            mid=mid,
            spread_ticks=(ask - bid) / self.price_tick,
            return_1s_ticks=returns[1],
            return_5s_ticks=returns[5],
            return_15s_ticks=returns[15],
            return_60s_ticks=returns[60],
            rms_1s_move_15s_ticks=rms_15,
            rms_1s_move_60s_ticks=rms_60,
            microprice=microprice,
            microprice_shift_ticks=(microprice - mid) / self.price_tick,
            depth_imbalance=imbalance,
        )

    def _return_ticks(
        self, received: float, current_mid: Decimal, horizon: int
    ) -> Decimal | None:
        cutoff = received - horizon
        for timestamp, mid in reversed(self._history):
            if timestamp <= cutoff:
                return (current_mid - mid) / self.price_tick
        return None

    def _rms_ticks(self, received: float, window_seconds: int) -> Decimal | None:
        points = tuple(self._history)
        cutoff = received - window_seconds
        prior_index = -1
        squared_moves: list[Decimal] = []
        for end_index, (end_timestamp, end_mid) in enumerate(points):
            target = end_timestamp - 1
            while (
                prior_index + 1 < end_index
                and points[prior_index + 1][0] <= target
            ):
                prior_index += 1
            if end_timestamp > cutoff and prior_index >= 0:
                move = (end_mid - points[prior_index][1]) / self.price_tick
                squared_moves.append(move * move)
        if not squared_moves:
            return None
        mean_square = sum(squared_moves, _ZERO) / Decimal(len(squared_moves))
        result = mean_square.sqrt()
        return result if result.is_finite() else None

    def _prune(self, received: float) -> None:
        cutoff = received - self.feature_window_seconds
        while len(self._history) > 1 and self._history[1][0] <= cutoff:
            self._history.popleft()

    def _resolve_now(self, now_monotonic: float | None) -> float:
        now = self._clock() if now_monotonic is None else now_monotonic
        if not _valid_timestamp(now):
            raise ValueError("now_monotonic must be finite")
        return now

    def _set_invalid(self, received: float, reason: str) -> MarketFeatureSnapshot:
        self._latest = replace(
            self._empty_snapshot(),
            health=FeatureHealth.INVALID,
            reason=reason,
            received_monotonic=received,
            sample_count=len(self._history),
        )
        return self._latest

    @staticmethod
    def _empty_snapshot() -> MarketFeatureSnapshot:
        return MarketFeatureSnapshot(
            health=FeatureHealth.WARMING,
            reason="no market samples",
            received_monotonic=0.0,
            sample_count=0,
            external_best_bid=None,
            external_best_ask=None,
            mid=None,
            spread_ticks=None,
            return_1s_ticks=None,
            return_5s_ticks=None,
            return_15s_ticks=None,
            return_60s_ticks=None,
            rms_1s_move_15s_ticks=None,
            rms_1s_move_60s_ticks=None,
            microprice=None,
            microprice_shift_ticks=None,
            depth_imbalance=None,
        )
