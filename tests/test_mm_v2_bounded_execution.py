"""Public V2 bridge contracts against the frozen manager and fake exchange only."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_market_maker_order_manager as legacy
from core.adapters.exchanges.models import OrderSide, OrderStatus
from core.services.market_maker.models import DesiredOrder, DesiredQuotes, MarketMetadata, RuntimeState
from core.services.market_maker.order_manager import MarketMakerOrderManager
from core.services.market_maker.risk_manager import RiskDecision
from core.services.market_maker_v2.domain import (
    AccountSnapshot, ExecutionHealth, ExecutionStatus, FlattenIntent,
    MarketStateSnapshot, QuotePlan, Side,
)
from core.services.market_maker_v2.execution_port import (
    ExecutionUnavailable, LegacyBoundedExecutionPort,
)


D = Decimal


class BoundedExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.time = legacy.Clock()
        self.clock = SimpleNamespace(monotonic=self.time)
        self.position = D("-0.2")
        self.fill_size = D("0.2")
        self.events = []
        self.sequence = 0
        self.adapter = SimpleNamespace(
            create_order=AsyncMock(side_effect=self.create),
            cancel_order=AsyncMock(side_effect=self.cancel),
            get_open_orders=AsyncMock(side_effect=self.open_orders),
            get_order_history=AsyncMock(return_value=[]),
            get_unresolved_submissions=Mock(return_value=[]),
            get_unresolved_cancellations=Mock(return_value=[]),
            get_terminal_cancellation_outcome=Mock(return_value=None),
            confirm_terminal_cancellation_outcome=Mock(return_value=False),
            resolve_unresolved_submissions=AsyncMock(return_value=[]),
        )
        # Reuse the proven legacy configuration fixture, not its strategy/runtime.
        config = legacy.MarketMakerOrderManagerTests().active_unwind_config()
        metadata = MarketMetadata("BTC", 1, 1, D("0.1"), D("0.1"), D("0.1"), D("0"))
        self.manager = MarketMakerOrderManager(self.adapter, config, metadata,
                                               monotonic=self.time, sleep=self.no_wait)
        self.account = SimpleNamespace(snapshot=AsyncMock(side_effect=self.account_snapshot))
        self.market = SimpleNamespace(snapshot=Mock(side_effect=self.market_snapshot))
        self.port = self.make_port()
        self.intent = FlattenIntent("BTC", Side.BUY, D("0.2"), D("101.2"), 105.0)

    async def no_wait(self, seconds):
        return None

    def make_port(self):
        return LegacyBoundedExecutionPort(self.manager, self.account, self.market,
                                          self.clock, authorize_bounded_flatten=True)

    async def open_orders(self, symbol):
        self.events.append("zero_orders")
        return []

    async def account_snapshot(self):
        self.events.append("account")
        return AccountSnapshot("BTC", self.time(), self.position, D("100"),
                               D("0.00012"), D("0.0004"), 0, True,
                               entry_price=D("100") if self.position else None)

    def market_snapshot(self):
        self.events.append("market")
        return MarketStateSnapshot("BTC", self.time(), D("100.9"), D("101"),
                                   D("0.1"), D("0.1"), D("0.1"), True)

    async def create(self, symbol, side, order_type, amount, price, params):
        self.sequence += 1
        is_ioc = params.get("time_in_force") == "IOC"
        self.events.append("ioc" if is_ioc else "maker")
        fill = min(self.fill_size, amount) if is_ioc else D("0")
        if is_ioc:
            self.assertIs(params["reduce_only"], True)
            self.position += fill if side is OrderSide.BUY else -fill
        status = (OrderStatus.FILLED if fill == amount else OrderStatus.CANCELED) if is_ioc else OrderStatus.OPEN
        return legacy.exchange_order(str(self.sequence), side, status=status,
                                     price=str(price), amount=str(amount),
                                     remaining=str(amount - fill), params=params)

    async def cancel(self, order_id, symbol):
        self.events.append("cancel")
        return legacy.exchange_order(order_id, OrderSide.BUY, price="100", amount="0.2",
                                     status=OrderStatus.CANCELED,
                                     params={"cancel_terminal": True})

    async def seed_maker(self):
        quote = DesiredOrder(OrderSide.BUY, D("100"), D("0.2"), True, "fixture")
        quotes = DesiredQuotes(quote, None, D("100"), D("100"), D("0.1"), D("-0.2"),
                               RuntimeState.RISK_REDUCTION, "fixture")
        risk = RiskDecision(D("0.2"), None, True, False, D("0.2"), D("0"),
                            D("0"), D("-0.2"), D("-0.2"), RuntimeState.RISK_REDUCTION,
                            "fixture", True)
        result = await self.manager.reconcile(quotes, risk)
        self.assertFalse(result.errors)
        self.assertEqual(self.port.snapshot().managed_order_count, 1)
        self.events.clear()

    def test_authorization_and_live_mode_required_before_any_exchange_call(self):
        for value in (False, None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ExecutionUnavailable):
                LegacyBoundedExecutionPort(self.manager, self.account, self.market,
                                           self.clock, authorize_bounded_flatten=value)
        self.manager.config = replace(self.manager.config, dry_run=True)
        with self.assertRaises(ExecutionUnavailable):
            self.make_port()
        self.adapter.create_order.assert_not_called()
        self.adapter.get_open_orders.assert_not_called()
        self.account.snapshot.assert_not_called()

    async def test_cancel_terminal_then_refresh_then_ioc_then_authenticated_flat(self):
        await self.seed_maker()
        exposure = self.port.snapshot()
        self.assertEqual(exposure.orders[0].remaining_size, D("0.2"))
        self.assertIs(exposure.orders[0].side, Side.BUY)
        result = await self.port.flatten_ioc(self.intent)
        self.assertEqual(self.events, ["cancel", "zero_orders", "account", "market",
                                      "zero_orders", "ioc", "account"])
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual((result.submitted_count, result.cancelled_count), (1, 1))
        self.assertEqual(result.account_snapshot.position, D("0"))
        self.assertEqual(result.snapshot.managed_order_count, 0)
        self.assertEqual(self.manager.active_unwind_order_ids, {"2"})
        self.assertTrue(self.manager.active_unwind_order_ids <= self.manager.terminal_order_ids)

    async def test_exact_partial_and_no_fill_return_fresh_residual_not_flat(self):
        for fill in (D("0"), D("0.1")):
            with self.subTest(fill=fill):
                self.setUp()
                self.fill_size = fill
                result = await self.port.flatten_ioc(self.intent)
                self.assertIs(result.status, ExecutionStatus.CONFIRMED)
                self.assertEqual(result.account_snapshot.position, D("-0.2") + fill)
                self.assertEqual(result.submitted_count, 1)
                self.adapter.create_order.assert_awaited_once()

    async def test_cancel_race_shrink_allowed_but_reversal_or_growth_blocks(self):
        for position, status in ((D("-0.1"), ExecutionStatus.CONFIRMED),
                                 (D("0.1"), ExecutionStatus.BLOCKED),
                                 (D("-0.3"), ExecutionStatus.BLOCKED)):
            with self.subTest(position=position):
                self.setUp()
                self.position = position
                result = await self.port.flatten_ioc(self.intent)
                self.assertIs(result.status, status)
                if status is ExecutionStatus.CONFIRMED:
                    self.assertEqual(self.adapter.create_order.call_args.args[3], D("0.1"))
                    self.assertEqual(self.adapter.create_order.call_args.args[4], D("101.2"))
                else:
                    self.adapter.create_order.assert_not_called()

    async def test_flat_after_prepare_and_cancel_only_have_fresh_auth_proof(self):
        self.position = D("0")
        result = await self.port.flatten_ioc(self.intent)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(result.submitted_count, 0)
        self.adapter.create_order.assert_not_called()
        self.market.snapshot.assert_not_called()
        self.position = D("-0.2")
        await self.seed_maker()
        result = await self.port.cancel_all_managed()
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(result.account_snapshot.position, D("-0.2"))
        self.assertEqual((result.submitted_count, result.cancelled_count), (0, 1))

    async def test_unknown_or_uncertain_state_never_enters_mutation_or_resolver(self):
        for kind in ("unknown", "uncertain"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "unknown":
                    self.manager.runtime_state = RuntimeState.PAUSED_ORDER_STATE
                else:
                    self.adapter.get_unresolved_submissions.return_value = [SimpleNamespace()]
                for result in (await self.port.flatten_ioc(self.intent),
                               await self.port.cancel_all_managed()):
                    self.assertIs(result.status, ExecutionStatus.BLOCKED)
                self.adapter.create_order.assert_not_called()
                self.adapter.get_open_orders.assert_not_called()
                self.adapter.resolve_unresolved_submissions.assert_not_called()

    async def test_missing_exact_terminal_latches_and_never_blind_retries(self):
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = legacy.exchange_order(
            "pending", OrderSide.BUY, price="101.2", status=OrderStatus.PENDING,
            params={"time_in_force": "IOC", "reduce_only": True})
        first = await self.port.flatten_ioc(self.intent)
        second = await self.port.flatten_ioc(self.intent)
        self.assertIs(first.status, ExecutionStatus.BLOCKED)
        self.assertIs(second.status, ExecutionStatus.BLOCKED)
        self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)
        self.assertTrue(self.manager.has_uncertain_state)
        self.adapter.create_order.assert_awaited_once()

    async def test_untrusted_or_pre_prepare_truth_and_price_outside_bound_block(self):
        account = await self.account_snapshot()
        market = self.market_snapshot()
        cases = (("account", replace(account, authenticated=False)),
                 ("account", replace(account, observed_monotonic=99)),
                 ("account", replace(account, open_order_count=1)),
                 ("market", replace(market, trusted=False)),
                 ("market", replace(market, observed_monotonic=99)),
                 ("market", replace(market, external_ask=D("102"))))
        for name, snapshot in cases:
            with self.subTest(name=name, snapshot=snapshot):
                self.setUp()
                port = self.account if name == "account" else self.market
                port.snapshot.side_effect = None
                port.snapshot.return_value = snapshot
                result = await self.port.flatten_ioc(self.intent)
                self.assertIs(result.status, ExecutionStatus.BLOCKED)
                self.adapter.create_order.assert_not_called()

    async def test_missing_post_ioc_audit_cannot_claim_terminal_flat(self):
        initial = await self.account_snapshot()
        self.account.snapshot.side_effect = [initial, RuntimeError("DO_NOT_ECHO_SECRET")]
        result = await self.port.flatten_ioc(self.intent)
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertIsNone(result.account_snapshot)
        self.assertEqual(result.submitted_count, 1)
        self.assertNotIn("DO_NOT_ECHO_SECRET", repr(result))
        self.adapter.create_order.assert_awaited_once()
        self.assertIs((await self.port.flatten_ioc(self.intent)).status, ExecutionStatus.BLOCKED)
        self.adapter.create_order.assert_awaited_once()

    async def test_deadline_before_prepare_or_during_account_wait_never_submits(self):
        result = await self.port.flatten_ioc(replace(self.intent, deadline_monotonic=100))
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.adapter.get_open_orders.assert_not_called()
        self.setUp()

        async def never():
            await asyncio.Event().wait()

        self.account.snapshot.side_effect = never
        result = await self.port.flatten_ioc(replace(self.intent, deadline_monotonic=100.01))
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)
        self.adapter.create_order.assert_not_called()

    async def test_canceled_ioc_wait_latches_and_rechecks_live_mode(self):
        started = asyncio.Event()

        async def never(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.adapter.create_order.side_effect = never
        task = asyncio.create_task(self.port.flatten_ioc(self.intent))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)
        self.assertTrue(self.manager.has_uncertain_state)
        self.assertIs((await self.port.flatten_ioc(self.intent)).status, ExecutionStatus.BLOCKED)
        self.adapter.create_order.assert_awaited_once()
        self.manager.config = replace(self.manager.config, dry_run=True)
        with self.assertRaises(ExecutionUnavailable):
            await self.port.cancel_all_managed()

    async def test_normal_quote_api_remains_unavailable(self):
        with self.assertRaises(ExecutionUnavailable):
            await self.port.reconcile_quotes(QuotePlan("BTC"))
        self.adapter.create_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
