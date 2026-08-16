import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.tradexyz_rest import TradeXYZRest
from run_grid_trading import create_exchange_adapter


class LighterMinimumOrderTests(unittest.TestCase):
    def setUp(self):
        self.market_info = {
            "min_base_amount": "0.05",
            "min_quote_amount": "10",
        }

    def test_rejects_below_base_minimum(self):
        with self.assertRaisesRegex(ValueError, "min_base_amount"):
            LighterRest._validate_order_minimums(
                Decimal("0.049"), Decimal("300"), self.market_info
            )

    def test_rejects_below_quote_minimum(self):
        with self.assertRaisesRegex(ValueError, "min_quote_amount"):
            LighterRest._validate_order_minimums(
                Decimal("0.05"), Decimal("100"), self.market_info
            )

    def test_accepts_market_minimums(self):
        LighterRest._validate_order_minimums(
            Decimal("0.05"), Decimal("200"), self.market_info
        )

    def test_reduce_only_bypasses_minimums_but_requires_positive_quantity(self):
        LighterRest._validate_order_minimums(
            Decimal("0.001"),
            Decimal("1"),
            self.market_info,
            enforce_market_minimums=False,
        )
        with self.assertRaisesRegex(ValueError, "greater than 0"):
            LighterRest._validate_order_minimums(
                Decimal("0"),
                Decimal("1"),
                self.market_info,
                enforce_market_minimums=False,
            )


class LighterMarketMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_market_info_keeps_discovered_minimums(self):
        rest = object.__new__(LighterRest)
        rest._market_info_cache = {}
        rest._symbol_to_market_index = {"PLTR": 34}
        rest._markets_cache = {
            34: {
                "market_id": 34,
                "symbol": "PLTR",
                "min_base_amount": "0.05",
                "min_quote_amount": "10",
            }
        }
        rest.markets = {"PLTR": rest._markets_cache[34]}
        detail = SimpleNamespace(
            supported_price_decimals=3,
            supported_size_decimals=3,
        )
        rest.order_api = SimpleNamespace(
            order_book_details=AsyncMock(
                return_value=SimpleNamespace(order_book_details=[detail])
            )
        )

        info = await rest._get_market_info("PLTR")

        self.assertEqual(info["min_base_amount"], "0.05")
        self.assertEqual(info["min_quote_amount"], "10")


class TradeXYZPositionQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_error_is_not_reported_as_flat(self):
        rest = object.__new__(TradeXYZRest)
        rest.config = SimpleNamespace(wallet_address="0x1")
        rest.logger = None
        rest.get_xyz_clearinghouse_state = AsyncMock(
            side_effect=RuntimeError("position API unavailable")
        )

        with self.assertRaisesRegex(RuntimeError, "position API unavailable"):
            await rest._get_xyz_positions(["NVDA"])


class AdapterConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_false_connect_result_is_fatal_and_disconnects(self):
        adapter = SimpleNamespace(
            connect=AsyncMock(return_value=False),
            disconnect=AsyncMock(),
        )
        config = {
            "grid_system": {
                "exchange": "tradexyz",
                "symbol": "NVDA",
            }
        }
        env = {
            "TRADEXYZ_API_KEY": "test-key",
            "TRADEXYZ_API_SECRET": "test-key",
            "TRADEXYZ_WALLET_ADDRESS": "0x0000000000000000000000000000000000000001",
        }

        with (
            patch("run_grid_trading._load_dotenv", return_value=Path(".env")),
            patch("core.adapters.exchanges.ExchangeFactory") as factory_class,
            patch.dict(os.environ, env, clear=False),
        ):
            factory_class.return_value.create_adapter.return_value = adapter
            with self.assertRaisesRegex(RuntimeError, "Failed to connect"):
                await create_exchange_adapter(config)

        adapter.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
