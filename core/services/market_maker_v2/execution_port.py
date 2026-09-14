"""V2 market, account and execution ports with exact order safety boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Protocol
from .api_budget import ApiBudgetUnavailable
from .lighter_runtime import AccountReadRace

from .domain import (
    AccountSnapshot,
    ExecutionHealth,
    ExecutionResult,
    ExecutionSnapshot,
    ExecutionStatus,
    FlattenIntent,
    MarketStateSnapshot,
    QuotePlan,
    QuoteAuthorization,
    QuoteIntent,
    Side,
    StrategyState,
    TelemetryEvent,
    WorkingOrder,
    OrderEvidence,
    _count,
    _symbol,
    _time,
)

if TYPE_CHECKING:
    from .order_manager import MarketMakerOrderManager


class MarketDataPort(Protocol):
    def snapshot(self) -> MarketStateSnapshot: ...


class AccountPort(Protocol):
    async def snapshot(self) -> AccountSnapshot: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...


class ExecutionPort(Protocol):
    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult: ...

    async def cancel_all_managed(self) -> ExecutionResult: ...

    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult: ...

    def snapshot(self) -> ExecutionSnapshot: ...


class ExecutionUnavailable(RuntimeError):
    """Execution is unavailable; backend details are intentionally not exposed."""

    def __init__(self, message, *, values=None):
        super().__init__(message)
        self.diagnostic_values = values or {}


class BoundedExecutionPort:
    """One authorized IOC per call; the caller owns the whole-exit attempt cap.

    The existing manager retains cancellation and exact terminal semantics. A
    confirmed result includes a new authenticated residual, not a claim of flat.
    Normal quote execution belongs to VolumeExecutionPort.
    """

    def __init__(self, manager: MarketMakerOrderManager, account: AccountPort,
                 market: MarketDataPort, clock: Clock, *,
                 authorize_bounded_flatten: bool = False, on_failure=None, on_order_evidence=None,
                 before_cleanup_reconcile=None, before_cleanup_audit=None, pre_cleanup_account=None):
        if authorize_bounded_flatten is not True:
            raise ExecutionUnavailable("per-run bounded flatten authorization required")
        self.manager, self.account, self.market, self.clock = manager, account, market, clock
        self._on_failure = on_failure
        self._on_order_evidence = on_order_evidence
        self._before_cleanup_reconcile = before_cleanup_reconcile
        self._before_cleanup_audit = before_cleanup_audit
        self._pre_cleanup_account = pre_cleanup_account or account.snapshot
        try:
            self._symbol = manager.config.symbol
        except Exception:
            raise ExecutionUnavailable("authorized bounded execution unavailable") from None
        self._failed = False
        self._cleanup_only = False
        self._fault_generation = 0
        self._cancel_recovery_ids = frozenset()
        self._lock = asyncio.Lock()
        self._require_authorized_mode()

    def _diagnose(self, stage, error):
        if self._on_failure is not None:
            try:
                self._on_failure(stage, error)
            except Exception:
                pass

    def _halt(self, cancellation_ids=()):
        # Only this exact known-cancel failure may be cleared by terminal proof.
        # A generic snapshot, IOC or interrupted operation remains latched.
        recoverable = not self._failed or bool(self._cancel_recovery_ids)
        self._failed = True
        self._fault_generation += 1
        self._cancel_recovery_ids = frozenset()
        if cancellation_ids and recoverable:
            try:
                self._cancel_recovery_ids = frozenset(
                    slot.order_id for slot in self.manager.snapshot()
                    if slot.order_id in cancellation_ids and slot.cancellation_uncertain)
            except Exception:
                pass  # An unavailable manager cannot authorize recovery.

    @property
    def can_reconcile_cancellation(self):
        return bool(self._failed and self._cancel_recovery_ids
                    and self._cancel_recovery_ids <= self.manager.known_order_ids
                    and self.manager.can_reconcile_known_cancellations)

    async def reconcile_cancellation_for_cleanup(self, deadline):
        """One bounded proof operation; an active order never proves cancel failure."""
        self._require_authorized_mode()
        _time(deadline)
        deadline = min(deadline, self.clock.monotonic() + 10)
        async with self._lock:
            return await self._reconcile_cancellation_for_cleanup(deadline)

    async def _reconcile_cancellation_for_cleanup(self, deadline):
        pending = self._cancel_recovery_ids
        reads, pending_count = 0, len(pending)

        def failed(reason):
            try:
                values = {"recovery_reads": Decimal(reads),
                          "recovery_pending_count": Decimal(len(pending - self.manager.terminal_order_ids)),
                          "recovery_deadline_remaining_ms": Decimal(str(max(
                              0, deadline - self.clock.monotonic()))) * 1000,
                          reason: Decimal(1)}
                self._diagnose("exit_order_sync", ExecutionUnavailable(
                    "bounded cancellation proof unavailable", values=values))
            except Exception:
                pass  # Diagnostic availability cannot authorize or delay cleanup.
            return False

        try:
            eligible = self.can_reconcile_cancellation
            self._cancel_recovery_ids = frozenset()  # Consume this failure's one operation.
            if not eligible:
                return failed("recovery_ineligible")
            fault_generation = self._fault_generation
            generation, known = self.manager.mutation_generation, self.manager.known_order_ids

            def invalid_scope():
                if (self._fault_generation != fault_generation
                        or self.manager.mutation_generation != generation
                        or self.manager.known_order_ids != known):
                    return "recovery_scope_changed"
                if (not self.manager.can_reconcile_known_cancellations
                        or self.manager.has_unknown_order_state
                        or any(identifier not in pending for _, identifier
                               in self.manager.get_unresolved_cancellations())):
                    return "recovery_ineligible"
                return None

            # Keep reading within this operation's existing deadline. The cap
            # also bounds a stalled or synthetic clock; no mutation is retried.
            for attempt in range(20):
                if reason := invalid_scope():
                    return failed(reason)
                if attempt:
                    if deadline - self.clock.monotonic() <= 0.5:
                        return failed("recovery_deadline_exhausted")
                    await self._bounded(lambda: asyncio.sleep(0.5), deadline)
                    if reason := invalid_scope():
                        return failed(reason)
                if self.clock.monotonic() >= deadline:
                    return failed("recovery_deadline_exhausted")
                if self._before_cleanup_reconcile is not None:
                    self._before_cleanup_reconcile()
                if reason := invalid_scope():
                    return failed(reason)
                reads += 1
                await self._bounded(self.manager.sync_open_orders, deadline)
                if reason := invalid_scope():
                    return failed(reason)
                pending_count = len(pending - self.manager.terminal_order_ids)
                if pending_count:
                    continue  # Only missing proof permits another admitted read.
                if self.manager.has_uncertain_state or self.manager.get_unresolved_cancellations():
                    return failed("recovery_registry_pending")
                self._failed = False
                if self.snapshot().health is not ExecutionHealth.HEALTHY:
                    self._halt()
                    return failed("recovery_unhealthy")
                return True
            return failed("recovery_terminal_pending")
        except ApiBudgetUnavailable:
            failed("recovery_budget_refused")
            if reads:
                # Extra proof is optional. Preserve the original cancellation
                # failure when its additional read cannot be admitted.
                return False
            raise
        except asyncio.CancelledError:
            failed("recovery_read_failed")
            raise
        except TimeoutError:
            return failed("recovery_deadline_exhausted")
        except Exception:
            return failed("recovery_read_failed")

    def _record_order(self, order_id, side, price, size, reduce_only, tif, started, market, state):
        if self._on_order_evidence is not None:
            try:
                self._on_order_evidence(OrderEvidence(self._symbol, order_id, side, price, size,
                    reduce_only, tif, started, self.clock.monotonic(), market, state))
            except Exception:
                # Missing diagnostic coverage cannot interrupt known-order cleanup.
                # The analyzer leaves unlinked fills unclassified.
                pass

    def _require_authorized_mode(self):
        try:
            valid = (self.manager.config.dry_run is False
                     and self.manager.config.active_unwind_enabled is True
                     and self.manager.config.symbol == self._symbol)
        except Exception:
            valid = False
        if not valid:
            raise ExecutionUnavailable("authorized bounded execution unavailable")

    def snapshot(self) -> ExecutionSnapshot:
        self._require_authorized_mode()
        try:
            from .execution_models import OrderSlotState, RuntimeState

            managed = self.manager.snapshot()
            healthy_orders = all(order.simulated is False and order.order_id
                                 and order.state in {OrderSlotState.LIVE,
                                                     OrderSlotState.PARTIALLY_FILLED}
                                 for order in managed)
            if self._failed:
                health = ExecutionHealth.HALTED
            elif (not healthy_orders or self.manager.has_uncertain_state
                  or self.manager.has_unknown_order_state):
                health = ExecutionHealth.PAUSED_ORDER_STATE
            elif self.manager.runtime_state in {RuntimeState.SYNCING, RuntimeState.ACTIVE,
                                                RuntimeState.RISK_REDUCTION}:
                health = ExecutionHealth.HEALTHY
            elif self.manager.runtime_state in {RuntimeState.PAUSED_DATA, RuntimeState.PAUSED_MARKET,
                                                RuntimeState.PAUSED_POSITION, RuntimeState.PAUSED_EXCHANGE}:
                health = ExecutionHealth.PAUSED_DATA
            elif self.manager.runtime_state is RuntimeState.PAUSED_ORDER_STATE:
                health = ExecutionHealth.PAUSED_ORDER_STATE
            else:
                health = ExecutionHealth.HALTED
            if not healthy_orders:
                return ExecutionSnapshot(health, len(managed), False)
            orders = tuple(WorkingOrder(str(order.order_id), Side(order.side.value),
                                        order.remaining, order.price, order.reduce_only)
                           for order in managed)
            return ExecutionSnapshot(health, len(managed), False, symbol=self._symbol,
                                     observed_monotonic=self.clock.monotonic(), orders=orders)
        except Exception:
            self._halt()
            raise ExecutionUnavailable("bounded execution snapshot unavailable") from None

    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult:
        self._require_authorized_mode()
        raise ExecutionUnavailable("normal quote execution awaits Phase 6 wiring")

    async def _bounded(self, operation, deadline):
        remaining = deadline - self.clock.monotonic()
        if remaining <= 0:
            raise TimeoutError
        result = await asyncio.wait_for(operation(), timeout=remaining)
        if self.clock.monotonic() >= deadline:
            raise TimeoutError
        return result

    def _ioc_chunk(self, size):
        cap, step = self.manager.config.max_position, self.manager.metadata.quantity_step
        values = (size, cap, step)
        precision = (sum(len(v.as_tuple().digits) for v in values)
                     + max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values) + 16)
        if precision > 4096 or cap <= 0 or step <= 0:
            raise ExecutionUnavailable("bounded IOC quantity unavailable")
        with localcontext() as context:
            context.prec = max(context.prec, precision)
            return (min(size, cap) // step) * step

    async def _account_after(self, after, deadline):
        account = await self._bounded(self.account.snapshot, deadline)
        now = self.clock.monotonic()
        if (type(account) is not AccountSnapshot or account.symbol != self._symbol
                or not account.authenticated or account.open_order_count != 0
                or not after <= account.observed_monotonic <= now
                or not account.fresh(now)):
            raise ExecutionUnavailable("fresh authenticated zero-order account required")
        return account

    def _confirmed(self, account, *, submitted=0, cancelled=0):
        snapshot = self.snapshot()
        if snapshot.health is not ExecutionHealth.HEALTHY or snapshot.managed_order_count:
            raise ExecutionUnavailable("exact execution boundary unavailable")
        return ExecutionResult(ExecutionStatus.CONFIRMED, snapshot, submitted, cancelled,
                               account_snapshot=account)

    async def cancel_all_managed(self) -> ExecutionResult:
        self._require_authorized_mode()
        async with self._lock:
            before = self.snapshot()
            if before.health is not ExecutionHealth.HEALTHY:
                return ExecutionResult(ExecutionStatus.BLOCKED, before)
            terminal = False
            try:
                deadline = self.clock.monotonic() + 10
                if self._cleanup_only:
                    # A definite no-send keeps ownership, never permission to
                    # reuse old account truth. Pay for this extra audit before
                    # using the exit's existing cancellation allowance.
                    if self._before_cleanup_audit is not None:
                        self._before_cleanup_audit()
                    started = self.clock.monotonic()
                    generation, fault = self.manager.mutation_generation, self._fault_generation
                    known = self.manager.known_order_ids
                    await self._bounded(self.manager.sync_open_orders, deadline)
                    account = await self._bounded(self._pre_cleanup_account, deadline)
                    current = self.snapshot()
                    if (self._fault_generation != fault or self.manager.mutation_generation != generation
                            or self.manager.known_order_ids != known
                            or current.health is not ExecutionHealth.HEALTHY
                            or type(account) is not AccountSnapshot or account.symbol != self._symbol
                            or not account.authenticated or not account.fresh(self.clock.monotonic())
                            or not started <= account.observed_monotonic <= self.clock.monotonic()
                            or account.open_order_ids is None or current.orders is None
                            or set(account.open_order_ids) != {o.order_id for o in current.orders}):
                        raise ExecutionUnavailable("fresh owned-order account required before cleanup")
                    before = current
                expected = {o.order_id for o in before.orders}
                for attempt in range(2):
                    result = await self._bounded(
                        lambda: self.manager.cancel_managed_orders("v2 bounded cancellation"), deadline)
                    if not result.errors and expected <= self.manager.terminal_order_ids:
                        break
                    self._halt(expected)
                    if attempt or not await self._reconcile_cancellation_for_cleanup(deadline):
                        raise ExecutionUnavailable("managed cancellation not confirmed")
                    # Exact terminal recovery may leave another known side to
                    # cancel. Only that remaining side is touched, in the same 10s.
                    if expected <= self.manager.terminal_order_ids:
                        break
                terminal = True
                account = await self._account_after(self.clock.monotonic(), deadline)
                return self._confirmed(account, cancelled=before.managed_order_count)
            except asyncio.CancelledError:
                self._halt()
                raise
            except ApiBudgetUnavailable:
                if not terminal:
                    self._halt()
                raise
            except Exception as error:
                self._diagnose("cancel_managed_orders", error)
                if not terminal or isinstance(error, TimeoutError):
                    self._halt()
                return ExecutionResult(ExecutionStatus.BLOCKED, self.snapshot())

    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult:
        self._require_authorized_mode()
        if not isinstance(intent, FlattenIntent) or intent.symbol != self._symbol:
            raise ExecutionUnavailable("flatten symbol does not match execution")
        async with self._lock:
            before = self.snapshot()
            if before.health is not ExecutionHealth.HEALTHY:
                return ExecutionResult(ExecutionStatus.BLOCKED, before)
            previous_ids = self.manager.active_unwind_order_ids
            try:
                return await self._flatten_once(intent, before)
            except asyncio.CancelledError:
                self._halt()
                raise
            except Exception as error:
                self._diagnose("flatten_ioc", error)
                self._halt()
                return ExecutionResult(ExecutionStatus.BLOCKED, self.snapshot(),
                                       submitted_count=len(self.manager.active_unwind_order_ids - previous_ids))

    async def _flatten_once(self, intent, before):
        from ...adapters.exchanges.models import OrderSide
        from .execution_models import DesiredOrder

        deadline = intent.deadline_monotonic
        desired = DesiredOrder(OrderSide(intent.side.value), intent.limit_price,
                               self._ioc_chunk(intent.size), True, "v2 bounded exit")
        old_generation = self.manager.active_unwind_prepared_generation
        prepared = await self._bounded(lambda: self.manager.execute_active_unwind(desired), deadline)
        generation = self.manager.active_unwind_prepared_generation
        if (prepared.errors or type(generation) is not int or generation == old_generation
                or not {o.order_id for o in before.orders} <= self.manager.terminal_order_ids):
            raise ExecutionUnavailable("fresh cancellation preparation required")
        prepared_at = self.clock.monotonic()
        account = await self._account_after(prepared_at, deadline)
        if account.position == 0:
            return self._confirmed(account, cancelled=before.managed_order_count)
        reducing = Side.SELL if account.position > 0 else Side.BUY
        if reducing is not intent.side or account.position.copy_abs() > intent.size:
            raise ExecutionUnavailable("cancel race changed authorized inventory")
        market = self.market.snapshot()
        now = self.clock.monotonic()
        if (type(market) is not MarketStateSnapshot or market.symbol != self._symbol
                or not market.trusted
                or market.tick_size != self.manager.metadata.price_tick
                or market.size_step != self.manager.metadata.quantity_step):
            raise ExecutionUnavailable("trusted exit market metadata required")
        if (not prepared_at <= market.observed_monotonic <= now
                or now - market.observed_monotonic > 3):
            raise ExecutionUnavailable("post-preparation exit book required", values={
                "exit_book_age_ms": (Decimal(str(now)) - Decimal(str(market.observed_monotonic))) * 1000,
                "exit_book_after_prepare_ms": (Decimal(str(market.observed_monotonic))
                                              - Decimal(str(prepared_at))) * 1000})
        # A moving book can leave this fixed limit nonmarketable. IOC then
        # cancels its unfilled quantity; that is a known outcome, not execution
        # uncertainty. Preserve the limit and require exact terminal/account
        # proof below before the caller can attempt the remaining quantity.
        desired = replace(desired, amount=self._ioc_chunk(account.position.copy_abs()))
        previous_ids = self.manager.active_unwind_order_ids
        submitted_at = self.clock.monotonic()
        result = await self._bounded(
            lambda: self.manager.execute_active_unwind(desired, prepared_generation=generation), deadline)
        submitted_ids = self.manager.active_unwind_order_ids - previous_ids
        if (result.errors or len(submitted_ids) != 1 or self.manager.active_unwind_pending
                or not submitted_ids <= self.manager.terminal_order_ids):
            raise ExecutionUnavailable("exact IOC terminal evidence required")
        self._record_order(next(iter(submitted_ids)), intent.side, desired.price, desired.amount,
                           True, "IOC", submitted_at, market, StrategyState.FLATTENING)
        final = await self._account_after(self.clock.monotonic(), deadline)
        if (final.position.copy_abs() > account.position.copy_abs()
                or (final.position != 0 and (final.position > 0) != (account.position > 0))):
            raise ExecutionUnavailable("IOC residual does not match reduce-only execution")
        return self._confirmed(final, submitted=1, cancelled=before.managed_order_count)


def _validate_quote_authorization(value, execution, symbol, now, *, after=0, dry=False):
    """The refresh callback owns ledger/governor calculation and full order audit."""
    if type(value) is not QuoteAuthorization:
        raise ExecutionUnavailable("fresh typed quote authorization required")
    account, market, risk, plan = value.account, value.market, value.decision, value.plan
    _time(now)
    expected_ids = () if dry else tuple(order.order_id for order in execution.orders or ())
    if (account.symbol != symbol or market.symbol != symbol or plan.symbol != symbol
            or not account.authenticated or not market.trusted
            or not after <= account.observed_monotonic <= now
            or not account.fresh(now)
            or not 0 <= now - market.observed_monotonic <= 3
            or account.open_order_ids is None
            or set(account.open_order_ids) != set(expected_ids)
            or account.open_order_count != len(expected_ids)
            or execution.orders is None or execution.health is not ExecutionHealth.HEALTHY
            or (dry and account.position != 0)):
        raise ExecutionUnavailable("fresh coherent quote/account/order truth required")
    inactive = risk.state in {StrategyState.FLATTENING, StrategyState.COOLDOWN,
                              StrategyState.SESSION_COMPLETE}
    if inactive and plan.quotes:
        raise ExecutionUnavailable("inactive inventory cannot authorize quotes")
    values = [market.tick_size, market.size_step, market.min_order_size,
              market.external_bid, market.external_ask, account.position,
              risk.buy_capacity, risk.sell_capacity]
    values += [value for quote in plan.quotes for value in (quote.price, quote.size)]
    precision = (sum(len(v.as_tuple().digits) for v in values)
                 + max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values) + 16)
    if precision > 4096:
        raise ExecutionUnavailable("quote authorization precision exceeds supported range")
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        for quote in plan.quotes:
            capacity = risk.buy_capacity if quote.side is Side.BUY else risk.sell_capacity
            reducing = account.position < 0 if quote.side is Side.BUY else account.position > 0
            passive = (quote.price < market.external_ask if quote.side is Side.BUY
                       else quote.price > market.external_bid)
            if (quote.size > capacity or quote.price % market.tick_size
                    or quote.size % market.size_step or quote.size < market.min_order_size
                    or not passive or (quote.reduce_only and (not reducing or quote.size > abs(account.position)))
                    or (risk.state is StrategyState.REDUCE_ONLY and not quote.reduce_only)):
                raise ExecutionUnavailable("quote exceeds fresh inventory/passive/lot authority")
    return value


def _quote_revision(orders, created, authorization, now, threshold, max_age):
    targets = {quote.side: quote for quote in authorization.plan.quotes}
    market = authorization.market
    revised = set()
    for order in orders:
        target = targets.get(order.side)
        if now < created[order.order_id]:
            raise ExecutionUnavailable("working quote clock moved backwards")
        passive = (order.price < market.external_ask if order.side is Side.BUY
                   else order.price > market.external_bid)
        if (target is None or not passive or target.reduce_only != order.reduce_only
                or target.size != order.remaining_size
                or abs(order.price - target.price) >= market.tick_size * threshold
                or (now - created[order.order_id]) * 1000 >= max_age):
            revised.add(order.side)
    retained = {order.side: order.price for order in orders if order.side not in revised}
    prices = {side: retained.get(side, target.price) for side, target in targets.items()}
    if len(prices) == 2 and prices[Side.BUY] >= prices[Side.SELL]:
        revised.update(retained)
    return revised


class VolumeExecutionPort(BoundedExecutionPort):
    """Fresh V2 permissions around exact order execution.

    refresh_quote(snapshot) must audit exact authenticated working orders, ingest
    fills, and recompute ledger/governor/policy. It runs again after cancellation.
    A caller's QuotePlan alone never grants placement permission.
    """

    def __init__(self, manager, account, market, clock, *, refresh_quote,
                 reprice_threshold_ticks: int, max_quote_age_ms: int,
                 authorize_bounded_flatten: bool = False, before_mutation=None, before_cancel=None,
                 on_failure=None, before_optional_revision=None, on_optional_refusal=None,
                 on_order_evidence=None, before_cleanup_reconcile=None, before_cleanup_audit=None,
                 pre_cleanup_account=None):
        for value in (reprice_threshold_ticks, max_quote_age_ms):
            _count(value)
            if value == 0:
                raise ValueError("positive quote revision bounds required")
        if not callable(refresh_quote):
            raise ValueError("quote refresh callback required")
        super().__init__(manager, account, market, clock,
                         authorize_bounded_flatten=authorize_bounded_flatten, on_failure=on_failure,
                         on_order_evidence=on_order_evidence,
                         before_cleanup_reconcile=before_cleanup_reconcile,
                         before_cleanup_audit=before_cleanup_audit, pre_cleanup_account=pre_cleanup_account)
        if manager.config.post_only is not True:
            raise ExecutionUnavailable("normal volume quotes require POST_ONLY")
        self.refresh_quote = refresh_quote
        self.before_mutation = before_mutation
        self.before_cancel = before_cancel
        self.before_optional_revision = before_optional_revision
        self.on_optional_refusal = on_optional_refusal
        self.reprice_threshold_ticks, self.max_quote_age_ms = reprice_threshold_ticks, max_quote_age_ms
        self._maker_fee = None
        self._post_only_refresh = (0, 0.0)

    def _admit_cancel(self, count):
        if count:
            if self.before_cancel is not None:
                self.before_cancel(count)
            elif self.before_mutation is not None:
                self.before_mutation()

    async def _fresh_quote(self, execution, deadline, after):
        value = await self._bounded(lambda: self.refresh_quote(execution), deadline)
        current = self.snapshot()
        if current.orders != execution.orders:
            previous = {row.order_id: row for row in execution.orders or ()}
            reduced = current.orders is not None and all(
                row.order_id in previous and row.side == previous[row.order_id].side
                and row.price == previous[row.order_id].price
                and row.reduce_only == previous[row.order_id].reduce_only
                and row.remaining_size <= previous[row.order_id].remaining_size
                for row in current.orders)
            removed = previous.keys() - {row.order_id for row in current.orders or ()}
            if not reduced or not removed <= self.manager.terminal_order_ids:
                raise ExecutionUnavailable("working orders changed during quote refresh")
        value = _validate_quote_authorization(value, current, self._symbol,
                                              self.clock.monotonic(), after=after)
        if (value.market.tick_size != self.manager.metadata.price_tick
                or value.market.size_step != self.manager.metadata.quantity_step):
            raise ExecutionUnavailable("quote metadata differs from execution")
        return value

    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult:
        self._require_authorized_mode()
        if type(plan) is not QuotePlan or plan.symbol != self._symbol:
            raise ExecutionUnavailable("quote symbol does not match execution")
        if self.manager.config.post_only is not True:
            raise ExecutionUnavailable("normal volume quotes require POST_ONLY")
        if not plan.quotes:
            self._admit_cancel(len(self.manager.snapshot()))
            return await self.cancel_all_managed()
        async with self._lock:
            before = self.snapshot()
            if before.health is not ExecutionHealth.HEALTHY or self._cleanup_only:
                return ExecutionResult(ExecutionStatus.BLOCKED, before)
            submitted = cancelled = 0
            try:
                deadline = self.clock.monotonic() + 10
                first = await self._reconcile_volume(deadline)
                submitted, cancelled = first.submitted_count, first.cancelled_count
                desired = {q.side for q in first.actual_plan.quotes} if first.actual_plan else set()
                working = {o.side for o in first.snapshot.orders or ()}
                if (first.status is not ExecutionStatus.CONFIRMED or not first.submitted_count
                        or desired <= working):
                    return first
                # The manager still creates one order per call. Read back and reauthorize
                # before filling the other side, within the original cycle deadline.
                second = await self._reconcile_volume(deadline, after=self.clock.monotonic())
                return replace(second, submitted_count=first.submitted_count + second.submitted_count,
                               cancelled_count=first.cancelled_count + second.cancelled_count)
            except asyncio.CancelledError:
                # The manager latches uncertainty if cancellation interrupted a mutation.
                # Cancelling a read must not disable known-order cleanup.
                raise
            except (ApiBudgetUnavailable, AccountReadRace):
                # The session distinguishes activity lag from fatal cash gaps;
                # neither can authorize another order without a complete audit.
                raise
            except Exception as error:
                self._diagnose("reconciling_quotes", error)
                # A pre-mutation data refusal must still permit known-safe cleanup.
                return ExecutionResult(ExecutionStatus.BLOCKED, self.snapshot(), submitted, cancelled)

    def _capture_post_only_rejection(self):
        count, generation = self.manager.consume_post_only_cancellations()
        if count:
            self._post_only_refresh = (generation, self.clock.monotonic())

    def _retained_orders_safe(self, authorization, *, sides=None):
        """Existing orders need current risk permission, not a new-order minimum."""
        execution, now = self.snapshot(), self.clock.monotonic()
        try:
            _validate_quote_authorization(authorization, execution, self._symbol, now)
        except ExecutionUnavailable:
            return False
        risk, market, account = authorization.decision, authorization.market, authorization.account
        if risk.state not in {StrategyState.QUOTING, StrategyState.SKEWED, StrategyState.REDUCE_ONLY}:
            return False
        if execution.orders and account.maker_fee_rate != self._maker_fee:
            return False
        targets = {quote.side: quote for quote in authorization.plan.quotes}
        created = {str(order.order_id): order.created_monotonic for order in self.manager.snapshot()}
        for order in execution.orders:
            if sides is not None and order.side not in sides:
                continue
            target = targets.get(order.side)
            capacity = risk.buy_capacity if order.side is Side.BUY else risk.sell_capacity
            reducing = account.position < 0 if order.side is Side.BUY else account.position > 0
            passive = order.price < market.external_ask if order.side is Side.BUY else order.price > market.external_bid
            age = now - created[order.order_id]
            if (target is None or target.reduce_only != order.reduce_only or not passive
                    or not 0 <= age * 1000 < self.max_quote_age_ms
                    or order.remaining_size > capacity
                    or (order.reduce_only and (not reducing or order.remaining_size > abs(account.position)))
                    or (risk.state is StrategyState.REDUCE_ONLY and not order.reduce_only)):
                return False
        return True

    def _defer_optional(self, authorization, cancelled):
        if self.on_optional_refusal is None or not self._retained_orders_safe(authorization):
            return None
        # The session checks stop/deadline and the next monitor plus exit budget.
        # This is not permission to skip a future account/market/risk audit.
        self.on_optional_refusal(authorization)
        snapshot = self.snapshot()
        actual = QuotePlan(self._symbol, tuple(QuoteIntent(order.side, order.price,
            order.remaining_size, order.reduce_only) for order in snapshot.orders))
        return ExecutionResult(ExecutionStatus.DEFERRED, snapshot, cancelled_count=cancelled,
                               account_snapshot=authorization.account, actual_plan=actual)

    async def _reconcile_volume(self, deadline, *, after=0):
        await self._bounded(self.manager.sync_open_orders, deadline)
        self._capture_post_only_rejection()
        execution = self.snapshot()
        if (execution.health is ExecutionHealth.PAUSED_ORDER_STATE
                and self.manager.can_reconcile_known_orders):
            # A known order may disappear before its terminal history arrives.
            # One additional read uses the same quote deadline and normal API
            # admission; only exact proof can restore a healthy execution state.
            await self._bounded(self.manager.sync_open_orders, deadline)
            self._capture_post_only_rejection()
            execution = self.snapshot()
        if execution.health is not ExecutionHealth.HEALTHY:
            return ExecutionResult(ExecutionStatus.BLOCKED, execution)
        authorization = await self._fresh_quote(execution, deadline, after)
        execution = self.snapshot()  # Refresh may have proven a concurrent fill.
        generation, rejected_at = self._post_only_refresh
        if generation and authorization.market.observed_monotonic > rejected_at:
            self.manager.acknowledge_post_only_book_refresh(generation)
        cancelled = 0
        # A one-side revision must not discard a still-authorized opposite order.
        # Cancellation can change inventory; recheck the retained side before
        # any create. At most two existing sides can need cancellation.
        for _ in range(2):
            managed = self.manager.snapshot()
            created = {str(order.order_id): order.created_monotonic for order in managed}
            revision = _quote_revision(execution.orders, created, authorization, self.clock.monotonic(),
                                       self.reprice_threshold_ticks, self.max_quote_age_ms)
            if managed and authorization.account.maker_fee_rate != self._maker_fee:
                revision.update(order.side for order in execution.orders)
            if not revision:
                break
            # Revoke unsafe sides first. A required cancellation must not turn
            # a safe opposite-side reprice into mandatory work and bypass its
            # budget preflight. The next iteration reauthorizes that side after
            # exact cancellation proof, within this same quote deadline.
            mandatory = {side for side in revision
                         if not self._retained_orders_safe(authorization, sides={side})}
            if mandatory:
                revision = mandatory
            elif len(execution.orders) == 1:
                # Restore a missing side before optional work on a safe quote.
                # Repricing would pay cancellation and another account audit
                # before the create gate can even consider the missing side.
                # Its fresh intent must also remain compatible with the actual
                # retained price; the normal create admission below still runs.
                retained = execution.orders[0]
                missing = next((quote for quote in authorization.plan.quotes
                                if quote.side is not retained.side), None)
                if missing is not None and (retained.price < missing.price
                        if retained.side is Side.BUY else missing.price < retained.price):
                    break
            from ...adapters.exchanges.models import OrderSide
            selected = {order.order_id for order in execution.orders if order.side in revision}
            if self.before_optional_revision is not None and self._retained_orders_safe(authorization):
                try:
                    self.before_optional_revision(len(selected))
                except ApiBudgetUnavailable:
                    deferred = self._defer_optional(authorization, cancelled)
                    if deferred is None:
                        raise
                    return deferred
            self._admit_cancel(len(selected))
            fault = self._fault_generation
            result = await self._bounded(
                lambda: self.manager.cancel_managed_orders("v2 quote revision",
                    sides=frozenset(OrderSide(side.value) for side in revision)), deadline)
            execution = self.snapshot()
            if (result.errors or not selected <= self.manager.terminal_order_ids
                    or execution.orders is None
                    or any(order.side in revision for order in execution.orders)):
                # A typed no-send did not alter the live order. Preserve only
                # cleanup, with a fresh audit; never restart normal quotation.
                if self._only_cancel_not_sent(result, selected, execution, fault):
                    self._cleanup_only = True
                else:
                    self._halt(selected)
                raise ExecutionUnavailable("quote cancellation lacks exact terminal proof")
            cancelled += len(selected)
            authorization = await self._fresh_quote(execution, deadline, self.clock.monotonic())
            execution = self.snapshot()
        self._maker_fee = authorization.account.maker_fee_rate
        # Keep the exact retained price and remaining amount, including when
        # restoring a missing side takes priority over an optional revision.
        # A changed target here could cause hidden cancellation without audit.
        retained = {order.side: order for order in execution.orders}
        effective = QuotePlan(self._symbol, tuple(
            QuoteIntent(quote.side, retained[quote.side].price,
                        retained[quote.side].remaining_size, quote.reduce_only)
            if quote.side in retained else quote for quote in authorization.plan.quotes))
        execution_plan, execution_risk = self._execution_quotes(effective, authorization)
        if self.before_mutation is not None and any(q.side not in retained for q in effective.quotes):
            try:
                self.before_mutation()
            except ApiBudgetUnavailable:
                deferred = self._defer_optional(authorization, cancelled)
                if deferred is None:
                    raise
                return deferred
        submitted_at = self.clock.monotonic()
        result = await self._bounded(lambda: self.manager.reconcile(execution_plan, execution_risk), deadline)
        for action in result.actions:
            if action.operation == "place" and action.order_id in self.manager.known_order_ids:
                self._record_order(action.order_id, Side(action.side.value), action.price, action.amount,
                    action.reduce_only, "POST_ONLY", submitted_at, authorization.market,
                    authorization.decision.state)
        self._capture_post_only_rejection()
        snapshot = self.snapshot()
        status = (ExecutionStatus.BLOCKED if result.errors or snapshot.health is not ExecutionHealth.HEALTHY
                  else ExecutionStatus.CONFIRMED)
        return ExecutionResult(status, snapshot,
            submitted_count=sum(action.operation == "place" and action.success is True for action in result.actions),
            cancelled_count=cancelled, actual_plan=effective)

    def _only_cancel_not_sent(self, result, selected, execution, fault):
        from .order_manager import ReconcileAction, ReconcileResult

        if (type(result) is not ReconcileResult or not result.errors or self._failed
                or self._fault_generation != fault or execution.health is not ExecutionHealth.HEALTHY):
            return False
        failed = [action for action in result.actions if type(action) is ReconcileAction
                  and action.cancellation_not_sent is True and action.success is False]
        if len(failed) != len(result.errors):
            return False
        live = {order.order_id for order in execution.orders or ()}
        return (all(action.order_id in selected & live for action in failed)
                and all(type(action) is ReconcileAction and action.operation == "cancel"
                        and action.order_id in selected
                        and (action in failed or action.order_id in self.manager.terminal_order_ids)
                        for action in result.actions))

    @staticmethod
    def _execution_quotes(plan, authorization):
        from ...adapters.exchanges.models import OrderSide
        from .execution_models import DesiredOrder, DesiredQuotes, RuntimeState
        from .execution_models import RiskDecision

        risk, account, market = authorization.decision, authorization.account, authorization.market
        state = RuntimeState.RISK_REDUCTION if risk.state is StrategyState.REDUCE_ONLY else RuntimeState.ACTIVE
        orders = {quote.side: DesiredOrder(OrderSide(quote.side.value), quote.price, quote.size,
                                          quote.reduce_only, "v2 authorized quote") for quote in plan.quotes}
        buy, sell = orders.get(Side.BUY), orders.get(Side.SELL)
        zero = Decimal("0")
        desired = DesiredQuotes(buy, sell, market.external_bid, market.external_bid, zero,
                                account.position, state, "v2 authorized quote")
        decision = RiskDecision(buy.amount if buy else None, sell.amount if sell else None,
            buy.reduce_only if buy else False, sell.reduce_only if sell else False,
            risk.buy_capacity, risk.sell_capacity, zero, zero, account.position, state,
            "v2 inventory permission", True)
        return desired, decision


class DryVolumeExecutionPort:
    """Local quote-intent model only: no adapter, exchange fills or flat claims."""

    def __init__(self, symbol, clock, *, refresh_quote,
                 reprice_threshold_ticks: int, max_quote_age_ms: int):
        _symbol(symbol)
        for value in (reprice_threshold_ticks, max_quote_age_ms):
            _count(value)
            if value == 0:
                raise ValueError("positive quote revision bounds required")
        self.symbol, self.clock, self.refresh_quote = symbol, clock, refresh_quote
        self.reprice_threshold_ticks, self.max_quote_age_ms = reprice_threshold_ticks, max_quote_age_ms
        self._orders, self._created, self._sequence = (), {}, 0
        self._maker_fee = None

    def snapshot(self):
        return ExecutionSnapshot(ExecutionHealth.HEALTHY, len(self._orders), True,
                                 self.symbol, self.clock.monotonic(), self._orders)

    async def reconcile_quotes(self, plan):
        if type(plan) is not QuotePlan or plan.symbol != self.symbol:
            raise ExecutionUnavailable("quote symbol does not match dry execution")
        if not plan.quotes:
            return await self.cancel_all_managed()
        authorization = await self.refresh_quote(self.snapshot())
        _validate_quote_authorization(authorization, self.snapshot(), self.symbol,
                                       self.clock.monotonic(), dry=True)
        now = self.clock.monotonic()
        revision = _quote_revision(self._orders, self._created, authorization, now,
                                   self.reprice_threshold_ticks, self.max_quote_age_ms)
        if self._orders and authorization.account.maker_fee_rate != self._maker_fee:
            revision.update(order.side for order in self._orders)
        self._maker_fee = authorization.account.maker_fee_rate
        cancelled = sum(order.side in revision for order in self._orders)
        previous = {order.side: order for order in self._orders if order.side not in revision}
        result, submitted = [], 0
        for quote in authorization.plan.quotes:
            order = previous.get(quote.side)
            if order is None:
                self._sequence += 1
                order = WorkingOrder(f"dry-v2-{self._sequence}", quote.side, quote.size,
                                     quote.price, quote.reduce_only)
                self._created[order.order_id] = now
                submitted += 1
            result.append(order)
        self._orders = tuple(result)
        self._created = {order.order_id: self._created[order.order_id] for order in self._orders}
        effective = QuotePlan(self.symbol, tuple(QuoteIntent(o.side, o.price, o.remaining_size,
                                                           o.reduce_only) for o in self._orders))
        return ExecutionResult(ExecutionStatus.SIMULATED, self.snapshot(), submitted, cancelled,
                               actual_plan=effective)

    async def cancel_all_managed(self):
        count = len(self._orders)
        self._orders, self._created = (), {}
        return ExecutionResult(ExecutionStatus.SIMULATED, self.snapshot(), cancelled_count=count,
                               actual_plan=QuotePlan(self.symbol))

    async def flatten_ioc(self, intent):
        raise ExecutionUnavailable("dry execution cannot create or simulate IOC fills")
