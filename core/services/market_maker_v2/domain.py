"""Small immutable V2 contracts. No adapters, credentials or legacy strategy state."""

from dataclasses import dataclass, fields
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
    CONFIRMED = "confirmed"


class ExitStatus(str, Enum):
    FLAT = "flat"
    BLOCKED = "blocked"
    DEADLINE = "deadline"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class LiquidityRole(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


class CashflowKind(str, Enum):
    FUNDING = "funding"
    TRANSFER = "transfer"


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


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid event/order identifier")


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
    open_order_ids: tuple[str, ...] | None = None
    inputs_observed_monotonic: float | None = None

    def __post_init__(self):
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        if self.inputs_observed_monotonic is not None:
            _time(self.inputs_observed_monotonic)
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
        if self.open_order_ids is not None:
            if (type(self.open_order_ids) is not tuple
                    or len(set(self.open_order_ids)) != len(self.open_order_ids)
                    or len(self.open_order_ids) != self.open_order_count):
                raise ValueError("authenticated order identities must match count")
            for order_id in self.open_order_ids:
                _identifier(order_id)

    def fresh(self, now):
        inputs = self.observed_monotonic if self.inputs_observed_monotonic is None else self.inputs_observed_monotonic
        return 0 <= now - self.observed_monotonic <= 10 and 0 <= now - inputs <= 10


@dataclass(frozen=True, slots=True)
class SessionRunResult:
    dry_run: bool
    completed: bool
    report: "SessionReport | None"
    final_account: AccountSnapshot | None
    failure: str | None


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
    """Governor state and total desired side capacities, not per-new-order room.

    Zero defaults grant no quote permission. The governor/executor must account
    for working orders, possible fills and session loss reserve before granting
    capacity; a policy proposal is not execution or reconciliation authority.
    """

    state: StrategyState
    flatten: FlattenIntent | None = None
    buy_capacity: Decimal = ZERO
    sell_capacity: Decimal = ZERO

    def __post_init__(self):
        if not isinstance(self.state, StrategyState):
            raise ValueError("typed strategy state required")
        # An exit starts with cancel/proof even if currently flat; only a fresh
        # nonflat account can supply an IOC intent after that boundary.
        if self.state != StrategyState.FLATTENING and self.flatten is not None:
            raise ValueError("flatten intent only allowed when flattening")
        if self.flatten is not None and not isinstance(self.flatten, FlattenIntent):
            raise ValueError("typed flatten intent required")
        for capacity in (self.buy_capacity, self.sell_capacity):
            _decimal(capacity)
            if capacity < ZERO:
                raise ValueError("quote capacity must be nonnegative")
        if self.state in {StrategyState.FLATTENING, StrategyState.COOLDOWN,
                          StrategyState.SESSION_COMPLETE} and (self.buy_capacity or self.sell_capacity):
            raise ValueError("inactive strategy cannot grant quote capacity")


@dataclass(frozen=True, slots=True)
class QuoteAuthorization:
    account: AccountSnapshot
    market: MarketStateSnapshot
    decision: InventoryDecision
    plan: QuotePlan

    def __post_init__(self):
        for value, expected in ((self.account, AccountSnapshot), (self.market, MarketStateSnapshot),
                                (self.decision, InventoryDecision), (self.plan, QuotePlan)):
            if type(value) is not expected:
                raise ValueError("typed quote authorization required")
        if len({self.account.symbol, self.market.symbol, self.plan.symbol}) != 1:
            raise ValueError("quote authorization symbol mismatch")


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    order_id: str
    side: Side
    remaining_size: Decimal
    price: Decimal
    reduce_only: bool = False

    def __post_init__(self):
        _identifier(self.order_id)
        if not isinstance(self.side, Side):
            raise ValueError("typed working-order side required")
        _decimal(self.remaining_size, positive=True)
        _decimal(self.price, positive=True)
        _boolean(self.reduce_only)


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    health: ExecutionHealth
    managed_order_count: int
    simulated: bool
    symbol: str | None = None
    observed_monotonic: float | None = None
    orders: tuple[WorkingOrder, ...] | None = None

    def __post_init__(self):
        if not isinstance(self.health, ExecutionHealth):
            raise ValueError("typed execution health required")
        _count(self.managed_order_count)
        _boolean(self.simulated)
        if self.symbol is not None or self.observed_monotonic is not None or self.orders is not None:
            _symbol(self.symbol)
            _time(self.observed_monotonic)
            if type(self.orders) is not tuple or any(type(o) is not WorkingOrder for o in self.orders):
                raise ValueError("complete typed working-order exposure required")
            if (len(self.orders) != self.managed_order_count
                    or len({o.order_id for o in self.orders}) != len(self.orders)):
                raise ValueError("working-order count/identity mismatch")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    snapshot: ExecutionSnapshot
    submitted_count: int = 0
    cancelled_count: int = 0
    account_snapshot: AccountSnapshot | None = None
    actual_plan: QuotePlan | None = None

    def __post_init__(self):
        if not isinstance(self.status, ExecutionStatus) or not isinstance(self.snapshot, ExecutionSnapshot):
            raise ValueError("typed execution result required")
        _count(self.submitted_count)
        _count(self.cancelled_count)
        if self.status == ExecutionStatus.SIMULATED and not self.snapshot.simulated:
            raise ValueError("simulated result requires simulated execution")
        if self.account_snapshot is not None and type(self.account_snapshot) is not AccountSnapshot:
            raise ValueError("typed result account required")
        if self.status == ExecutionStatus.CONFIRMED and self.snapshot.simulated:
            raise ValueError("simulation cannot assert confirmed exchange execution")
        if self.actual_plan is not None and type(self.actual_plan) is not QuotePlan:
            raise ValueError("typed actual quote plan required")


@dataclass(frozen=True, slots=True)
class BoundedExitReport:
    flatten_id: str
    symbol: str
    observed_monotonic: float
    status: ExitStatus
    attempts: int
    final_result: ExecutionResult | None = None

    def __post_init__(self):
        _identifier(self.flatten_id)
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        _count(self.attempts)
        if not isinstance(self.status, ExitStatus):
            raise ValueError("typed exit status required")
        if self.final_result is not None and type(self.final_result) is not ExecutionResult:
            raise ValueError("typed exit execution result required")
        if self.complete:
            result = self.final_result
            account = result.account_snapshot if result else None
            if (result is None or result.status != ExecutionStatus.CONFIRMED
                    or result.snapshot.health != ExecutionHealth.HEALTHY
                    or result.snapshot.managed_order_count != 0 or account is None
                    or not account.authenticated or account.symbol != self.symbol
                    or account.position != ZERO or account.open_order_count != 0):
                raise ValueError("flat exit requires confirmed execution and authenticated flat")

    @property
    def complete(self) -> bool:
        return self.status == ExitStatus.FLAT


@dataclass(frozen=True, slots=True)
class SessionReport:
    """Session-wide report; complete is a reconciled boundary, not a strategy GO."""

    symbol: str
    complete: bool
    final_position: Decimal
    final_open_order_count: int | None
    all_in_net_pnl: Decimal | None = None
    all_in_net_cost_bps: Decimal | None = None
    ledger_position: Decimal = ZERO
    final_authenticated: bool = False
    equity_reconciliation_difference: Decimal | None = None
    failed: bool = False
    telemetry_errors: int = 0
    maker_buy_turnover: Decimal = ZERO
    maker_sell_turnover: Decimal = ZERO
    maker_turnover_total: Decimal = ZERO
    taker_flatten_turnover: Decimal = ZERO
    maker_fill_count: int = 0
    taker_fill_count: int = 0
    realized_gross_pnl: Decimal = ZERO
    maker_fee: Decimal = ZERO
    taker_fee: Decimal = ZERO
    funding: Decimal = ZERO
    external_transfers: Decimal = ZERO
    realized_net_pnl: Decimal = ZERO
    marked_net_pnl: Decimal | None = None
    max_drawdown: Decimal = ZERO
    current_drawdown: Decimal = ZERO
    average_abs_inventory: Decimal = ZERO
    p95_abs_inventory: Decimal = ZERO
    inventory_age: Decimal = ZERO
    forced_flatten_count: int = 0
    forced_flatten_loss: Decimal = ZERO
    quote_uptime_seconds: Decimal = ZERO
    buy_quote_seconds: Decimal | None = None
    sell_quote_seconds: Decimal | None = None
    two_sided_quote_seconds: Decimal | None = None
    duration_seconds: Decimal = ZERO
    fee_cover_ratio: Decimal | None = None
    maker_turnover_per_quote_hour: Decimal | None = None
    fills_per_quote_hour: Decimal | None = None
    spread_capture: Decimal | None = None
    inventory_markout: Decimal | None = None
    flatten_concession: Decimal | None = None
    decomposition_complete: bool = False

    def __post_init__(self):
        _symbol(self.symbol)
        for item in fields(self):
            value = getattr(self, item.name)
            if item.type is Decimal or (item.type == (Decimal | None) and value is not None):
                _decimal(value)
            elif item.type is bool:
                _boolean(value)
            elif item.type is int or (item.type == (int | None) and value is not None):
                _count(value)
        if self.complete and (self.final_position != ZERO or self.final_open_order_count != 0):
            raise ValueError("nonflat session cannot be complete")
        if self.complete and (not self.final_authenticated or self.ledger_position != ZERO
                              or self.failed or self.equity_reconciliation_difference != ZERO
                              or self.all_in_net_pnl is None):
            raise ValueError("complete session requires reconciled authenticated flat proof")
        if not self.complete and any(value is not None for value in (
                self.all_in_net_pnl, self.all_in_net_cost_bps, self.fee_cover_ratio)):
            raise ValueError("incomplete session cannot publish final economics")


@dataclass(frozen=True, slots=True)
class FillEvent:
    """One incremental fill, not a cumulative order quantity; source time survives replay."""

    fill_id: str
    order_id: str
    symbol: str
    side: Side
    size: Decimal
    price: Decimal
    fee: Decimal
    liquidity: LiquidityRole
    observed_monotonic: float
    reference_price: Decimal | None = None
    flatten_id: str | None = None

    def __post_init__(self):
        _identifier(self.fill_id)
        _identifier(self.order_id)
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        if not isinstance(self.side, Side) or not isinstance(self.liquidity, LiquidityRole):
            raise ValueError("typed fill side and liquidity required")
        _decimal(self.size, positive=True)
        _decimal(self.price, positive=True)
        _decimal(self.fee)
        if self.fee < ZERO:
            raise ValueError("negative fees/rebates unsupported")
        if self.reference_price is not None:
            _decimal(self.reference_price, positive=True)
        if self.flatten_id is not None:
            _identifier(self.flatten_id)
        if self.liquidity == LiquidityRole.TAKER and self.flatten_id is None:
            raise ValueError("taker fills must belong to bounded flatten")


@dataclass(frozen=True, slots=True)
class MarkEvent:
    """Trusted external reference and whether at least one maker order is working."""

    symbol: str
    observed_monotonic: float
    reference_price: Decimal
    quoting: bool
    quote_sides: tuple[Side, ...] | None = None

    def __post_init__(self):
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        _decimal(self.reference_price, positive=True)
        _boolean(self.quoting)
        if self.quote_sides is not None and (
                type(self.quote_sides) is not tuple
                or any(type(side) is not Side for side in self.quote_sides)
                or len(set(self.quote_sides)) != len(self.quote_sides)
                or bool(self.quote_sides) != self.quoting):
            raise ValueError("quote sides must match observed working orders")


@dataclass(frozen=True, slots=True)
class CashflowEvent:
    event_id: str
    symbol: str
    observed_monotonic: float
    amount: Decimal
    kind: CashflowKind

    def __post_init__(self):
        _identifier(self.event_id)
        _symbol(self.symbol)
        _time(self.observed_monotonic)
        _decimal(self.amount)
        if not isinstance(self.kind, CashflowKind):
            raise ValueError("typed cashflow kind required")


@dataclass(frozen=True, slots=True)
class FillAccounting:
    fill: FillEvent
    realized_gross_pnl: Decimal
    spread_capture_at_fill: Decimal | None
    inventory_markout: Decimal | None
    flatten_concession: Decimal | None

    def __post_init__(self):
        if type(self.fill) is not FillEvent:
            raise ValueError("typed incremental fill required")
        _decimal(self.realized_gross_pnl)
        for value in (self.spread_capture_at_fill, self.inventory_markout, self.flatten_concession):
            if value is not None:
                _decimal(value)


TelemetryEvent = AccountSnapshot | QuotePlan | ExecutionResult | FillAccounting | MarkEvent | CashflowEvent | SessionReport | BoundedExitReport | InventoryDecision
