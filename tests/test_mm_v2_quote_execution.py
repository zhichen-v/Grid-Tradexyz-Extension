"""Normal V2 execution: V2 manager and fake exchange; no actual connection."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import mm_v2_execution_fixtures as fixtures
from core.adapters.exchanges.models import OrderSide, OrderStatus
from core.services.market_maker_v2.execution_models import MarketMetadata, RuntimeState
from core.services.market_maker_v2.order_manager import MarketMakerOrderManager
from core.services.market_maker_v2.domain import (
    AccountSnapshot, ExecutionHealth, ExecutionStatus, FlattenIntent,
    InventoryDecision, MarketStateSnapshot, QuoteAuthorization, QuoteIntent,
    QuotePlan, SessionReport, Side, StrategyState,
)
from core.services.market_maker_v2.execution_port import (
    DryVolumeExecutionPort, ExecutionUnavailable, VolumeExecutionPort,
)
from core.services.market_maker_v2.config import ExecutionSettings, execution_settings
from core.services.market_maker_v2.inventory_governor import InventoryGovernor
from core.services.market_maker_v2.quote_policy import VolumeQuotePolicy


class QuoteExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.time = fixtures.Clock()
        self.clock = SimpleNamespace(monotonic=self.time)
        self.position, self.maker_fee = D("0"), D("0")
        self.sequence, self.events, self.open, self.history = 0, [], {}, []
        self.bid, self.ask = D("99.9"), D("100.1")
        self.adapter = SimpleNamespace(
            create_order=AsyncMock(side_effect=self.create),
            cancel_order=AsyncMock(side_effect=self.cancel),
            get_open_orders=AsyncMock(side_effect=lambda symbol: list(self.open.values())),
            get_order_history=AsyncMock(side_effect=lambda symbol: self.history),
            get_unresolved_submissions=Mock(return_value=[]),
            get_unresolved_cancellations=Mock(return_value=[]),
            get_terminal_cancellation_outcome=Mock(return_value=None),
            confirm_terminal_cancellation_outcome=Mock(return_value=False),
            resolve_unresolved_submissions=AsyncMock(return_value=[]),
        )
        settings = ExecutionSettings("BTC", D("0.2"), D("1"), 5, False)
        metadata = MarketMetadata("BTC", 1, 1, D("0.1"), D("0.1"), D("0.1"), D("0"))
        self.manager = MarketMakerOrderManager(self.adapter, settings, metadata, monotonic=self.time)
        self.account = SimpleNamespace(snapshot=AsyncMock(side_effect=self.account_snapshot))
        self.market = SimpleNamespace(snapshot=Mock(side_effect=self.market_snapshot))
        self.governor = InventoryGovernor(order_size=D("0.2"), soft_limit=D("0.5"),
            hard_limit=D("1"), stop_loss_usdg=D("10"), max_hold_seconds=60,
            cooldown_seconds=5, max_session_loss_usdg=D("1000"),
            session_started_monotonic=0, session_deadline_monotonic=1000, ioc_slippage_ticks=2)
        self.policy = VolumeQuotePolicy(order_size=D("0.2"), target_net_edge_bps=D("0"),
            volatility_multiplier=D("0"), hard_inventory_limit=D("1"), skew_bps_at_hard=D("0"))
        self.refresh = AsyncMock(side_effect=self.refresh_quote)
        self.port = self.make_port()
        self.proposal = QuotePlan("BTC", (QuoteIntent(Side.BUY, D("77"), D("0.2")),))

    def make_port(self, **changes):
        values = dict(refresh_quote=self.refresh, reprice_threshold_ticks=5,
                      max_quote_age_ms=5000, authorize_bounded_flatten=True)
        return VolumeExecutionPort(self.manager, self.account, self.market,
                                         self.clock, **(values | changes))

    async def create(self, symbol, side, order_type, amount, price, params):
        self.sequence += 1
        self.events.append(("create", side, amount, price))
        self.assertEqual(params.get("time_in_force"), "POST_ONLY")
        order = fixtures.exchange_order(str(self.sequence), side, amount=str(amount),
                                      price=str(price), params=params)
        self.open[order.id] = order
        return order

    async def cancel(self, order_id, symbol):
        self.events.append(("cancel", order_id))
        order = self.open.pop(order_id)
        terminal = replace(order, status=OrderStatus.CANCELED, params={"cancel_terminal": True})
        self.history.append(terminal)
        return terminal

    async def account_snapshot(self):
        self.events.append(("account", self.position))
        return AccountSnapshot("BTC", self.time(), self.position, D("1000"),
            self.maker_fee, D("0.0004"), len(self.open), True,
            entry_price=D("100") if self.position else None, open_order_ids=tuple(self.open))

    def market_snapshot(self):
        return MarketStateSnapshot("BTC", self.time(), self.bid, self.ask,
                                   D("0.1"), D("0.1"), D("0.1"), True)

    async def refresh_quote(self, execution):
        account = await self.account.snapshot()
        market = self.market.snapshot()
        report = SessionReport("BTC", False, self.position, None,
            ledger_position=self.position, duration_seconds=D(str(self.time())),
            inventory_age=D("5") if self.position else D("0"))
        risk = self.governor.evaluate(market, account, report, execution, now=self.time())
        plan = self.policy.propose(market, account, risk, now=self.time())
        self.events.append(("authorize", tuple(o.order_id for o in execution.orders), self.position))
        return QuoteAuthorization(account, market, risk, plan)

    async def quote_both(self):
        for _ in range(2):
            result = await self.port.reconcile_quotes(self.proposal)
            self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(len(self.open), 2)

    async def test_cycle_places_both_sides_with_fresh_authority_between_creates(self):
        first = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(first.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(first.submitted_count, 2)
        self.assertEqual(len(first.snapshot.orders), 2)
        self.assertIsNone(first.account_snapshot)
        kinds = [event[0] for event in self.events]
        creates = [i for i, kind in enumerate(kinds) if kind == "create"]
        self.assertIn("authorize", kinds[creates[0] + 1:creates[1]])
        self.assertIn("account", kinds[creates[0] + 1:creates[1]])
        second = await self.port.reconcile_quotes(self.proposal)
        self.assertEqual(second.submitted_count, 0)
        self.assertEqual({o.side for o in second.snapshot.orders}, {Side.BUY, Side.SELL})
        self.assertEqual(second.actual_plan.symbol, "BTC")
        self.assertFalse(second.snapshot.simulated)

    async def test_under_threshold_quotes_are_kept_without_cancelling_or_repricing(self):
        await self.quote_both()
        ids = tuple(self.open)
        self.bid, self.ask = D("100"), D("100.2")
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(tuple(self.open), ids)
        self.assertEqual(result.submitted_count, 0)
        self.adapter.cancel_order.assert_not_called()
        self.assertEqual({q.price for q in result.actual_plan.quotes}, {D("100"), D("100.1")})

    async def test_first_create_fill_is_reaudited_before_reducing_opposite_create(self):
        async def filled_first(*args, **kwargs):
            order = await self.create(*args, **kwargs)
            if self.sequence == 1:
                self.position = order.amount
                self.open.pop(order.id)
                order = replace(order, status=OrderStatus.FILLED,
                                filled=order.amount, remaining=D("0"))
                self.history.append(order)
            return order
        self.adapter.create_order.side_effect = filled_first
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual([e[1] for e in self.events if e[0] == "create"],
                         [OrderSide.BUY, OrderSide.SELL])
        self.assertEqual([e[2] for e in self.events if e[0] == "authorize"],
                         [D("0"), D("0.2")])

    async def test_second_read_uses_original_deadline_and_retains_first_create_count(self):
        original = self.refresh_quote
        async def delayed(execution):
            if self.sequence:
                self.time.value += 11
            return await original(execution)
        self.refresh.side_effect = delayed
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(result.submitted_count, 1)
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertIs(result.snapshot.health, ExecutionHealth.HEALTHY)

    async def test_quote_age_and_reprice_threshold_force_cancel_proof_then_new_authority(self):
        for cause in ("age", "price"):
            with self.subTest(cause=cause):
                self.setUp()
                await self.quote_both()
                old_ids = set(self.open)
                if cause == "age":
                    self.time.value += 5
                else:
                    self.bid, self.ask = D("101"), D("101.2")
                self.events.clear()
                result = await self.port.reconcile_quotes(self.proposal)
                self.assertIs(result.status, ExecutionStatus.CONFIRMED)
                self.assertEqual(result.cancelled_count, 2)
                self.assertTrue(old_ids <= self.manager.terminal_order_ids)
                kinds = [event[0] for event in self.events]
                self.assertLess(max(i for i, kind in enumerate(kinds) if kind == "cancel"),
                                max(i for i, kind in enumerate(kinds) if kind == "authorize"))
                first_create = kinds.index("create")
                last_cancel = max(i for i, kind in enumerate(kinds) if kind == "cancel")
                self.assertIn("authorize", kinds[last_cancel + 1:first_create])

    async def test_cancel_fill_race_recomputes_hard_band_before_any_new_create(self):
        self.position = D("-0.9")
        await self.quote_both()
        async def race(order_id, symbol):
            if self.open[order_id].side is OrderSide.SELL:
                self.position = D("-1")
            return await self.cancel(order_id, symbol)
        self.adapter.cancel_order.side_effect = race
        self.time.value += 5
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(len(result.actual_plan.quotes), 1)
        self.assertTrue(result.actual_plan.quotes[0].reduce_only)
        self.assertIs(result.actual_plan.quotes[0].side, Side.BUY)
        self.assertTrue(self.adapter.create_order.call_args.kwargs["params"]["reduce_only"])

    async def test_fee_change_refreshes_old_quotes_even_below_reprice_threshold(self):
        await self.quote_both()
        self.maker_fee = D("0.001")
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(result.cancelled_count, 2)

    async def test_retained_price_cannot_cross_a_new_opposite_quote(self):
        async def buy_only(execution):
            value = await self.refresh_quote(execution)
            return replace(value, plan=QuotePlan("BTC", value.plan.quotes[:1]))
        self.refresh.side_effect = buy_only
        await self.port.reconcile_quotes(self.proposal)
        original = self.refresh_quote
        async def fresh(execution):
            value = await original(execution)
            return replace(value, plan=QuotePlan("BTC", (
                QuoteIntent(Side.BUY, D("99.8"), D("0.2")),
                QuoteIntent(Side.SELL, D("100"), D("0.2")))))
        self.refresh.side_effect = fresh
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(result.cancelled_count, 1)
        self.assertEqual(result.actual_plan.quotes[0].price, D("99.8"))

    async def test_known_hard_breach_is_exited_in_bounded_reduce_only_chunks(self):
        import test_mm_v2_bounded_execution as bridge
        from core.services.market_maker_v2.orchestrator import bounded_exit
        fixture = bridge.BoundedExecutionTests()
        fixture.setUp()
        fixture.manager.config = replace(fixture.manager.config, max_position=D("0.3"))
        fixture.position, fixture.fill_size = D("-0.5"), D("0.5")
        report = await bounded_exit(fixture.port, fixture.market, fixture.clock,
            symbol="BTC", flatten_id="cap-breach", deadline_monotonic=105,
            ioc_slippage_ticks=2, authorize_bounded_flatten=True)
        self.assertTrue(report.complete)
        self.assertEqual(report.attempts, 2)
        self.assertEqual([call.args[3] for call in fixture.adapter.create_order.await_args_list],
                         [D("0.3"), D("0.2")])
        self.assertEqual(fixture.position, D("0"))

    async def test_empty_requested_plan_is_cancel_only_and_never_reauthorized_into_create(self):
        await self.quote_both()
        self.refresh.reset_mock()
        self.adapter.create_order.reset_mock()
        result = await self.port.reconcile_quotes(QuotePlan("BTC"))
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(result.snapshot.managed_order_count, 0)
        self.refresh.assert_not_called()
        self.adapter.create_order.assert_not_called()

    async def test_unknown_orders_or_execution_pause_never_place(self):
        self.manager.runtime_state = RuntimeState.PAUSED_ORDER_STATE
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.refresh.assert_not_called()
        self.adapter.create_order.assert_not_called()
        self.adapter.get_open_orders.assert_not_called()

    async def test_cancelled_create_keeps_uncertainty_and_blocks_further_mutations(self):
        self.adapter.create_order.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.port.reconcile_quotes(self.proposal)
        self.assertTrue(self.manager.has_uncertain_state)
        result = await self.port.cancel_all_managed()
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.adapter.cancel_order.assert_not_called()

    async def test_moving_book_never_starves_the_opposite_side(self):
        for _ in range(6):
            result = await self.port.reconcile_quotes(self.proposal)
            self.assertIs(result.status, ExecutionStatus.CONFIRMED)
            self.assertEqual({o.side for o in result.snapshot.orders}, {Side.BUY, Side.SELL})
            self.time.value += 3
            self.bid += 1
            self.ask += 1

    async def test_post_only_rejection_requires_new_book_then_resumes_after_cooldown(self):
        async def canceled(*args, **kwargs):
            order = await self.create(*args, **kwargs)
            self.open.pop(order.id)
            terminal = replace(order, status=OrderStatus.CANCELED,
                               raw_data={"post_only_canceled": True})
            self.history.append(terminal)
            return terminal

        self.adapter.create_order.side_effect = canceled
        stale_book = self.market_snapshot()
        await self.port.reconcile_quotes(self.proposal)
        self.adapter.create_order.side_effect = self.create
        self.market.snapshot.side_effect = lambda: stale_book
        self.time.value += 0.5
        await self.port.reconcile_quotes(self.proposal)
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertTrue(self.manager.post_only_book_refresh_required)
        self.market.snapshot.side_effect = self.market_snapshot
        self.time.value += 10
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertEqual({o.side for o in result.snapshot.orders}, {Side.BUY, Side.SELL})
        self.assertFalse(self.manager.post_only_book_refresh_required)

    async def test_same_count_but_wrong_authenticated_order_identity_is_rejected(self):
        await self.quote_both()
        original = self.refresh_quote
        async def wrong(execution):
            authorization = await original(execution)
            return replace(authorization, account=replace(authorization.account,
                open_order_ids=("foreign-1", "foreign-2")))
        self.refresh.side_effect = wrong
        self.adapter.create_order.reset_mock()
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.adapter.create_order.assert_not_called()

    async def test_invalid_fresh_authority_cannot_be_bypassed_by_requested_plan(self):
        for invalid in ("stale", "capacity", "crossed", "raw"):
            with self.subTest(invalid=invalid):
                self.setUp()
                original = self.refresh_quote
                async def bad(execution):
                    value = await original(execution)
                    if invalid == "stale":
                        return replace(value, account=replace(value.account, observed_monotonic=89))
                    if invalid == "capacity":
                        return replace(value, decision=InventoryDecision(StrategyState.QUOTING))
                    if invalid == "crossed":
                        return replace(value, plan=QuotePlan("BTC", (QuoteIntent(Side.BUY,D("101"),D("0.2")),)))
                    return {}
                self.refresh.side_effect = bad
                result = await self.port.reconcile_quotes(self.proposal)
                self.assertIs(result.status, ExecutionStatus.BLOCKED)
                self.adapter.create_order.assert_not_called()

    async def test_failed_terminal_cancellation_never_refreshes_into_new_order(self):
        await self.quote_both()
        self.time.value += 5
        self.adapter.cancel_order.side_effect = ConnectionError("not proof")
        self.adapter.create_order.reset_mock()
        result = await self.port.reconcile_quotes(self.proposal)
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.adapter.create_order.assert_not_called()
        self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)

    async def test_authorization_missing_rejects_before_any_read_or_mutation(self):
        with self.assertRaises(ExecutionUnavailable):
            self.make_port(authorize_bounded_flatten=False)
        self.adapter.get_open_orders.assert_not_called()
        self.account.snapshot.assert_not_called()

    async def test_dry_nonempty_model_has_no_exchange_calls_or_fake_fills(self):
        async def refresh(execution):
            account = await self.account_snapshot()
            risk = InventoryDecision(StrategyState.QUOTING, buy_capacity=D("0.2"), sell_capacity=D("0.2"))
            plan = self.policy.propose(self.market_snapshot(), account, risk, now=self.time())
            return QuoteAuthorization(account, self.market_snapshot(), risk, plan)
        port = DryVolumeExecutionPort("BTC", self.clock, refresh_quote=refresh,
                                     reprice_threshold_ticks=5, max_quote_age_ms=5000)
        first = await port.reconcile_quotes(self.proposal)
        self.assertIs(first.status, ExecutionStatus.SIMULATED)
        self.assertTrue(first.snapshot.simulated)
        self.assertEqual(first.submitted_count, 2)
        self.assertIsNone(first.account_snapshot)
        self.assertEqual((await port.reconcile_quotes(self.proposal)).submitted_count, 0)
        self.time.value += 5
        aged = await port.reconcile_quotes(self.proposal)
        self.assertEqual((aged.cancelled_count, aged.submitted_count), (2, 2))
        cancelled = await port.cancel_all_managed()
        self.assertIs(cancelled.status, ExecutionStatus.SIMULATED)
        self.assertEqual(cancelled.snapshot.orders, ())
        with self.assertRaises(ExecutionUnavailable):
            await port.flatten_ioc(FlattenIntent("BTC", Side.BUY, D("0.1"), D("101"), 110))
        self.adapter.create_order.assert_not_called()
        self.adapter.cancel_order.assert_not_called()
        self.assertEqual(self.position, D("0"))

    def test_settings_builder_is_execution_only_and_accepts_public_manager_constructor(self):
        from core.services.market_maker_v2.config import load_config
        config = load_config("config/market_maker_v2/lighter_btc_volume.example.yaml")
        settings = execution_settings(config)
        self.assertTrue(settings.dry_run)
        self.assertEqual(settings.max_position, config.inventory.hard_limit)
        self.assertFalse(hasattr(settings, "ping_pong_enabled"))
        self.assertFalse(hasattr(settings, "max_episode_loss_for_unwind"))
        manager = MarketMakerOrderManager(self.adapter, settings, self.manager.metadata)
        self.assertEqual(manager.snapshot(), ())


if __name__ == "__main__":
    unittest.main()
