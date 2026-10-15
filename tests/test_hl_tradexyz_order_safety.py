"""Offline safety checks for Hyperliquid / TradeXYZ order paths."""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.adapters.exchanges.adapters.hyperliquid_rest import HyperliquidRest
from core.adapters.exchanges.adapters.tradexyz_rest import TradeXYZRest
from core.adapters.exchanges.models import OrderSide, OrderStatus, OrderType


def xyz_rest() -> TradeXYZRest:
    rest = TradeXYZRest.__new__(TradeXYZRest)
    rest.logger = None
    rest.is_xyz_symbol = MagicMock(return_value=True)
    rest._ensure_xyz_sdk = MagicMock()
    rest.to_xyz_coin = MagicMock(return_value="xyz:NVDA")
    rest._round_to_sig_figs = lambda value: value
    rest._xyz_sdk_exchange = MagicMock()
    return rest


class HyperliquidSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_is_not_retried_and_maps_reduce_only(self):
        rest = HyperliquidRest.__new__(HyperliquidRest)
        rest.map_symbol = lambda symbol: symbol
        rest._place_order = AsyncMock(side_effect=TimeoutError("lost response"))
        rest.get_ticker = AsyncMock(return_value=SimpleNamespace(last=Decimal("100"), close=None))

        with self.assertRaises(TimeoutError):
            await rest.create_order(
                "BTC/USDC:USDC", OrderSide.SELL, OrderType.MARKET, Decimal("1"),
                params={"reduce_only": True},
            )

        rest._place_order.assert_awaited_once()
        args = rest._place_order.await_args.args
        self.assertEqual(args[4], 100.0)  # reference price for the ccxt slippage bound
        self.assertEqual(args[5], {"reduceOnly": True})


class TradeXYZOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_order_without_price_is_bounded_from_ticker(self):
        rest = xyz_rest()
        rest.get_ticker = AsyncMock(return_value=SimpleNamespace(last=Decimal("200"), close=None))
        rest._parse_xyz_sdk_order_result = MagicMock(return_value="parsed")

        await rest.create_order("NVDA", OrderSide.BUY, OrderType.MARKET, Decimal("1"))

        self.assertAlmostEqual(rest._xyz_sdk_exchange.order.call_args.kwargs["limit_px"], 210.0)

    def test_partial_ioc_fill_reports_real_size(self):
        rest = xyz_rest()
        result = {"response": {"data": {"statuses": [
            {"filled": {"oid": 7, "totalSz": "0.4", "avgPx": "200"}}
        ]}}}
        order = rest._parse_xyz_sdk_order_result(
            result, "NVDA", OrderSide.BUY, Decimal("1"), Decimal("210")
        )
        self.assertEqual(order.filled, Decimal("0.4"))
        self.assertEqual(order.remaining, Decimal("0.6"))
        self.assertEqual(order.status, OrderStatus.CANCELED)

    async def test_open_order_read_failure_is_not_an_empty_book(self):
        rest = xyz_rest()
        rest.xyz_market_enabled = True
        rest._get_xyz_open_orders_list = AsyncMock(side_effect=RuntimeError("timeout"))
        with self.assertRaises(RuntimeError):
            await rest.get_open_orders("NVDA")

    async def test_cancel_all_raises_when_any_cancel_fails(self):
        rest = xyz_rest()
        rest.xyz_market_enabled = True
        rest._get_xyz_open_orders_list = AsyncMock(
            return_value=[{"oid": 1, "coin": "xyz:NVDA"}, {"oid": 2, "coin": "xyz:NVDA"}]
        )
        rest.cancel_order = AsyncMock(side_effect=[object(), RuntimeError("rejected")])
        with self.assertRaisesRegex(RuntimeError, "Failed to cancel 1"):
            await rest.cancel_all_orders("NVDA")
        self.assertEqual(rest.cancel_order.await_count, 2)


if __name__ == "__main__":
    unittest.main()
