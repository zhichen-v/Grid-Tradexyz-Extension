"""Bounded session coordination; strategy, accounting and execution stay in ports."""

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from math import isfinite
import warnings
import time
from types import SimpleNamespace

from .domain import (
    AccountSnapshot, ExecutionHealth, ExecutionResult, ExecutionSnapshot,
    ExecutionStatus, MarketStateSnapshot, QuotePlan, QuoteAuthorization,
    InventoryDecision, MarkEvent, SessionReport, StrategyState, ZERO,
    BoundedExitReport, ExitStatus, FlattenIntent, Side, _boolean, _count, _identifier, _symbol, _time,
)
from .execution_port import AccountPort, Clock, ExecutionPort, MarketDataPort, TelemetrySink
from .config import require_authorization
from .inventory_governor import InventoryGovernor
from .lighter_runtime import LighterAccountPort, LighterMarketData
from .quote_policy import VolumeQuotePolicy
from .session_ledger import SessionLedger


class DryCycleUnavailable(RuntimeError):
    """A synthetic cycle could not establish its input/execution contract."""


async def dry_synthetic_cycle(
    market_data: MarketDataPort,
    account_port: AccountPort,
    execution: ExecutionPort,
    clock: Clock,
    telemetry: TelemetrySink,
) -> QuotePlan:
    """Empty synthetic plumbing; fixture authentication is not exchange evidence."""
    before = execution.snapshot()
    if (not isinstance(before, ExecutionSnapshot) or not before.simulated
            or before.health != ExecutionHealth.HEALTHY):
        raise DryCycleUnavailable("healthy simulated execution required")
    try:
        market = market_data.snapshot()
        account = await account_port.snapshot()
        now = clock.monotonic()
    except Exception:
        raise DryCycleUnavailable("synthetic input read failed") from None
    if not isinstance(market, MarketStateSnapshot) or not isinstance(account, AccountSnapshot):
        raise DryCycleUnavailable("typed market and account snapshots required")
    if type(now) not in (int, float) or not isfinite(now) or now < 0:
        raise DryCycleUnavailable("valid monotonic clock required")
    if (market.symbol != account.symbol or not market.trusted or not account.authenticated
            or not 0 <= now - market.observed_monotonic <= 3
            or not 0 <= now - account.observed_monotonic <= 10):
        raise DryCycleUnavailable("fresh trusted same-symbol snapshots required")
    if account.position != 0 or account.open_order_count != 0:
        raise DryCycleUnavailable("synthetic flat start with zero exchange orders required")
    plan = QuotePlan(symbol=market.symbol)
    result = await execution.reconcile_quotes(plan)
    if (not isinstance(result, ExecutionResult) or result.status != ExecutionStatus.SIMULATED
            or not result.snapshot.simulated
            or result.snapshot.health != ExecutionHealth.HEALTHY):
        raise DryCycleUnavailable("empty dry plan was not reconciled")
    try:
        telemetry.emit(plan)
        telemetry.emit(result)
    except Exception:
        # Telemetry is not account/risk truth; do not convert a safe cycle to a risk halt.
        warnings.warn("V2 dry-cycle telemetry unavailable", RuntimeWarning, stacklevel=2)
    return plan


async def bounded_exit(
    execution: ExecutionPort, market_data: MarketDataPort, clock: Clock, *,
    symbol: str, flatten_id: str, deadline_monotonic: float,
    ioc_slippage_ticks: int, authorize_bounded_flatten: bool = False,
) -> BoundedExitReport:
    """Cancel/prove, then at most 3 reducing IOC attempts within 30s total.

    The first post-cancel book fixes the price bound for the entire exit. Later
    partials only shrink quantity; uncertainty stops this operation, never retries.
    Caller records the returned report with SessionLedger.record_exit after fills.
    """
    _symbol(symbol)
    _identifier(flatten_id)
    _time(deadline_monotonic)
    _count(ioc_slippage_ticks)
    _boolean(authorize_bounded_flatten)
    if not authorize_bounded_flatten:
        raise DryCycleUnavailable("bounded flatten requires per-run authorization")
    started = clock.monotonic()
    _time(started)
    deadline = min(deadline_monotonic, started + 30)
    attempts, result, last_now = 0, None, started

    def finish(status):
        nonlocal last_now
        try:
            now = clock.monotonic()
            _time(now)
            if now < last_now:
                raise ValueError("clock moved backwards")
            last_now = now
            if status == ExitStatus.FLAT and now >= deadline:
                status = ExitStatus.DEADLINE
        except Exception:
            status = ExitStatus.BLOCKED
        return BoundedExitReport(flatten_id, symbol, last_now, status, attempts,
                                 result if type(result) is ExecutionResult else None)

    def remaining():
        nonlocal last_now
        now = clock.monotonic()
        _time(now)
        if now < last_now:
            raise ValueError("clock moved backwards")
        last_now = now
        if now >= deadline:
            raise TimeoutError
        return deadline - now

    try:
        remaining()
        before = execution.snapshot()
        if before.health != ExecutionHealth.HEALTHY or before.simulated:
            return finish(ExitStatus.BLOCKED)
        boundary = clock.monotonic()
        result = await asyncio.wait_for(execution.cancel_all_managed(), remaining())
        account = _exit_account(result, symbol, boundary, clock.monotonic())
        remaining()
        if account.position == 0:
            return finish(ExitStatus.FLAT)
        market = market_data.snapshot()
        intent = _exit_intent(market, account, clock.monotonic(), deadline, ioc_slippage_ticks)
        for _ in range(3):
            remaining()
            boundary = clock.monotonic()
            attempts += 1
            result = await asyncio.wait_for(execution.flatten_ioc(intent), remaining())
            account = _exit_account(result, symbol, boundary, clock.monotonic())
            remaining()
            if account.position == 0:
                return finish(ExitStatus.FLAT)
            expected = Side.BUY if account.position < 0 else Side.SELL
            if expected != intent.side or account.position.copy_abs() > intent.size:
                return finish(ExitStatus.BLOCKED)
            # The bridge refreshes/revalidates tick/lot and market before every send.
            intent = replace(intent, size=account.position.copy_abs())
        return finish(ExitStatus.ATTEMPTS_EXHAUSTED)
    except TimeoutError:
        return finish(ExitStatus.DEADLINE)
    except Exception:
        return finish(ExitStatus.BLOCKED)


def _exit_account(result, symbol, boundary, now):
    _time(now)
    if type(result) is not ExecutionResult:
        raise ValueError("typed exit execution required")
    account = result.account_snapshot
    if (result.status != ExecutionStatus.CONFIRMED or result.snapshot.simulated
            or result.snapshot.health != ExecutionHealth.HEALTHY
            or result.snapshot.managed_order_count != 0 or type(account) is not AccountSnapshot
            or result.snapshot.symbol != symbol or result.snapshot.orders != ()
            or result.snapshot.observed_monotonic is None
            or not boundary <= result.snapshot.observed_monotonic <= now
            or not account.authenticated or account.symbol != symbol or account.open_order_count != 0
            or not boundary <= account.observed_monotonic <= now
            or now - account.observed_monotonic > 10):
        raise ValueError("fresh post-terminal authenticated exit truth required")
    return account


def _exit_intent(market, account, now, deadline, slippage_ticks):
    if (type(market) is not MarketStateSnapshot or market.symbol != account.symbol
            or not market.trusted or not 0 <= now - market.observed_monotonic <= 3):
        raise ValueError("fresh trusted market required for bounded exit")
    quantity = account.position.copy_abs()
    values = (quantity, market.external_bid, market.external_ask, market.tick_size,
              market.size_step, Decimal(slippage_ticks))
    precision = (sum(len(v.as_tuple().digits) for v in values)
                 + max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values) + 16)
    if precision > 4096:
        raise ValueError("exit input precision exceeds supported range")
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        if quantity < market.min_order_size or quantity % market.size_step:
            raise ValueError("residual is not an executable lot; never inflate")
        side = Side.BUY if account.position < 0 else Side.SELL
        limit = (market.external_ask + market.tick_size * slippage_ticks if side == Side.BUY
                 else market.external_bid - market.tick_size * slippage_ticks)
        return FlattenIntent(account.symbol, side, quantity, limit, deadline)


@dataclass(frozen=True, slots=True)
class SessionRunResult:
    dry_run: bool
    completed: bool
    report: SessionReport | None
    final_account: AccountSnapshot | None
    failure: str | None


class VolumeSession:
    """One bounded run; recheck CLI authorization before connecting; dry has no fills."""

    def __init__(self, config, adapter, *, account_index, expected_l1_address,
                 authorize_bounded_flatten=False, telemetry=None, clock=None,
                 sleep=asyncio.sleep):
        require_authorization(config, authorize_bounded_flatten)
        self.config, self.adapter = config, adapter
        self.clock, self.sleep = clock or time, sleep
        self.telemetry = telemetry
        self.manager = self.execution = self.ledger = self.governor = None
        self.final_account = None
        self._stop = None
        self._stop_at = None
        self._used = False
        self._exit_id = None
        self._exit_sequence = 0
        self._exit_orders = {}
        self._passive_until = None
        self._cleanup_attempted = False
        self.account = LighterAccountPort(adapter, config.symbol, self.clock,
            account_index=account_index, expected_l1_address=expected_l1_address,
            known_order_ids=self._known_ids, flatten_id_for=self._flatten_id,
            terminal_order_ids=lambda: self.manager.terminal_order_ids if self.manager else frozenset())
        self.market = LighterMarketData(adapter, config.symbol, self.clock,
                                        working_orders=lambda: self.account.latest_orders)

    def _known_ids(self):
        return (self.manager.known_order_ids | self.manager.active_unwind_order_ids
                if self.manager else frozenset())

    def _flatten_id(self, order_id):
        if (self._exit_id and self.manager
                and order_id in self.manager.active_unwind_order_ids):
            self._exit_orders.setdefault(order_id, self._exit_id)
        return self._exit_orders.get(order_id)

    async def _io(self, operation, timeout=10):
        deadline = self.governor.exit_deadline if self.governor else None
        if self._stop_at is not None:
            deadline = min(deadline or float("inf"), self._stop_at + 30)
        if deadline is not None:
            timeout = min(timeout, deadline - self.clock.monotonic())
        if timeout <= 0:
            raise TimeoutError
        return await asyncio.wait_for(operation(), timeout)

    def _emit(self, event):
        if (self.ledger and type(event) is ExecutionResult
                and event.status is ExecutionStatus.CONFIRMED
                and event.snapshot.health is ExecutionHealth.HEALTHY):
            try:
                market = self.market.snapshot()
                self.ledger.observe(MarkEvent(self.config.symbol, self.clock.monotonic(),
                    (market.external_bid + market.external_ask) / 2, bool(event.snapshot.orders)))
            except Exception:
                pass  # No fresh reference: retain observed intervals, never invent a mark.
        if self.telemetry:
            try:
                self.telemetry.emit(event)
            except Exception:
                warnings.warn("V2 session telemetry unavailable", RuntimeWarning, stacklevel=2)

    async def snapshot(self, *, exiting=False):
        """Bridge post-cancel/IOC read; MUST NOT invalidate the OM preparation token."""
        account = await self.account.snapshot()
        self.final_account = account
        try:
            market = await self.market.refresh()
        except Exception:
            if exiting and account.position == ZERO and account.open_order_ids == ():
                return account  # Flat cancellation proof does not require a book.
            raise
        if self.ledger:
            self.ledger.observe(MarkEvent(self.config.symbol, self.clock.monotonic(),
                (market.external_bid + market.external_ask) / 2,
                bool(account.open_order_count)))
        return account

    async def _start(self):
        from ..market_maker.models import MarketMetadata
        from ..market_maker.order_manager import MarketMakerOrderManager
        from .execution_port import (
            LegacyVolumeExecutionPort, DryVolumeExecutionPort, legacy_execution_settings,
        )
        if await self._io(self.adapter.connect, 30) is not True:
            raise ValueError("connection unavailable")
        if await self._io(self.adapter.authenticate) is not True:
            raise ValueError("authentication unavailable")
        await self._io(self.market.initialize)
        initial = await self._io(self.snapshot)
        if not initial.authenticated or initial.position or initial.open_order_ids != ():
            raise ValueError("authenticated flat empty start required")
        market, cfg = self.market.snapshot(), self.config
        if (cfg.quote.order_size < market.min_order_size
                or cfg.quote.order_size % market.size_step
                or cfg.quote.order_size * market.external_bid < self.market.min_quote_amount):
            raise ValueError("configured lot/notional is not executable; never upscale")
        self.ledger = SessionLedger(initial, telemetry=self.telemetry)
        self.account.attach_ledger(self.ledger)
        started = initial.observed_monotonic
        self.governor = InventoryGovernor(order_size=cfg.quote.order_size,
            soft_limit=cfg.inventory.soft_limit, hard_limit=cfg.inventory.hard_limit,
            stop_loss_usdg=cfg.flatten.stop_loss_usdg, max_hold_seconds=cfg.flatten.max_hold_seconds,
            cooldown_seconds=cfg.session.cooldown_seconds, max_session_loss_usdg=cfg.session.max_loss_usdg,
            session_started_monotonic=started, session_deadline_monotonic=started + cfg.session.duration_seconds,
            ioc_slippage_ticks=cfg.flatten.ioc_slippage_ticks)
        self.policy = VolumeQuotePolicy(order_size=cfg.quote.order_size,
            target_net_edge_bps=cfg.quote.target_net_edge_bps,
            volatility_multiplier=cfg.quote.volatility_multiplier,
            hard_inventory_limit=cfg.inventory.hard_limit, skew_bps_at_hard=cfg.inventory.skew_bps_at_hard)
        if cfg.dry_run:
            self.execution = DryVolumeExecutionPort(cfg.symbol, self.clock, refresh_quote=self._authorize,
                reprice_threshold_ticks=cfg.quote.reprice_threshold_ticks,
                max_quote_age_ms=cfg.quote.max_quote_age_ms)
        else:
            metadata = MarketMetadata(cfg.symbol, -market.tick_size.as_tuple().exponent,
                -market.size_step.as_tuple().exponent, market.tick_size, market.size_step,
                market.min_order_size, self.market.min_quote_amount)
            self.manager = MarketMakerOrderManager(self.adapter, legacy_execution_settings(cfg),
                metadata, monotonic=self.clock.monotonic, sleep=self.sleep)
            await self._io(self.manager.initialize)
            exit_account = SimpleNamespace(snapshot=lambda: self.snapshot(exiting=True))
            self.execution = LegacyVolumeExecutionPort(self.manager, exit_account, self.market, self.clock,
                authorize_bounded_flatten=True, refresh_quote=self._authorize,
                reprice_threshold_ticks=cfg.quote.reprice_threshold_ticks,
                max_quote_age_ms=cfg.quote.max_quote_age_ms)

    async def _authorize(self, exposure):
        account = await self._io(self.snapshot)
        now, market = self.clock.monotonic(), self.market.snapshot()
        if self.config.dry_run:
            # Only the governor's exposure input is synthetic. Actual account,
            # ledger and telemetry remain authenticated zero-order observations.
            if account.position or account.open_order_ids != ():
                raise ValueError("dry account changed")
            risk_account = replace(account, open_order_count=len(exposure.orders),
                                   open_order_ids=tuple(o.order_id for o in exposure.orders))
        else:
            if (account.open_order_ids is None or exposure.orders is None
                    or set(account.open_order_ids) != {o.order_id for o in exposure.orders}
                    or set(self.account.latest_orders) != set(exposure.orders)):
                raise ValueError("account and managed order identity/exposure disagree")
            risk_account = account
        decision = self.governor.evaluate(market, risk_account, self.ledger.snapshot(now=now),
            exposure, now=now, stop_requested=self._stop.is_set())
        if (self._passive_until is not None and now < self._passive_until
                and decision.state is StrategyState.FLATTENING
                and ZERO < abs(account.position) <= self.config.inventory.hard_limit):
            capacity = min(abs(account.position), self.config.quote.order_size)
            decision = InventoryDecision(StrategyState.REDUCE_ONLY,
                buy_capacity=capacity if account.position < ZERO else ZERO,
                sell_capacity=capacity if account.position > ZERO else ZERO)
        plan = self.policy.propose(market, account, decision, now=now)
        # Both normal and reducing proposals obey the current minimum notional.
        plan = replace(plan, quotes=tuple(q for q in plan.quotes
            if q.price * q.size >= self.market.min_quote_amount))
        return QuoteAuthorization(account, market, decision, plan)

    async def _pause(self, seconds):
        # Stop wakes the normal loop; passive cleanup has its own bounded clock.
        sleeper = asyncio.create_task(self.sleep(max(0, seconds)))
        stopper = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait((sleeper, stopper), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, stopper):
                task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    async def _watch_stop(self):
        await self._stop.wait()
        self._stop_at = self.clock.monotonic()

    async def _exit(self, *, allow_passive=True):
        self._cleanup_attempted = True
        deadline = self.governor.exit_deadline
        if deadline is None:
            deadline = min(self.clock.monotonic(), self.governor.session_deadline_monotonic) + 30
        if self._stop_at is not None:
            deadline = min(deadline, self._stop_at + 30)
        self._exit_sequence += 1
        self._exit_id = f"exit-{self._exit_sequence}"
        try:
            if allow_passive and self.config.flatten.passive_grace_seconds:
                # Cancel increasing quotes and prove residual before the grace.
                result = await self._io(self.execution.cancel_all_managed)
                self._emit(result)
                account = _exit_account(result, self.config.symbol, 0, self.clock.monotonic())
                self._passive_until = min(self.clock.monotonic()
                    + self.config.flatten.passive_grace_seconds, deadline - 10)
                while (account.position and abs(account.position) <= self.config.inventory.hard_limit
                       and self.clock.monotonic() < self._passive_until):
                    await self._io(self.manager.sync_open_orders)
                    auth = await self._authorize(self.execution.snapshot())
                    if not auth.plan.quotes:
                        break
                    before_ids = self._known_ids()
                    result = await self._io(lambda: self.execution.reconcile_quotes(auth.plan))
                    for order_id in self._known_ids() - before_ids:
                        self._exit_orders[order_id] = self._exit_id
                    self._emit(result)
                    if result.status is ExecutionStatus.BLOCKED:
                        raise ValueError("passive execution blocked")
                    await self._io(lambda: self.sleep(max(0, min(1, self._passive_until - self.clock.monotonic()))))
                    account = await self._io(self.snapshot)
            self._passive_until = None
            report = await bounded_exit(self.execution, self.market, self.clock,
                symbol=self.config.symbol, flatten_id=self._exit_id,
                deadline_monotonic=deadline, ioc_slippage_ticks=self.config.flatten.ioc_slippage_ticks,
                authorize_bounded_flatten=True)
        except (Exception, asyncio.CancelledError):
            report = BoundedExitReport(self._exit_id, self.config.symbol,
                self.clock.monotonic(), ExitStatus.BLOCKED, 0)
        finally:
            self._passive_until = None
        self.ledger.record_exit(report)
        if report.complete and self.governor.exit_deadline is not None:
            self.governor.confirm_exit(report.final_result, now=self.clock.monotonic())
        self._exit_id = None
        return report.complete

    async def run(self, stop_event):
        if self._used or not isinstance(stop_event, asyncio.Event):
            raise ValueError("one run and an explicit stop event required")
        self._used, self._stop = True, stop_event
        watcher = asyncio.create_task(self._watch_stop())
        failure, cleaned, report = None, False, None
        try:
            await self._start()
            while True:
                if self.manager:
                    await self._io(self.manager.sync_open_orders)
                auth = await self._authorize(self.execution.snapshot())
                if auth.decision.state is StrategyState.SESSION_COMPLETE:
                    break
                if auth.decision.state is StrategyState.FLATTENING:
                    if self.config.dry_run:
                        break
                    cleaned = await self._exit()
                    if not cleaned:
                        raise ValueError("bounded cleanup incomplete")
                    # A nonterminal risk exit may cooldown, then quote again.
                    continue
                cleaned = False
                self._cleanup_attempted = False
                self._emit(auth.plan)
                result = await self._io(lambda: self.execution.reconcile_quotes(auth.plan))
                self._emit(result)
                if result.status is ExecutionStatus.BLOCKED:
                    raise ValueError("execution blocked")
                await self._pause(min(1, max(0, self.governor.session_deadline_monotonic - self.clock.monotonic())))
        except (Exception, asyncio.CancelledError):
            failure = "session_failed_closed"
            stop_event.set()
        finally:
            if self.execution:
                try:
                    if self.config.dry_run:
                        result = await self._io(self.execution.cancel_all_managed)
                        self._emit(result)
                        cleaned = (result.status is ExecutionStatus.SIMULATED
                                   and result.snapshot.managed_order_count == 0)
                    elif not cleaned and not self._cleanup_attempted:
                        cleaned = await self._exit(allow_passive=False)
                    # A separate authenticated read after cleanup, never an ack.
                    self.final_account = await asyncio.wait_for(self.account.snapshot(), 10)
                    cleaned = bool(cleaned and self.final_account.authenticated
                        and not self.final_account.position and self.final_account.open_order_ids == ())
                except (Exception, asyncio.CancelledError):
                    cleaned = False
                    self.final_account = None
            try:
                if self.ledger:
                    if self.config.dry_run:
                        report = self.ledger.snapshot(now=self.clock.monotonic())
                        if self.final_account:
                            self._emit(self.final_account)
                        self._emit(report)  # Deliberately incomplete: no dry economics.
                    else:
                        report = self.ledger.finalize(self.final_account, now=self.clock.monotonic())
                        if not report.complete:
                            failure = failure or "accounting_incomplete"
            except Exception:
                failure = "accounting_unavailable"
            try:
                await asyncio.wait_for(self.adapter.disconnect(), 10)
            except (Exception, asyncio.CancelledError):
                failure = "disconnect_unconfirmed"
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if not cleaned:
            failure = failure or "cleanup_unconfirmed"
        return SessionRunResult(self.config.dry_run, cleaned and failure is None,
                                report, self.final_account, failure)
