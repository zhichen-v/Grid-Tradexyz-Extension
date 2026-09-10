"""Offline admission checks with endpoint-derived minimum costs, not wire evidence.

The real session/ledger/order manager run against RuntimeAdapter. This proxy
charges known public calls only; SDK retries and native startup are absent.
Selected scenarios add protocol pings explicitly. Actual wire amplification and
source latency still require the production observer.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from core.adapters.exchanges.models import OrderSide, OrderStatus
from core.services.market_maker_v2.api_budget import ApiBudget, ApiBudgetUnavailable
from core.services.market_maker_v2.domain import BoundedExitReport, CashflowEvent, FailureDiagnostic
from core.services.market_maker_v2.orchestrator import VolumeSession
from core.services.market_maker_v2.lighter_runtime import AccountReadRace
from test_mm_v2_session_runner import ADDRESS, RuntimeAdapter, RuntimeClock, config


class MinimumCostAdapter(RuntimeAdapter):
    def __init__(self):
        super().__init__()
        self.observer = None
        self.transactions = []
        self.ioc_parts = []
        paths = {
            "get_exchange_info": ("orderBooks",),
            "get_balances": ("account",),
            "get_account_trades": ("trades",),
            "get_account_fee_and_funding": ("accountLimits", "positionFunding"),
            "get_settlement_asset": ("assetDetails",),
            "get_order_history": ("accountInactiveOrders",),
            "get_order": ("accountActiveOrders", "accountInactiveOrders"),
            "get_open_orders": (1, 1),
        }
        for name, targets in paths.items():
            original = getattr(self, name)
            async def counted(*args, _original=original, _targets=targets, **kwargs):
                for target in _targets:
                    self.charge(target)
                return await _original(*args, **kwargs)
            setattr(self, name, counted)

    def set_market_maker_request_observer(self, observer, *, enforce_admission):
        self.observer = observer
        self.enforce_admission = enforce_admission

    def charge(self, target):
        self.observer("ws" if type(target) is int else "rest", target)

    async def open_read_stream(self, *args, **kwargs):
        self.charge(1)  # Initial public-book subscription.
        stream = await super().open_read_stream(*args, **kwargs)
        original = stream.request_snapshot
        async def snapshot(channel):
            self.charge(1)
            return await original(channel)
        stream.request_snapshot = snapshot
        return stream

    async def create_order(self, *args, **kwargs):
        tif = kwargs.get("params", {}).get("time_in_force")
        self.charge("sendTx")
        self.transactions.append((self.clock.now, "create", tif))
        if tif != "IOC":
            return await super().create_order(*args, **kwargs)
        # The current MM adapter confirms IOC via terminal history, not active WS.
        reader, self.confirmation_reader = self.confirmation_reader, None
        try:
            result = await super().create_order(*args, **kwargs)
            self.charge("accountInactiveOrders")
            return result
        finally:
            self.confirmation_reader = reader

    async def cancel_order(self, *args, **kwargs):
        self.charge("sendTx")
        self.transactions.append((self.clock.now, "cancel", None))
        result = await super().cancel_order(*args, **kwargs)
        self.charge("accountInactiveOrders")
        return result

    def fill(self, order, *, role="maker", price=None):
        cash = (D(self.account.assets[0].margin_balance)
                if self.account.account_trading_mode == 1 else None)
        collateral = D(self.account.collateral)
        if role != "taker" or not self.ioc_parts:
            super().fill(order, role=role, price=price)
        else:
            size = self.ioc_parts.pop(0)
            assert D("0") < size <= order.remaining
            super().fill(replace(order, amount=size, remaining=size), role=role, price=price)
            terminal = self.history[order.id]
            self.history[order.id] = replace(terminal, amount=order.amount, filled=size,
                status=OrderStatus.CANCELED if size < order.amount else OrderStatus.FILLED)
        if cash is not None:
            if not D(self.position.position):
                self.position.unrealized_pnl = "0"
            self.unified_cash(cash + D(self.account.collateral) - collateral)

    async def get_order_history(self, symbol=None, since=None, limit=None):
        # Match the production endpoint's bounded latest window on long sessions.
        rows = await super().get_order_history(symbol, since, limit)
        return rows[-limit:] if limit else rows


class BudgetSessionTests(unittest.IsolatedAsyncioTestCase):
    async def run_bursty_tape(self, *, whole_pair):
        """Fixed 10-minute synthetic order flow; missed opportunities never wait.

        This tests operational cost and cleanup, not venue fills or profitability.
        Each maker event fills at most 0.1 of a 0.2 working order at its price.
        Both implementations see the same one-edge-at-a-time external price tape.
        """
        from core.services.market_maker_v2 import execution_port
        adapter, clock, stop = MinimumCostAdapter(), RuntimeClock(), asyncio.Event()
        adapter.clock = clock
        adapter.unified("299.00000056076")
        configured = config(dry=False, duration=600)
        configured = replace(configured, quote=replace(configured.quote,
            order_size=D("0.2"), max_quote_age_ms=60000))
        schedule = [(wave * 120 + offset, side) for wave in range(5)
            for offset, side in zip((10, 25, 45),
                (OrderSide.SELL, OrderSide.SELL, OrderSide.BUY) if wave % 2 == 0
                else (OrderSide.BUY, OrderSide.BUY, OrderSide.SELL))]
        fills, missed, events, callback_errors = [], [], [], []
        started, last_ping = None, 0
        original_fill = adapter.fill

        def fill(order, *, role="maker", price=None):
            old = D(adapter.position.position) * adapter.position.sign
            entry = D(adapter.position.avg_entry_price)
            if role == "taker":
                price = (adapter.book.bids[0].price if order.side is OrderSide.SELL
                         else adapter.book.asks[0].price)
                self.assertTrue(price >= order.price if order.side is OrderSide.SELL
                                else price <= order.price, "fixture cannot fill beyond the IOC limit")
            original_fill(order, role=role, price=price)
            signed = order.remaining if order.side is OrderSide.BUY else -order.remaining
            if old * signed > 0:
                adapter.position.avg_entry_price = str((abs(old) * entry
                    + order.remaining * (order.price if price is None else price)) / abs(old + signed))
        adapter.fill = fill

        async def advance(seconds):
            nonlocal started, last_ping
            try:
                if adapter.observer and clock.now - last_ping >= 30:
                    adapter.charge(9)
                    last_ping = clock.now
                if session.ledger is not None and started is None:
                    started = clock.now - float(session.ledger.snapshot(now=clock.now).duration_seconds)
                if started is not None:
                    elapsed = clock.now - started
                    step = int(elapsed // 15) % 4
                    adapter.book.bids[0].price = D("99") + (1 if step in (1, 2) else 0)
                    adapter.book.asks[0].price = D("101") + (1 if step in (2, 3) else 0)
                    if schedule and elapsed >= schedule[0][0]:
                        due, side = schedule.pop(0)
                        candidates = [order for order in adapter.orders if order.side is side]
                        if session.phase == "waiting" and candidates:
                            order = candidates[0]
                            quantity = min(D("0.1"), order.remaining)
                            adapter.fill(replace(order, amount=quantity, remaining=quantity))
                            remaining = order.remaining - quantity
                            if remaining:
                                active = replace(order, filled=order.filled + quantity,
                                    remaining=remaining, status=OrderStatus.OPEN)
                                adapter.orders.append(active)
                                adapter.history[order.id] = active
                                adapter._counts()
                            else:
                                adapter.history[order.id] = replace(adapter.history[order.id],
                                    amount=order.amount, filled=order.amount)
                            fills.append((due, elapsed, side))
                        else:
                            missed.append((due, elapsed, side))
                    position = D(adapter.position.position) * adapter.position.sign
                    mid = (adapter.book.bids[0].price + adapter.book.asks[0].price) / 2
                    adapter.position.unrealized_pnl = str(position * (mid - D(adapter.position.avg_entry_price)))
                    adapter.unified_cash(D(adapter.account.assets[0].margin_balance))
                clock.now += float(seconds)
                await asyncio.sleep(0)
            except Exception as error:
                callback_errors.append(error)
                raise
        session = VolumeSession(configured, adapter, account_index=7,
            expected_l1_address=ADDRESS, authorize_bounded_flatten=True,
            telemetry=NS(emit=events.append), clock=clock, sleep=advance)
        revision = execution_port._quote_revision
        def all_sides(orders, *args):
            return {order.side for order in orders} if revision(orders, *args) else set()
        with patch.object(execution_port, "_quote_revision", all_sides if whole_pair else revision):
            result = await session.run(stop)
        self.assertFalse(callback_errors)
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertFalse(any(isinstance(event, FailureDiagnostic) for event in events))
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual(result.report.equity_reconciliation_difference, D("0"))
        self.assertEqual(len(fills) + len(missed) + len(schedule), 15)
        if result.report.duration_seconds < 600:
            self.assertEqual(result.stop_reason, "api_backpressure_repeated")
            self.assertEqual(session.api_budget.deferrals, 3)
        for bucket, peak in session.api_budget.peaks.items():
            self.assertLessEqual(peak, ApiBudget.LIMITS[bucket])
        budget = session.api_budget.snapshot()
        return {"duration": str(result.report.duration_seconds), "stop": result.stop_reason,
            "maker_opportunities_filled": len(fills), "maker_opportunities_total": 15,
            "deferrals": budget["deferrals"], "rest": sum(budget["rest_weight_by_phase"].values()),
            "tx": budget["attempts"].get("rest:sendTx", 0), "ioc": result.report.taker_fill_count}

    async def test_bursty_partial_fills_measure_churn_and_keep_exact_cleanup(self):
        selected = await self.run_bursty_tape(whole_pair=False)
        whole = await self.run_bursty_tape(whole_pair=True)
        self.assertGreaterEqual(selected["maker_opportunities_filled"], whole["maker_opportunities_filled"])
        self.assertLess(selected["tx"] * whole["maker_opportunities_filled"],
                        whole["tx"] * selected["maker_opportunities_filled"])
        print("BURSTY_SIMULATED_SESSION_METRICS " + json.dumps(
            {"selected_sides": selected, "whole_pair": whole}, sort_keys=True))

    def test_backpressure_diagnostics_are_bounded_and_window_is_inclusive(self):
        now = [0.0]
        budget = ApiBudget(lambda: now[0])
        def record():
            return budget.record_backpressure_exit(phase="authorizing_quotes",
                exit_id=f"exit-{budget.deferrals + 1}",
                error=ApiBudgetUnavailable("no payload retained", values={"api_next_rest": D("900")}))
        self.assertEqual(record(), 1)
        now[0] = 600
        self.assertEqual(record(), 2)
        now[0] += 0.001
        self.assertEqual(record(), 2)
        for _ in range(65):
            now[0] += 601
            self.assertEqual(record(), 1)
        snapshot = budget.snapshot()
        self.assertEqual(len(snapshot["recent_backpressure_exits"]), 64)
        self.assertEqual(snapshot["deferrals"], 68)
        self.assertEqual(snapshot["recent_backpressure_exits"][-1]["next"], {"rest": 900})
        self.assertEqual(snapshot["limits"], ApiBudget.LIMITS)
        self.assertNotIn("no payload retained", json.dumps(snapshot))

    async def run_injected_backpressure(self, schedule, *, fail_cleanup=False, fail_final=False):
        adapter, clock, stop = MinimumCostAdapter(), RuntimeClock(), asyncio.Event()
        adapter.clock = clock
        configured = config(dry=False, duration=schedule[-1][0] + 100)
        configured = replace(configured, quote=replace(configured.quote, max_quote_age_ms=60000))
        pending, injected, events = [], [], []
        started = None
        async def advance(seconds):
            nonlocal started
            if session.phase == "waiting" and adapter.orders:
                if started is None:
                    started = clock.now
                if len(injected) < len(schedule):
                    when, error_type = schedule[len(injected)]
                    if clock.now - started >= when:
                        adapter.fill(next(row for row in adapter.orders if row.side is OrderSide.BUY))
                        injected.append((clock.now, len(adapter.transactions)))
                        pending.append(error_type)
            clock.now += float(seconds)
            await asyncio.sleep(0)
        session = VolumeSession(configured, adapter, account_index=7,
            expected_l1_address=ADDRESS, authorize_bounded_flatten=True,
            telemetry=NS(emit=events.append), clock=clock, sleep=advance)
        original_authorize = session._authorize
        async def authorize(exposure):
            if pending and not session._budget_exiting:
                raise pending.pop(0)("injected local admission pressure")
            return await original_authorize(exposure)
        session._authorize = authorize
        original_cancel, original_balances = adapter.cancel_order, adapter.get_balances
        async def cancel(*args, **kwargs):
            if fail_cleanup and len(injected) == 3:
                raise RuntimeError("injected cleanup failure")
            return await original_cancel(*args, **kwargs)
        async def balances():
            if fail_final and session.phase == "final_account":
                raise RuntimeError("injected final proof failure")
            return await original_balances()
        adapter.cancel_order, adapter.get_balances = cancel, balances
        result = await session.run(stop)
        self.assertEqual(adapter.disconnections, 1)
        self.assertEqual(len(injected), len(schedule), result.failure)
        return session, adapter, result, events, injected

    async def test_third_backpressure_exit_stops_after_cleanup_and_fresh_final_proof(self):
        session, adapter, result, events, injected = await self.run_injected_backpressure(
            [(20, ApiBudgetUnavailable), (120, ApiBudgetUnavailable), (220, ApiBudgetUnavailable)])
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertEqual(result.stop_reason, "api_backpressure_repeated")
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertLess(result.report.duration_seconds, D("320"))
        self.assertEqual(result.report.taker_fill_count, 3)
        self.assertFalse(any(tif == "POST_ONLY" for _, _, tif in adapter.transactions[injected[-1][1]:]))
        exits = [row for row in events if isinstance(row, BoundedExitReport)]
        self.assertEqual(len(exits), 3)
        self.assertTrue(all(row.complete for row in exits))
        rows = session.api_budget.snapshot()["recent_backpressure_exits"]
        self.assertEqual([row["exit_id"] for row in rows], [row.flatten_id for row in exits])
        self.assertEqual([row["number"] for row in rows], [1, 2, 3])
        self.assertTrue(all(row["reason"] == "local_api_budget" for row in rows))
        self.assertEqual(session.api_budget.deferrals, 3)
        self.assertEqual(session.api_budget.account_read_deferrals, 0)

    async def test_backpressure_window_expires_and_account_races_do_not_count(self):
        for schedule in (
            [(20, ApiBudgetUnavailable), (400, ApiBudgetUnavailable), (750, ApiBudgetUnavailable)],
            [(20, AccountReadRace), (120, ApiBudgetUnavailable),
             (220, AccountReadRace), (320, ApiBudgetUnavailable)],
        ):
            with self.subTest(schedule=schedule):
                session, adapter, result, events, injected = await self.run_injected_backpressure(schedule)
                self.assertTrue(result.completed, result.failure)
                self.assertIsNone(result.stop_reason)
                self.assertGreaterEqual(result.report.duration_seconds, D(schedule[-1][0] + 100))
                self.assertTrue(any(tif == "POST_ONLY" for _, _, tif in adapter.transactions[injected[-1][1]:]))
                self.assertEqual(session.api_budget.deferrals,
                    sum(error_type is ApiBudgetUnavailable for _, error_type in schedule))
                self.assertEqual(session.api_budget.account_read_deferrals,
                    sum(error_type is AccountReadRace for _, error_type in schedule))

    async def test_repeated_backpressure_does_not_disguise_cleanup_or_final_proof_failure(self):
        for failure in ("fail_cleanup", "fail_final"):
            with self.subTest(failure=failure):
                session, adapter, result, events, injected = await self.run_injected_backpressure(
                    [(20, ApiBudgetUnavailable), (120, ApiBudgetUnavailable), (220, ApiBudgetUnavailable)],
                    **{failure: True})
                self.assertFalse(result.completed)
                self.assertIsNotNone(result.failure)
                self.assertFalse(result.report.complete)
                self.assertEqual(session.api_budget.deferrals, 3)
                self.assertFalse(any(tif == "POST_ONLY" for _, _, tif in adapter.transactions[injected[-1][1]:]))

    async def run_session(self, *, calm=False, partial_exit=False, moving_inventory=False, cooldown_end=None):
        adapter, clock, stop = MinimumCostAdapter(), RuntimeClock(), asyncio.Event()
        adapter.clock = clock
        configured = config(dry=False, duration=600 if moving_inventory else 90)
        if cooldown_end == "deadline":
            # End inside the deliberate post-fill API burst's rolling window;
            # do not depend on inefficient normal reads causing a natural denial.
            configured = replace(configured, session=replace(configured.session, duration_seconds=220))
        if calm:
            configured = replace(configured, quote=replace(configured.quote, max_quote_age_ms=60000))
        if partial_exit:
            configured = replace(configured, quote=replace(configured.quote, order_size=D("0.3")))
            adapter.ioc_parts = [D("0.1")] * 3
        last_ping, filled = 0, False
        adapter.cooldown_observations = []
        async def advance(seconds):
            nonlocal last_ping, filled
            if session.phase == "api_cooldown":
                self.assertFalse(adapter.orders)
                self.assertEqual(D(adapter.position.position), D("0"))
                adapter.cooldown_observations.append((clock.now, len(adapter.transactions)))
                if cooldown_end == "stop":
                    stop.set()
            if moving_inventory:
                if clock.now - last_ping >= 30:
                    adapter.charge(9)
                    last_ping = clock.now
                if clock.now >= 240 and not filled and adapter.orders:
                    adapter.fill(next(row for row in adapter.orders if row.side is OrderSide.BUY))
                    filled = True
                # Price valuation moves without another fill or cash change.
                adapter.position.unrealized_pnl = str(
                    D(str(clock.now % 3)) / 100 if D(adapter.position.position) else D("0"))
            if partial_exit and not adapter.trades:
                buys = [row for row in adapter.orders if row.side == OrderSide.BUY]
                if buys:
                    adapter.fill(buys[0])
                    stop.set()
            clock.now += float(seconds)
            await asyncio.sleep(0)
        events = []
        session = VolumeSession(configured, adapter, account_index=7,
            expected_l1_address=ADDRESS, authorize_bounded_flatten=True,
            telemetry=NS(emit=events.append), clock=clock, sleep=advance)
        denials, authorizations = [], []
        original_authorize = session._authorize
        async def authorize(exposure):
            value = await original_authorize(exposure)
            if value.plan.quotes:
                authorizations.append((clock.now, value.account))
            return value
        session._authorize = authorize
        original = session.api_budget.require_normal
        burst_injected = False
        def admit(cost):
            nonlocal burst_injected
            if cooldown_end and filled and not burst_injected and not session._budget_exiting:
                # Explicit external-load fault for stop/deadline-in-cooldown;
                # the calm moving-inventory test now runs without a deferral.
                burst_injected = True
                while session.api_budget.snapshot()["used"].get("rest", 0) < 12000:
                    adapter.charge("account")
            try:
                original(cost)
            except ApiBudgetUnavailable:
                denials.append((clock.now, len(adapter.transactions), tuple(row.id for row in adapter.orders),
                                D(adapter.position.position) * adapter.position.sign))
                raise
        session.api_budget.require_normal = admit
        result = await session.run(stop)
        self.assertTrue(session._budget_active)
        self.assertTrue(adapter.enforce_admission)
        self.assertEqual(adapter.disconnections, 1)
        self.assertIsNotNone(result.final_account, result.failure)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        exits = [event for event in events if isinstance(event, BoundedExitReport)]
        for denied_at, index, orders, position in denials:
            completed_exit = next(event for event in exits if event.observed_monotonic >= denied_at)
            self.assertTrue(completed_exit.complete)
            self.assertLess(completed_exit.observed_monotonic - denied_at, 30)
            account = completed_exit.final_result.account_snapshot
            self.assertTrue(account.authenticated)
            self.assertEqual((account.position, account.open_order_ids), (D("0"), ()))
            following = adapter.transactions[index:]
            if orders:
                self.assertEqual(following[0][1], "cancel")
            elif not position:
                self.assertEqual(completed_exit.attempts, 0, "flat refusal needs no invented exit trade")
            resumed = [at for at, operation, tif in following if tif == "POST_ONLY"]
            self.assertTrue(all(at > completed_exit.observed_monotonic for at in resumed))
            if resumed:
                self.assertTrue(any(completed_exit.observed_monotonic < at <= resumed[0]
                                    and fresh.authenticated and fresh.fresh(resumed[0])
                                    for at, fresh in authorizations),
                                "re-entry requires a new strict account/quote authorization")
        for bucket, peak in session.api_budget.peaks.items():
            self.assertLessEqual(peak, session.api_budget.LIMITS[bucket], (bucket, peak))
        return session, adapter, result, denials

    async def test_calm_session_runs_full_deadline_with_active_admission(self):
        session, adapter, result, denials = await self.run_session(calm=True)
        self.assertTrue(result.completed, result.failure)
        self.assertFalse(denials)
        self.assertGreaterEqual(result.report.duration_seconds, D("90"))
        self.assertGreater(adapter.creates, 0)

    async def test_reprice_pressure_cleans_waits_and_resumes_before_same_deadline(self):
        session, adapter, result, denials = await self.run_session()
        self.assertTrue(denials, "frequent replacement must reach the reserved-capacity boundary")
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertGreater(adapter.creates, 2)
        after_refusal = adapter.transactions[denials[0][1]:]
        self.assertTrue(after_refusal)
        if denials[0][2]:
            self.assertEqual(after_refusal[0][1], "cancel")
            self.assertLessEqual(after_refusal[0][0] - denials[0][0], 30)
        else:
            self.assertEqual(denials[0][3], D("0"))
            self.assertTrue(any(denials[0][0] <= at < after_refusal[0][0]
                                for at, _ in adapter.cooldown_observations),
                            "an already-flat refusal must wait before its next quote")
        self.assertTrue(any(tif == "POST_ONLY" for _, _, tif in after_refusal))
        self.assertGreaterEqual(result.report.duration_seconds, D("90"))
        self.assertTrue(adapter.cooldown_observations)

    async def test_continuous_hour_recovers_arrival_and_quota_pressure(self):
        adapter, clock, stop = MinimumCostAdapter(), RuntimeClock(), asyncio.Event()
        adapter.clock = clock
        adapter.unified("299.00000056076")
        adapter.ioc_parts = [D("0.1")] * 3
        configured = config(dry=False, duration=3600)
        configured = replace(configured, quote=replace(configured.quote,
            order_size=D("0.3"), max_quote_age_ms=60000))
        events, waits, denials, fills, quote_times = [], [], [], [], []
        started = missing_id = original_deadline = None
        history_lags, counter_lags, lag_until, last_ping = 0, 0, 0, 0
        funding_amount, funding_id = D("-0.00038400912"), "67904"
        funding_posted, funding_refreshes, public_funding_reads = [], [], []
        schedule = [(block * 600 + when, side) for block in range(6)
                    for when, side in ((20, OrderSide.SELL), (35, OrderSide.BUY),
                        (120, OrderSide.BUY), (300, OrderSide.SELL),
                        (315, OrderSide.BUY), (420, OrderSide.BUY), (435, OrderSide.SELL))]
        original_history, original_stream = adapter.get_order_history, adapter.open_read_stream
        original_balances, original_fees = adapter.get_balances, adapter.get_account_fee_and_funding

        async def balances():
            position = D(adapter.position.position) * adapter.position.sign
            if (not funding_posted and position > 0 and session.phase == "authorizing_quotes"
                    and started is not None and clock.now - started >= 1800
                    and session.ledger is not None
                    and session.ledger.snapshot(now=clock.now).ledger_position == position
                    and 0 <= clock.now - session.account._fees_at < 7):
                # Settlement arrives while the position is open and metadata is still cached.
                timestamp_ms = int(clock.now * 1000)
                funding_posted.append((clock.now, position, timestamp_ms))
                adapter.fees["fundings"] = ({"id": funding_id, "timestamp": timestamp_ms,
                                             "change": funding_amount},)
                adapter.unified_cash(D(adapter.account.assets[0].margin_balance) + funding_amount)
            return await original_balances()

        async def fees(*args, **kwargs):
            value = await original_fees(*args, **kwargs)
            if value["fundings"] and not public_funding_reads:
                adapter.charge("fundings")  # Exact public round is fetched once, then cached by ID.
                public_funding_reads.append(clock.now)
            return value

        async def history(*args, **kwargs):
            nonlocal history_lags
            rows = await original_history(*args, **kwargs)
            if missing_id is not None and not history_lags:
                history_lags += 1
                return [row for row in rows if row.id != missing_id]
            return rows

        async def stream(*args, **kwargs):
            result = await original_stream(*args, **kwargs)
            snapshot = result.request_snapshot
            async def delayed_counter(channel):
                nonlocal counter_lags
                row = await snapshot(channel)
                if clock.now < lag_until:
                    row["total_trades_count"] = 0
                    counter_lags += 1
                row["funding_histories"] = {}  # An unchanged WS summary must not hide real settlement.
                return row
            result.request_snapshot = delayed_counter
            return result

        async def advance(seconds):
            nonlocal started, missing_id, lag_until, last_ping, original_deadline
            if clock.now - last_ping >= 30 and adapter.observer:
                adapter.charge(9)
                last_ping = clock.now
            if session.phase == "api_cooldown":
                self.assertFalse(adapter.orders)
                self.assertEqual(D(adapter.position.position), D("0"))
                waits.append((clock.now, len(adapter.transactions)))
            if session.phase == "waiting" and adapter.orders:
                if started is None:
                    started = clock.now
                    original_deadline = session.governor.session_deadline_monotonic
                elapsed = clock.now - started
                quote_times.append(elapsed)
                if schedule and elapsed >= schedule[0][0]:
                    candidates = [row for row in adapter.orders if row.side is schedule[0][1]]
                    if candidates:
                        self.assertLess(elapsed - schedule[0][0], 120,
                            "budget recovery must not park the session indefinitely")
                        order = candidates[0]
                        adapter.fill(order)
                        fills.append((elapsed, order.side))
                        schedule.pop(0)
                        if missing_id is None:
                            missing_id, lag_until = order.id, clock.now + 10
                # Interleave quiet 60s quote expiry with faster price revisions.
                phase = elapsed % 600
                pressure = 60 <= phase < 110 or 240 <= phase < 290 or 480 <= phase < 530
                offset = D(int(elapsed // 10) % 2) if pressure else D("0")
                adapter.book.bids[0].price = D("99") + offset
                adapter.book.asks[0].price = D("101") + offset
            adapter.position.unrealized_pnl = (str(D(int(clock.now) % 3) / 100)
                if D(adapter.position.position) else "0")
            adapter.unified_cash(D(adapter.account.assets[0].margin_balance))
            clock.now += float(seconds)
            await asyncio.sleep(0)

        adapter.get_order_history, adapter.open_read_stream = history, stream
        adapter.get_balances, adapter.get_account_fee_and_funding = balances, fees
        session = VolumeSession(configured, adapter, account_index=7,
            expected_l1_address=ADDRESS, authorize_bounded_flatten=True,
            telemetry=NS(emit=events.append), clock=clock, sleep=advance)
        exit_rest_weight = 0
        original_observe = adapter.observer
        def observe(transport, target):
            nonlocal exit_rest_weight
            if transport == "rest" and session._budget_exiting:
                exit_rest_weight += session.api_budget.WEIGHTS.get(target, 0)
            original_observe(transport, target)
        adapter.observer = observe
        original_admit = session.api_budget.require_normal
        def admit(cost):
            try:
                original_admit(cost)
            except ApiBudgetUnavailable:
                denials.append((clock.now, len(adapter.transactions)))
                raise
        session.api_budget.require_normal = admit
        original_read_admit = session.account.before_read
        def read_admit(kind):
            if kind == "funding_refresh" and funding_posted:
                funding_refreshes.append(clock.now)
            return original_read_admit(kind)
        session.account.before_read = read_admit
        result = await session.run(stop)
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual(result.report.equity_reconciliation_difference, D("0"))
        self.assertEqual(len(funding_posted), 1)
        self.assertGreaterEqual(funding_posted[0][0] - started, 1800)
        self.assertGreater(funding_posted[0][1], D("0"))
        self.assertTrue(funding_refreshes, "cash mismatch must force a fresh funding read inside its TTL")
        self.assertEqual(len(public_funding_reads), 1)
        self.assertEqual(session.api_budget.counts["rest:fundings"], 1)
        self.assertEqual(session.account._fundings[funding_id], (funding_posted[0][2], funding_amount))
        self.assertEqual(result.report.funding, funding_amount)
        funding_events = [row for row in events if isinstance(row, CashflowEvent)]
        self.assertEqual([(row.event_id, row.amount) for row in funding_events],
                         [("funding:" + funding_id, funding_amount)])
        self.assertEqual(result.final_account.equity - D("299.00000056076"),
                         result.report.realized_gross_pnl - result.report.maker_fee
                         - result.report.taker_fee + funding_amount)
        self.assertGreaterEqual(result.report.duration_seconds, D("3600"))
        self.assertEqual(session.governor.session_deadline_monotonic, original_deadline)
        self.assertLess(clock.now, session.governor.session_deadline_monotonic + 30)
        self.assertFalse(any(isinstance(row, FailureDiagnostic) for row in events))
        self.assertEqual(history_lags, 1)
        self.assertGreater(counter_lags, 0)
        self.assertEqual(session.account._stream_trade_ahead, 0)
        self.assertGreater(len(fills), 20)
        self.assertEqual({side for _, side in fills}, {OrderSide.BUY, OrderSide.SELL})
        self.assertFalse(schedule, "the session must resume enough to execute every planned maker event")
        # This unchanged workload formerly passed despite 41 forced cleanups
        # and only 64.7% quoting. Guard the operational improvement, not profit:
        # scheduled fills wait for eligible orders and do not model a venue.
        self.assertLessEqual(session.api_budget.deferrals, 1)
        self.assertGreaterEqual(result.report.quote_uptime_seconds, D("3240"))
        self.assertGreaterEqual(result.report.two_sided_quote_seconds, D("3240"))
        self.assertLessEqual(result.report.taker_fill_count, 10)
        rest_weight = sum(session.api_budget.WEIGHTS.get(endpoint, 0)
            * session.api_budget.counts["rest:" + endpoint]
            for endpoint in session.api_budget.WEIGHTS)
        self.assertLessEqual(rest_weight, 400000)
        self.assertEqual({int(at // 600) for at in quote_times}, set(range(6)))
        for denied_at, index in denials:
            following = adapter.transactions[index:]
            self.assertTrue(any(tif == "POST_ONLY" for _, _, tif in following), denied_at)
        exits = [row for row in events if isinstance(row, BoundedExitReport)]
        self.assertTrue(any(row.attempts == 3 for row in exits))
        self.assertTrue(all(row.complete and row.attempts <= 3 for row in exits))
        for bucket, peak in session.api_budget.peaks.items():
            self.assertLessEqual(peak, session.api_budget.LIMITS[bucket], (bucket, peak))
        self.assertEqual(adapter.disconnections, 1)
        print("CONTINUOUS_SESSION_METRICS " + json.dumps({
            "simulated_seconds": str(result.report.duration_seconds),
            "deferrals": session.api_budget.deferrals,
            "maker_fills": result.report.maker_fill_count,
            "taker_fills": result.report.taker_fill_count,
            "funding": str(result.report.funding),
            "funding_events": len(funding_events),
            "public_funding_reads": len(public_funding_reads),
            "api_peaks": dict(session.api_budget.peaks),
            "quote_uptime_seconds": str(result.report.quote_uptime_seconds),
            "two_sided_quote_seconds": str(result.report.two_sided_quote_seconds),
            "exit_rest_weight": exit_rest_weight,
            "rest_weight": rest_weight,
            "rest_weight_by_phase": session.api_budget.snapshot()["rest_weight_by_phase"],
            "send_tx_count": session.api_budget.counts["rest:sendTx"],
            "ten_minute_windows_with_quotes": sorted({int(at // 600) for at in quote_times}),
        }, sort_keys=True))

    async def test_moving_inventory_continues_across_rolling_windows(self):
        session, adapter, result, denials = await self.run_session(calm=True, moving_inventory=True)
        self.assertTrue(result.completed, result.failure)
        self.assertFalse(denials, "unchanged calm inventory must not be driven out by coarse admission")
        self.assertGreaterEqual(result.report.duration_seconds, D("600"))
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (1, 1))
        self.assertGreater(session.api_budget.counts["ws:9"], 10)
        # The genuine inventory hold-time exit still waits for re-entry
        # headroom; zero normal-read refusals does not remove that protection.
        self.assertTrue(adapter.cooldown_observations)
        ioc = next(i for i, row in enumerate(adapter.transactions) if row[2] == "IOC")
        self.assertTrue(any(row[2] == "POST_ONLY" for row in adapter.transactions[ioc + 1:]))

    async def test_flat_budget_wait_honors_stop_and_original_deadline(self):
        for end in ("stop", "deadline"):
            with self.subTest(end=end):
                session, adapter, result, denials = await self.run_session(
                    calm=True, moving_inventory=True, cooldown_end=end)
                self.assertTrue(result.completed, result.failure)
                self.assertTrue(denials, "stop/deadline must be exercised during budget recovery")
                self.assertTrue(adapter.cooldown_observations)
                self.assertEqual(len(adapter.transactions), adapter.cooldown_observations[0][1])
                self.assertLess(session.clock.now,
                    (session._stop_at if end == "stop" else session.governor.session_deadline_monotonic) + 2)

    async def test_operator_stop_can_complete_three_partial_iocs_in_same_budget(self):
        session, adapter, result, denials = await self.run_session(partial_exit=True)
        self.assertTrue(result.completed, result.failure)
        self.assertFalse(denials)
        self.assertEqual(adapter.created_tifs.count("IOC"), 3)
        self.assertEqual(result.report.taker_fill_count, 3)
        self.assertEqual(result.report.forced_flatten_count, 1)
        self.assertFalse(adapter.ioc_parts)
        self.assertLessEqual(adapter.transactions[-1][0] - session._stop_at, 30)


if __name__ == "__main__":
    unittest.main()
