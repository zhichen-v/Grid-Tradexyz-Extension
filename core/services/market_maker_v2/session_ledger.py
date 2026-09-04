"""Session economics from incremental fills, not inventory-episode promotion."""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from math import isfinite

from .domain import (
    AccountSnapshot, BoundedExitReport, CashflowEvent, CashflowKind, FillAccounting, FillEvent,
    LiquidityRole, MarkEvent, SessionReport, Side,
)


D = Decimal
ZERO = D("0")


class LedgerError(ValueError):
    """An economic event cannot safely be accepted."""


def _time(value: float) -> Decimal:
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise LedgerError("invalid event time")
    return D(str(value))


def _sign(value: Decimal) -> Decimal:
    return D(1) if value > 0 else D(-1) if value < 0 else ZERO


@dataclass
class _State:
    time: Decimal
    high_water: Decimal
    position: Decimal = ZERO
    cost_basis: Decimal = ZERO
    signed_cash: Decimal = ZERO
    gross: Decimal = ZERO
    maker_buy: Decimal = ZERO
    maker_sell: Decimal = ZERO
    taker_turnover: Decimal = ZERO
    maker_fee: Decimal = ZERO
    taker_fee: Decimal = ZERO
    funding: Decimal = ZERO
    transfers: Decimal = ZERO
    maker_fills: int = 0
    taker_fills: int = 0
    reference: Decimal | None = None
    opened_at: Decimal | None = None
    quoting: bool = False
    quote_seconds: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    spread: Decimal = ZERO
    drift: Decimal = ZERO
    concession: Decimal = ZERO
    decomposition_complete: bool = True
    inventory_seconds: dict[Decimal, Decimal] = field(default_factory=dict)
    flatten_net: dict[str, Decimal] = field(default_factory=dict)


class SessionLedger:
    """One fixed session; complete means reconciled accounting, never strategy GO."""

    def __init__(self, initial: AccountSnapshot, *, telemetry=None):
        if (not isinstance(initial, AccountSnapshot) or not initial.authenticated
                or initial.position != ZERO or initial.open_order_count != 0):
            raise LedgerError("authenticated flat and empty starting account required")
        self._initial = initial
        self._started = _time(initial.observed_monotonic)
        self._state = _State(self._started, initial.equity)
        self._telemetry = telemetry
        self._telemetry_errors = 0
        self._failed = False
        self._final: SessionReport | None = None
        # ponytail: session-scoped registries; Phase 11 adds bounded persistence for 24h/restarts.
        self._fills: dict[str, FillEvent] = {}
        self._cashflows: dict[str, CashflowEvent] = {}
        self._exits: dict[str, BoundedExitReport] = {}
        self._emit(initial)

    def _emit(self, event) -> None:
        if self._telemetry is not None:
            try:
                self._telemetry.emit(event)
            except Exception:
                self._telemetry_errors += 1

    def _fail(self, message: str):
        self._failed = True
        raise LedgerError(message)

    def _open(self) -> None:
        if self._final is not None:
            raise LedgerError("session is sealed")
        if self._failed:
            raise LedgerError("ledger is failed")

    def _duplicate(self, event, registry, identifier: str) -> bool:
        previous = registry.get(identifier)
        if previous is None:
            return False
        # Reference marks are optional analytics, not exchange fill identity.
        compared = replace(event, reference_price=previous.reference_price) if isinstance(event, FillEvent) else event
        if previous != compared:
            self._fail("conflicting duplicate event")
        return True

    def _copy_at(self, now: Decimal) -> _State:
        if now < self._state.time:
            raise LedgerError("event time moved backwards")
        state = replace(
            self._state, inventory_seconds=dict(self._state.inventory_seconds),
            flatten_net=dict(self._state.flatten_net),
        )
        duration = now - state.time
        if duration:
            quantity = abs(state.position)
            state.inventory_seconds[quantity] = state.inventory_seconds.get(quantity, ZERO) + duration
            if state.quoting:
                state.quote_seconds += duration
        state.time = now
        return state

    def _event_state(self, event) -> _State:
        if event.symbol != self._initial.symbol:
            self._fail("event symbol differs from session")
        return self._copy_at(_time(event.observed_monotonic))

    @staticmethod
    def _net(state: _State) -> Decimal:
        return state.gross - state.maker_fee - state.taker_fee + state.funding

    def _marked_net(self, state: _State) -> Decimal | None:
        if state.position == ZERO:
            return self._net(state)
        if state.reference is None:
            return None
        unrealized = _sign(state.position) * (abs(state.position) * state.reference - state.cost_basis)
        return self._net(state) + unrealized

    def _sample_drawdown(self, state: _State) -> None:
        marked = self._marked_net(state)
        if marked is not None:
            self._equity_drawdown(state, self._initial.equity + marked)

    @staticmethod
    def _equity_drawdown(state: _State, adjusted_equity: Decimal) -> None:
        state.high_water = max(state.high_water, adjusted_equity)
        state.max_drawdown = max(state.max_drawdown, state.high_water - adjusted_equity)

    @staticmethod
    def _reference(state: _State, reference: Decimal | None) -> Decimal | None:
        if reference is None:
            state.decomposition_complete = False
            return None
        drift = None
        if state.decomposition_complete:
            if state.reference is None and state.position != ZERO:
                state.decomposition_complete = False
            else:
                drift = state.position * (reference - state.reference) if state.reference is not None else ZERO
                state.drift += drift
        state.reference = reference
        return drift

    @staticmethod
    def _fill_cost(state: _State, fill: FillEvent) -> Decimal:
        old_position, old_gross = state.position, state.gross
        signed_size = fill.size if fill.side is Side.BUY else -fill.size
        state.signed_cash -= signed_size * fill.price
        if old_position == ZERO or _sign(old_position) == _sign(signed_size):
            state.cost_basis += fill.size * fill.price
        else:
            closed = min(abs(old_position), fill.size)
            released = (state.cost_basis if closed == abs(old_position)
                        else state.cost_basis * closed / abs(old_position))
            state.gross += _sign(old_position) * (closed * fill.price - released)
            state.cost_basis -= released
            if fill.size > abs(old_position):
                state.cost_basis = (fill.size - abs(old_position)) * fill.price
        state.position += signed_size
        if state.position == ZERO:
            state.cost_basis = ZERO
            # Closing gets the fractional cost-basis remainder, never an account/equity plug.
            state.gross = state.signed_cash
            state.opened_at = None
        elif old_position == ZERO or _sign(old_position) != _sign(state.position):
            state.opened_at = state.time
        return state.gross - old_gross

    def ingest_fill(self, fill: FillEvent) -> bool:
        self._open()
        if not isinstance(fill, FillEvent):
            self._fail("typed fill required")
        if self._duplicate(fill, self._fills, fill.fill_id):
            return False
        if fill.flatten_id is not None:
            direction = D(1) if fill.side is Side.BUY else D(-1)
            if (self._state.position * direction >= ZERO
                    or fill.size > abs(self._state.position)):
                self._fail("flatten fill must reduce without crossing zero")
        try:
            state = self._event_state(fill)
            drift = self._reference(state, fill.reference_price)
            gross = self._fill_cost(state, fill)
            turnover = fill.size * fill.price
            if fill.liquidity is LiquidityRole.MAKER:
                if fill.side is Side.BUY:
                    state.maker_buy += turnover
                else:
                    state.maker_sell += turnover
                state.maker_fee += fill.fee
                state.maker_fills += 1
            else:
                state.taker_turnover += turnover
                state.taker_fee += fill.fee
                state.taker_fills += 1
            spread = concession = None
            if fill.reference_price is not None:
                edge = (D(1) if fill.side is Side.BUY else D(-1)) * fill.size * (fill.reference_price - fill.price)
                spread, concession = (ZERO, -edge) if fill.flatten_id is not None else (edge, ZERO)
                state.spread += spread
                state.concession += concession
            if fill.flatten_id is not None:
                state.flatten_net[fill.flatten_id] = state.flatten_net.get(fill.flatten_id, ZERO) + gross - fill.fee
            self._sample_drawdown(state)
            accounting = FillAccounting(fill, gross, spread, drift, concession)
        except (ValueError, ArithmeticError, TypeError):
            self._fail("fill accounting rejected")
        self._state = state
        self._fills[fill.fill_id] = fill
        self._emit(accounting)
        return True

    def ingest_cashflow(self, cashflow: CashflowEvent) -> bool:
        self._open()
        if not isinstance(cashflow, CashflowEvent):
            self._fail("typed cashflow required")
        if self._duplicate(cashflow, self._cashflows, cashflow.event_id):
            return False
        try:
            state = self._event_state(cashflow)
            if cashflow.kind is CashflowKind.FUNDING:
                state.funding += cashflow.amount
            else:
                state.transfers += cashflow.amount
            self._sample_drawdown(state)
        except (ValueError, ArithmeticError, TypeError):
            self._fail("cashflow accounting rejected")
        self._state = state
        self._cashflows[cashflow.event_id] = cashflow
        self._emit(cashflow)
        return True

    def record_exit(self, report: BoundedExitReport) -> bool:
        """Count attempted exits even with zero fills; fees still come only from fills."""
        self._open()
        if type(report) is not BoundedExitReport:
            self._fail("typed bounded exit report required")
        if self._duplicate(report, self._exits, report.flatten_id):
            return False
        try:
            state = self._event_state(report)
            if report.attempts:
                state.flatten_net.setdefault(report.flatten_id, ZERO)
        except (ValueError, ArithmeticError, TypeError):
            self._fail("exit accounting rejected")
        self._state = state
        self._exits[report.flatten_id] = report
        self._emit(report)
        return True

    def observe(self, mark: MarkEvent) -> None:
        self._open()
        if not isinstance(mark, MarkEvent):
            self._fail("typed mark required")
        try:
            state = self._event_state(mark)
            self._reference(state, mark.reference_price)
            state.quoting = mark.quoting
            self._sample_drawdown(state)
        except (ValueError, ArithmeticError, TypeError):
            self._fail("mark accounting rejected")
        self._state = state
        self._emit(mark)

    @staticmethod
    def _inventory_stats(state: _State, duration: Decimal) -> tuple[Decimal, Decimal]:
        if duration == ZERO:
            return ZERO, ZERO
        average = sum((size * seconds for size, seconds in state.inventory_seconds.items()), ZERO) / duration
        cumulative, threshold = ZERO, duration * D("0.95")
        for size, seconds in sorted(state.inventory_seconds.items()):
            cumulative += seconds
            if cumulative >= threshold:
                return average, size
        return average, ZERO

    def _report(self, state: _State, *, final=None, complete=False, difference=None) -> SessionReport:
        duration = state.time - self._started
        average, p95 = self._inventory_stats(state, duration)
        turnover, fees = state.maker_buy + state.maker_sell, state.maker_fee + state.taker_fee
        net = self._net(state)
        return SessionReport(
            symbol=self._initial.symbol, complete=complete,
            final_position=final.position if final is not None else state.position,
            final_open_order_count=final.open_order_count if final is not None else None,
            all_in_net_pnl=net if complete else None,
            all_in_net_cost_bps=-net * D("10000") / turnover if complete and turnover else None,
            ledger_position=state.position,
            final_authenticated=final.authenticated if final is not None else False,
            equity_reconciliation_difference=difference, failed=self._failed,
            telemetry_errors=self._telemetry_errors,
            maker_buy_turnover=state.maker_buy, maker_sell_turnover=state.maker_sell,
            maker_turnover_total=turnover, taker_flatten_turnover=state.taker_turnover,
            maker_fill_count=state.maker_fills, taker_fill_count=state.taker_fills,
            realized_gross_pnl=state.gross, maker_fee=state.maker_fee, taker_fee=state.taker_fee,
            funding=state.funding, external_transfers=state.transfers, realized_net_pnl=net,
            marked_net_pnl=self._marked_net(state), max_drawdown=state.max_drawdown,
            average_abs_inventory=average, p95_abs_inventory=p95,
            inventory_age=state.time - state.opened_at if state.opened_at is not None else ZERO,
            forced_flatten_count=len(state.flatten_net),
            forced_flatten_loss=sum((max(ZERO, -net) for net in state.flatten_net.values()), ZERO),
            quote_uptime_seconds=state.quote_seconds, duration_seconds=duration,
            fee_cover_ratio=state.gross / fees if complete and fees else None,
            maker_turnover_per_quote_hour=turnover * D("3600") / state.quote_seconds if state.quote_seconds else None,
            fills_per_quote_hour=D(state.maker_fills) * D("3600") / state.quote_seconds if state.quote_seconds else None,
            spread_capture=state.spread if state.decomposition_complete else None,
            inventory_markout=state.drift if state.decomposition_complete else None,
            flatten_concession=state.concession if state.decomposition_complete else None,
            decomposition_complete=state.decomposition_complete,
        )

    def snapshot(self, *, now: float) -> SessionReport:
        """Project the current interval without consuming time or changing accounting."""
        if self._final is not None:
            return self._final
        return self._report(self._copy_at(_time(now)))

    def finalize(self, final: AccountSnapshot, *, now: float) -> SessionReport:
        """Seal once, retaining incomplete sessions instead of discarding their costs."""
        if self._final is not None:
            raise LedgerError("session is sealed")
        clock_valid = True
        try:
            now_decimal = _time(now)
            state = self._copy_at(now_decimal)
        except (ValueError, ArithmeticError, TypeError):
            clock_valid = False
            state = self._copy_at(self._state.time)
        if not isinstance(final, AccountSnapshot):
            final = None
        difference, complete = None, False
        if final is not None:
            difference = final.equity - self._initial.equity - state.transfers - self._net(state)
            proof_valid = (
                clock_valid and final.authenticated and final.symbol == self._initial.symbol
                and self._state.time <= _time(final.observed_monotonic) <= state.time
                and state.time - _time(final.observed_monotonic) <= D("10")
            )
            if proof_valid:
                self._equity_drawdown(state, final.equity - state.transfers)
            complete = (
                proof_valid and not self._failed and state.position == ZERO
                and final.position == ZERO and final.open_order_count == 0 and difference == ZERO
            )
        if final is not None:
            self._emit(final)
        report = self._report(state, final=final, complete=complete, difference=difference)
        self._emit(report)
        self._final = replace(report, telemetry_errors=self._telemetry_errors)
        return self._final
