"""Small immutable V2 contracts. No adapters, credentials or legacy strategy state."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from math import isfinite
import re


ZERO = Decimal("0")


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ExecutionHealth(str, Enum):
    HEALTHY = "healthy"
    PAUSED_DATA = "paused_data"
    PAUSED_ORDER_STATE = "paused_order_state"
    HALTED = "halted"


class StrategyState(str, Enum):
    QUOTING = "quoting"
    SKEWED = "skewed"
    REDUCE_ONLY = "reduce_only"
    FLATTENING = "flattening"
    COOLDOWN = "cooldown"
    SESSION_COMPLETE = "session_complete"


class ExecutionStatus(str, Enum):
    SIMULATED = "simulated"
    BLOCKED = "blocked"


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9_:/-]{1,32}", value):
        raise ValueError("invalid symbol")


def _decimal(value, *, positive=False):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("financial values must be finite Decimal")
    if positive and value <= ZERO:
        raise ValueError("financial value must be positive")


def _time(value):
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise ValueError("invalid monotonic timestamp")


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("count must be a nonnegative integer")


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("expected boolean")


@dataclass(frozen=True, slots=True)
class MarketStateSnapshot:
    symbol: str
    observed_monotonic: float
    external_bid: Decimal
    external_ask: Decimal
    tick_size: Decimal
    size_step: Decimal
    min_order_size: Decimal
    trusted: bool
    microprice: Decimal | None = None
    ewma_move_bps: Decimal = ZERO

    def __post_init__(self):
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        _boolean(self.trusted)
        for value in (self.external_bid, self.external_ask, self.tick_size,
                      self.size_step, self.min_order_size):
            _decimal(value, positive=True)
        if self.external_bid >= self.external_ask:
            raise ValueError("external BBO must not be locked or crossed")
        if self.external_bid % self.tick_size or self.external_ask % self.tick_size:
            raise ValueError("external BBO must align to price tick")
        if self.microprice is not None:
            _decimal(self.microprice)
        _decimal(self.ewma_move_bps)
        if self.ewma_move_bps < ZERO:
            raise ValueError("volatility must be nonnegative")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    symbol: str
    observed_monotonic: float
    position: Decimal
    equity: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    open_order_count: int
    authenticated: bool
    entry_price: Decimal | None = None
    unrealized_pnl: Decimal = ZERO

    def __post_init__(self):
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        _count(self.open_order_count)
        _boolean(self.authenticated)
        for value in (self.position, self.equity, self.maker_fee_rate,
                      self.taker_fee_rate, self.unrealized_pnl):
            _decimal(value)
        if not (ZERO <= self.maker_fee_rate < 1 and ZERO <= self.taker_fee_rate < 1):
            raise ValueError("nonnegative fee rates below one required; rebates unsupported")
        if self.entry_price is not None:
            _decimal(self.entry_price, positive=True)
        if self.position != ZERO and self.entry_price is None:
            raise ValueError("nonflat account requires entry price")


@dataclass(frozen=True, slots=True)
class QuoteIntent:
    side: Side
    price: Decimal
    size: Decimal
    reduce_only: bool = False

    def __post_init__(self):
        if not isinstance(self.side, Side):
            raise ValueError("typed side required")
        _decimal(self.price, positive=True)
        _decimal(self.size, positive=True)
        _boolean(self.reduce_only)

    @property
    def time_in_force(self) -> str:
        return "POST_ONLY"


@dataclass(frozen=True, slots=True)
class QuotePlan:
    symbol: str
    quotes: tuple[QuoteIntent, ...] = ()

    def __post_init__(self):
        _symbol(self.symbol)
        if type(self.quotes) is not tuple or any(not isinstance(q, QuoteIntent) for q in self.quotes):
            raise ValueError("quotes must be an immutable tuple of QuoteIntent")
        by_side = {q.side: q for q in self.quotes}
        if len(by_side) != len(self.quotes):
            raise ValueError("at most one quote per side")
        if len(by_side) == 2 and by_side[Side.BUY].price >= by_side[Side.SELL].price:
            raise ValueError("own bid and ask must not cross")


@dataclass(frozen=True, slots=True)
class FlattenIntent:
    symbol: str
    side: Side
    size: Decimal
    limit_price: Decimal
    deadline_monotonic: float

    def __post_init__(self):
        _symbol(self.symbol)
        if not isinstance(self.side, Side):
            raise ValueError("typed side required")
        _decimal(self.size, positive=True)
        _decimal(self.limit_price, positive=True)
        _time(self.deadline_monotonic)

    @property
    def reduce_only(self) -> bool:
        return True

    @property
    def time_in_force(self) -> str:
        return "IOC"


@dataclass(frozen=True, slots=True)
class InventoryDecision:
    state: StrategyState
    flatten: FlattenIntent | None = None

    def __post_init__(self):
        if not isinstance(self.state, StrategyState):
            raise ValueError("typed strategy state required")
        if (self.state == StrategyState.FLATTENING) != (self.flatten is not None):
            raise ValueError("flatten intent required exactly when flattening")
        if self.flatten is not None and not isinstance(self.flatten, FlattenIntent):
            raise ValueError("typed flatten intent required")


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    health: ExecutionHealth
    managed_order_count: int
    simulated: bool

    def __post_init__(self):
        if not isinstance(self.health, ExecutionHealth):
            raise ValueError("typed execution health required")
        _count(self.managed_order_count)
        _boolean(self.simulated)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    snapshot: ExecutionSnapshot
    submitted_count: int = 0
    cancelled_count: int = 0

    def __post_init__(self):
        if not isinstance(self.status, ExecutionStatus) or not isinstance(self.snapshot, ExecutionSnapshot):
            raise ValueError("typed execution result required")
        _count(self.submitted_count)
        _count(self.cancelled_count)
        if self.status == ExecutionStatus.SIMULATED and not self.snapshot.simulated:
            raise ValueError("simulated result requires simulated execution")


@dataclass(frozen=True, slots=True)
class SessionReport:
    """Boundary-only skeleton; economics remains unavailable until Phase 3 ledger."""

    symbol: str
    complete: bool
    final_position: Decimal
    final_open_order_count: int
    all_in_net_pnl: Decimal | None = None
    all_in_net_cost_bps: Decimal | None = None

    def __post_init__(self):
        _symbol(self.symbol)
        _boolean(self.complete)
        _decimal(self.final_position)
        _count(self.final_open_order_count)
        for value in (self.all_in_net_pnl, self.all_in_net_cost_bps):
            if value is not None:
                _decimal(value)
        if self.complete and (self.final_position != ZERO or self.final_open_order_count != 0):
            raise ValueError("nonflat session cannot be complete")
