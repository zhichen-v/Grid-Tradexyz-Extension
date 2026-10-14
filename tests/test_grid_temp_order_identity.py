import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.implementations.order_health_checker import OrderHealthChecker
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus, GridType


def lighter_order(order_id, client_id, status, filled="0"):
    return OrderData(
        id=order_id,
        client_id=client_id,
        symbol="BTC",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        amount=Decimal("0.00100"),
        price=Decimal("83200"),
        filled=Decimal(filled),
        remaining=Decimal("0.00100") - Decimal(filled),
        cost=Decimal("0"),
        average=Decimal("83200") if Decimal(filled) else None,
        status=status,
        timestamp=datetime.now(),
        updated=None,
        fee=None,
        trades=[],
        params={},
        raw_data={},
    )


def acknowledged_placeholder(client_id):
    order = lighter_order(None, client_id, OrderStatus.PENDING)
    order.raw_data = {"submission_uncertain": True, "submission_acknowledged": True}
    return order


def reverse_buy():
    # A long-grid re-entry after a TP fill; same grid, side, price and amount
    # repeat whenever the price oscillates around one level.
    return GridOrder(
        order_id="",
        grid_id=35,
        side=GridOrderSide.BUY,
        price=Decimal("83200"),
        amount=Decimal("0.00100"),
        status=GridOrderStatus.PENDING,
        created_at=datetime.now(),
        parent_order_id="tp-sell",
    )


class LighterTemporaryOrderIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = GridEngineImpl(self.exchange)
        engine.config = SimpleNamespace(
            exchange="lighter",
            symbol="BTC",
            grid_type=GridType.LONG,
            quantity_precision=5,
        )
        engine._running = True
        engine._exchange_sync_grace_period = 0
        engine.coordinator = SimpleNamespace(
            _grid_level_locks={},
            _request_fatal_stop=MagicMock(),
        )
        self.callback = AsyncMock()
        engine._order_callbacks = [self.callback]
        self.engine = engine

        checker = OrderHealthChecker.__new__(OrderHealthChecker)
        checker.engine = engine
        checker.config = SimpleNamespace(
            symbol="BTC",
            quantity_precision=5,
            grid_type=GridType.LONG,
        )
        checker.logger = MagicMock()
        checker._missing_order_seen_at = {}
        self.checker = checker

    async def _place(self, client_id):
        self.exchange.create_order.return_value = acknowledged_placeholder(client_id)
        order = await self.engine.place_order(reverse_buy())
        order.created_at = datetime.now() - timedelta(seconds=10)
        return order

    async def test_temporary_ids_are_unique_per_submission(self):
        first = await self._place("1791990486728")
        second = await self._place("1791990486729")

        self.assertNotEqual(first.order_id, second.order_id)
        self.assertIs(self.engine._pending_orders[first.order_id], first)
        self.assertIs(self.engine._pending_orders[second.order_id], second)

    async def test_repeated_level_fill_found_by_health_sync_triggers_reverse(self):
        first = await self._place("1791990486728")
        self.exchange.get_order_history.return_value = [
            lighter_order("844424789567203", "1791990486728", OrderStatus.FILLED, "0.00100")
        ]
        await self.checker._sync_orders_into_engine([])
        self.callback.assert_awaited_once_with(first)

        second = await self._place("1791990486729")
        self.exchange.get_order_history.return_value = [
            lighter_order("844424789567999", "1791990486729", OrderStatus.FILLED, "0.00100")
        ]
        await self.checker._sync_orders_into_engine([])

        self.assertEqual(self.callback.await_count, 2)
        self.callback.assert_awaited_with(second)
        self.assertEqual(second.status, GridOrderStatus.FILLED)


if __name__ == "__main__":
    unittest.main()
