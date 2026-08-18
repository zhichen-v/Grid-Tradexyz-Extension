import asyncio
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.services.grid.coordinator.grid_coordinator import GridCoordinator
from core.services.grid.coordinator.order_operations import OrderOperations
from core.services.grid.coordinator.position_monitor import PositionMonitor
from core.services.grid.coordinator.scalping_operations import ScalpingOperations
from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus, GridType


def make_order(order_id: str, side: GridOrderSide, amount: str = "0.0002") -> GridOrder:
    return GridOrder(
        order_id=order_id,
        grid_id=1,
        side=side,
        price=Decimal("100"),
        amount=Decimal(amount),
        status=GridOrderStatus.PENDING,
        created_at=datetime.now(),
    )


class PauseStateTests(unittest.IsolatedAsyncioTestCase):
    def test_public_pause_property_and_fill_gate_share_one_state(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator._paused = False

        coordinator.is_paused = True
        self.assertTrue(coordinator._paused)

        coordinator._paused = False
        self.assertFalse(coordinator.is_paused)

    async def test_rest_recovery_only_releases_rest_owned_pause(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = False
        coordinator._manual_pause_owned = False
        coordinator._emergency_stop_requested = False
        coordinator._resetting = False
        coordinator.is_emergency_stopped = False
        coordinator._deferred_fills = {}
        coordinator._deferred_fill_drain_task = None
        coordinator.state = SimpleNamespace(pause=MagicMock(), resume=MagicMock())
        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor.coordinator = coordinator
        monitor.engine = SimpleNamespace()
        monitor.tracker = SimpleNamespace(
            get_current_position=MagicMock(return_value=Decimal("0"))
        )
        monitor._rest_query_interval = 10
        monitor._rest_max_backoff = 60
        monitor._rest_max_failures = 3
        monitor._rest_failure_count = 3
        monitor._rest_is_available = True
        monitor._rest_pause_owned = False
        monitor._rest_next_query_time = 0
        coordinator.position_monitor = monitor

        with (
            patch("core.services.grid.coordinator.position_monitor.time.time", return_value=100),
            patch("core.services.grid.coordinator.position_monitor.random.uniform", return_value=0),
        ):
            await monitor._handle_rest_failure()

        self.assertTrue(coordinator.is_paused)
        self.assertTrue(monitor._rest_pause_owned)
        self.assertEqual(monitor._rest_next_query_time, 160)

        monitor._record_rest_success(Decimal("200"))
        self.assertFalse(coordinator.is_paused)
        self.assertFalse(monitor._rest_pause_owned)

        await coordinator.pause()
        monitor._rest_failure_count = 3
        with (
            patch("core.services.grid.coordinator.position_monitor.time.time", return_value=300),
            patch("core.services.grid.coordinator.position_monitor.random.uniform", return_value=0),
        ):
            await monitor._handle_rest_failure()
        await coordinator.resume()
        self.assertTrue(coordinator.is_paused)
        self.assertTrue(monitor._rest_pause_owned)

        monitor._record_rest_success(Decimal("400"))
        self.assertFalse(coordinator.is_paused)
        self.assertFalse(monitor._rest_pause_owned)

    async def test_manual_resume_does_not_clear_preexisting_rest_pause(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = False
        coordinator._manual_pause_owned = False
        coordinator._emergency_stop_requested = False
        coordinator._resetting = False
        coordinator._error_count = 0
        coordinator.is_emergency_stopped = False
        coordinator._deferred_fills = {}
        coordinator._deferred_fill_drain_task = None
        coordinator.state = SimpleNamespace(pause=MagicMock(), resume=MagicMock())

        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor.coordinator = coordinator
        monitor.engine = SimpleNamespace()
        monitor.tracker = SimpleNamespace(
            get_current_position=MagicMock(return_value=Decimal("0"))
        )
        monitor._rest_query_interval = 10
        monitor._rest_max_backoff = 60
        monitor._rest_max_failures = 3
        monitor._rest_failure_count = 3
        monitor._rest_is_available = True
        monitor._rest_pause_owned = False
        monitor._rest_next_query_time = 0
        coordinator.position_monitor = monitor

        with (
            patch("core.services.grid.coordinator.position_monitor.time.time", return_value=100),
            patch("core.services.grid.coordinator.position_monitor.random.uniform", return_value=0),
        ):
            await monitor._handle_rest_failure()
        await coordinator.pause()
        await coordinator.resume()

        self.assertTrue(coordinator.is_paused)
        self.assertFalse(coordinator._manual_pause_owned)
        self.assertTrue(monitor._rest_pause_owned)
        self.assertIsNone(coordinator._deferred_fill_drain_task)

    async def test_manual_pause_defers_fill_until_resume(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = False
        coordinator._manual_pause_owned = False
        coordinator._resetting = False
        coordinator.is_emergency_stopped = False
        coordinator._deferred_fills = {}
        coordinator._deferred_fill_drain_task = None
        coordinator._error_count = 0
        coordinator.state = SimpleNamespace(
            pause=MagicMock(),
            resume=MagicMock(),
        )
        coordinator.position_monitor = SimpleNamespace(_rest_pause_owned=False)
        order = make_order("manual-fill", GridOrderSide.BUY)
        order.mark_filled(Decimal("100"), Decimal("0.0002"))

        await coordinator.pause()
        self.assertTrue(coordinator._defer_fill_during_rest_pause(order))
        replay = AsyncMock()
        coordinator._on_order_filled = replay

        await coordinator.resume()
        drain_task = coordinator._deferred_fill_drain_task
        self.assertIsNotNone(drain_task)
        await drain_task

        replay.assert_awaited_once_with(order)
        self.assertEqual(coordinator._deferred_fills, {})

    async def test_manual_pause_defers_each_batch_fill(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = True
        coordinator._manual_pause_owned = True
        coordinator._resetting = False
        coordinator.is_emergency_stopped = False
        coordinator._deferred_fills = {}
        coordinator._deferred_fill_drain_task = None
        coordinator.position_monitor = SimpleNamespace(_rest_pause_owned=False)
        orders = [
            make_order("batch-one", GridOrderSide.BUY),
            make_order("batch-two", GridOrderSide.BUY),
        ]
        for order in orders:
            order.mark_filled(Decimal("100"), Decimal("0.0002"))

        await coordinator._on_batch_orders_filled(orders)

        self.assertEqual(len(coordinator._deferred_fills), 2)

    async def test_event_query_respects_active_failure_backoff(self):
        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor._running = True
        monitor._query_lock = asyncio.Lock()
        monitor._event_query_tasks = set()
        monitor._rest_is_available = False
        monitor._rest_next_query_time = 100
        monitor._last_event_query_time = 0
        monitor._rest_last_query_time = 0
        monitor._rest_query_debounce = 5
        monitor._query_and_update_position = AsyncMock()

        with patch("core.services.grid.coordinator.position_monitor.time.time", return_value=50):
            await monitor.trigger_event_query("fill")

        monitor._query_and_update_position.assert_not_awaited()

    async def test_stop_monitoring_cancels_inflight_event_query(self):
        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor._running = True
        monitor._monitor_task = None
        monitor._query_lock = asyncio.Lock()
        monitor._event_query_tasks = set()
        monitor._rest_is_available = True
        monitor._rest_next_query_time = 0
        monitor._last_event_query_time = 0
        monitor._rest_last_query_time = 0
        monitor._rest_query_debounce = 0
        query_started = asyncio.Event()
        never_finishes = asyncio.Event()

        async def query(**_kwargs):
            query_started.set()
            await never_finishes.wait()

        monitor._query_and_update_position = AsyncMock(side_effect=query)
        event_task = asyncio.create_task(monitor.trigger_event_query("fill"))
        await query_started.wait()

        await monitor.stop_monitoring()

        self.assertTrue(event_task.cancelled())
        self.assertEqual(monitor._event_query_tasks, set())

    async def test_periodic_loop_rechecks_deadline_extended_while_asleep(self):
        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor._running = True
        monitor._query_lock = asyncio.Lock()
        monitor._rest_next_query_time = 100
        monitor._rest_query_interval = 10
        monitor._query_and_update_position = AsyncMock()
        sleep_delays = []

        async def fake_sleep(delay):
            sleep_delays.append(delay)
            if len(sleep_delays) == 1:
                monitor._rest_next_query_time = 200
            else:
                monitor._running = False

        with (
            patch("core.services.grid.coordinator.position_monitor.time.time", return_value=90),
            patch(
                "core.services.grid.coordinator.position_monitor.asyncio.sleep",
                side_effect=fake_sleep,
            ),
        ):
            await monitor._rest_position_query_loop()

        self.assertEqual(sleep_delays, [10, 110])
        monitor._query_and_update_position.assert_not_awaited()


class ConfigSafetyTests(unittest.TestCase):
    def test_position_interval_and_max_position_are_loaded(self):
        from run_grid_trading import create_grid_config

        config = create_grid_config(
            {
                "grid_system": {
                    "exchange": "tradexyz",
                    "symbol": "NVDA",
                    "grid_type": "long",
                    "lower_price": 90,
                    "upper_price": 110,
                    "grid_interval": 1,
                    "order_amount": 0.1,
                    "max_position": 0.5,
                    "position_monitor_interval": 12,
                }
            }
        )

        self.assertEqual(config.max_position, Decimal("0.5"))
        self.assertEqual(config.position_monitor_interval, 12)

    def test_zero_max_position_is_rejected_instead_of_disabled(self):
        from run_grid_trading import create_grid_config

        with self.assertRaisesRegex(ValueError, "max_position"):
            create_grid_config(
                {
                    "grid_system": {
                        "exchange": "tradexyz",
                        "symbol": "NVDA",
                        "grid_type": "long",
                        "lower_price": 90,
                        "upper_price": 110,
                        "grid_interval": 1,
                        "order_amount": 0.1,
                        "max_position": 0,
                    }
                }
            )


class MaxPositionTests(unittest.TestCase):
    def make_coordinator(self, current: str, pending=None) -> GridCoordinator:
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            max_position=Decimal("0.001"),
            is_long=lambda: True,
            is_short=lambda: False,
        )
        coordinator.tracker = SimpleNamespace(
            get_current_position=MagicMock(return_value=Decimal(current))
        )
        coordinator.engine = SimpleNamespace(
            get_pending_orders=MagicMock(return_value=pending or [])
        )
        return coordinator

    def test_opening_orders_are_capped_by_position_plus_pending_exposure(self):
        coordinator = self.make_coordinator(
            "0.0006",
            pending=[make_order("existing", GridOrderSide.BUY)],
        )
        orders = [
            make_order("buy-1", GridOrderSide.BUY),
            make_order("buy-2", GridOrderSide.BUY),
            make_order("sell-1", GridOrderSide.SELL),
        ]

        accepted = coordinator.filter_orders_within_max_position(orders, "test")

        self.assertEqual([order.order_id for order in accepted], ["buy-1", "sell-1"])

    def test_closing_order_is_allowed_even_when_position_is_over_limit(self):
        coordinator = self.make_coordinator("0.002")

        self.assertTrue(
            coordinator.can_place_order_within_max_position(
                GridOrderSide.SELL,
                Decimal("0.0002"),
            )
        )


class EnginePlacementSafetyTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, create_order, current="0.0006", limit="0.0008"):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=create_order,
        )
        engine = GridEngineImpl(exchange)
        engine.config = SimpleNamespace(
            exchange="lighter",
            symbol="BTC",
            grid_type=GridType.LONG,
        )
        engine._running = True

        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            max_position=Decimal(limit),
            is_long=lambda: True,
            is_short=lambda: False,
        )
        coordinator.tracker = SimpleNamespace(
            get_current_position=MagicMock(return_value=Decimal(current))
        )
        coordinator.engine = engine
        coordinator._paused = False
        engine.coordinator = coordinator
        return engine, coordinator

    async def test_concurrent_limit_orders_share_atomic_exposure_gate(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def create_order(**_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return SimpleNamespace(id=str(calls), client_id=None, raw_data={})

        engine, _ = self.make_engine(create_order)
        first = make_order("first", GridOrderSide.BUY)
        second = make_order("second", GridOrderSide.BUY)

        first_task = asyncio.create_task(engine.place_order(first))
        await started.wait()
        second_task = asyncio.create_task(engine.place_order(second))
        release.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)

        self.assertIsNotNone(first_result)
        self.assertIsNone(second_result)
        self.assertEqual(calls, 1)

    async def test_concurrent_market_open_uses_unconfirmed_reservation(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def create_order(**_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return SimpleNamespace(id=str(calls))

        engine, _ = self.make_engine(create_order)
        first_task = asyncio.create_task(
            engine.place_market_order(GridOrderSide.BUY, Decimal("0.0002"))
        )
        await started.wait()
        second_task = asyncio.create_task(
            engine.place_market_order(GridOrderSide.BUY, Decimal("0.0002"))
        )
        release.set()
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)

        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], RuntimeError)
        self.assertEqual(calls, 1)

    async def test_reduce_only_market_close_bypasses_position_cap(self):
        create_order = AsyncMock(return_value=SimpleNamespace(id="close"))
        engine, _ = self.make_engine(create_order, current="0.002", limit="0.001")

        await engine.place_market_order(
            GridOrderSide.SELL,
            Decimal("0.0002"),
            reduce_only=True,
        )

        create_order.assert_awaited_once()


class ReversePlacementSafetyTests(unittest.IsolatedAsyncioTestCase):
    def _make_coordinator(self, calculate_reverse, place_order):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            exchange="lighter",
            grid_interval=Decimal("1"),
            reverse_order_grid_distance=1,
            max_position=None,
            is_long=lambda: True,
            is_short=lambda: False,
        )
        coordinator.strategy = SimpleNamespace(
            calculate_reverse_order=MagicMock(side_effect=calculate_reverse)
            if isinstance(calculate_reverse, Exception)
            else MagicMock(return_value=calculate_reverse)
        )
        coordinator.engine = SimpleNamespace(
            get_pending_orders=MagicMock(return_value=[]),
            place_order=place_order,
            pause_placements=MagicMock(),
        )
        coordinator.state = SimpleNamespace(
            active_orders={},
            mark_order_filled=MagicMock(return_value=True),
            pause=MagicMock(),
            add_order=MagicMock(),
        )
        coordinator.tracker = SimpleNamespace(record_filled_order=MagicMock())
        coordinator.position_monitor = SimpleNamespace(
            _rest_pause_owned=False,
            trigger_event_query=AsyncMock(),
        )
        coordinator.reserve_manager = None
        coordinator.scalping_manager = None
        coordinator.scalping_ops = None
        coordinator.capital_protection_manager = None
        coordinator._running = True
        coordinator._paused = False
        coordinator._manual_pause_owned = False
        coordinator._resetting = False
        coordinator._active_fill_callbacks = 0
        coordinator._recent_fills = {}
        coordinator._fill_dedup_window = 10
        coordinator._last_fill_time = 0
        coordinator._grid_level_locks = {}
        coordinator._emergency_stop_requested = False
        coordinator._emergency_stop_task = None
        coordinator._fatal_stop_reason = None
        coordinator._error_count = 0
        coordinator._max_errors = 3
        coordinator.stop = AsyncMock()
        return coordinator

    async def test_reverse_intent_stays_locked_and_fail_stops_on_placement_failure(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            exchange="lighter",
            grid_interval=Decimal("1"),
            reverse_order_grid_distance=1,
            max_position=None,
            is_long=lambda: True,
            is_short=lambda: False,
        )
        coordinator.strategy = SimpleNamespace(
            calculate_reverse_order=MagicMock(
                return_value=(GridOrderSide.SELL, Decimal("101"), 2)
            )
        )
        coordinator.engine = SimpleNamespace(
            get_pending_orders=MagicMock(return_value=[]),
            place_order=AsyncMock(return_value=None),
        )
        coordinator.state = SimpleNamespace(
            active_orders={},
            mark_order_filled=MagicMock(return_value=True),
        )
        coordinator.tracker = SimpleNamespace(record_filled_order=MagicMock())
        coordinator.position_monitor = SimpleNamespace(
            _rest_pause_owned=False,
            trigger_event_query=AsyncMock(),
        )
        coordinator.reserve_manager = None
        coordinator.scalping_manager = None
        coordinator.scalping_ops = None
        coordinator.capital_protection_manager = None
        coordinator._running = True
        coordinator._paused = False
        coordinator._resetting = False
        coordinator._active_fill_callbacks = 0
        coordinator._recent_fills = {}
        coordinator._fill_dedup_window = 10
        coordinator._last_fill_time = 0
        coordinator._grid_level_locks = {}
        coordinator._emergency_stop_requested = False
        coordinator._emergency_stop_task = None
        coordinator._fatal_stop_reason = None
        coordinator.stop = AsyncMock()
        filled_order = make_order("filled", GridOrderSide.BUY)
        filled_order.mark_filled(Decimal("100"), Decimal("0.0002"))

        await coordinator._on_order_filled(filled_order)
        await coordinator._emergency_stop_task

        self.assertEqual(
            coordinator._grid_level_locks[2],
            {
                "tp_side": "sell",
                "tp_price": Decimal("101"),
                "tp_order_id": None,
            },
        )
        coordinator.stop.assert_awaited_once_with()
        self.assertIn(
            "reverse order placement returned no result",
            coordinator.get_fatal_stop_reason(),
        )

    async def test_calculate_reverse_failure_immediately_locks_fill_and_stops(self):
        coordinator = self._make_coordinator(
            RuntimeError("reverse calculation failed"),
            AsyncMock(),
        )
        filled_order = make_order("calc-failed", GridOrderSide.BUY)
        filled_order.mark_filled(Decimal("100"), Decimal("0.0002"))

        await coordinator._on_order_filled(filled_order)
        await coordinator._emergency_stop_task

        self.assertTrue(coordinator._paused)
        coordinator.engine.pause_placements.assert_called_once_with()
        coordinator.engine.place_order.assert_not_awaited()
        self.assertEqual(
            coordinator._grid_level_locks[filled_order.grid_id]["reason"],
            "unmanaged_fill",
        )
        self.assertIn("reverse calculation failed", coordinator.get_fatal_stop_reason())

    async def test_first_unmanaged_fill_synchronously_blocks_second_fill(self):
        place_order = AsyncMock(return_value=None)
        coordinator = self._make_coordinator(
            (GridOrderSide.SELL, Decimal("101"), 2),
            place_order,
        )
        first = make_order("first-fill", GridOrderSide.BUY)
        second = make_order("second-fill", GridOrderSide.BUY)
        first.mark_filled(Decimal("100"), Decimal("0.0002"))
        second.mark_filled(Decimal("100"), Decimal("0.0002"))

        await coordinator._on_order_filled(first)
        await coordinator._on_order_filled(second)
        await coordinator._emergency_stop_task

        self.assertEqual(place_order.await_count, 1)
        self.assertTrue(coordinator._emergency_stop_requested)


class CancellationCallerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_zero_position_take_profit_cancel_keeps_state(self):
        take_profit = make_order("zero-tp", GridOrderSide.SELL)
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.scalping_manager = SimpleNamespace(
            get_current_take_profit_order=MagicMock(return_value=take_profit)
        )
        coordinator.engine = SimpleNamespace(
            cancel_order=AsyncMock(return_value=False)
        )
        coordinator.state = SimpleNamespace(remove_order=MagicMock())

        await coordinator._update_take_profit_order_after_position_change(
            Decimal("0"),
            Decimal("0"),
        )

        coordinator.state.remove_order.assert_not_called()

    async def test_failed_take_profit_cancel_keeps_state_and_suppresses_replacement(self):
        old_take_profit = make_order("old-tp", GridOrderSide.SELL)
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.scalping_manager = SimpleNamespace(
            get_current_take_profit_order=MagicMock(return_value=old_take_profit)
        )
        coordinator.engine = SimpleNamespace(
            cancel_order=AsyncMock(return_value=False)
        )
        coordinator.state = SimpleNamespace(remove_order=MagicMock())
        coordinator._place_take_profit_order = AsyncMock()

        await coordinator._update_take_profit_order_after_position_change(
            Decimal("0.0002"),
            Decimal("100"),
        )

        coordinator.state.remove_order.assert_not_called()
        coordinator._place_take_profit_order.assert_not_awaited()

    async def test_filter_cancel_false_does_not_remove_local_order(self):
        order = make_order("filter-order", GridOrderSide.BUY)
        state = SimpleNamespace(
            active_orders={order.order_id: order},
            remove_order=MagicMock(),
        )
        engine = SimpleNamespace(
            exchange=SimpleNamespace(),
            cancel_order=AsyncMock(return_value=False),
        )
        operations = OrderOperations(
            engine,
            state,
            SimpleNamespace(symbol="BTC"),
        )
        operations.verifier = SimpleNamespace(
            verify_no_orders_by_filter=AsyncMock(return_value=True)
        )

        with patch(
            "core.services.grid.coordinator.order_operations.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await operations.cancel_orders_by_filter_with_verification(
                lambda candidate: candidate is order,
                "test orders",
                max_attempts=1,
            )

        self.assertFalse(result)
        state.remove_order.assert_not_called()

    async def test_scalping_nonterminal_tp_cancel_keeps_state_and_suppresses_replacement(self):
        old_take_profit = make_order("old-tp", GridOrderSide.SELL)
        operations = ScalpingOperations.__new__(ScalpingOperations)
        operations.logger = MagicMock()
        operations.coordinator = SimpleNamespace(
            is_emergency_stopped=False,
            is_paused=False,
        )
        operations.scalping_manager = SimpleNamespace(
            is_active=MagicMock(return_value=True),
            is_take_profit_order_outdated=MagicMock(return_value=True),
            get_current_take_profit_order=MagicMock(return_value=old_take_profit),
        )
        operations.tracker = SimpleNamespace(
            get_current_position=MagicMock(return_value=Decimal("0.0004"))
        )
        operations.engine = SimpleNamespace(
            cancel_order=AsyncMock(return_value=False),
            exchange=SimpleNamespace(get_open_orders=AsyncMock(return_value=[])),
        )
        operations.state = SimpleNamespace(remove_order=MagicMock())
        operations.config = SimpleNamespace(symbol="BTC")
        operations.place_take_profit_order_with_verification = AsyncMock()

        with patch(
            "core.services.grid.coordinator.scalping_operations.asyncio.sleep",
            new=AsyncMock(),
        ):
            await operations.update_take_profit_order_if_needed()

        self.assertEqual(operations.engine.cancel_order.await_count, 3)
        operations.state.remove_order.assert_not_called()
        operations.engine.exchange.get_open_orders.assert_not_awaited()
        operations.place_take_profit_order_with_verification.assert_not_awaited()

    async def test_cancel_all_error_cannot_be_verified_by_empty_snapshot(self):
        engine = SimpleNamespace(
            cancel_all_orders=AsyncMock(
                side_effect=RuntimeError("submissions remain uncertain")
            ),
            exchange=SimpleNamespace(),
        )
        operations = OrderOperations(
            engine,
            SimpleNamespace(),
            SimpleNamespace(symbol="BTC"),
        )
        operations.verifier = SimpleNamespace(
            get_open_orders_count=AsyncMock(return_value=0)
        )

        with patch(
            "core.services.grid.coordinator.order_operations.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await operations.cancel_all_orders_with_verification(
                max_retries=1,
                first_delay=0,
            )

        self.assertFalse(result)
        engine.cancel_all_orders.assert_awaited_once()

    async def test_cancel_all_retry_can_succeed_after_uncertain_failure(self):
        engine = SimpleNamespace(
            cancel_all_orders=AsyncMock(
                side_effect=[RuntimeError("submissions remain uncertain"), 0]
            ),
            exchange=SimpleNamespace(),
        )
        operations = OrderOperations(
            engine,
            SimpleNamespace(),
            SimpleNamespace(symbol="BTC"),
        )
        operations.verifier = SimpleNamespace(
            get_open_orders_count=AsyncMock(return_value=0)
        )

        with patch(
            "core.services.grid.coordinator.order_operations.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await operations.cancel_all_orders_with_verification(
                max_retries=2,
                first_delay=0,
                retry_delay=0,
            )

        self.assertTrue(result)
        self.assertEqual(engine.cancel_all_orders.await_count, 2)


class ShutdownRetryTests(unittest.IsolatedAsyncioTestCase):
    def make_coordinator(self, cancel_side_effect) -> GridCoordinator:
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = True
        coordinator._paused = False
        coordinator._stop_loss_monitor_task = None
        coordinator._shutdown_order_cancellation_failed = False
        coordinator._shutdown_cleanup_error = None
        coordinator.balance_monitor = SimpleNamespace(stop_monitoring=AsyncMock())
        coordinator.position_monitor = SimpleNamespace(stop_monitoring=AsyncMock())
        coordinator.engine = SimpleNamespace(
            begin_shutdown=MagicMock(),
            cancel_all_orders=AsyncMock(side_effect=cancel_side_effect),
            stop=AsyncMock(),
        )
        coordinator.state = SimpleNamespace(stop=MagicMock())
        return coordinator

    async def test_shutdown_retries_rate_limited_cancel_and_recovers(self):
        coordinator = self.make_coordinator(
            [RuntimeError("429 Too Many Requests"), RuntimeError("429"), 3]
        )

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            await coordinator.stop()

        self.assertEqual(coordinator.engine.cancel_all_orders.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1.0, 2.0])
        self.assertFalse(coordinator._shutdown_order_cancellation_failed)
        self.assertEqual(coordinator.get_status_text(), "Stopped")

    async def test_shutdown_exhaustion_is_reported_as_unsafe(self):
        coordinator = self.make_coordinator(RuntimeError("429 Too Many Requests"))

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "order cancellation"):
                await coordinator.stop()

        self.assertEqual(coordinator.engine.cancel_all_orders.await_count, 5)
        self.assertTrue(coordinator._shutdown_order_cancellation_failed)
        self.assertIn("UNSAFE", coordinator.get_status_text())

    async def test_concurrent_stop_calls_share_one_cleanup(self):
        cancel_started = asyncio.Event()
        allow_cancel = asyncio.Event()

        async def cancel_once():
            cancel_started.set()
            await allow_cancel.wait()
            return 2

        coordinator = self.make_coordinator(cancel_once)
        first_stop = asyncio.create_task(coordinator.stop())
        await cancel_started.wait()
        second_stop = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0)
        allow_cancel.set()

        await asyncio.gather(first_stop, second_stop)

        coordinator.engine.cancel_all_orders.assert_awaited_once_with()
        coordinator.engine.stop.assert_awaited_once_with()

    async def test_unsafe_cleanup_incident_stays_sticky_after_later_recovery(self):
        coordinator = self.make_coordinator(
            [
                RuntimeError("429") for _ in range(5)
            ] + [3]
        )

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "order cancellation"):
                await coordinator.stop()
            await coordinator.stop()

        self.assertIsNone(coordinator._shutdown_cleanup_error)
        self.assertIsNotNone(coordinator.get_unsafe_shutdown_incident())
        self.assertIn("incident recovered", coordinator.get_status_text())


class FatalStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_threshold_records_reason_for_nonzero_runner_exit(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._error_count = 0
        coordinator._max_errors = 1
        coordinator._emergency_stop_requested = False
        coordinator._fatal_stop_reason = None
        coordinator._manual_pause_owned = False
        coordinator._paused = False
        coordinator.engine = SimpleNamespace(pause_placements=MagicMock())
        coordinator.state = SimpleNamespace(pause=MagicMock())
        coordinator.stop = AsyncMock()

        coordinator._handle_error(RuntimeError("reverse placement failed"))

        self.assertTrue(coordinator._paused)
        coordinator.engine.pause_placements.assert_called_once_with()
        coordinator.state.pause.assert_called_once_with()
        await coordinator._emergency_stop_task

        self.assertIn("reverse placement failed", coordinator.get_fatal_stop_reason())
        coordinator.stop.assert_awaited_once_with()

    async def test_position_anomaly_immediately_closes_gate_and_schedules_stop(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._emergency_stop_requested = False
        coordinator._fatal_stop_reason = None
        coordinator._manual_pause_owned = False
        coordinator._paused = False
        coordinator.is_emergency_stopped = False
        coordinator.engine = SimpleNamespace(pause_placements=MagicMock())
        coordinator.state = SimpleNamespace(pause=MagicMock())
        coordinator.stop = AsyncMock()

        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor.coordinator = coordinator
        monitor.config = SimpleNamespace(order_amount=Decimal("0.0002"))
        monitor._initial_phase = False
        monitor._last_position_size = Decimal("0.0002")
        monitor._position_change_alert_threshold = 100
        monitor._position_max_multiplier = 10

        await monitor._check_position_anomaly(Decimal("0.003"))

        self.assertTrue(coordinator.is_emergency_stopped)
        self.assertTrue(coordinator._paused)
        coordinator.engine.pause_placements.assert_called_once_with()
        coordinator.state.pause.assert_called_once_with()
        self.assertIn("Position anomaly", coordinator.get_fatal_stop_reason())
        await coordinator._emergency_stop_task
        coordinator.stop.assert_awaited_once_with()


class StartupPlacementTests(unittest.IsolatedAsyncioTestCase):
    def make_coordinator(self, placed_count: int, open_order_ids=()):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            symbol="BTC",
            grid_count=2,
            lower_price=Decimal("90"),
            upper_price=Decimal("110"),
            max_position=None,
            order_amount=Decimal("0.0002"),
            get_grid_price=lambda _index: Decimal("100"),
            check_stop_loss=lambda _price: False,
            is_follow_mode=lambda: False,
            is_long=lambda: True,
            is_short=lambda: False,
        )
        initial_orders = [
            make_order("one", GridOrderSide.BUY),
            make_order("two", GridOrderSide.BUY),
        ]
        coordinator.strategy = SimpleNamespace(
            initialize=MagicMock(return_value=initial_orders)
        )
        exchange_orders = [
            SimpleNamespace(id=order_id, client_id=None)
            for order_id in open_order_ids
        ]
        coordinator.engine = SimpleNamespace(
            exchange=SimpleNamespace(
                get_open_orders=AsyncMock(return_value=exchange_orders)
            ),
            initialize=AsyncMock(),
            get_current_price=AsyncMock(return_value=Decimal("100")),
            subscribe_order_updates=MagicMock(),
            suspend_health_repairs=MagicMock(),
            resume_health_repairs=MagicMock(),
            is_running=MagicMock(return_value=False),
            start=AsyncMock(),
            place_batch_orders=AsyncMock(return_value=initial_orders[:placed_count]),
            get_pending_orders=MagicMock(return_value=[]),
            _pending_keys_for_order=MagicMock(
                side_effect=lambda order: [order.order_id]
            ),
            begin_shutdown=MagicMock(),
            cancel_all_orders=AsyncMock(return_value=1),
            stop=AsyncMock(),
        )
        coordinator.position_monitor = SimpleNamespace(
            start_monitoring=AsyncMock(),
            stop_monitoring=AsyncMock(),
        )
        coordinator.balance_monitor = SimpleNamespace(stop_monitoring=AsyncMock())
        coordinator.state = SimpleNamespace(
            initialize_grid_levels=MagicMock(),
            set_error=MagicMock(),
            stop=MagicMock(),
        )
        coordinator.tracker = SimpleNamespace(get_current_position=MagicMock(return_value=Decimal("0")))
        coordinator.capital_protection_manager = None
        coordinator.take_profit_manager = None
        coordinator.scalping_manager = None
        coordinator.reserve_manager = None
        coordinator._symbol_initial_capital = Decimal("0")
        coordinator._symbol_reference_price = Decimal("0")
        coordinator._running = False
        coordinator._paused = False
        coordinator._stop_loss_monitor_task = None
        coordinator._shutdown_order_cancellation_failed = False
        coordinator._shutdown_cleanup_error = None
        coordinator._emergency_stop_requested = False
        return coordinator

    async def test_partial_initial_batch_fails_startup_and_cancels_orders(self):
        coordinator = self.make_coordinator(placed_count=1)

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "Partial initial grid placement"):
                await coordinator.initialize()

        coordinator.engine.cancel_all_orders.assert_awaited_once_with()
        coordinator.engine.stop.assert_awaited_once_with()
        coordinator.state.set_error.assert_called_once_with()

    async def test_accepted_but_cancelled_initial_order_fails_verification(self):
        coordinator = self.make_coordinator(
            placed_count=2,
            open_order_ids=("one",),
        )

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "Initial grid verification failed"):
                await coordinator.initialize()

        coordinator.engine.cancel_all_orders.assert_awaited_once_with()
        coordinator.engine.stop.assert_awaited_once_with()


class GridLockResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_start_clears_stale_grid_level_locks_before_cleanup(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._running = False
        coordinator._paused = True
        coordinator._grid_level_locks = {7: {"tp_order_id": "stale"}}

        async def inspect_cleanup():
            self.assertEqual(coordinator._grid_level_locks, {})
            raise RuntimeError("stop after assertion")

        coordinator._cleanup_before_start = inspect_cleanup

        with self.assertRaisesRegex(RuntimeError, "stop after assertion"):
            await coordinator.start()

    async def test_fixed_range_reset_clears_stale_grid_level_locks(self):
        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator._grid_level_locks = {7: {"tp_order_id": "stale"}}
        coordinator.scalping_manager = None
        coordinator.capital_protection_manager = None
        coordinator.take_profit_manager = None
        coordinator.tracker = SimpleNamespace(reset=MagicMock())
        coordinator.state = SimpleNamespace(
            active_orders={},
            pending_buy_orders=1,
            pending_sell_orders=1,
            initialize_grid_levels=MagicMock(
                side_effect=RuntimeError("stop after assertion")
            ),
        )
        coordinator.config = SimpleNamespace(
            grid_count=1,
            get_grid_price=lambda _grid_id: Decimal("100"),
        )
        coordinator.engine = SimpleNamespace()

        with self.assertRaisesRegex(RuntimeError, "stop after assertion"):
            await coordinator._reset_fixed_range_grid()

        self.assertEqual(coordinator._grid_level_locks, {})


if __name__ == "__main__":
    unittest.main()
