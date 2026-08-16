import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.adapters.exchanges.models import PositionSide
from core.services.grid.implementations.order_health_checker import (
    OrderHealthChecker,
    PositionHealthResult,
)
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus


class OrderHealthCheckerIncidentTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_client_id_keeps_local_order_open(self):
        local_order = GridOrder(
            order_id="25471046",
            grid_id=4,
            side=GridOrderSide.BUY,
            price=Decimal("62800"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
        )
        exchange = SimpleNamespace(get_order=AsyncMock())
        engine = SimpleNamespace(
            exchange=exchange,
            _pending_orders={"25471046": local_order},
            _expected_cancellations=set(),
            _order_callbacks=[],
            get_pending_orders=MagicMock(return_value=[local_order]),
        )
        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(symbol="BTC")
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {"25471046": 1.0}
        checker._build_grid_order_from_exchange_order = MagicMock()

        unresolved = await checker._sync_orders_into_engine(
            [SimpleNamespace(id=844424909730865, client_id=25471046)]
        )

        self.assertEqual(unresolved, 0)
        self.assertEqual(local_order.status, GridOrderStatus.PENDING)
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


if __name__ == "__main__":
    unittest.main()
