"""Whole-exit contracts: actual V2 manager, fake exchange, no live connection."""

import asyncio
from dataclasses import replace
from decimal import Decimal
import itertools
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_mm_v2_bounded_execution as bridge
from core.adapters.exchanges.models import OrderSide, OrderStatus
from core.services.market_maker_v2.execution_models import RuntimeState
from core.services.market_maker_v2.domain import (
    AccountSnapshot, ExecutionHealth, ExecutionResult, ExecutionSnapshot,
    ExecutionStatus, ExitStatus, FillEvent, LiquidityRole, Side,
)
from core.services.market_maker_v2.orchestrator import bounded_exit, DryCycleUnavailable
from core.services.market_maker_v2.session_ledger import SessionLedger
from core.services.market_maker_v2.telemetry import JsonlTelemetrySink


D = Decimal


class BoundedExitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = bridge.BoundedExecutionTests()
        self.fixture.setUp()

    async def exit(self, **changes):
        values = dict(symbol="BTC", flatten_id="exit-1", deadline_monotonic=105.0,
                      ioc_slippage_ticks=2, authorize_bounded_flatten=True)
        values.update(changes)
        fixture = self.fixture
        return await bounded_exit(fixture.port, fixture.market, fixture.clock, **values)

    async def test_partial_ioc_retries_only_residual_with_fixed_price_and_deadline(self):
        fixture = self.fixture
        fixture.fill_size = D("0.1")
        fixture.port.flatten_ioc = AsyncMock(wraps=fixture.port.flatten_ioc)
        report = await self.exit()
        self.assertIs(report.status, ExitStatus.FLAT)
        self.assertTrue(report.complete)
        self.assertEqual(report.attempts, 2)
        self.assertEqual(report.final_result.account_snapshot.position, D("0"))
        intents = [call.args[0] for call in fixture.port.flatten_ioc.call_args_list]
        self.assertEqual([intent.size for intent in intents], [D("0.2"), D("0.1")])
        self.assertEqual({intent.limit_price for intent in intents}, {D("101.2")})
        self.assertEqual({intent.deadline_monotonic for intent in intents}, {105.0})
        self.assertEqual(fixture.adapter.create_order.await_count, 2)

    async def test_zero_fill_stops_at_three_attempts_without_widening_or_inflation(self):
        fixture = self.fixture
        fixture.fill_size = D("0")
        fixture.port.flatten_ioc = AsyncMock(wraps=fixture.port.flatten_ioc)
        report = await self.exit(deadline_monotonic=1000)
        self.assertIs(report.status, ExitStatus.ATTEMPTS_EXHAUSTED)
        self.assertFalse(report.complete)
        self.assertEqual(report.attempts, 3)
        self.assertEqual(report.final_result.account_snapshot.position, D("-0.2"))
        intents = [call.args[0] for call in fixture.port.flatten_ioc.call_args_list]
        self.assertEqual({intent.size for intent in intents}, {D("0.2")})
        self.assertEqual({intent.limit_price for intent in intents}, {D("101.2")})
        self.assertEqual({intent.deadline_monotonic for intent in intents}, {130.0})
        self.assertEqual(fixture.adapter.create_order.await_count, 3)

    async def test_initial_cancel_race_uses_new_full_residual_before_authorizing_ioc(self):
        fixture = self.fixture
        await fixture.seed_maker()

        async def cancel_fill_race(order_id, symbol):
            fixture.position = D("-0.3")
            return await fixture.cancel(order_id, symbol)

        fixture.adapter.cancel_order.side_effect = cancel_fill_race
        fixture.fill_size = D("0.3")
        report = await self.exit()
        self.assertIs(report.status, ExitStatus.FLAT)
        self.assertEqual(report.attempts, 1)
        self.assertEqual(fixture.adapter.create_order.call_args.args[3], D("0.3"))
        self.assertLess(fixture.events.index("cancel"), fixture.events.index("ioc"))

    async def test_flat_start_requires_cancel_proof_and_never_ioc(self):
        for proved in (True, False):
            with self.subTest(proved=proved):
                self.setUp()
                fixture = self.fixture
                await fixture.seed_maker()
                fixture.position = D("0")
                fixture.adapter.create_order.reset_mock()
                if not proved:
                    fixture.adapter.cancel_order.side_effect = ConnectionError("unavailable")
                report = await self.exit()
                self.assertIs(report.status, ExitStatus.FLAT if proved else ExitStatus.BLOCKED)
                self.assertEqual(report.attempts, 0)
                fixture.adapter.create_order.assert_not_called()
                fixture.adapter.cancel_order.assert_awaited_once()

    async def test_missing_terminal_or_post_ioc_audit_never_retries(self):
        for missing in ("terminal", "account"):
            with self.subTest(missing=missing):
                self.setUp()
                fixture = self.fixture
                if missing == "terminal":
                    fixture.adapter.create_order.side_effect = None
                    fixture.adapter.create_order.return_value = bridge.fixtures.exchange_order(
                        "pending", OrderSide.BUY, price="101.2", status=OrderStatus.PENDING,
                        params={"time_in_force": "IOC", "reduce_only": True})
                else:
                    initial = await fixture.account_snapshot()
                    fixture.account.snapshot.side_effect = [initial, initial, ConnectionError("audit")]
                report = await self.exit()
                self.assertIs(report.status, ExitStatus.BLOCKED)
                self.assertFalse(report.complete)
                self.assertEqual(report.attempts, 1)
                fixture.adapter.create_order.assert_awaited_once()
                self.assertIs(fixture.port.snapshot().health, ExecutionHealth.HALTED)

    async def test_bad_post_cancel_market_and_unknown_execution_block_before_ioc(self):
        for problem in ("stale", "untrusted", "unknown"):
            with self.subTest(problem=problem):
                self.setUp()
                fixture = self.fixture
                if problem == "unknown":
                    fixture.manager.runtime_state = RuntimeState.PAUSED_ORDER_STATE
                else:
                    market = fixture.market_snapshot()
                    fixture.market.snapshot.side_effect = None
                    fixture.market.snapshot.return_value = replace(
                        market, observed_monotonic=90 if problem == "stale" else 100,
                        trusted=problem != "untrusted")
                report = await self.exit()
                self.assertIs(report.status, ExitStatus.BLOCKED)
                self.assertEqual(report.attempts, 0)
                fixture.adapter.create_order.assert_not_called()
                fixture.adapter.resolve_unresolved_submissions.assert_not_called()
                if problem == "unknown":
                    fixture.account.snapshot.assert_not_called()
                    fixture.adapter.get_open_orders.assert_not_called()

    async def test_missing_authorization_is_rejected_before_any_port_or_clock_read(self):
        fixture = self.fixture
        fixture.clock.monotonic = Mock(side_effect=AssertionError("clock read forbidden"))
        fixture.port.snapshot = Mock(side_effect=AssertionError("port read forbidden"))
        with self.assertRaises(DryCycleUnavailable):
            await self.exit(authorize_bounded_flatten=False)
        fixture.clock.monotonic.assert_not_called()
        fixture.port.snapshot.assert_not_called()
        fixture.account.snapshot.assert_not_called()
        fixture.adapter.create_order.assert_not_called()

    async def test_wall_deadline_during_cancel_audit_and_late_flat_are_not_success(self):
        fixture = self.fixture

        async def never():
            await asyncio.Event().wait()

        fixture.account.snapshot.side_effect = never
        report = await self.exit(deadline_monotonic=100.01)
        self.assertIs(report.status, ExitStatus.DEADLINE)
        self.assertEqual(report.attempts, 0)
        fixture.adapter.create_order.assert_not_called()
        self.assertIs(fixture.port.snapshot().health, ExecutionHealth.HALTED)

        # A clock that advances at each boundary reaches the deadline only while
        # forming the report; the earlier flat account must not become late GO.
        account = AccountSnapshot("BTC", 104, D("0"), D("100"), D("0.0001"), D("0.0003"), 0, True)
        snapshot = ExecutionSnapshot(ExecutionHealth.HEALTHY, 0, False, "BTC", 104, ())
        result = ExecutionResult(ExecutionStatus.CONFIRMED, snapshot, account_snapshot=account)
        execution = SimpleNamespace(snapshot=Mock(return_value=snapshot),
                                    cancel_all_managed=AsyncMock(return_value=result),
                                    flatten_ioc=AsyncMock())
        ticks = itertools.count(100)
        clock = SimpleNamespace(monotonic=lambda: next(ticks))
        report = await bounded_exit(execution, fixture.market, clock, symbol="BTC",
                                    flatten_id="late", deadline_monotonic=106,
                                    ioc_slippage_ticks=2, authorize_bounded_flatten=True)
        self.assertIs(report.status, ExitStatus.DEADLINE)
        self.assertFalse(report.complete)
        self.assertGreaterEqual(report.observed_monotonic, 106)
        execution.flatten_ioc.assert_not_called()

    async def test_clock_or_port_connection_errors_become_sanitized_blocked_reports(self):
        for source in ("clock", "execution", "market"):
            with self.subTest(source=source):
                self.setUp()
                fixture = self.fixture
                failure = ConnectionError("DO_NOT_ECHO_SECRET")
                if source == "clock":
                    fixture.clock.monotonic = Mock(side_effect=[100, failure, failure])
                elif source == "execution":
                    fixture.port.snapshot = Mock(side_effect=failure)
                else:
                    fixture.market.snapshot.side_effect = failure
                report = await self.exit()
                self.assertIs(report.status, ExitStatus.BLOCKED)
                self.assertNotIn("DO_NOT_ECHO_SECRET", repr(report))
                fixture.adapter.create_order.assert_not_called()

    async def test_zero_fill_exit_is_one_idempotent_ledger_event_without_inventing_fees(self):
        fixture = self.fixture
        fixture.fill_size = D("0")
        report = await self.exit()
        initial = AccountSnapshot("BTC", 99, D("0"), D("100"), D("0.00012"), D("0.0004"), 0, True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with JsonlTelemetrySink(path) as telemetry:
                ledger = SessionLedger(initial, telemetry=telemetry)
                ledger.ingest_fill(FillEvent("maker", "maker", "BTC", Side.SELL,
                                             D("0.2"), D("100"), D("0.0024"),
                                             LiquidityRole.MAKER, 100))
                before = ledger.snapshot(now=100)
                self.assertTrue(ledger.record_exit(report))
                self.assertFalse(ledger.record_exit(report))
                after = ledger.snapshot(now=100)
                self.assertEqual(after.forced_flatten_count, 1)
                self.assertEqual(after.forced_flatten_loss, D("0"))
                for field in ("maker_fee", "taker_fee", "realized_gross_pnl", "realized_net_pnl",
                              "maker_turnover_total", "taker_flatten_turnover", "ledger_position"):
                    self.assertEqual(getattr(before, field), getattr(after, field), field)
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        exits = [event for event in events if event["event"] == "bounded_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["data"]["attempts"], 3)
        self.assertEqual(exits[0]["data"]["final_result"]["account_snapshot"]["position"], "-0.2")


if __name__ == "__main__":
    unittest.main()
