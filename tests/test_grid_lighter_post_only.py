import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus


class LighterGridPostOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_lighter_limit_order_is_post_only(self):
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
        engine.config = SimpleNamespace(exchange="lighter", symbol="BTC")
        engine._running = True
        order = GridOrder(
            order_id="pending",
            grid_id=1,
            side=GridOrderSide.SELL,
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


if __name__ == "__main__":
    unittest.main()
