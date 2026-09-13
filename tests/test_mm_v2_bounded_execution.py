"""Public V2 bridge contracts against the V2 manager and fake exchange only."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import mm_v2_execution_fixtures as fixtures
from core.adapters.exchanges.models import OrderSide, OrderStatus
from core.services.market_maker_v2.execution_models import DesiredOrder, DesiredQuotes, MarketMetadata, OrderSlotState, RuntimeState
from core.services.market_maker_v2.order_manager import MarketMakerOrderManager
from core.services.market_maker_v2.execution_models import RiskDecision
from core.services.market_maker_v2.config import ExecutionSettings
from core.services.market_maker_v2.domain import (
    AccountSnapshot, ExecutionHealth, ExecutionStatus, FlattenIntent,
    MarketStateSnapshot, QuotePlan, Side,
)
from core.services.market_maker_v2.execution_port import (
    ExecutionUnavailable, BoundedExecutionPort,
)
from core.services.market_maker_v2.telemetry import failure_diagnostic


D = Decimal


class BoundedExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.time = fixtures.Clock()
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
        config = ExecutionSettings("BTC", D("0.2"), D("1"), 1, False,
                                   active_unwind_max_attempts=2,
                                   active_unwind_confirmation_timeout_seconds=1)
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
        return BoundedExecutionPort(self.manager, self.account, self.market,
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
        return fixtures.exchange_order(str(self.sequence), side, status=status,
                                     price=str(price), amount=str(amount),
                                     remaining=str(amount - fill), params=params)

    async def cancel(self, order_id, symbol):
        self.events.append("cancel")
        return fixtures.exchange_order(order_id, OrderSide.BUY, price="100", amount="0.2",
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
                BoundedExecutionPort(self.manager, self.account, self.market,
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

    async def test_cancel_cleanup_reads_bounded_terminal_proof_without_resending(self):
        for outcome in ("terminal", "still_open", "absent", "wrong_id", "same_client_known_id",
                        "wrong_amount", "wrong_price", "generation", "new_halt", "deadline"):
            with self.subTest(outcome=outcome):
                self.setUp()
                await self.seed_maker()
                order = fixtures.exchange_order("1", OrderSide.BUY, price="100", amount="0.2")
                pending = []
                def cancel(identifier, symbol):
                    pending.append((symbol, identifier))
                    return None
                self.adapter.cancel_order.side_effect = cancel
                self.adapter.get_unresolved_cancellations.side_effect = lambda: pending[:]
                if outcome == "same_client_known_id":
                    self.manager._known_order_ids.add("other")
                proofs = [] if outcome in {"still_open", "absent"} else [
                    replace(order, id="other" if outcome in {"wrong_id", "same_client_known_id"} else "1",
                            client_id="unrelated" if outcome == "wrong_id" else order.client_id,
                            amount=D("0.3") if outcome == "wrong_amount" else order.amount,
                            remaining=D("0.3") if outcome == "wrong_amount" else order.remaining,
                            price=D("101") if outcome == "wrong_price" else order.price,
                            status=OrderStatus.CANCELED)]
                self.adapter.get_order_history.return_value = proofs

                def confirm(proof):
                    pending.clear()
                    return True

                async def read(symbol):
                    if outcome == "generation":
                        self.manager._record_mutation()  # A concurrent mutation invalidates this read.
                    if outcome == "new_halt":
                        self.port._halt()  # New read-time fault must not be cleared by older recovery.
                    if outcome == "deadline":
                        self.time.value += 11
                    return [order] if outcome == "still_open" else []

                self.adapter.confirm_terminal_cancellation_outcome.side_effect = confirm
                self.adapter.get_open_orders.side_effect = read
                admitted = []
                self.port._before_cleanup_reconcile = lambda: admitted.append(self.time())
                self.adapter.get_open_orders.reset_mock()
                async def advance(seconds):
                    self.time.value += seconds
                with patch("core.services.market_maker_v2.execution_port.asyncio.sleep", new=AsyncMock(side_effect=advance)):
                    result = await self.port.cancel_all_managed()
                expected_reads = 1 if outcome in {"terminal", "generation", "new_halt", "deadline"} else 2
                self.assertEqual(len(admitted), expected_reads)
                self.assertEqual(self.adapter.cancel_order.await_count, 1)
                self.assertEqual(self.adapter.get_open_orders.await_count, expected_reads)
                self.assertEqual(self.adapter.create_order.await_count, 1, "only the seeded maker")
                self.assertFalse(self.port.can_reconcile_cancellation)
                reads = self.adapter.get_open_orders.await_count
                self.assertFalse(await self.port.reconcile_cancellation_for_cleanup(self.time() + 10))
                self.assertEqual(self.adapter.get_open_orders.await_count, reads)
                if outcome == "terminal":
                    self.assertIs(result.status, ExecutionStatus.CONFIRMED)
                    self.assertIs(result.snapshot.health, ExecutionHealth.HEALTHY)
                    self.assertEqual(result.account_snapshot.open_order_count, 0)
                    self.assertEqual(pending, [])
                else:
                    self.assertIs(result.status, ExecutionStatus.BLOCKED)
                    self.assertIs(result.snapshot.health, ExecutionHealth.HALTED)
                    self.account.snapshot.assert_not_called()

    async def test_cancel_recovery_delayed_proof_keeps_original_deadline_and_mutations(self):
        for outcome in ("canceled", "partial_fill", "filled", "still_active", "absent"):
            with self.subTest(outcome=outcome):
                self.setUp()
                await self.seed_maker()
                order = fixtures.exchange_order("1", OrderSide.BUY, price="100", amount="0.2")
                pending = []
                self.adapter.get_unresolved_cancellations.side_effect = lambda: pending[:]
                def cancel(identifier, symbol):
                    pending.append((symbol, identifier))
                    return None
                self.adapter.cancel_order.side_effect = cancel
                diagnostics = []
                self.port._on_failure = lambda stage, error: diagnostics.append((stage, error))
                admissions = []
                self.port._before_cleanup_reconcile = lambda: admissions.append(self.time())
                terminal = replace(order, status=OrderStatus.FILLED if outcome == "filled" else OrderStatus.CANCELED,
                    filled=D("0.1") if outcome == "partial_fill" else D("0.2") if outcome == "filled" else D("0"),
                    remaining=D("0.1") if outcome == "partial_fill" else D("0") if outcome == "filled" else D("0.2"))

                async def read(symbol):
                    return [order] if self.adapter.get_open_orders.await_count == 1 or outcome == "still_active" else []

                def confirm(proof):
                    if proof is terminal:
                        pending.clear()
                        self.position += proof.filled
                        return True
                    return False

                async def advance(seconds):
                    self.assertEqual(seconds, 0.5)
                    self.time.value += seconds

                self.adapter.get_open_orders.side_effect = read
                self.adapter.get_order_history.return_value = [] if outcome in {"absent", "still_active"} else [terminal]
                self.adapter.confirm_terminal_cancellation_outcome.side_effect = confirm
                with patch("core.services.market_maker_v2.execution_port.asyncio.sleep", new=AsyncMock(side_effect=advance)) as sleep:
                    result = await self.port.cancel_all_managed()
                self.assertEqual(admissions, [100.0, 100.5])
                sleep.assert_awaited_once_with(0.5)
                self.assertEqual(self.adapter.get_open_orders.await_count, 2)
                self.adapter.cancel_order.assert_awaited_once_with("1", "BTC")
                self.assertEqual(self.adapter.create_order.await_count, 1, "only the pre-existing maker")
                self.assertFalse(self.port.can_reconcile_cancellation)
                valid = outcome in {"canceled", "partial_fill", "filled"}
                if valid:
                    self.assertIs(result.status, ExecutionStatus.CONFIRMED)
                    self.assertIs(result.snapshot.health, ExecutionHealth.HEALTHY)
                    self.assertEqual(result.account_snapshot.position, D("-0.2") + terminal.filled)
                    self.assertFalse(diagnostics)
                else:
                    self.assertIs(result.status, ExecutionStatus.BLOCKED)
                    self.assertIs(result.snapshot.health, ExecutionHealth.HALTED)
                    self.account.snapshot.assert_not_awaited()
                    recovery = [error.diagnostic_values for stage, error in diagnostics if stage == "exit_order_sync"]
                    self.assertEqual(recovery, [{"recovery_reads": D(2), "recovery_pending_count": D(1),
                        "recovery_deadline_remaining_ms": D("9500.0"), "recovery_terminal_pending": D(1)}])

    async def test_cancel_recovery_second_read_requires_unchanged_scope_admission_and_time(self):
        from core.services.market_maker_v2.api_budget import ApiBudgetUnavailable

        for outcome in ("new_fault", "new_unknown", "new_submission", "invalid_slot", "foreign_registry", "other_owned_cancel",
                        "generation", "known_ids", "budget_refusal", "short_deadline", "read_timeout"):
            with self.subTest(outcome=outcome):
                self.setUp()
                await self.seed_maker()
                if outcome == "other_owned_cancel":
                    self.manager._known_order_ids.add("other-owned")
                order = fixtures.exchange_order("1", OrderSide.BUY, price="100", amount="0.2")
                self.adapter.cancel_order.side_effect = None
                self.adapter.cancel_order.return_value = None
                diagnostics, admissions = [], []
                self.port._on_failure = lambda stage, error: diagnostics.append((stage, error))

                def admit():
                    admissions.append(self.time())
                    if len(admissions) == 2 and outcome == "budget_refusal":
                        raise ApiBudgetUnavailable("fixture refusal without provider payload")

                async def read(symbol):
                    if outcome == "short_deadline":
                        self.time.value = 109.6
                    if outcome == "read_timeout":
                        raise TimeoutError
                    return [order]

                async def advance(seconds):
                    self.time.value += seconds
                    if outcome == "new_fault":
                        self.port._halt()
                    elif outcome == "new_unknown":
                        self.manager._pause("unknown open order during recovery delay")
                    elif outcome == "new_submission":
                        self.adapter.get_unresolved_submissions.return_value = [{}]
                    elif outcome == "invalid_slot":
                        self.manager._slots[OrderSide.BUY].state = OrderSlotState.UNCERTAIN_SUBMISSION
                    elif outcome == "foreign_registry":
                        self.adapter.get_unresolved_cancellations.return_value = [("ETH", "1")]
                    elif outcome == "other_owned_cancel":
                        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "other-owned")]
                    elif outcome == "generation":
                        self.manager._record_mutation()
                    elif outcome == "known_ids":
                        self.manager._known_order_ids.add("new-id")

                self.port._before_cleanup_reconcile = admit
                self.adapter.get_open_orders.side_effect = read
                with patch("core.services.market_maker_v2.execution_port.asyncio.sleep", new=AsyncMock(side_effect=advance)) as sleep:
                    result = await self.port.cancel_all_managed()
                    self.assertIs(result.status, ExecutionStatus.BLOCKED)
                self.assertEqual(self.adapter.get_open_orders.await_count, 1)
                self.adapter.get_order_history.assert_not_awaited()
                self.adapter.cancel_order.assert_awaited_once_with("1", "BTC")
                self.assertEqual(self.adapter.create_order.await_count, 1)
                self.account.snapshot.assert_not_awaited()
                self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)
                self.assertFalse(self.port.can_reconcile_cancellation)
                self.assertEqual(len(admissions), 2 if outcome == "budget_refusal" else 1)
                self.assertEqual(sleep.await_count, 0 if outcome in {"short_deadline", "read_timeout"} else 1)
                recovery = [error.diagnostic_values for stage, error in diagnostics if stage == "exit_order_sync"]
                self.assertEqual(len(recovery), 1)
                self.assertEqual(recovery[0]["recovery_reads"], D(1))
                self.assertEqual(recovery[0]["recovery_pending_count"], D(1))
                reasons = set(recovery[0]) - {"recovery_reads", "recovery_pending_count", "recovery_deadline_remaining_ms"}
                expected = ("recovery_scope_changed" if outcome in {"new_fault", "generation", "known_ids"}
                    else "recovery_budget_refused" if outcome == "budget_refusal"
                    else "recovery_deadline_exhausted" if outcome in {"short_deadline", "read_timeout"}
                    else "recovery_ineligible")
                self.assertEqual(reasons, {expected})
                if outcome == "budget_refusal":
                    cleanup = [error for stage, error in diagnostics if stage == "cancel_managed_orders"]
                    self.assertEqual(len(cleanup), 1)
                    self.assertIs(type(cleanup[0]), ExecutionUnavailable)
                    self.assertTrue(self.manager.last_result.errors, "original cancel failure remains available")

    async def test_cancel_recovery_never_clears_a_generic_halt_or_read_refusal(self):
        from core.services.market_maker_v2.api_budget import ApiBudgetUnavailable

        for outcome in ("generic_halt", "generic_during_cancel", "read_refusal"):
            with self.subTest(outcome=outcome):
                self.setUp()
                await self.seed_maker()
                self.adapter.cancel_order.side_effect = None
                self.adapter.cancel_order.return_value = None
                if outcome == "generic_halt":
                    self.port._halt()
                    self.assertFalse(await self.port.reconcile_cancellation_for_cleanup(110))
                    self.adapter.cancel_order.assert_not_called()
                elif outcome == "generic_during_cancel":
                    def cancel(identifier, symbol):
                        self.port._halt()
                        return None
                    self.adapter.cancel_order.side_effect = cancel
                    result = await self.port.cancel_all_managed()
                    self.assertIs(result.status, ExecutionStatus.BLOCKED)
                    self.assertEqual(self.adapter.cancel_order.await_count, 1)
                    self.assertFalse(self.port.can_reconcile_cancellation)
                else:
                    def refuse():
                        raise ApiBudgetUnavailable("fixture read denial")
                    self.port._before_cleanup_reconcile = refuse
                    with self.assertRaises(ApiBudgetUnavailable):
                        await self.port.cancel_all_managed()
                    self.assertEqual(self.adapter.cancel_order.await_count, 1)
                self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)
                self.adapter.get_open_orders.assert_not_called()
                self.account.snapshot.assert_not_called()

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
        self.adapter.create_order.return_value = fixtures.exchange_order(
            "pending", OrderSide.BUY, price="101.2", status=OrderStatus.PENDING,
            params={"time_in_force": "IOC", "reduce_only": True})
        first = await self.port.flatten_ioc(self.intent)
        second = await self.port.flatten_ioc(self.intent)
        self.assertIs(first.status, ExecutionStatus.BLOCKED)
        self.assertIs(second.status, ExecutionStatus.BLOCKED)
        self.assertIs(self.port.snapshot().health, ExecutionHealth.HALTED)
        self.assertTrue(self.manager.has_uncertain_state)
        self.adapter.create_order.assert_awaited_once()

    async def test_untrusted_or_pre_prepare_truth_blocks(self):
        account = await self.account_snapshot()
        market = self.market_snapshot()
        cases = (("account", replace(account, authenticated=False)),
                 ("account", replace(account, observed_monotonic=99)),
                 ("account", replace(account, open_order_count=1)),
                 ("market", replace(market, trusted=False)),
                 ("market", replace(market, observed_monotonic=99)))
        for name, snapshot in cases:
            with self.subTest(name=name, snapshot=snapshot):
                self.setUp()
                diagnostics = []
                self.port._on_failure = lambda stage, error: diagnostics.append(
                    failure_diagnostic("BTC", stage, error))
                port = self.account if name == "account" else self.market
                port.snapshot.side_effect = None
                port.snapshot.return_value = snapshot
                result = await self.port.flatten_ioc(self.intent)
                self.assertIs(result.status, ExecutionStatus.BLOCKED)
                self.adapter.create_order.assert_not_called()
                values = {row.name: row.value for row in diagnostics[0].values}
                if name == "market" and snapshot.trusted:
                    self.assertEqual(values, {"exit_book_age_ms": D("1000"),
                                              "exit_book_after_prepare_ms": D("-1000")})
                else:
                    self.assertEqual(values, {})

    async def test_nonmarketable_fixed_ioc_returns_known_zero_fill_on_each_side(self):
        for side, position, limit, bid, ask in (
                (Side.BUY, D("-0.2"), D("101.2"), D("101.9"), D("102")),
                (Side.SELL, D("0.2"), D("100.7"), D("100.6"), D("100.8"))):
            with self.subTest(side=side):
                self.setUp()
                self.position, self.fill_size = position, D("0")
                market = replace(self.market_snapshot(), external_bid=bid, external_ask=ask)
                self.market.snapshot.side_effect = lambda: market
                result = await self.port.flatten_ioc(replace(
                    self.intent, side=side, limit_price=limit))
                self.assertIs(result.status, ExecutionStatus.CONFIRMED)
                self.assertIs(result.snapshot.health, ExecutionHealth.HEALTHY)
                self.assertEqual(result.account_snapshot.position, position)
                self.assertEqual(result.submitted_count, 1)
                call = self.adapter.create_order.call_args
                self.assertEqual(call.args[3:5], (D("0.2"), limit))
                self.assertEqual(call.kwargs["params"], {"time_in_force": "IOC", "reduce_only": True})
                self.assertFalse(self.manager.has_uncertain_state)
                self.assertTrue(self.manager.active_unwind_order_ids <= self.manager.terminal_order_ids)

    async def test_moving_book_exit_preserves_bound_until_recovery_or_exhaustion(self):
        from core.services.market_maker_v2.domain import ExitStatus
        from core.services.market_maker_v2.orchestrator import bounded_exit

        for side in (Side.BUY, Side.SELL):
            for recovers in (False, True):
                with self.subTest(side=side, recovers=recovers):
                    self.setUp()
                    self.position = D("-0.2") if side is Side.BUY else D("0.2")
                    initial = self.market_snapshot()
                    outside = replace(initial, external_bid=D("102"), external_ask=D("102.1")) \
                        if side is Side.BUY else replace(initial, external_bid=D("100"), external_ask=D("100.1"))
                    # The initial book sets the bound. The first IOC observes
                    # a moved book and is terminal with zero fill; only a later
                    # price recovery permits execution at the original limit.
                    books = iter([initial, outside, initial if recovers else outside, outside])
                    current = initial

                    def market_snapshot():
                        nonlocal current
                        current = next(books)
                        return current

                    async def create(symbol, order_side, order_type, amount, price, params):
                        marketable = (current.external_ask <= price if order_side is OrderSide.BUY
                                      else current.external_bid >= price)
                        self.fill_size = amount if marketable else D("0")
                        return await self.create(symbol, order_side, order_type, amount, price, params)

                    self.market.snapshot.side_effect = market_snapshot
                    self.adapter.create_order.side_effect = create
                    self.port.flatten_ioc = AsyncMock(wraps=self.port.flatten_ioc)
                    report = await bounded_exit(self.port, self.market, self.clock, symbol="BTC",
                        flatten_id="exit-moving-book", deadline_monotonic=105,
                        ioc_slippage_ticks=2, authorize_bounded_flatten=True)
                    self.assertIs(report.status, ExitStatus.FLAT if recovers else ExitStatus.ATTEMPTS_EXHAUSTED)
                    self.assertEqual(report.attempts, 2 if recovers else 3)
                    expected_position = D("0") if recovers else (D("-0.2") if side is Side.BUY else D("0.2"))
                    self.assertEqual(report.final_result.account_snapshot.position, expected_position)
                    self.assertIs(report.final_result.snapshot.health, ExecutionHealth.HEALTHY)
                    self.assertFalse(self.manager.has_uncertain_state)
                    intents = [call.args[0] for call in self.port.flatten_ioc.call_args_list]
                    self.assertEqual({intent.limit_price for intent in intents},
                                     {D("101.2") if side is Side.BUY else D("100.7")})
                    self.assertEqual({intent.deadline_monotonic for intent in intents}, {105})
                    self.assertEqual({intent.size for intent in intents}, {D("0.2")})
                    self.assertEqual(self.adapter.create_order.await_count, report.attempts)

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
