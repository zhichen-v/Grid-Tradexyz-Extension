"""External BBO and one sampled Decimal EWMA; no exchange or strategy imports."""

from decimal import Decimal, DecimalException

from .domain import MarketStateSnapshot, ZERO, _boolean, _decimal, _symbol, _time


MAX_VOLATILITY_BUFFER_BPS = Decimal("5")
_SAMPLE_SECONDS = Decimal("1")
_HALF_LIFE_SECONDS = Decimal("5")
_BPS = Decimal("10000")
Levels = tuple[tuple[Decimal, Decimal], ...]


class MarketState:
    """Accept a coherent bounded-depth book plus its exactly known own sizes.

    Own prices beyond the supplied depth are ignored; missing prices inside or
    better than that depth reject the update. A rejected update invalidates the
    published snapshot. Freshness against the current clock belongs to the caller.
    """

    def __init__(self, symbol: str, *, tick_size: Decimal, size_step: Decimal,
                 min_order_size: Decimal):
        _symbol(symbol)
        for value in (tick_size, size_step, min_order_size):
            _decimal(value, positive=True)
        self._symbol = symbol
        self._tick = tick_size
        self._step = size_step
        self._minimum = min_order_size
        self._snapshot: MarketStateSnapshot | None = None
        self._last_update: float | None = None
        self._sample_time: Decimal | None = None
        self._sample_mid: Decimal | None = None
        self._ewma = ZERO

    def _levels(self, levels: Levels, *, own: bool = False) -> dict[Decimal, Decimal]:
        if type(levels) is not tuple:
            raise ValueError("book levels must be immutable tuples")
        result = {}
        for level in levels:
            if type(level) is not tuple or len(level) != 2:
                raise ValueError("book level must contain price and size")
            price, size = level
            _decimal(price, positive=True)
            _decimal(size)
            if price % self._tick or size < ZERO:
                raise ValueError("book price must align to tick and size be nonnegative")
            if not own and price in result:
                raise ValueError("book prices must be unique")
            result[price] = result.get(price, ZERO) + size
        return result

    @staticmethod
    def _external(book: dict[Decimal, Decimal], own: dict[Decimal, Decimal],
                  *, bid: bool) -> tuple[Decimal, Decimal]:
        if not book:
            raise ValueError("empty book side")
        worst = min(book) if bid else max(book)
        for price, size in own.items():
            if size == ZERO:
                continue
            if price not in book:
                if (bid and price < worst) or (not bid and price > worst):
                    continue
                raise ValueError("own order absent from supplied book range")
            book[price] -= size
            if book[price] < ZERO:
                raise ValueError("own size exceeds visible book size")
        positive = {price: size for price, size in book.items() if size > ZERO}
        if not positive:
            raise ValueError("empty external book side")
        best = max(positive) if bid else min(positive)
        return best, positive[best]

    def update(self, *, bids: Levels, asks: Levels, own_bids: Levels,
               own_asks: Levels, observed_monotonic: float,
               trusted: bool) -> MarketStateSnapshot:
        self._snapshot = None
        _time(observed_monotonic)
        _boolean(trusted)
        if not trusted:
            raise ValueError("untrusted book")
        if self._last_update is not None and observed_monotonic <= self._last_update:
            raise ValueError("book time must strictly increase")
        try:
            bid_book, ask_book = self._levels(bids), self._levels(asks)
            visible_bids = [price for price, size in bid_book.items() if size > ZERO]
            visible_asks = [price for price, size in ask_book.items() if size > ZERO]
            if not visible_bids or not visible_asks or max(visible_bids) >= min(visible_asks):
                raise ValueError("empty, locked or crossed source book")
            bid, bid_size = self._external(bid_book, self._levels(own_bids, own=True), bid=True)
            ask, ask_size = self._external(ask_book, self._levels(own_asks, own=True), bid=False)
            mid = bid / 2 + ask / 2
            try:
                weight = bid_size / (bid_size + ask_size)
                microprice = bid + (ask - bid) * weight
                if not microprice.is_finite() or not bid <= microprice <= ask:
                    microprice = mid
            except DecimalException:
                microprice = mid
            now = Decimal(str(observed_monotonic))
            sample_time, sample_mid, ewma = self._sample_time, self._sample_mid, self._ewma
            if sample_time is None:
                sample_time, sample_mid = now, mid
            elif now - sample_time >= _SAMPLE_SECONDS:
                # ponytail: one sampled absolute mid move, no interval resampling;
                # calibrate this fixed profile against replay before live economics.
                elapsed = now - sample_time
                alpha = 1 - Decimal("0.5") ** (elapsed / _HALF_LIFE_SECONDS)
                move_bps = abs(mid / sample_mid - 1) * _BPS
                ewma += alpha * (move_bps - ewma)
                sample_time, sample_mid = now, mid
            snapshot = MarketStateSnapshot(
                self._symbol, observed_monotonic, bid, ask, self._tick,
                self._step, self._minimum, True, microprice, ewma,
            )
        except DecimalException as exc:
            raise ValueError("unusable book arithmetic") from exc
        self._sample_time, self._sample_mid, self._ewma = sample_time, sample_mid, ewma
        self._last_update, self._snapshot = observed_monotonic, snapshot
        return snapshot

    def snapshot(self) -> MarketStateSnapshot:
        if self._snapshot is None:
            raise ValueError("no trusted market snapshot available")
        return self._snapshot
