"""Inventory bands and conservative pre-trade reserve; no exchange mutations."""

from decimal import Decimal, DecimalException, localcontext

from .domain import (
    AccountSnapshot, ExecutionHealth, ExecutionResult, ExecutionSnapshot,
    ExecutionStatus, FlattenIntent, GovernorDiagnostic, InventoryDecision, MarketStateSnapshot,
    SessionReport, Side, StrategyState, ZERO, _boolean, _count, _decimal, _time,
)


EXIT_TIMEOUT_SECONDS = 30


class GovernorUnavailable(ValueError):
    """Orthogonal execution pause, not a new strategy state or risk-budget reset."""

    def __init__(self, message, health=ExecutionHealth.PAUSED_DATA):
        super().__init__(message)
        self.health = health


class InventoryGovernor:
    def __init__(self, *, order_size: Decimal, soft_limit: Decimal,
                 hard_limit: Decimal, stop_loss_usdg: Decimal,
                 max_hold_seconds: float, cooldown_seconds: float,
                 max_session_loss_usdg: Decimal, session_started_monotonic: float,
                 session_deadline_monotonic: float, ioc_slippage_ticks: int):
        for value in (order_size, soft_limit, hard_limit, stop_loss_usdg,
                      max_session_loss_usdg):
            _decimal(value, positive=True)
        for value in (max_hold_seconds, cooldown_seconds, session_started_monotonic,
                      session_deadline_monotonic):
            _time(value)
        _count(ioc_slippage_ticks)
        if (not soft_limit < hard_limit or order_size > hard_limit
                or max_hold_seconds == 0
                or session_deadline_monotonic <= session_started_monotonic):
            raise ValueError("invalid inventory/session bounds")
        self.order_size, self.soft_limit, self.hard_limit = order_size, soft_limit, hard_limit
        self.stop_loss_usdg = stop_loss_usdg
        self.max_hold_seconds, self.cooldown_seconds = max_hold_seconds, cooldown_seconds
        self.max_session_loss_usdg = max_session_loss_usdg
        self.session_started_monotonic = session_started_monotonic
        self.session_deadline_monotonic = session_deadline_monotonic
        self.ioc_slippage_ticks = ioc_slippage_ticks
        self._state = StrategyState.QUOTING
        self._last_now = session_started_monotonic
        self._symbol = None
        self._terminal_stop = False
        self.stop_reason = None
        self._exit_started = None
        self._cooldown_until = None
        self._exit_reason = None
        self.last_diagnostic = None
        self._minimum_order_size = None
        self._diagnostic_values = {}
        self._decision_reason = None

    @property
    def exit_deadline(self) -> float | None:
        return (self._exit_started + EXIT_TIMEOUT_SECONDS
                if self._state is StrategyState.FLATTENING else None)

    def _clock(self, now):
        try:
            _time(now)
        except ValueError:
            raise GovernorUnavailable("valid monotonic clock required") from None
        if now < self._last_now:
            raise GovernorUnavailable("governor clock moved backwards")
        self._last_now = now

    def _begin_exit(self, now, reason="exit_in_progress"):
        if self._state is not StrategyState.FLATTENING:
            self._exit_started = now
            self._exit_reason = reason
        else:
            self._exit_started = min(self._exit_started, now)
        self._state = StrategyState.FLATTENING

    def _require_exit_time(self, now):
        if self.exit_deadline is not None and now >= self.exit_deadline:
            self._terminal_stop = True
            raise GovernorUnavailable("bounded exit deadline exhausted", ExecutionHealth.HALTED)

    def evaluate(self, market: MarketStateSnapshot, account: AccountSnapshot,
                 ledger_report: SessionReport, execution_snapshot: ExecutionSnapshot,
                 *, now: float, stop_requested: bool = False) -> InventoryDecision:
        previous_state = self._state
        self.last_diagnostic = None
        self._diagnostic_values = {}
        self._decision_reason = None
        self._clock(now)
        try:
            _boolean(stop_requested)
        except ValueError:
            raise GovernorUnavailable("typed stop request required") from None
        if self._state is StrategyState.SESSION_COMPLETE:
            return self._diagnose(InventoryDecision(self._state), previous_state, now)
        # Stop/deadline survive stale market, failed monitor and unknown orders.
        if stop_requested or now >= self.session_deadline_monotonic:
            self._terminal_stop = True
            self._begin_exit(min(now, self.session_deadline_monotonic),
                "operator_stop" if stop_requested else "session_deadline")
        self._require_exit_time(now)
        if (type(market) is not MarketStateSnapshot or type(account) is not AccountSnapshot
                or type(ledger_report) is not SessionReport
                or type(execution_snapshot) is not ExecutionSnapshot):
            raise GovernorUnavailable("typed market/account/ledger/execution required")
        values = [self.order_size, self.soft_limit, self.hard_limit, self.stop_loss_usdg,
                  self.max_session_loss_usdg, market.external_bid, market.external_ask,
                  market.tick_size, market.size_step, market.min_order_size,
                  account.position, account.entry_price or ZERO, account.unrealized_pnl,
                  account.maker_fee_rate, account.taker_fee_rate,
                  ledger_report.realized_net_pnl, ledger_report.max_drawdown,
                  ledger_report.current_drawdown, ledger_report.marked_net_pnl or ZERO]
        values += [value for order in execution_snapshot.orders or ()
                   for value in (order.remaining_size, order.price)]
        precision = (sum(len(v.as_tuple().digits) for v in values)
                     + max(v.adjusted() for v in values)
                     - min(v.as_tuple().exponent for v in values) + 16)
        if precision > 4096:
            raise GovernorUnavailable("inventory input precision exceeds supported range")
        try:
            with localcontext() as context:
                context.prec = max(context.prec, precision)
                self._validate(market, account, ledger_report, execution_snapshot, now)
                self._minimum_order_size = market.min_order_size
                decision = self._evaluate(market, account, ledger_report, execution_snapshot, now)
                return self._diagnose(decision, previous_state, now)
        except DecimalException:
            raise GovernorUnavailable("unusable inventory arithmetic") from None

    def _validate(self, market, account, report, execution, now):
        if (not account.authenticated or report.symbol != account.symbol
                or self._symbol not in (None, account.symbol)
                or not account.fresh(now)):
            raise GovernorUnavailable("fresh authenticated same-symbol account required")
        if (report.failed or report.complete or report.ledger_position != account.position
                or report.duration_seconds != Decimal(str(now)) - Decimal(str(self.session_started_monotonic))
                or not ZERO <= report.inventory_age <= report.duration_seconds
                or report.max_drawdown < ZERO
                or report.current_drawdown < ZERO
                or (account.position == ZERO and report.inventory_age != ZERO)):
            raise GovernorUnavailable("fresh coherent open session ledger required",
                                      ExecutionHealth.PAUSED_ORDER_STATE)
        self._symbol = account.symbol
        # Known account/ledger risk survives a paused book/order monitor. Starting
        # a cleanup timer grants no mutation permission without the other proofs.
        self._latch_risk(account, report, max(ZERO, -account.unrealized_pnl), now)
        self._require_exit_time(now)
        if execution.health is not ExecutionHealth.HEALTHY:
            raise GovernorUnavailable("execution is not healthy", execution.health)
        if (execution.orders is None or execution.symbol != account.symbol
                or not 0 <= now - execution.observed_monotonic <= 10
                or execution.managed_order_count != account.open_order_count):
            raise GovernorUnavailable("fresh coherent full order truth required",
                                      ExecutionHealth.PAUSED_ORDER_STATE)
        if (not market.trusted or market.symbol != account.symbol
                or not 0 <= now - market.observed_monotonic <= 3):
            raise GovernorUnavailable("fresh trusted same-symbol market required")

    def _latch_risk(self, account, report, loss, now):
        if (max(ZERO, -report.realized_net_pnl) + loss >= self.max_session_loss_usdg
                or report.max_drawdown >= self.max_session_loss_usdg):
            self._terminal_stop = True
            self._begin_exit(now, "session_loss" if
                max(ZERO, -report.realized_net_pnl) + loss >= self.max_session_loss_usdg
                else "max_drawdown")
        if account.position:
            if report.inventory_age >= Decimal(str(self.max_hold_seconds)):
                crossed = Decimal(str(now)) - report.inventory_age + Decimal(str(self.max_hold_seconds))
                self._begin_exit(float(crossed), "inventory_hold")
            if loss >= self.stop_loss_usdg or abs(account.position) > self.hard_limit:
                self._begin_exit(now, "inventory_stop_loss" if loss >= self.stop_loss_usdg
                                 else "inventory_hard_limit")

    def _evaluate(self, market, account, report, execution, now):
        quantity = abs(account.position)
        touch = market.external_bid if account.position > ZERO else market.external_ask
        inventory_pnl = (account.position * (touch - account.entry_price)
                         if quantity else ZERO)
        loss = max(ZERO, -account.unrealized_pnl, -inventory_pnl)
        self._latch_risk(account, report, loss, now)
        self._require_exit_time(now)
        if self._state is StrategyState.COOLDOWN:
            if quantity or account.open_order_count:
                self._begin_exit(now, "cooldown_residual")
            elif now < self._cooldown_until:
                return InventoryDecision(self._state)
            else:
                self._state = StrategyState.QUOTING
        if self._state is StrategyState.FLATTENING:
            intent = None
            if quantity:
                slippage = market.tick_size * self.ioc_slippage_ticks
                price = touch - slippage if account.position > ZERO else touch + slippage
                if price > ZERO:
                    intent = FlattenIntent(account.symbol,
                        Side.SELL if account.position > ZERO else Side.BUY,
                        quantity, price, self.exit_deadline)
            return InventoryDecision(self._state, flatten=intent)
        if quantity == self.hard_limit:
            self._decision_reason = "hard_inventory"
            return self._reducing(account, market)
        capacities = self._capacities(market, account, report, execution.orders, loss)
        if not any(capacities):
            if quantity == ZERO and account.open_order_count == 0:
                # No executable quote can fit the remaining reserve. A flat
                # session cannot earn back that headroom by waiting unquoted.
                # Completion still requires the existing fresh exit proof.
                self._terminal_stop = True
                self.stop_reason = "risk_capacity_exhausted"
                self._begin_exit(now, "risk_capacity_exhausted")
                self._decision_reason = "risk_capacity_exhausted"
                return InventoryDecision(self._state)
            return self._reducing(account, market)
        self._state = StrategyState.SKEWED if quantity >= self.soft_limit else StrategyState.QUOTING
        return InventoryDecision(self._state, buy_capacity=capacities[0], sell_capacity=capacities[1])

    def _reducing(self, account, market):
        self._state = StrategyState.REDUCE_ONLY
        capacity = (min(abs(account.position), self.order_size) // market.size_step) * market.size_step
        if capacity < market.min_order_size:
            capacity = ZERO
        return InventoryDecision(self._state,
            buy_capacity=capacity if account.position < ZERO else ZERO,
            sell_capacity=capacity if account.position > ZERO else ZERO)

    def _capacities(self, market, account, report, orders, inventory_loss):
        position, step = account.position, market.size_step
        old_buy = sum((o.remaining_size for o in orders if o.side is Side.BUY), ZERO)
        old_sell = sum((o.remaining_size for o in orders if o.side is Side.SELL), ZERO)
        prices = [market.external_bid, market.external_ask, account.entry_price or ZERO]
        price = max(prices + [o.price for o in orders])
        old_gap = sum((o.remaining_size * max(ZERO,
            o.price - market.external_bid if o.side is Side.BUY else market.external_ask - o.price)
            for o in orders), ZERO)
        realized_loss = max(ZERO, -report.realized_net_pnl)
        current_loss = realized_loss + inventory_loss + old_gap
        marked = report.marked_net_pnl if report.marked_net_pnl is not None else report.realized_net_pnl
        # Reserve against the same equity peak used by the drawdown stop. Current
        # headroom can recover; historical maximum drawdown must not consume it forever.
        drawdown_loss = max(ZERO, report.current_drawdown + marked
                            - report.realized_net_pnl + inventory_loss) + old_gap
        current_loss = max(current_loss, drawdown_loss)
        slip = market.tick_size * self.ioc_slippage_ticks
        self._diagnostic_values = dict(realized_loss=realized_loss,
            inventory_loss=inventory_loss, working_order_gap_loss=old_gap,
            drawdown_loss=drawdown_loss, current_loss=current_loss,
            remaining_loss_headroom=self.max_session_loss_usdg - current_loss)

        def allowed(buy, sell):
            # Capacities are total targets for the executor's one slot per side.
            # A replacement requires terminal cancellation and a fresh evaluation;
            # retaining one side while adding the other still reserves both sides.
            # Keep every old quantity at risk until that cancellation is proven.
            buy_exposure, sell_exposure = max(old_buy, buy), max(old_sell, sell)
            worst = max(abs(position), abs(position + buy_exposure),
                        abs(position - sell_exposure))
            stop = max(self.stop_loss_usdg, worst * self.stop_loss_usdg / self.order_size) if worst else ZERO
            taker_fee = worst * (price + slip) * account.taker_fee_rate
            slippage = worst * slip
            maker_fee = (buy_exposure + sell_exposure) * price * account.maker_fee_rate
            reserve = stop + taker_fee + slippage + maker_fee
            self._diagnostic_values.update(candidate_buy=buy, candidate_sell=sell,
                worst_position=worst, stop_reserve=stop, taker_fee_reserve=taker_fee,
                slippage_reserve=slippage, maker_fee_reserve=maker_fee,
                total_reserve=reserve)
            return worst <= self.hard_limit and current_loss + reserve < self.max_session_loss_usdg

        buy_cap = min(self.order_size, max(ZERO, self.hard_limit - position))
        sell_cap = min(self.order_size, max(ZERO, self.hard_limit + position))
        if abs(position) >= self.soft_limit:
            if position > ZERO:
                buy_cap = min(buy_cap, self.order_size / 2)
            else:
                sell_cap = min(sell_cap, self.order_size / 2)
        buy_lots, sell_lots = int(buy_cap // step), int(sell_cap // step)
        low, high = 0, max(buy_lots, sell_lots)
        if not allowed(ZERO, ZERO):
            self._decision_reason = "insufficient_reserve"
            return ZERO, ZERO
        while low < high:
            middle = (low + high + 1) // 2
            if allowed(min(middle, buy_lots) * step, min(middle, sell_lots) * step):
                low = middle
            else:
                high = middle - 1
        buy, sell = min(low, buy_lots) * step, min(low, sell_lots) * step
        capacities = (buy if buy >= market.min_order_size else ZERO,
                      sell if sell >= market.min_order_size else ZERO)
        if any(capacities):
            allowed(*capacities)
            return capacities
        allowed(buy, sell)
        self._decision_reason = "capacity_below_minimum"
        if position:
            # A shared size search can be limited by the inventory-increasing
            # side while an ordinary closing quote still fits the same reserve.
            # Only recover actual reduction: no new opposite inventory, no
            # release of old orders, and no exception to the existing budget.
            low, high = 0, int(min(abs(position), self.order_size) // step)
            while low < high:
                middle = (low + high + 1) // 2
                buy, sell = ((middle * step, ZERO) if position < ZERO
                             else (ZERO, middle * step))
                if allowed(buy, sell):
                    low = middle
                else:
                    high = middle - 1
            capacity = low * step
            buy, sell = ((capacity, ZERO) if position < ZERO else (ZERO, capacity))
            allowed(buy, sell)
            if capacity >= market.min_order_size:
                self._decision_reason = "reducing_capacity_only"
                return buy, sell
        return ZERO, ZERO

    def _diagnose(self, decision, previous_state, now):
        reason = self._decision_reason
        if reason is None:
            reason = ({StrategyState.QUOTING: "quoting",
                       StrategyState.SKEWED: "soft_inventory",
                       StrategyState.COOLDOWN: "cooldown",
                       StrategyState.SESSION_COMPLETE: "session_complete"}
                      .get(decision.state, self._exit_reason or "exit_in_progress"))
        self.last_diagnostic = GovernorDiagnostic(symbol=self._symbol,
            reason=reason, previous_state=previous_state, state=decision.state,
            observed_monotonic=now, minimum_order_size=self._minimum_order_size,
            buy_capacity=decision.buy_capacity, sell_capacity=decision.sell_capacity,
            **self._diagnostic_values)
        return decision

    def confirm_exit(self, result: ExecutionResult, *, now: float) -> InventoryDecision:
        """Only the safe execution bridge's exact terminal result completes cleanup."""
        previous_state = self._state
        self.last_diagnostic = None
        self._diagnostic_values = {}
        self._decision_reason = None
        self._clock(now)
        if (self._state is not StrategyState.FLATTENING or type(result) is not ExecutionResult
                or result.status is not ExecutionStatus.CONFIRMED):
            raise GovernorUnavailable("confirmed terminal exit result required",
                                      ExecutionHealth.PAUSED_ORDER_STATE)
        self._require_exit_time(now)
        execution, account = result.snapshot, result.account_snapshot
        if (account is None or not account.authenticated or account.symbol != self._symbol
                or account.position != ZERO or account.open_order_count != 0
                or not self._exit_started < account.observed_monotonic <= now
                or not account.fresh(now)
                or execution.health is not ExecutionHealth.HEALTHY
                or execution.managed_order_count != 0 or execution.orders != ()
                or execution.symbol != self._symbol or execution.simulated
                or not self._exit_started < execution.observed_monotonic <= now
                or now - execution.observed_monotonic > 10):
            raise GovernorUnavailable("fresh authenticated terminal flat proof required",
                                      ExecutionHealth.PAUSED_ORDER_STATE)
        if now >= self.session_deadline_monotonic:
            self._terminal_stop = True
        self._state = (StrategyState.SESSION_COMPLETE if self._terminal_stop
                       else StrategyState.COOLDOWN)
        self._cooldown_until = now + self.cooldown_seconds
        return self._diagnose(InventoryDecision(self._state), previous_state, now)
