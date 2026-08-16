import asyncio
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.adapters.exchanges.models import OrderSide, PositionSide
from core.services.grid.coordinator.grid_coordinator import GridCoordinator
from core.services.grid.coordinator.grid_reset_manager import GridResetManager
from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.implementations.order_health_checker import OrderHealthChecker
from core.services.grid.models import (
    GridOrder,
    GridOrderSide,
    GridOrderStatus,
)


def make_position(side: PositionSide, size: str = "2") -> SimpleNamespace:
    return SimpleNamespace(
        side=side,
        size=Decimal(size),
        entry_price=Decimal("10"),
        unrealized_pnl=Decimal("0"),
    )


class StartupCleanupTests(unittest.IsolatedAsyncioTestCase):
    def make_coordinator(self, exchange) -> GridCoordinator:
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(symbol="PLTR")
        coordinator.engine = SimpleNamespace(exchange=exchange)
        return coordinator

    async def test_short_position_closes_with_reduce_only_buy(self):
        exchange = SimpleNamespace(
            get_open_orders=AsyncMock(return_value=[]),
            get_positions=AsyncMock(
                side_effect=[[make_position(PositionSide.SHORT)], []]
            ),
            get_ticker=AsyncMock(return_value=SimpleNamespace(last=Decimal("10"))),
            create_order=AsyncMock(return_value=SimpleNamespace(id="close-1")),
        )
        coordinator = self.make_coordinator(exchange)

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            await coordinator._cleanup_before_start()

        kwargs = exchange.create_order.await_args.kwargs
        self.assertEqual(kwargs["side"], OrderSide.BUY)
        self.assertEqual(kwargs["amount"], Decimal("2"))
        self.assertEqual(kwargs["params"], {"reduce_only": True})

    async def test_cancel_error_aborts_startup_cleanup(self):
        order = SimpleNamespace(id="old-1")
        exchange = SimpleNamespace(
            get_open_orders=AsyncMock(side_effect=[[order], []]),
            cancel_order=AsyncMock(side_effect=RuntimeError("cancel failed")),
            get_positions=AsyncMock(),
        )
        coordinator = self.make_coordinator(exchange)

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "order cleanup failed"):
                await coordinator._cleanup_before_start()

        exchange.get_positions.assert_not_awaited()

    async def test_remaining_position_aborts_startup_cleanup(self):
        position = make_position(PositionSide.LONG)
        exchange = SimpleNamespace(
            get_open_orders=AsyncMock(return_value=[]),
            get_positions=AsyncMock(side_effect=[[position], [position]]),
            get_ticker=AsyncMock(return_value=SimpleNamespace(last=Decimal("10"))),
            create_order=AsyncMock(return_value=SimpleNamespace(id="close-1")),
        )
        coordinator = self.make_coordinator(exchange)

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "not fully closed"):
                await coordinator._cleanup_before_start()


class ResetCloseTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, positions) -> GridResetManager:
        exchange = SimpleNamespace(get_positions=AsyncMock(side_effect=positions))
        engine = SimpleNamespace(
            exchange=exchange,
            place_market_order=AsyncMock(),
            pause_placements=MagicMock(),
            resume_placements=MagicMock(),
            wait_for_inflight_placements=AsyncMock(),
        )
        coordinator = SimpleNamespace(
            _is_resetting=False,
            _resetting=False,
            is_emergency_stopped=False,
            _stop_loss_trigger_count=0,
            stop=AsyncMock(),
        )
        state = SimpleNamespace(
            active_orders={"old": object()},
            pending_buy_orders=1,
            pending_sell_orders=1,
        )
        tracker = SimpleNamespace(reset=MagicMock())

        manager = GridResetManager.__new__(GridResetManager)
        manager.logger = MagicMock()
        manager.config = SimpleNamespace(
            symbol="PLTR",
            stop_loss_price=Decimal("8"),
        )
        manager.engine = engine
        manager.coordinator = coordinator
        manager.state = state
        manager.tracker = tracker
        manager.strategy = MagicMock()
        manager.order_ops = SimpleNamespace(
            cancel_all_orders_with_verification=AsyncMock(return_value=True)
        )
        return manager

    async def test_stop_loss_short_close_is_reduce_only_and_verified(self):
        manager = self.make_manager([[make_position(PositionSide.SHORT)], []])

        with patch(
            "core.services.grid.coordinator.grid_reset_manager.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await manager.execute_stop_loss_shutdown(Decimal("7"))

        self.assertTrue(result)
        manager.engine.place_market_order.assert_awaited_once_with(
            side=GridOrderSide.BUY,
            amount=Decimal("2"),
            reduce_only=True,
            allow_while_paused=True,
        )
        self.assertEqual(manager.state.active_orders, {})
        manager.tracker.reset.assert_called_once_with()
        manager.coordinator.stop.assert_awaited_once_with()

    async def test_stop_loss_retains_state_when_position_remains(self):
        position = make_position(PositionSide.LONG)
        manager = self.make_manager([[position], [position]])

        with patch(
            "core.services.grid.coordinator.grid_reset_manager.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await manager.execute_stop_loss_shutdown(Decimal("7"))

        self.assertFalse(result)
        self.assertIn("old", manager.state.active_orders)
        manager.tracker.reset.assert_not_called()
        manager.coordinator.stop.assert_awaited_once_with()

    async def test_stop_loss_query_error_retains_state_and_stops(self):
        manager = self.make_manager([RuntimeError("position api down")])

        result = await manager.execute_stop_loss_shutdown(Decimal("7"))

        self.assertFalse(result)
        self.assertIn("old", manager.state.active_orders)
        manager.engine.place_market_order.assert_not_awaited()
        manager.tracker.reset.assert_not_called()
        manager.coordinator.stop.assert_awaited_once_with()

    async def test_reset_aborts_before_state_clear_when_close_is_not_flat(self):
        position = make_position(PositionSide.SHORT)
        manager = self.make_manager([[position], [position]])

        with patch(
            "core.services.grid.coordinator.grid_reset_manager.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await manager._generic_reset_workflow(
                reset_type="test",
                should_close_position=True,
                should_reinit_capital=False,
            )

        self.assertIsNone(result)
        manager.engine.place_market_order.assert_awaited_once_with(
            side=GridOrderSide.BUY,
            amount=Decimal("2"),
            reduce_only=True,
            allow_while_paused=True,
        )
        self.assertIn("old", manager.state.active_orders)
        manager.tracker.reset.assert_not_called()


class EngineShutdownTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, exchange) -> GridEngineImpl:
        engine = GridEngineImpl.__new__(GridEngineImpl)
        engine.logger = MagicMock()
        engine.exchange = exchange
        engine.config = SimpleNamespace(symbol="PLTR")
        engine._pending_orders = {}
        engine._expected_cancellations = set()
        engine._running = True
        engine._shutting_down = False
        engine._placements_paused = False
        engine._placement_epoch = 0
        engine._inflight_placements = 0
        engine._placements_drained = asyncio.Event()
        engine._placements_drained.set()
        return engine

    async def test_shutdown_drains_inflight_placement_before_cancel_snapshot(self):
        placement_started = asyncio.Event()
        release_placement = asyncio.Event()
        events = []

        async def create_order(**kwargs):
            placement_started.set()
            await release_placement.wait()
            events.append("placed")
            return SimpleNamespace(
                id="late-1",
                order_id=None,
                client_id=None,
                raw_data={},
            )

        async def cancel_all_orders(symbol):
            events.append("cancelled")
            return []

        exchange = SimpleNamespace(
            create_order=AsyncMock(side_effect=create_order),
            cancel_all_orders=AsyncMock(side_effect=cancel_all_orders),
            get_open_orders=AsyncMock(return_value=[]),
        )
        engine = self.make_engine(exchange)
        order = GridOrder(
            order_id="pending",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("10"),
            amount=Decimal("1"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
        )

        placement_task = asyncio.create_task(engine.place_order(order))
        await placement_started.wait()
        engine.begin_shutdown()
        cancel_task = asyncio.create_task(engine.cancel_all_orders())
        await asyncio.sleep(0)

        exchange.cancel_all_orders.assert_not_awaited()
        release_placement.set()
        await placement_task
        await cancel_task

        self.assertEqual(events, ["placed", "cancelled"])

    async def test_reset_pause_aborts_remaining_serial_batch(self):
        placement_started = asyncio.Event()
        release_placement = asyncio.Event()

        async def create_order(**kwargs):
            placement_started.set()
            await release_placement.wait()
            return SimpleNamespace(
                id="first-1",
                order_id=None,
                client_id=None,
                raw_data={},
            )

        exchange = SimpleNamespace(create_order=AsyncMock(side_effect=create_order))
        engine = self.make_engine(exchange)
        engine._supports_batch_mode = MagicMock(return_value=True)
        orders = [
            GridOrder(
                order_id="pending",
                grid_id=grid_id,
                side=GridOrderSide.BUY,
                price=Decimal(str(10 + grid_id)),
                amount=Decimal("1"),
                status=GridOrderStatus.PENDING,
                created_at=datetime.now(),
            )
            for grid_id in (1, 2)
        ]

        batch_task = asyncio.create_task(engine.place_batch_orders(orders))
        await placement_started.wait()
        engine.pause_placements()
        release_placement.set()
        placed = await batch_task

        self.assertEqual(len(placed), 1)
        self.assertEqual(exchange.create_order.await_count, 1)

    async def test_cancel_failure_keeps_local_pending_orders(self):
        exchange = SimpleNamespace(
            cancel_all_orders=AsyncMock(side_effect=RuntimeError("cancel failed")),
            get_open_orders=AsyncMock(),
        )
        engine = self.make_engine(exchange)
        order = MagicMock()
        engine._pending_orders["old-1"] = order

        with self.assertRaisesRegex(RuntimeError, "cancel failed"):
            await engine.cancel_all_orders()

        self.assertIn("old-1", engine._pending_orders)
        order.mark_cancelled.assert_not_called()

    async def test_cancel_success_clears_only_after_exchange_verification(self):
        exchange = SimpleNamespace(
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
        )
        engine = self.make_engine(exchange)
        order = MagicMock()
        engine._pending_orders["old-1"] = order

        count = await engine.cancel_all_orders()

        self.assertEqual(count, 1)
        self.assertEqual(engine._pending_orders, {})
        order.mark_cancelled.assert_called_once_with()

    async def test_cancel_verification_failure_keeps_local_pending_orders(self):
        exchange = SimpleNamespace(
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[SimpleNamespace(id="old-1")]),
        )
        engine = self.make_engine(exchange)
        order = MagicMock()
        engine._pending_orders["old-1"] = order

        with self.assertRaisesRegex(RuntimeError, "remain open"):
            await engine.cancel_all_orders()

        self.assertIn("old-1", engine._pending_orders)
        order.mark_cancelled.assert_not_called()

    async def test_market_close_rejects_empty_exchange_result(self):
        exchange = SimpleNamespace(create_order=AsyncMock(return_value=None))
        engine = self.make_engine(exchange)

        with self.assertRaisesRegex(RuntimeError, "no market order"):
            await engine.place_market_order(
                GridOrderSide.SELL,
                Decimal("1"),
                reduce_only=True,
            )

        self.assertEqual(
            exchange.create_order.await_args.kwargs["params"],
            {"reduce_only": True},
        )

    async def test_coordinator_stop_stops_engine_when_cancel_fails(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = False
        coordinator._stop_loss_monitor_task = None
        coordinator.balance_monitor = SimpleNamespace(stop_monitoring=AsyncMock())
        coordinator.position_monitor = SimpleNamespace(stop_monitoring=AsyncMock())
        coordinator.engine = SimpleNamespace(
            begin_shutdown=MagicMock(),
            cancel_all_orders=AsyncMock(side_effect=RuntimeError("cancel failed")),
            stop=AsyncMock(),
        )
        coordinator.state = SimpleNamespace(stop=MagicMock())

        with self.assertRaisesRegex(RuntimeError, "order cancellation"):
            await coordinator.stop()

        coordinator.engine.stop.assert_awaited_once_with()
        coordinator.state.stop.assert_called_once_with()

    async def test_monitor_stop_error_does_not_skip_exchange_cancellation(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = False
        coordinator._stop_loss_monitor_task = None
        coordinator.balance_monitor = SimpleNamespace(
            stop_monitoring=AsyncMock(side_effect=RuntimeError("monitor failed"))
        )
        coordinator.position_monitor = SimpleNamespace(stop_monitoring=AsyncMock())
        coordinator.engine = SimpleNamespace(
            begin_shutdown=MagicMock(),
            cancel_all_orders=AsyncMock(return_value=0),
            stop=AsyncMock(),
        )
        coordinator.state = SimpleNamespace(stop=MagicMock())

        with self.assertRaisesRegex(RuntimeError, "balance monitor"):
            await coordinator.stop()

        coordinator.engine.cancel_all_orders.assert_awaited_once_with()
        coordinator.engine.stop.assert_awaited_once_with()


class HealthRepairSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_position_repair_close_is_tracked_and_reduce_only(self):
        engine = SimpleNamespace(place_market_order=AsyncMock())
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.logger = MagicMock()

        result = await checker._close_position(
            PositionSide.SHORT,
            Decimal("0.2"),
            Decimal("100"),
        )

        self.assertTrue(result)
        engine.place_market_order.assert_awaited_once_with(
            side=GridOrderSide.BUY,
            amount=Decimal("0.2"),
            reduce_only=True,
            reference_price=Decimal("100"),
        )

    async def test_stale_health_repair_cannot_place_after_reset_resume(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="fake"),
            create_order=AsyncMock(),
        )
        engine = GridEngineImpl(exchange)
        engine.config = SimpleNamespace(symbol="ETH")
        engine._running = True

        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.logger = MagicMock()
        checker._health_cycle_placement_epoch = engine.placement_epoch

        engine.pause_placements()
        engine.resume_placements()

        result = await checker._open_position(
            PositionSide.LONG,
            Decimal("0.2"),
            Decimal("100"),
        )

        self.assertFalse(result)
        exchange.create_order.assert_not_awaited()


class StopLossMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_shutdown_rearms_monitor(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._stop_loss_triggered = False
        coordinator._resetting = False
        coordinator._is_resetting = False
        coordinator.config = SimpleNamespace(
            stop_loss_check_interval=1,
            stop_loss_price=Decimal("8"),
            check_stop_loss=lambda _price: True,
        )
        coordinator.engine = SimpleNamespace(
            get_current_price=AsyncMock(return_value=Decimal("7"))
        )
        coordinator.reset_manager = SimpleNamespace(
            execute_stop_loss_shutdown=AsyncMock(side_effect=[False, True])
        )

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            await coordinator._stop_loss_monitor()

        self.assertEqual(
            coordinator.reset_manager.execute_stop_loss_shutdown.await_count, 2
        )


class ResetGateTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self):
        manager = GridResetManager.__new__(GridResetManager)
        manager.logger = MagicMock()
        manager.coordinator = SimpleNamespace(
            _is_resetting=False,
            _resetting=False,
            price_lock_manager=None,
            _price_escape_trigger_count=0,
        )
        manager.config = SimpleNamespace(
            is_long=lambda: False,
            is_short=lambda: False,
        )
        manager._generic_reset_workflow = AsyncMock(return_value=None)
        return manager

    def test_reset_gate_uses_both_legacy_flags(self):
        manager = self.make_manager()

        self.assertTrue(manager._try_begin_reset())
        self.assertTrue(manager.coordinator._is_resetting)
        self.assertTrue(manager.coordinator._resetting)
        self.assertFalse(manager._try_begin_reset())

        manager._end_reset()
        self.assertFalse(manager.coordinator._is_resetting)
        self.assertFalse(manager.coordinator._resetting)

    async def test_price_follow_reports_cancel_failure(self):
        manager = self.make_manager()

        await manager.execute_price_follow_reset(Decimal("10"), "up")

        self.assertTrue(
            any(
                "重置失败" in str(call.args[0])
                for call in manager.logger.error.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
