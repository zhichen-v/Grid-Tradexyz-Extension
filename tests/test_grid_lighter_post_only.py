import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.models import (
    GridOrder,
    GridOrderSide,
    GridOrderStatus,
    GridType,
)


class LighterGridPostOnlyTests(unittest.IsolatedAsyncioTestCase):
    def _build_engine(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(
                return_value=SimpleNamespace(
                    id="123",
                    client_id="456",
                    raw_data={},
                )
            ),
        )
        engine = GridEngineImpl(exchange)
        engine.config = SimpleNamespace(
            exchange="lighter",
            symbol="BTC",
            grid_type=GridType.LONG,
        )
        engine._running = True
        return engine, exchange

    async def test_lighter_initial_opening_order_remains_post_only(self):
        engine, exchange = self._build_engine()
        order = GridOrder(
            order_id="pending",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62900"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
        )

        await engine.place_order(order)

        self.assertEqual(
            exchange.create_order.await_args.kwargs["params"],
            {"time_in_force": "POST_ONLY"},
        )

    async def test_lighter_reverse_opening_order_uses_gtt_without_reduce_only(self):
        engine, exchange = self._build_engine()
        order = GridOrder(
            order_id="pending",
            grid_id=1,
            side=GridOrderSide.BUY,
            price=Decimal("62900"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
            parent_order_id="filled-sell",
        )

        await engine.place_order(order)

        self.assertEqual(
            exchange.create_order.await_args.kwargs["params"],
            {"time_in_force": "GTT", "skip_order_index_query": True},
        )

    async def test_lighter_closing_order_uses_gtt_and_reduce_only(self):
        engine, exchange = self._build_engine()
        order = GridOrder(
            order_id="pending",
            grid_id=1,
            side=GridOrderSide.SELL,
            price=Decimal("62925"),
            amount=Decimal("0.00020"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
            parent_order_id="filled-buy",
        )

        await engine.place_order(order)

        self.assertEqual(
            exchange.create_order.await_args.kwargs["params"],
            {
                "time_in_force": "GTT",
                "skip_order_index_query": True,
                "reduce_only": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
