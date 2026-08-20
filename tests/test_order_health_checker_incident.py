import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.adapters.exchanges.models import OrderSide, OrderStatus, PositionSide
from core.services.grid.implementations.order_health_checker import (
    OrderHealthChecker,
    PositionHealthResult,
)
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus, GridType


class OrderHealthCheckerIncidentTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_failure_aborts_instead_of_becoming_empty_state(self):
        exchange = SimpleNamespace(
            get_open_orders=AsyncMock(side_effect=RuntimeError("429")),
            get_positions=AsyncMock(),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = SimpleNamespace(exchange=exchange)
        checker.config = SimpleNamespace(symbol="BTC")

        with self.assertRaisesRegex(RuntimeError, "open orders"):
            await checker._fetch_orders_and_positions()

        exchange.get_positions.assert_not_awaited()

    async def test_position_snapshot_failure_aborts_health_snapshot(self):
        exchange = SimpleNamespace(
            get_open_orders=AsyncMock(return_value=[]),
            get_positions=AsyncMock(side_effect=RuntimeError("429")),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = SimpleNamespace(exchange=exchange)
        checker.config = SimpleNamespace(symbol="BTC")

        with self.assertRaisesRegex(RuntimeError, "positions"):
            await checker._fetch_orders_and_positions()

    async def test_missing_orders_use_one_bulk_history_query(self):
        local_orders = [
            GridOrder(
                order_id=str(order_id),
                grid_id=grid_id,
                side=GridOrderSide.BUY,
                price=Decimal(price),
                amount=Decimal("0.00020"),
                status=GridOrderStatus.PENDING,
                created_at=datetime(2000, 1, 1),
            )
            for order_id, grid_id, price in (("101", 1, "62500"), ("102", 2, "62525"))
        ]
        history = [
            SimpleNamespace(
                id=order.order_id,
                client_id=None,
                status=OrderStatus.CANCELED,
                average=None,
                price=order.price,
                filled=Decimal("0"),
                amount=order.amount,
                params={},
            )
            for order in local_orders
        ]
        exchange = SimpleNamespace(
            get_order=AsyncMock(side_effect=AssertionError("N+1 query")),
            get_order_history=AsyncMock(return_value=history),
        )
        engine = SimpleNamespace(
            exchange=exchange,
            coordinator=None,
            _pending_orders={order.order_id: order for order in local_orders},
            _expected_cancellations=set(),
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            get_pending_orders=MagicMock(return_value=local_orders),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC", quantity_precision=5)
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {}

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 0)
        exchange.get_order_history.assert_awaited_once_with("BTC", limit=100)
        exchange.get_order.assert_not_awaited()

    async def test_saturated_history_defers_unknown_missing_order(self):
        local_order = GridOrder(
            order_id="missing-old",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62500"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        history = [
            SimpleNamespace(id=str(index), client_id=None)
            for index in range(100)
        ]
        restore = AsyncMock(return_value=True)
        engine = SimpleNamespace(
            exchange=SimpleNamespace(get_order_history=AsyncMock(return_value=history)),
            coordinator=None,
            _pending_orders={local_order.order_id: local_order},
            _expected_cancellations=set(),
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            _restore_cancelled_grid_order=restore,
            get_pending_orders=MagicMock(return_value=[local_order]),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {local_order.order_id: 0}
        checker._missing_order_resolution_timeout = 0
        checker._last_unresolved_order_price_keys = set()
        checker._restored_missing_orders_in_sync = 0

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 1)
        restore.assert_not_awaited()

    async def test_scheduled_missing_order_restore_defers_remaining_repairs(self):
        local_order = GridOrder(
            order_id="missing",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62500"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        restore = AsyncMock(return_value=True)
        engine = SimpleNamespace(
            exchange=SimpleNamespace(get_order_history=AsyncMock(return_value=[])),
            coordinator=None,
            _pending_orders={local_order.order_id: local_order},
            _expected_cancellations=set(),
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            _restore_cancelled_grid_order=restore,
            get_pending_orders=MagicMock(return_value=[local_order]),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {local_order.order_id: 0}
        checker._missing_order_resolution_timeout = 0
        checker._last_unresolved_order_price_keys = set()
        checker._restored_missing_orders_in_sync = 0

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 0)
        self.assertEqual(checker._restored_missing_orders_in_sync, 1)
        restore.assert_awaited_once_with(local_order, local_order.order_id)

    async def test_bulk_history_failure_aborts_sync_without_per_order_queries(self):
        local_order = GridOrder(
            order_id="101",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62500"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        exchange = SimpleNamespace(
            get_order=AsyncMock(side_effect=AssertionError("N+1 query")),
            get_order_history=AsyncMock(side_effect=RuntimeError("429")),
        )
        engine = SimpleNamespace(
            exchange=exchange,
            _pending_orders={"101": local_order},
            _expected_cancellations=set(),
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            get_pending_orders=MagicMock(return_value=[local_order]),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")

        with self.assertRaisesRegex(RuntimeError, "bulk order history"):
            await checker._sync_orders_into_engine([])

        exchange.get_order.assert_not_awaited()

    async def test_uncertain_expected_cancel_absence_preserves_pending_order(self):
        local_order = GridOrder(
            order_id="101",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62500"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            _rest=rest,
            get_order_history=AsyncMock(return_value=[]),
        )
        pending = {"101": local_order, "client-101": local_order}
        engine = SimpleNamespace(
            exchange=exchange,
            coordinator=None,
            _pending_orders=pending,
            _expected_cancellations={"101", "client-101"},
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            _uncertain_cancel_order_ids={"101", "client-101"},
            _pending_keys_for_order=lambda _order: ["101", "client-101"],
            _is_uncertain_cancel=lambda *keys: any(
                key in {"101", "client-101"} for key in keys
            ),
            get_pending_orders=MagicMock(return_value=[local_order]),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {}

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 1)
        self.assertEqual(local_order.status, GridOrderStatus.PENDING)
        self.assertEqual(engine._pending_orders, pending)
        self.assertEqual(engine._expected_cancellations, {"101", "client-101"})
        self.assertEqual(rest._uncertain_cancellations, {("BTC", "101")})
        exchange.get_order_history.assert_awaited_once_with("BTC", limit=100)

    async def test_uncertain_expected_cancel_exact_fill_finalizes_once(self):
        local_order = GridOrder(
            order_id="101",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62500"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        terminal = SimpleNamespace(
            id="101",
            client_id="client-101",
            status=OrderStatus.FILLED,
            average=Decimal("62500"),
            price=Decimal("62500"),
            filled=Decimal("0.00020"),
            amount=Decimal("0.00020"),
            params={},
            raw_data={},
        )
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        callback = AsyncMock()
        engine = SimpleNamespace(
            exchange=SimpleNamespace(
                _rest=rest,
                get_order_history=AsyncMock(return_value=[terminal]),
            ),
            coordinator=None,
            _pending_orders={"101": local_order, "client-101": local_order},
            _expected_cancellations={"101", "client-101"},
            _order_callbacks=[callback],
            _exchange_sync_grace_period=0,
            _uncertain_cancel_order_ids={"101", "client-101"},
            _pending_keys_for_order=lambda _order: ["101", "client-101"],
            _is_uncertain_cancel=lambda *keys: any(
                key in engine._uncertain_cancel_order_ids for key in keys
            ),
            _claim_order_finalization=MagicMock(return_value=True),
            _clear_restore_state=MagicMock(),
            get_pending_orders=MagicMock(return_value=[local_order]),
        )

        def clear_uncertain(keys):
            engine._uncertain_cancel_order_ids.difference_update(keys)
            rest._uncertain_cancellations.clear()

        engine._clear_uncertain_cancellation_markers = MagicMock(
            side_effect=clear_uncertain
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {}

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 0)
        self.assertEqual(local_order.status, GridOrderStatus.FILLED)
        self.assertEqual(local_order.filled_amount, Decimal("0.00020"))
        callback.assert_awaited_once_with(local_order)
        self.assertEqual(engine._pending_orders, {})
        self.assertEqual(engine._expected_cancellations, set())
        self.assertEqual(rest._uncertain_cancellations, set())

    async def test_uncertain_expected_cancel_exact_cancel_cleans_without_restore(self):
        local_order = GridOrder(
            order_id="101",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62500"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        terminal = SimpleNamespace(
            id="101",
            client_id="client-101",
            status=OrderStatus.CANCELED,
            average=None,
            price=Decimal("62500"),
            filled=Decimal("0"),
            amount=Decimal("0.00020"),
            params={},
            raw_data={},
        )
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        restore = AsyncMock()
        engine = SimpleNamespace(
            exchange=SimpleNamespace(
                _rest=rest,
                get_order_history=AsyncMock(return_value=[terminal]),
            ),
            coordinator=None,
            _pending_orders={"101": local_order, "client-101": local_order},
            _expected_cancellations={"101", "client-101"},
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            _uncertain_cancel_order_ids={"101", "client-101"},
            _pending_keys_for_order=lambda _order: ["101", "client-101"],
            _is_uncertain_cancel=lambda *keys: any(
                key in engine._uncertain_cancel_order_ids for key in keys
            ),
            _claim_order_finalization=MagicMock(return_value=True),
            _clear_restore_state=MagicMock(),
            _restore_cancelled_grid_order=restore,
            get_pending_orders=MagicMock(return_value=[local_order]),
        )

        def clear_uncertain(keys):
            engine._uncertain_cancel_order_ids.difference_update(keys)
            rest._uncertain_cancellations.clear()

        engine._clear_uncertain_cancellation_markers = MagicMock(
            side_effect=clear_uncertain
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {}

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 0)
        self.assertEqual(local_order.status, GridOrderStatus.CANCELLED)
        self.assertEqual(engine._pending_orders, {})
        self.assertEqual(engine._expected_cancellations, set())
        self.assertEqual(rest._uncertain_cancellations, set())
        restore.assert_not_awaited()

    def test_expected_long_position_uses_tp_remaining_and_partial_base_fill(self):
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.config = SimpleNamespace(grid_type=GridType.LONG)
        orders = [
            SimpleNamespace(
                side=OrderSide.SELL,
                amount=Decimal("0.00020"),
                filled=Decimal("0.00019"),
                remaining=Decimal("0.00001"),
            ),
            SimpleNamespace(
                side=OrderSide.SELL,
                amount=Decimal("0.00020"),
                filled=Decimal("0"),
                remaining=Decimal("0.00020"),
            ),
            SimpleNamespace(
                side=OrderSide.BUY,
                amount=Decimal("0.00020"),
                filled=Decimal("0.00007"),
                remaining=Decimal("0.00013"),
            ),
        ]

        self.assertEqual(checker._calculate_expected_position(orders), Decimal("0.00028"))

    def test_expected_short_position_is_partial_fill_aware(self):
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.config = SimpleNamespace(grid_type=GridType.SHORT)
        orders = [
            SimpleNamespace(
                side=OrderSide.BUY,
                amount=Decimal("0.00020"),
                filled=Decimal("0.00019"),
                remaining=Decimal("0.00001"),
            ),
            SimpleNamespace(
                side=OrderSide.SELL,
                amount=Decimal("0.00020"),
                filled=Decimal("0.00007"),
                remaining=Decimal("0.00013"),
            ),
        ]

        self.assertEqual(checker._calculate_expected_position(orders), Decimal("-0.00008"))

    def test_expected_position_includes_active_entry_continuation_carry(self):
        continuation = GridOrder(
            order_id="replacement",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("64000"),
            amount=Decimal("0.00001"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
            exchange_data={
                "tradexyz_fill_tracking": {
                    "target_amount": "0.00020",
                    "carry_filled_amount": "0.00019",
                    "cumulative_filled": "0.00019",
                    "remaining_amount": "0.00001",
                }
            },
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.config = SimpleNamespace(grid_type=GridType.LONG)
        checker.engine = SimpleNamespace(
            coordinator=None,
            get_pending_orders=lambda: [continuation],
        )
        exchange_continuation = SimpleNamespace(
            id="replacement",
            client_id=None,
            side=OrderSide.BUY,
            amount=Decimal("0.00001"),
            filled=Decimal("0"),
            remaining=Decimal("0.00001"),
        )

        self.assertEqual(
            checker._calculate_expected_position([exchange_continuation]),
            Decimal("0.00019"),
        )

    async def test_open_partial_order_does_not_trigger_fill_callback(self):
        local_order = GridOrder(
            order_id="101",
            grid_id=1,
            side=GridOrderSide.SELL,
            price=Decimal("64250"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        callback = AsyncMock()
        exchange = SimpleNamespace(get_order_history=AsyncMock())
        engine = SimpleNamespace(
            exchange=exchange,
            _pending_orders={"101": local_order},
            _expected_cancellations=set(),
            _order_callbacks=[callback],
            _exchange_sync_grace_period=0,
            get_pending_orders=MagicMock(return_value=[local_order]),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker._missing_order_seen_at = {}
        checker._build_grid_order_from_exchange_order = MagicMock()

        unresolved = await checker._sync_orders_into_engine(
            [
                SimpleNamespace(
                    id="101",
                    client_id=None,
                    side=OrderSide.SELL,
                    amount=Decimal("0.00020"),
                    filled=Decimal("0.00019"),
                    remaining=Decimal("0.00001"),
                    status=OrderStatus.OPEN,
                    price=Decimal("64250"),
                )
            ]
        )

        self.assertEqual(unresolved, 0)
        callback.assert_not_awaited()
        exchange.get_order_history.assert_not_awaited()

    async def test_below_minimum_position_repair_is_skipped_without_clamping(self):
        exchange = SimpleNamespace(
            _market_info={
                "BTC": {
                    "symbol": "BTC",
                    "min_base_amount": "0.00020",
                    "min_quote_amount": "0",
                }
            }
        )
        engine = SimpleNamespace(
            exchange=exchange,
            place_market_order=AsyncMock(),
            placement_epoch=0,
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()

        for _ in range(2):
            submitted = await checker._submit_market_order(
                side=OrderSide.BUY,
                amount=Decimal("0.00019"),
                current_price=Decimal("64000"),
                reason="open_position",
            )
            self.assertFalse(submitted)

        engine.place_market_order.assert_not_awaited()
        checker.logger.warning.assert_called_once()

    async def test_failed_restore_releases_matching_grid_lock(self):
        local_order = GridOrder(
            order_id="101",
            grid_id=78,
            side=GridOrderSide.BUY,
            price=Decimal("64425"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime(2000, 1, 1),
        )
        locks = {
            78: {
                "tp_side": "buy",
                "tp_price": Decimal("64425"),
                "tp_order_id": "101",
            }
        }
        coordinator = SimpleNamespace(_grid_level_locks=locks, _last_fill_time=0)
        exchange = SimpleNamespace(get_order_history=AsyncMock(return_value=[]))
        engine = SimpleNamespace(
            exchange=exchange,
            coordinator=coordinator,
            _pending_orders={"101": local_order},
            _expected_cancellations=set(),
            _order_callbacks=[],
            _exchange_sync_grace_period=0,
            get_pending_orders=MagicMock(return_value=[local_order]),
            _restore_cancelled_grid_order=AsyncMock(return_value=None),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {"101": 0}
        checker._missing_order_resolution_timeout = 0
        checker._restored_missing_orders_in_sync = 0

        unresolved = await checker._sync_orders_into_engine([])

        self.assertEqual(unresolved, 1)
        self.assertNotIn(78, locks)

    async def test_remote_client_id_adopts_exact_order_id(self):
        local_order = GridOrder(
            order_id="25471046",
            grid_id=4,
            side=GridOrderSide.BUY,
            price=Decimal("62800"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
        )
        local_order.exchange_data = {"submission_uncertain": True}
        exchange = SimpleNamespace(get_order=AsyncMock())
        adopt_submission = MagicMock()
        engine = SimpleNamespace(
            exchange=exchange,
            _pending_orders={"25471046": local_order},
            _expected_cancellations=set(),
            _order_callbacks=[],
            get_pending_orders=MagicMock(return_value=[local_order]),
            _is_submission_uncertain=MagicMock(return_value=True),
            _adopt_reconciled_grid_submission=adopt_submission,
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {"25471046": 1.0}
        checker._build_grid_order_from_exchange_order = MagicMock()

        remote_order = SimpleNamespace(id=844424909730865, client_id=25471046)
        unresolved = await checker._sync_orders_into_engine([remote_order])

        self.assertEqual(unresolved, 0)
        self.assertEqual(local_order.status, GridOrderStatus.PENDING)
        adopt_submission.assert_called_once_with(local_order, remote_order)
        exchange.get_order.assert_not_awaited()
        checker._build_grid_order_from_exchange_order.assert_not_called()

    async def test_zero_target_positions_are_closed_reduce_only(self):
        cases = (
            (Decimal("-0.00020"), GridOrderSide.BUY),
            (Decimal("0.00020"), GridOrderSide.SELL),
        )
        for actual_position, close_side in cases:
            with self.subTest(actual_position=actual_position):
                engine = SimpleNamespace(
                    get_current_price=AsyncMock(return_value=Decimal("63030.5")),
                    place_market_order=AsyncMock(),
                    placement_epoch=0,
                )
                checker = OrderHealthChecker.__new__(OrderHealthChecker)
                checker.engine = engine
                checker.logger = MagicMock()
                checker._open_position = AsyncMock()

                adjusted = await checker._adjust_position(
                    PositionHealthResult(
                        expected_position=Decimal("0"),
                        actual_position=actual_position,
                        tolerance=Decimal("0.00001"),
                        needs_adjustment=True,
                    )
                )

                self.assertTrue(adjusted)
                engine.place_market_order.assert_awaited_once_with(
                    side=close_side,
                    amount=Decimal("0.00020"),
                    reduce_only=True,
                    reference_price=Decimal("63030.5"),
                )
                checker._open_position.assert_not_awaited()

    async def test_zero_target_close_failure_does_not_open_opposite_side(self):
        engine = SimpleNamespace(
            get_current_price=AsyncMock(return_value=Decimal("63030.5")),
            place_market_order=AsyncMock(side_effect=RuntimeError("rejected")),
            placement_epoch=0,
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.logger = MagicMock()
        checker._open_position = AsyncMock()

        adjusted = await checker._adjust_position(
            PositionHealthResult(
                expected_position=Decimal("0"),
                actual_position=Decimal("-0.00020"),
                tolerance=Decimal("0.00001"),
                needs_adjustment=True,
            )
        )

        self.assertFalse(adjusted)
        checker._open_position.assert_not_awaited()

    async def test_position_drift_requires_two_matching_health_cycles(self):
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.logger = MagicMock()
        checker._check_position_health = MagicMock(
            return_value=PositionHealthResult(
                expected_position=Decimal("0"),
                actual_position=Decimal("0.00020"),
                tolerance=Decimal("0.00001"),
                needs_adjustment=True,
            )
        )
        checker._adjust_position = AsyncMock(return_value=True)

        first = await checker._repair_position_if_confirmed([], [])
        second = await checker._repair_position_if_confirmed([], [])

        self.assertFalse(first)
        self.assertTrue(second)
        checker._adjust_position.assert_awaited_once()

    def test_active_fill_callback_blocks_position_repair(self):
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = SimpleNamespace(
            coordinator=SimpleNamespace(_active_fill_callbacks=1)
        )
        checker.config = SimpleNamespace(exchange="lighter")

        self.assertFalse(checker._should_repair_position())
        self.assertEqual(checker._position_drift_cycles, 0)


if __name__ == "__main__":
    unittest.main()
