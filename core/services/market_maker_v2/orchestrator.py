"""Bounded session coordination; strategy, accounting and execution stay in ports."""

import asyncio
from dataclasses import replace
from decimal import Decimal, localcontext
import warnings
import time
from types import SimpleNamespace

from .domain import (
    AccountSnapshot, ExecutionHealth, ExecutionResult,
    ExecutionStatus, MarketStateSnapshot, QuotePlan, QuoteAuthorization,
    InventoryDecision, MarkEvent, SessionRunResult, StrategyState, ZERO,
    BoundedExitReport, ExitStatus, FlattenIntent, Side, _boolean, _count, _identifier, _symbol, _time,
)
from .execution_port import Clock, ExecutionPort, MarketDataPort, ExecutionUnavailable
from .config import require_authorization
from .inventory_governor import InventoryGovernor
from .lighter_runtime import AccountReadRace, LighterAccountPort, LighterMarketData
from .quote_policy import VolumeQuotePolicy
from .session_ledger import SessionLedger
from .api_budget import ApiBudget, ApiBudgetUnavailable
from .telemetry import failure_diagnostic


async def bounded_exit(
    execution: ExecutionPort, market_data: MarketDataPort, clock: Clock, *,
    symbol: str, flatten_id: str, deadline_monotonic: float,
    ioc_slippage_ticks: int, authorize_bounded_flatten: bool = False,
    on_failure=None,
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
        raise ExecutionUnavailable("bounded flatten requires per-run authorization")
    started = clock.monotonic()
    _time(started)
    deadline = min(deadline_monotonic, started + 30)
    attempts, result, last_now = 0, None, started
    stage = "exit_health"

    def diagnose(error=None):
        if on_failure is not None:
            try:
                on_failure(stage, error)
            except Exception:
                pass  # A diagnostic failure cannot interrupt the authorized exit.

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
            diagnose()
            return finish(ExitStatus.BLOCKED)
        stage = "exit_cancel"
        boundary = clock.monotonic()
        result = await asyncio.wait_for(execution.cancel_all_managed(), remaining())
        stage = "exit_account"
        account = _exit_account(result, symbol, boundary, clock.monotonic())
        remaining()
        if account.position == 0:
            return finish(ExitStatus.FLAT)
        stage = "exit_market"
        market = market_data.snapshot()
        intent = _exit_intent(market, account, clock.monotonic(), deadline, ioc_slippage_ticks)
        for _ in range(3):
            remaining()
            boundary = clock.monotonic()
            attempts += 1
            stage = "exit_ioc"
            result = await asyncio.wait_for(execution.flatten_ioc(intent), remaining())
            stage = "exit_account"
            account = _exit_account(result, symbol, boundary, clock.monotonic())
            remaining()
            if account.position == 0:
                return finish(ExitStatus.FLAT)
            expected = Side.BUY if account.position < 0 else Side.SELL
            if expected != intent.side or account.position.copy_abs() > intent.size:
                diagnose()
                return finish(ExitStatus.BLOCKED)
            # The bridge refreshes/revalidates tick/lot and market before every send.
            intent = replace(intent, size=account.position.copy_abs())
        diagnose()
        return finish(ExitStatus.ATTEMPTS_EXHAUSTED)
    except TimeoutError as error:
        diagnose(error)
        return finish(ExitStatus.DEADLINE)
    except Exception as error:
        diagnose(error)
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
            or not account.fresh(now)):
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
        if quantity <= ZERO or quantity % market.size_step:
            raise ValueError("residual is not an executable lot; never inflate")
        side = Side.BUY if account.position < 0 else Side.SELL
        limit = (market.external_ask + market.tick_size * slippage_ticks if side == Side.BUY
                 else market.external_bid - market.tick_size * slippage_ticks)
        return FlattenIntent(account.symbol, side, quantity, limit, deadline)


class VolumeSession:
    """One bounded run; recheck CLI authorization before connecting; dry has no fills."""

    def __init__(self, config, adapter, *, account_index, expected_l1_address,
                 authorize_bounded_flatten=False, telemetry=None, clock=None,
                 sleep=asyncio.sleep, allow_delayed_dry_book=False):
        require_authorization(config, authorize_bounded_flatten,
                              allow_delayed_dry_book=allow_delayed_dry_book)
        self.config, self.adapter = config, adapter
        self._allow_delayed_dry_book = allow_delayed_dry_book
        self.clock, self.sleep = clock or SimpleNamespace(monotonic=time.perf_counter), sleep
        exact_funding = getattr(adapter, "enable_market_maker_exact_funding", None)
        if exact_funding is not None:
            exact_funding()
        self.api_budget = ApiBudget(self.clock.monotonic, work_phase=lambda:
            "exit" if self._budget_exiting else "normal" if self.ledger else "startup")
        observe_requests = getattr(adapter, "set_market_maker_request_observer", None)
        self._budget_active = observe_requests is not None and not config.dry_run
        self._budget_exiting, self._exit_read_retries = False, 0
        self._exit_funding_refreshes = 0
        if observe_requests is not None:
            observe_requests(self.api_budget.observe, enforce_admission=self._budget_active)
        self.telemetry, self.final_account = telemetry, None
        self.cleanup_account = None
        self.phase = "starting"  # Local console diagnostics; never execution authority.
        self.manager = self.execution = self.ledger = self.governor = None
        self._stop, self._stop_at = None, None
        self._stop_reason = None
        self._used = False
        self._exit_id, self._exit_sequence = None, 0
        self._exit_orders = {}
        self._passive_until = None
        self._cleanup_attempted = False
        self._session_complete = False
        self._quote_account = None
        self.account = LighterAccountPort(adapter, config.symbol, self.clock,
            account_index=account_index, expected_l1_address=expected_l1_address,
            known_order_ids=self._known_ids, flatten_id_for=self._flatten_id,
            mutation_generation=lambda: (self.manager.mutation_generation if self.manager
                                         else 0 if config.dry_run else None),
            terminal_order_ids=lambda: self.manager.terminal_order_ids if self.manager else frozenset())
        self.market = LighterMarketData(adapter, config.symbol, self.clock,
            working_orders=lambda: self.account.latest_orders, aligned_book=lambda: self.account.aligned_book)
        self.account.before_read = self._admit_read

    def _admit_read(self, kind):
        if not self._budget_active:
            return
        if self._budget_exiting:
            if kind == "retry":
                # One normal arrival race is included in the exit envelope.
                # Additional races need new headroom before any extra read.
                if self._exit_read_retries:
                    self.api_budget.require_normal({"rest": 1000, "ws": 5, "tx": 0})
                self._exit_read_retries += 1
            elif kind == "funding_refresh":
                if self._exit_funding_refreshes:
                    self.api_budget.require_normal({"rest": 900, "ws": 0, "tx": 0})
                self._exit_funding_refreshes += 1
            return
        if kind not in {"retry", "funding_refresh"}:
            # Conditional REST reads are admitted when needed. The later
            # trades/history gates preserve exit capacity after metadata reads.
            conditional = {"fees": 900, "settlement": 300, "trades": 600, "terminal_history": 100}
            # Only cash precedes the next REST gate. Trades and terminal
            # history each retain their own admission, including on retries.
            rest = {"audit": 300, "sync": 200, **conditional}.get(kind, 0)
            self.api_budget.require_normal({"rest": rest,
                "ws": 0 if kind in conditional else 5 if kind == "audit" else 2, "tx": 0})

    def _admit_mutation(self):
        if self._budget_active and not self._budget_exiting:
            # One create with bounded SDK lookup/history.
            self.api_budget.require_normal({"rest": 1400, "ws": 8, "tx": 2})

    def _admit_cancel(self, count):
        if type(count) is not int or not 0 <= count <= 2:
            raise ValueError("normal cancellation count must be between zero and two")
        if count and self._budget_active and not self._budget_exiting:
            # MM terminal-only cancellation: at most four history reads (400),
            # initial nonce plus invalid-nonce refresh (12), and one send.
            # No active-order WS or market-metadata lookup occurs in this path.
            self.api_budget.require_normal({"rest": 412 * count, "ws": 0, "tx": count})

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
                    (market.external_bid + market.external_ask) / 2, bool(event.snapshot.orders),
                    tuple(dict.fromkeys(o.side for o in event.snapshot.orders or ()))))
            except Exception:
                pass  # No fresh reference: retain observed intervals, never invent a mark.
        if self.telemetry:
            try:
                self.telemetry.emit(event)
            except Exception:
                warnings.warn("V2 session telemetry unavailable", RuntimeWarning, stacklevel=2)

    def _diagnose(self, stage, error=None):
        try:
            self._emit(failure_diagnostic(self.config.symbol, stage, error,
                       execution=self.execution, manager=self.manager))
        except Exception:
            pass  # Even warning-as-error or broken state capture must preserve cleanup.

    async def snapshot(self, *, exiting=False):
        """Bridge post-cancel/IOC read; MUST NOT invalidate the OM preparation token."""
        if exiting:
            # Exit preparation establishes a new read boundary even without a
            # mutation. Do not reuse an earlier same-generation book handoff.
            self.account.begin_quote_cycle()
        account = await self.account.snapshot(allow_cash_reuse=not exiting,
                                              allow_metadata_cache=not exiting,
                                              allow_unreconciled_cash=exiting)
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
                bool(account.open_order_count), tuple(dict.fromkeys(o.side for o in self.account.latest_orders))))
        return account

    async def _start(self):
        from .execution_models import MarketMetadata
        from .order_manager import MarketMakerOrderManager
        from .config import execution_settings
        from .execution_port import (
            VolumeExecutionPort, DryVolumeExecutionPort,
        )
        self.phase = "connecting"
        if await self._io(self.adapter.connect, 30) is not True:
            raise ValueError("connection unavailable")
        self.phase = "authenticating"
        if await self._io(self.adapter.authenticate) is not True:
            raise ValueError("authentication unavailable")
        if self._budget_active:
            # Native signer startup checks cannot be observed by Python. Let
            # their rolling minute expire before exposing the account to risk.
            self.phase = "api_quarantine_60s"
            await self._pause(60)
            if self._stop.is_set():
                raise TimeoutError
        stream_options = {"allow_delayed_dry_book": True} if self._allow_delayed_dry_book else {}
        self.phase = "opening_market_stream"
        open_stream = lambda: self.adapter.open_read_stream(self.config.symbol,
            clock=self.clock.monotonic, **stream_options)
        self.account.stream = self.market.stream = await self._io(open_stream)
        await self._io(self.market.initialize)
        self.phase = "checking_account"
        initial = await self._io(self.snapshot)
        if not initial.authenticated or initial.position or initial.open_order_ids != ():
            raise ValueError("authenticated flat empty start required")
        self.api_budget.account_profile = {
            "tier": self.account.account_tier, "symbol": self.config.symbol,
            "maker_fee_rate": str(initial.maker_fee_rate), "taker_fee_rate": str(initial.taker_fee_rate),
            "observed_monotonic": initial.terms_observed_monotonic or initial.observed_monotonic,
            "limits_source": "local_conservative_guard"}
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
            self.manager = MarketMakerOrderManager(self.adapter, execution_settings(cfg),
                metadata, monotonic=self.clock.monotonic, sleep=self.sleep,
                read_open_orders=self.account.read_execution_orders)
            await self._io(self.manager.initialize)
            self.adapter.enable_market_maker_cancellation_outcomes()
            self.adapter.set_market_maker_confirmation_reader(self.account.read_confirmation_orders)
            exit_account = SimpleNamespace(snapshot=lambda: self.snapshot(exiting=True))
            self.execution = VolumeExecutionPort(self.manager, exit_account, self.market, self.clock,
                authorize_bounded_flatten=True, refresh_quote=self._authorize,
                before_mutation=self._admit_mutation, before_cancel=self._admit_cancel,
                on_failure=self._diagnose,
                reprice_threshold_ticks=cfg.quote.reprice_threshold_ticks,
                max_quote_age_ms=cfg.quote.max_quote_age_ms)

    async def _authorize(self, exposure):
        deadline = self.clock.monotonic() + 10
        key = (self._known_ids(), self.manager.terminal_order_ids if self.manager else frozenset(), exposure.orders)
        cached = self._quote_account
        account = (cached[1] if cached and cached[0] == key and cached[1].fresh(self.clock.monotonic())
                   else await self._io(self.snapshot))
        if (not self.config.dry_run and not self._budget_exiting and self.manager
                and (exposure.orders is None or set(self.account.latest_orders) != set(exposure.orders))
                and self.manager.can_reconcile_known_orders):
            # A fill can arrive after the top-of-cycle OM sync. Consume the
            # completed account's owned-order proof once, then re-audit terminal
            # fills and risk. Never retry a mutation or reuse the obsolete plan.
            await self._io(self.manager.sync_open_orders,
                           timeout=deadline - self.clock.monotonic())
            exposure = self.execution.snapshot()
            account = await self._io(self.snapshot, timeout=deadline - self.clock.monotonic())
            key = (self._known_ids(), self.manager.terminal_order_ids, exposure.orders)
        self._quote_account = (key, account)
        await self._io(self.market.refresh, timeout=deadline - self.clock.monotonic())
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
        report = self.ledger.snapshot(now=now)
        report = replace(report, inventory_age=max(report.inventory_age, self.account.inventory_age_bound(now)))
        decision = self.governor.evaluate(market, risk_account, report,
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
        self._emit(decision)
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
        self.phase = "bounded_exit"
        self.cleanup_account = None
        self._budget_exiting, self._exit_read_retries = True, 0
        self._exit_funding_refreshes = 0
        if self._budget_active:
            allow_passive = False  # Optional grace has no reserved API envelope.
        self.account.begin_quote_cycle()
        self._cleanup_attempted = True
        deadline = self.governor.exit_deadline
        if deadline is None:
            deadline = min(self.clock.monotonic(), self.governor.session_deadline_monotonic) + 30
        if self._stop_at is not None:
            deadline = min(deadline, self._stop_at + 30)
        self._exit_sequence += 1
        self._exit_id = f"exit-{self._exit_sequence}"
        try:
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
            except (Exception, asyncio.CancelledError):
                # Grace is optional; preserve the same exit deadline and IOC budget.
                # Unknown mutations remain blocked by the execution port.
                pass
            self._passive_until = None
            if (self.execution.snapshot().health is ExecutionHealth.PAUSED_ORDER_STATE
                    and self.manager.can_reconcile_known_orders):
                # Known receipts and disappeared orders may need fresh evidence.
                # Empty adapter registries keep the generic resolver free of I/O.
                # Spend the existing retry allowance on one read-only sync;
                # cleanup still requires healthy, exact ownership afterward.
                self.phase = "exit_order_sync"
                self._admit_read("retry")
                await self._io(self.manager.sync_open_orders,
                               timeout=min(10, deadline - self.clock.monotonic()))
            self.phase = "bounded_exit"
            report = await bounded_exit(self.execution, self.market, self.clock,
                symbol=self.config.symbol, flatten_id=self._exit_id,
                deadline_monotonic=deadline, ioc_slippage_ticks=self.config.flatten.ioc_slippage_ticks,
                authorize_bounded_flatten=True, on_failure=self._diagnose)
        except (Exception, asyncio.CancelledError) as error:
            self._diagnose(self.phase, error)
            report = BoundedExitReport(self._exit_id, self.config.symbol,
                self.clock.monotonic(), ExitStatus.BLOCKED, 0)
        finally:
            self._passive_until = None
        self.ledger.record_exit(report)
        if report.complete:
            self.cleanup_account = report.final_result.account_snapshot
        if report.complete and self.governor.exit_deadline is not None:
            decision = self.governor.confirm_exit(report.final_result, now=self.clock.monotonic())
            self._session_complete = decision.state is StrategyState.SESSION_COMPLETE
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
                self.phase = "running"
                self._budget_exiting = False
                if cleaned and self._budget_active:
                    # A completed exit spent its reserved capacity. While
                    # authenticated flat/empty, let that rolling load expire
                    # before paying for another two-sided quote cycle. This
                    # re-entry headroom target is not a whole-cycle cost bound;
                    # every read/mutation still passes its own admission gate.
                    self.phase = "api_cooldown"
                    while (not self._stop.is_set()
                           and self.clock.monotonic() < self.governor.session_deadline_monotonic
                           and not self.api_budget.scheduled_live_available(
                               {"rest": 6000, "ws": 32, "tx": 4})):
                        await self._pause(1)
                    if self._stop.is_set() or self.clock.monotonic() >= self.governor.session_deadline_monotonic:
                        break
                try:
                    cycle_started, self._quote_account = self.clock.monotonic(), None
                    self.account.begin_quote_cycle()
                    if self.manager:
                        self.phase = "syncing_orders"
                        await self._io(self.manager.sync_open_orders)
                    self.phase = "authorizing_quotes"
                    auth = await self._authorize(self.execution.snapshot())
                    if auth.decision.state is StrategyState.SESSION_COMPLETE:
                        break
                    if auth.decision.state is StrategyState.FLATTENING:
                        if self.config.dry_run:
                            break
                        cleaned = await self._exit()
                        if not cleaned:
                            raise ValueError("bounded cleanup incomplete")
                        if self._session_complete:
                            break
                        # A nonterminal risk exit may cooldown, then quote again.
                        continue
                    cleaned = False
                    self.cleanup_account = None
                    self._cleanup_attempted = False
                    self._emit(auth.plan)
                    self.phase = "reconciling_quotes"
                    result = await self._io(lambda: self.execution.reconcile_quotes(auth.plan))
                    self._emit(result)
                    if result.status is ExecutionStatus.BLOCKED:
                        raise ValueError("execution blocked")
                    self.phase = "waiting"
                    interval = 5 if self._budget_active else 3
                    await self._pause(max(0, min(cycle_started + interval, self.governor.session_deadline_monotonic) - self.clock.monotonic()))
                except (ApiBudgetUnavailable, AccountReadRace) as error:
                    # Activity can outlast one coherent audit's bounded retry.
                    # Cash gaps (a subclass), invalid data, and unknown wire
                    # outcomes never become permission to resume normal work.
                    if (not self._budget_active or self._budget_exiting
                            or not self.manager.can_reconcile_known_orders):
                        raise
                    repeated_backpressure = False
                    if type(error) is AccountReadRace:
                        if self.account.stream is None or not self.account.stream.transport_healthy:
                            raise
                        self.api_budget.account_read_deferrals += 1
                    elif isinstance(error, ApiBudgetUnavailable):
                        repeated_backpressure = self.api_budget.record_backpressure_exit(
                            phase=self.phase, exit_id=f"exit-{self._exit_sequence + 1}", error=error) >= 3
                    else:
                        raise
                else:
                    continue
                # Cleanup failures are independent of the handled deferral;
                # leave its exception context before starting the exit.
                cleaned = await self._exit(allow_passive=False)
                if not cleaned:
                    raise ValueError("deferred normal work cleanup incomplete")
                if repeated_backpressure:
                    # Stop only after the existing bounded exit; finally still
                    # requires fresh authenticated 0/0 and exact accounting.
                    self._stop_reason = "api_backpressure_repeated"
                    break
                if self._session_complete:
                    break
        except (Exception, asyncio.CancelledError) as error:
            self._diagnose(self.phase, error)
            failure = "session_failed_closed"
            stop_event.set()
        finally:
            if self.execution:
                try:
                    if (failure and self.account.stream is not None
                            and not self.account.stream.transport_healthy):
                        # Exit-only REST proof after transport loss; market stays invalid.
                        self.phase = "exit_stream_close"
                        await self._io(self.adapter.close_read_stream)
                        self.account.stream = None
                    if self.config.dry_run:
                        result = await self._io(self.execution.cancel_all_managed)
                        self._emit(result)
                        cleaned = (result.status is ExecutionStatus.SIMULATED
                                   and result.snapshot.managed_order_count == 0)
                    elif not cleaned and not self._cleanup_attempted:
                        cleaned = await self._exit(allow_passive=False)
                    # A separate authenticated read after cleanup, never an ack.
                    self.phase = "final_account"
                    self._budget_exiting = True
                    self.final_account = await asyncio.wait_for(self.account.snapshot(), 10)
                    cleaned = bool(cleaned and self.final_account.authenticated
                        and not self.final_account.position and self.final_account.open_order_ids == ())
                except (Exception, asyncio.CancelledError) as error:
                    self._diagnose(self.phase, error)
                    cleaned = False
                    self.final_account = None
            try:
                self.phase = "finalizing_ledger"
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
            except Exception as error:
                self._diagnose(self.phase, error)
                failure = "accounting_unavailable"
            try:
                self.phase = "disconnecting"
                await asyncio.wait_for(self.adapter.disconnect(), 10)
            except (Exception, asyncio.CancelledError) as error:
                self._diagnose(self.phase, error)
                failure = "disconnect_unconfirmed"
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if not cleaned:
            failure = failure or "cleanup_unconfirmed"
        return SessionRunResult(self.config.dry_run, cleaned and failure is None,
                                report, self.final_account, failure, self._allow_delayed_dry_book,
                                self.cleanup_account,
                                self._stop_reason or getattr(self.governor, "stop_reason", None))
