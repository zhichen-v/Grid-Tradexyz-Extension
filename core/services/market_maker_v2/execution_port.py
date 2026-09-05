"""V2 market, account and execution ports with exact order safety boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Protocol

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
    _count,
    _symbol,
    _time,
)

if TYPE_CHECKING:
    from .order_manager import MarketMakerOrderManager, ReconcileResult


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


class DrySafetyExecutionPort:
    """Exercise the V2 order manager without enabling trading.

    Supports empty quote plans and simulated cancellation only. Normal quoting
    and bounded flatten use the dedicated volume and bounded execution ports.
    A simulated result is not authenticated account or terminal-flat evidence.
    """

    def __init__(self, manager: MarketMakerOrderManager):
        self.manager = manager
        self._require_dry()

    def _require_dry(self) -> str:
        try:
            dry = self.manager.config.dry_run is True
            symbol = self.manager.config.symbol
        except Exception:
            raise ExecutionUnavailable("dry execution unavailable") from None
        if not dry:
            raise ExecutionUnavailable("dry execution unavailable")
        return symbol

    def snapshot(self) -> ExecutionSnapshot:
        self._require_dry()
        try:
            from .execution_models import RuntimeState

            managed = self.manager.snapshot()
            simulated = all(order.simulated is True for order in managed)
            if not simulated or self.manager.has_uncertain_state or self.manager.has_unknown_order_state:
                health = ExecutionHealth.PAUSED_ORDER_STATE
            elif self.manager.runtime_state in {
                RuntimeState.SYNCING, RuntimeState.ACTIVE, RuntimeState.RISK_REDUCTION,
            }:
                health = ExecutionHealth.HEALTHY
            elif self.manager.runtime_state in {
                RuntimeState.PAUSED_DATA, RuntimeState.PAUSED_MARKET,
                RuntimeState.PAUSED_POSITION, RuntimeState.PAUSED_EXCHANGE,
            }:
                health = ExecutionHealth.PAUSED_DATA
            elif self.manager.runtime_state is RuntimeState.PAUSED_ORDER_STATE:
                health = ExecutionHealth.PAUSED_ORDER_STATE
            else:
                health = ExecutionHealth.HALTED
            return ExecutionSnapshot(health, len(managed), simulated=simulated)
        except Exception:
            raise ExecutionUnavailable("dry execution snapshot unavailable") from None

    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult:
        symbol = self._require_dry()
        if not isinstance(plan, QuotePlan) or plan.symbol != symbol:
            raise ExecutionUnavailable("quote symbol does not match execution")
        if plan.quotes:
            raise ExecutionUnavailable("Phase 2 supports empty dry plans only")
        snapshot = self.snapshot()
        if snapshot.health is not ExecutionHealth.HEALTHY:
            return ExecutionResult(ExecutionStatus.BLOCKED, snapshot)
        from .execution_models import DesiredQuotes, RuntimeState
        from .execution_models import RiskDecision

        zero = Decimal("0")
        desired = DesiredQuotes(
            bid=None, ask=None, reference_price=zero, reservation_price=zero,
            half_spread=zero, inventory_ratio=zero, runtime_state=RuntimeState.ACTIVE,
            reason="v2 empty quote plan",
        )
        risk = RiskDecision(
            buy_amount=None, sell_amount=None, buy_reduce_only=False,
            sell_reduce_only=False, buy_capacity=zero, sell_capacity=zero,
            worst_long=zero, worst_short=zero, inventory_ratio=zero,
            runtime_state=RuntimeState.ACTIVE, reason="v2 no new orders", safe=True,
        )
        try:
            result = await self.manager.reconcile(desired, risk)
            return self._result(result)
        except Exception:
            raise ExecutionUnavailable("dry quote reconciliation unavailable") from None

    async def cancel_all_managed(self) -> ExecutionResult:
        self._require_dry()
        snapshot = self.snapshot()
        if snapshot.health is not ExecutionHealth.HEALTHY or not snapshot.simulated:
            return ExecutionResult(ExecutionStatus.BLOCKED, snapshot)
        try:
            result = await self.manager.cancel_managed_orders("v2 dry cancellation")
            return self._result(result)
        except Exception:
            raise ExecutionUnavailable("dry cancellation unavailable") from None

    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult:
        symbol = self._require_dry()
        if not isinstance(intent, FlattenIntent) or intent.symbol != symbol:
            raise ExecutionUnavailable("flatten symbol does not match execution")
        raise ExecutionUnavailable("Bounded flatten is not implemented")

    def _result(self, result: ReconcileResult) -> ExecutionResult:
        snapshot = self.snapshot()
        status = (
            ExecutionStatus.BLOCKED
            if result.errors or snapshot.health is not ExecutionHealth.HEALTHY
            else ExecutionStatus.SIMULATED
        )
        return ExecutionResult(
            status, snapshot, submitted_count=0,
            cancelled_count=sum(action.operation == "would_cancel" for action in result.actions),
        )


class BoundedExecutionPort:
    """One authorized IOC per call; the caller owns the whole-exit attempt cap.

    The existing manager retains cancellation and exact terminal semantics. A
    confirmed result includes a new authenticated residual, not a claim of flat.
    Normal quote execution belongs to VolumeExecutionPort.
    """

    def __init__(self, manager: MarketMakerOrderManager, account: AccountPort,
                 market: MarketDataPort, clock: Clock, *,
                 authorize_bounded_flatten: bool = False):
        if authorize_bounded_flatten is not True:
            raise ExecutionUnavailable("per-run bounded flatten authorization required")
        self.manager, self.account, self.market, self.clock = manager, account, market, clock
        try:
            self._symbol = manager.config.symbol
        except Exception:
            raise ExecutionUnavailable("authorized bounded execution unavailable") from None
        self._failed = False
        self._lock = asyncio.Lock()
        self._require_authorized_mode()

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
            self._failed = True
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
            try:
                deadline = self.clock.monotonic() + 10
                result = await self._bounded(
                    lambda: self.manager.cancel_managed_orders("v2 bounded cancellation"), deadline)
                if (result.errors or not {o.order_id for o in before.orders}
                        <= self.manager.terminal_order_ids):
                    raise ExecutionUnavailable("managed cancellation not confirmed")
                account = await self._account_after(self.clock.monotonic(), deadline)
                return self._confirmed(account, cancelled=before.managed_order_count)
            except asyncio.CancelledError:
                self._failed = True
                raise
            except Exception:
                self._failed = True
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
                self._failed = True
                raise
            except Exception:
                self._failed = True
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
                or not market.trusted or not prepared_at <= market.observed_monotonic <= now
                or now - market.observed_monotonic > 3
                or market.tick_size != self.manager.metadata.price_tick
                or market.size_step != self.manager.metadata.quantity_step
                or (intent.side is Side.BUY and market.external_ask > intent.limit_price)
                or (intent.side is Side.SELL and market.external_bid < intent.limit_price)):
            raise ExecutionUnavailable("fresh bounded executable market required")
        desired = replace(desired, amount=self._ioc_chunk(account.position.copy_abs()))
        previous_ids = self.manager.active_unwind_order_ids
        result = await self._bounded(
            lambda: self.manager.execute_active_unwind(desired, prepared_generation=generation), deadline)
        submitted_ids = self.manager.active_unwind_order_ids - previous_ids
        if (result.errors or len(submitted_ids) != 1 or self.manager.active_unwind_pending
                or not submitted_ids <= self.manager.terminal_order_ids):
            raise ExecutionUnavailable("exact IOC terminal evidence required")
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
            return True
    retained = {order.side: order.price for order in orders}
    prices = {side: retained.get(side, target.price) for side, target in targets.items()}
    return len(prices) == 2 and prices[Side.BUY] >= prices[Side.SELL]


class VolumeExecutionPort(BoundedExecutionPort):
    """Fresh V2 permissions around exact order execution.

    refresh_quote(snapshot) must audit exact authenticated working orders, ingest
    fills, and recompute ledger/governor/policy. It runs again after cancellation.
    A caller's QuotePlan alone never grants placement permission.
    """

    def __init__(self, manager, account, market, clock, *, refresh_quote,
                 reprice_threshold_ticks: int, max_quote_age_ms: int,
                 authorize_bounded_flatten: bool = False):
        for value in (reprice_threshold_ticks, max_quote_age_ms):
            _count(value)
            if value == 0:
                raise ValueError("positive quote revision bounds required")
        if not callable(refresh_quote):
            raise ValueError("quote refresh callback required")
        super().__init__(manager, account, market, clock,
                         authorize_bounded_flatten=authorize_bounded_flatten)
        if manager.config.post_only is not True:
            raise ExecutionUnavailable("normal volume quotes require POST_ONLY")
        self.refresh_quote = refresh_quote
        self.reprice_threshold_ticks, self.max_quote_age_ms = reprice_threshold_ticks, max_quote_age_ms
        self._maker_fee = None
        self._post_only_refresh = (0, 0.0)

    async def _fresh_quote(self, execution, deadline, after):
        value = await self._bounded(lambda: self.refresh_quote(execution), deadline)
        current = self.snapshot()
        if current.orders != execution.orders:
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
            return await self.cancel_all_managed()
        async with self._lock:
            before = self.snapshot()
            if before.health is not ExecutionHealth.HEALTHY:
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
            except Exception:
                # A pre-mutation data refusal must still permit known-safe cleanup.
                return ExecutionResult(ExecutionStatus.BLOCKED, self.snapshot(), submitted, cancelled)

    def _capture_post_only_rejection(self):
        count, generation = self.manager.consume_post_only_cancellations()
        if count:
            self._post_only_refresh = (generation, self.clock.monotonic())

    async def _reconcile_volume(self, deadline, *, after=0):
        await self._bounded(self.manager.sync_open_orders, deadline)
        self._capture_post_only_rejection()
        execution = self.snapshot()
        if execution.health is not ExecutionHealth.HEALTHY:
            return ExecutionResult(ExecutionStatus.BLOCKED, execution)
        authorization = await self._fresh_quote(execution, deadline, after)
        generation, rejected_at = self._post_only_refresh
        if generation and authorization.market.observed_monotonic > rejected_at:
            self.manager.acknowledge_post_only_book_refresh(generation)
        managed = self.manager.snapshot()
        created = {str(order.order_id): order.created_monotonic for order in managed}
        revision = _quote_revision(execution.orders, created, authorization, self.clock.monotonic(),
                                   self.reprice_threshold_ticks, self.max_quote_age_ms)
        revision |= bool(managed and authorization.account.maker_fee_rate != self._maker_fee)
        cancelled = 0
        if managed and revision:
            result = await self._bounded(
                lambda: self.manager.cancel_managed_orders("v2 quote revision"), deadline)
            if (result.errors or not set(created) <= self.manager.terminal_order_ids
                    or self.manager.snapshot()):
                self._failed = True
                raise ExecutionUnavailable("quote cancellation lacks exact terminal proof")
            cancelled = len(managed)
            execution = self.snapshot()
            authorization = await self._fresh_quote(execution, deadline, self.clock.monotonic())
        self._maker_fee = authorization.account.maker_fee_rate
        # Keep proven working prices below the revision threshold. Passing a new
        # target to the manager here could cause hidden cancellation without our fresh audit.
        retained = {order.side: order for order in execution.orders}
        effective = QuotePlan(self._symbol, tuple(
            QuoteIntent(quote.side, retained[quote.side].price,
                        retained[quote.side].remaining_size, quote.reduce_only)
            if quote.side in retained else quote for quote in authorization.plan.quotes))
        execution_plan, execution_risk = self._execution_quotes(effective, authorization)
        result = await self._bounded(lambda: self.manager.reconcile(execution_plan, execution_risk), deadline)
        self._capture_post_only_rejection()
        snapshot = self.snapshot()
        status = (ExecutionStatus.BLOCKED if result.errors or snapshot.health is not ExecutionHealth.HEALTHY
                  else ExecutionStatus.CONFIRMED)
        return ExecutionResult(status, snapshot,
            submitted_count=sum(action.operation == "place" and action.success is True for action in result.actions),
            cancelled_count=cancelled, actual_plan=effective)

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
        revision |= bool(self._orders and authorization.account.maker_fee_rate != self._maker_fee)
        self._maker_fee = authorization.account.maker_fee_rate
        cancelled = len(self._orders) if revision else 0
        previous = {} if revision else {order.side: order for order in self._orders}
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
