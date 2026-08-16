import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from core.adapters.exchanges.adapters.tradexyz_rest import TradeXYZRest
from core.adapters.exchanges.models import OrderSide, OrderType


class TradeXYZReduceOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_xyz_close_forwards_reduce_only_to_sdk(self):
        rest = TradeXYZRest.__new__(TradeXYZRest)
        rest.is_xyz_symbol = MagicMock(return_value=True)
        rest._ensure_xyz_sdk = MagicMock()
        rest.to_xyz_coin = MagicMock(return_value="xyz:PLTR")
        rest._round_to_sig_figs = MagicMock(return_value=10.0)
        rest._xyz_sdk_exchange = MagicMock()
        rest._xyz_sdk_exchange.order.return_value = {"status": "ok"}
        expected = object()
        rest._parse_xyz_sdk_order_result = MagicMock(return_value=expected)

        result = await rest.create_order(
            symbol="PLTR",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=Decimal("1"),
            price=Decimal("10"),
            params={"reduce_only": True},
        )

        self.assertIs(result, expected)
        self.assertTrue(rest._xyz_sdk_exchange.order.call_args.kwargs["reduce_only"])


if __name__ == "__main__":
    unittest.main()
