"""Phase 2 public dry-port contracts; no live adapter or private strategy state."""

import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.services.market_maker_v2.config import ExecutionSettings
from core.services.market_maker_v2.execution_models import MarketMetadata, RuntimeState
from core.services.market_maker_v2.order_manager import (
    MarketMakerOrderManager, ReconcileAction, ReconcileResult,
)
from core.services.market_maker_v2.domain import (
    ExecutionHealth, ExecutionStatus, FlattenIntent, QuoteIntent, QuotePlan, Side,
)
from core.services.market_maker_v2.execution_port import (
    ExecutionUnavailable, DrySafetyExecutionPort,
)


D = Decimal


class DrySafetyExecutionPortTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = SimpleNamespace(monotonic=lambda: 100.0)
        self.manager = self.stub_manager()
        self.port = DrySafetyExecutionPort(self.manager)
        self.empty = QuotePlan("BTC")
        self.flatten = FlattenIntent("BTC", Side.BUY, D("0.0002"), D("80000"), 105.0)

    def stub_manager(self):
        result = ReconcileResult((), RuntimeState.ACTIVE)
        return SimpleNamespace(
            config=SimpleNamespace(symbol="BTC", dry_run=True),
            runtime_state=RuntimeState.ACTIVE,
            has_uncertain_state=False,
            has_unknown_order_state=False,
            snapshot=Mock(return_value=()),
            reconcile=AsyncMock(return_value=result),
            cancel_managed_orders=AsyncMock(return_value=result),
        )

    def assert_no_delegation(self, manager):
        manager.reconcile.assert_not_called()
        manager.cancel_managed_orders.assert_not_called()
        manager.snapshot.assert_not_called()

    async def test_real_manager_empty_reconcile_and_cancel_make_no_exchange_calls(self):
        network = {name: AsyncMock(side_effect=AssertionError("exchange call forbidden"))
                   for name in ("connect", "create_order", "cancel_order", "cancel_all_orders",
                                "get_open_orders", "get_order_history",
                                "resolve_unresolved_submissions")}
        adapter = SimpleNamespace(
            **network, get_unresolved_submissions=Mock(return_value=[]),
            get_unresolved_cancellations=Mock(return_value=[]),
        )
        config = ExecutionSettings(symbol="BTC", order_size=D("0.0002"),
                                   max_position=D("0.0004"), reprice_threshold_ticks=1,
                                   dry_run=True)
        metadata = MarketMetadata("BTC", 1, 5, D("0.1"), D("0.00001"),
                                  D("0.0002"), D("10"))
        manager = MarketMakerOrderManager(adapter, config, metadata,
                                          monotonic=self.clock.monotonic)
        port = DrySafetyExecutionPort(manager)
        for result in (await port.reconcile_quotes(self.empty),
                       await port.cancel_all_managed()):
            self.assertEqual(result.status, ExecutionStatus.SIMULATED)
            self.assertTrue(result.snapshot.simulated)
            self.assertEqual(result.snapshot.managed_order_count, 0)
            self.assertEqual((result.submitted_count, result.cancelled_count), (0, 0))
        for method in network.values():
            method.assert_not_called()

    async def test_empty_plan_uses_public_reconcile_without_order_intents(self):
        result = await self.port.reconcile_quotes(self.empty)
        self.assertEqual(result.status, ExecutionStatus.SIMULATED)
        self.manager.reconcile.assert_awaited_once()
        desired, risk = self.manager.reconcile.call_args.args
        self.assertIsNone(desired.bid)
        self.assertIsNone(desired.ask)
        self.assertIsNone(risk.buy_amount)
        self.assertIsNone(risk.sell_amount)
        self.assertEqual(result.submitted_count, 0)

    async def test_public_cancel_reports_simulation_not_real_cancellation(self):
        self.manager.cancel_managed_orders.return_value = ReconcileResult(
            (ReconcileAction(None, "would_cancel", "dry cancellation"),),
            RuntimeState.ACTIVE,
        )
        result = await self.port.cancel_all_managed()
        self.manager.cancel_managed_orders.assert_awaited_once()
        self.assertEqual(result.status, ExecutionStatus.SIMULATED)
        self.assertTrue(result.snapshot.simulated)
        self.assertEqual((result.submitted_count, result.cancelled_count), (0, 1))

    async def test_nonempty_quotes_and_flatten_are_explicitly_unavailable(self):
        plan = QuotePlan("BTC", (QuoteIntent(Side.BUY, D("79000"), D("0.0002")),))
        with self.assertRaises(ExecutionUnavailable):
            await self.port.reconcile_quotes(plan)
        with self.assertRaises(ExecutionUnavailable):
            await self.port.flatten_ioc(self.flatten)
        self.assert_no_delegation(self.manager)

    async def test_wrong_symbol_is_rejected_before_delegation(self):
        with self.assertRaises(ExecutionUnavailable):
            await self.port.reconcile_quotes(QuotePlan("ETH"))
        self.assert_no_delegation(self.manager)

    def test_constructor_requires_literal_true_dry_run_before_delegation(self):
        for dry in (False, None, 1, "true"):
            with self.subTest(dry=dry):
                manager = self.stub_manager()
                manager.config.dry_run = dry
                with self.assertRaises(ExecutionUnavailable):
                    DrySafetyExecutionPort(manager)
                self.assert_no_delegation(manager)

    async def test_every_operation_rechecks_dry_config_after_construction(self):
        self.manager.config.dry_run = False
        with self.assertRaises(ExecutionUnavailable):
            self.port.snapshot()
        for operation, args in ((self.port.reconcile_quotes, (self.empty,)),
                                (self.port.cancel_all_managed, ()),
                                (self.port.flatten_ioc, (self.flatten,))):
            with self.subTest(operation=operation.__name__), self.assertRaises(ExecutionUnavailable):
                await operation(*args)
        self.assert_no_delegation(self.manager)

    async def test_unknown_or_unresolved_orders_block_and_pause_execution(self):
        for flag in ("has_uncertain_state", "has_unknown_order_state"):
            with self.subTest(flag=flag):
                manager = self.stub_manager()
                setattr(manager, flag, True)
                port = DrySafetyExecutionPort(manager)
                self.assertEqual(port.snapshot().health, ExecutionHealth.PAUSED_ORDER_STATE)
                for result in (await port.reconcile_quotes(self.empty),
                               await port.cancel_all_managed()):
                    self.assertEqual(result.status, ExecutionStatus.BLOCKED)
                    self.assertEqual(result.snapshot.health, ExecutionHealth.PAUSED_ORDER_STATE)
                # Do not enter manager reconciliation's possible REST resolver path.
                manager.reconcile.assert_not_called()
                manager.cancel_managed_orders.assert_not_called()

    async def test_unhealthy_or_non_simulated_orders_cannot_delegate(self):
        for state, health in ((RuntimeState.PAUSED_DATA, ExecutionHealth.PAUSED_DATA),
                              (RuntimeState.STOPPED, ExecutionHealth.HALTED)):
            with self.subTest(state=state):
                self.manager.runtime_state = state
                result = await self.port.reconcile_quotes(self.empty)
                self.assertEqual(result.status, ExecutionStatus.BLOCKED)
                self.assertEqual(result.snapshot.health, health)
        self.manager.runtime_state = RuntimeState.ACTIVE
        self.manager.snapshot.return_value = (SimpleNamespace(simulated=False),)
        for result in (await self.port.reconcile_quotes(self.empty),
                       await self.port.cancel_all_managed()):
            self.assertEqual(result.status, ExecutionStatus.BLOCKED)
            self.assertEqual(result.snapshot.health, ExecutionHealth.PAUSED_ORDER_STATE)
            self.assertFalse(result.snapshot.simulated)
        self.manager.reconcile.assert_not_called()
        self.manager.cancel_managed_orders.assert_not_called()

    async def test_backend_errors_block_without_echoing_backend_details(self):
        self.manager.reconcile.return_value = ReconcileResult(
            (), RuntimeState.ACTIVE, errors=("DO_NOT_ECHO_SECRET",),
        )
        result = await self.port.reconcile_quotes(self.empty)
        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertNotIn("DO_NOT_ECHO_SECRET", repr(result))

    async def test_backend_exceptions_are_sanitized_for_each_public_operation(self):
        self.manager.snapshot.side_effect = RuntimeError("DO_NOT_ECHO_SECRET")
        with self.assertRaises(ExecutionUnavailable) as caught:
            self.port.snapshot()
        self.assertNotIn("DO_NOT_ECHO_SECRET", str(caught.exception))
        self.manager.snapshot.side_effect = None
        for method, operation, args in (
            (self.manager.reconcile, self.port.reconcile_quotes, (self.empty,)),
            (self.manager.cancel_managed_orders, self.port.cancel_all_managed, ()),
        ):
            with self.subTest(operation=operation.__name__):
                method.side_effect = RuntimeError("DO_NOT_ECHO_SECRET")
                with self.assertRaises(ExecutionUnavailable) as caught:
                    await operation(*args)
                self.assertNotIn("DO_NOT_ECHO_SECRET", str(caught.exception))

    def test_removed_runtime_and_launch_paths_are_absent(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("core/services/market_maker", "run_market_maker.py",
                         "config/market_maker"):
            with self.subTest(path=relative):
                self.assertFalse((root / relative).exists())

    def test_importing_current_entrypoint_and_ports_does_not_load_removed_runtime(self):
        check = subprocess.run(
            [sys.executable, "-c", "import sys; import run_volume_market_maker; "
             "from core.services.market_maker_v2.orchestrator import VolumeSession; "
             "from core.services.market_maker_v2.order_manager import MarketMakerOrderManager; "
             "from core.services.market_maker_v2.execution_port import "
             "MarketDataPort, AccountPort, Clock, TelemetrySink, ExecutionPort; "
             "assert not any(name == 'core.services.market_maker' or "
             "name.startswith('core.services.market_maker.') for name in sys.modules)"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
