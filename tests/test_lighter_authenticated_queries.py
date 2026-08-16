import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.models import OrderStatus, PositionSide


class LighterAuthenticatedQueryTests(unittest.IsolatedAsyncioTestCase):
    def make_rest(self) -> LighterRest:
        rest = LighterRest.__new__(LighterRest)
        rest.signer_client = MagicMock()
        rest.signer_client.create_auth_token_with_expiry.return_value = (
            "token",
            None,
        )
        rest.api_key_index = 4
        rest.account_index = 123
        rest.order_api = SimpleNamespace(
            account_active_orders=AsyncMock(),
            account_inactive_orders=AsyncMock(),
        )
        rest.account_api = SimpleNamespace(account=AsyncMock())
        return rest

    async def test_open_orders_requires_signer(self):
        rest = self.make_rest()
        rest.signer_client = None

        with self.assertRaisesRegex(RuntimeError, "SignerClient"):
            await rest.get_open_orders()

    async def test_open_orders_api_error_is_not_reported_as_empty(self):
        rest = self.make_rest()
        rest.order_api.account_active_orders.side_effect = RuntimeError("api down")

        with self.assertRaisesRegex(RuntimeError, "api down"):
            await rest.get_open_orders()

    async def test_open_orders_legitimate_empty_response(self):
        rest = self.make_rest()
        rest.order_api.account_active_orders.return_value = SimpleNamespace(
            code=200, orders=[]
        )

        self.assertEqual(await rest.get_open_orders(), [])

    async def test_get_order_matches_active_client_id(self):
        rest = self.make_rest()
        active_order = SimpleNamespace(
            id="844424909730862",
            client_id="25471203",
            status=OrderStatus.OPEN,
        )
        rest.get_open_orders = AsyncMock(return_value=[active_order])
        rest.get_order_history = AsyncMock()

        result = await rest.get_order("25471203", "BTC")

        self.assertIs(result, active_order)
        rest.get_order_history.assert_not_awaited()

    async def test_get_order_returns_parsed_inactive_fill(self):
        rest = self.make_rest()
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.get_market_index = MagicMock(return_value=0)
        rest._get_symbol_from_market_index = MagicMock(return_value="BTC")
        rest.order_api.account_inactive_orders.return_value = SimpleNamespace(
            code=200,
            orders=[SimpleNamespace(
                order_id="844424909730862",
                client_order_id="25471203",
                market_index=0,
                initial_base_amount="0.00020",
                filled_base_amount="0.00020",
                remaining_base_amount="0",
                price="62900",
                filled_quote_amount="12.60610",
                is_ask=True,
                type="limit",
                status="filled",
                timestamp=None,
            )],
        )

        result = await rest.get_order("25471203", "BTC")

        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.filled, Decimal("0.00020"))
        self.assertEqual(result.average, Decimal("63030.5"))
        self.assertEqual(result.id, "844424909730862")

    async def test_get_order_missing_fails_closed(self):
        rest = self.make_rest()
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.get_market_index = MagicMock(return_value=0)
        rest.order_api.account_inactive_orders.return_value = SimpleNamespace(
            code=200, orders=[]
        )

        with self.assertRaisesRegex(LookupError, "not found"):
            await rest.get_order("missing", "BTC")

    async def test_get_order_rejects_inactive_api_error(self):
        rest = self.make_rest()
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.get_market_index = MagicMock(return_value=0)
        rest.order_api.account_inactive_orders.return_value = SimpleNamespace(
            code=500, orders=[]
        )

        with self.assertRaisesRegex(RuntimeError, "inactive orders query failed"):
            await rest.get_order("missing", "BTC")

    async def test_positions_requires_signer(self):
        rest = self.make_rest()
        rest.signer_client = None

        with self.assertRaisesRegex(RuntimeError, "SignerClient"):
            await rest.get_positions()

    async def test_positions_api_error_is_not_reported_as_flat(self):
        rest = self.make_rest()
        rest.account_api.account.side_effect = RuntimeError("api down")

        with self.assertRaisesRegex(RuntimeError, "api down"):
            await rest.get_positions()

    async def test_positions_missing_account_is_not_reported_as_flat(self):
        rest = self.make_rest()
        rest.account_api.account.return_value = SimpleNamespace(code=200, accounts=[])

        with self.assertRaisesRegex(RuntimeError, "未返回"):
            await rest.get_positions()

    async def test_positions_legitimate_empty_account(self):
        rest = self.make_rest()
        account = SimpleNamespace(positions=[])
        rest.account_api.account.return_value = SimpleNamespace(
            code=200, accounts=[account]
        )

        self.assertEqual(await rest.get_positions(), [])

    async def test_positions_uses_sdk_sign_field_for_short(self):
        rest = self.make_rest()
        position = SimpleNamespace(
            symbol="HYPE",
            position="0.078",
            sign=-1,
            avg_entry_price="40",
            unrealized_pnl="0",
            realized_pnl="0",
            initial_margin_fraction="10",
            margin_mode=1,
            allocated_margin="1",
            liquidation_price="0",
        )
        rest.account_api.account.return_value = SimpleNamespace(
            code=200, accounts=[SimpleNamespace(positions=[position])]
        )

        positions = await rest.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].side, PositionSide.SHORT)
        self.assertEqual(positions[0].size, Decimal("0.078"))

    async def test_positions_rejects_unknown_sdk_sign(self):
        rest = self.make_rest()
        position = SimpleNamespace(symbol="ETH", position="0.1", sign=2)
        rest.account_api.account.return_value = SimpleNamespace(
            code=200, accounts=[SimpleNamespace(positions=[position])]
        )

        with self.assertRaisesRegex(RuntimeError, "position sign"):
            await rest.get_positions()

    async def test_positions_rejects_invalid_sdk_magnitude(self):
        for value in ("NaN", "Infinity", "-0.1"):
            with self.subTest(value=value):
                rest = self.make_rest()
                position = SimpleNamespace(symbol="ETH", position=value, sign=1)
                rest.account_api.account.return_value = SimpleNamespace(
                    code=200,
                    accounts=[SimpleNamespace(positions=[position])],
                )

                with self.assertRaises(RuntimeError):
                    await rest.get_positions()

    async def test_balance_api_error_is_not_reported_as_zero(self):
        rest = self.make_rest()
        rest.network = "robinhood"
        rest.account_api.account.side_effect = RuntimeError("api down")

        with self.assertRaisesRegex(RuntimeError, "api down"):
            await rest.get_account_balance()

    async def test_zero_balance_is_an_explicit_usdg_record(self):
        rest = self.make_rest()
        rest.network = "robinhood"
        account = SimpleNamespace(available_balance="0", collateral="0")
        rest.account_api.account.return_value = SimpleNamespace(
            code=200, accounts=[account]
        )

        balances = await rest.get_account_balance()

        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].currency, "USDG")
        self.assertEqual(balances[0].total, Decimal("0"))

    async def test_cancel_rejects_non_200_sdk_response(self):
        rest = self.make_rest()
        rest.get_market_index = MagicMock(return_value=0)
        rest.signer_client.cancel_order = AsyncMock(
            return_value=(
                "tx-info",
                SimpleNamespace(code=400, message="rejected", tx_hash=""),
                None,
            )
        )

        self.assertFalse(await rest.cancel_order("ETH", "12"))

    async def test_order_rejects_non_200_sdk_response(self):
        rest = self.make_rest()

        result = await rest._handle_order_result(
            "tx-info",
            SimpleNamespace(code=400, message="rejected", tx_hash=""),
            None,
            "ETH",
            "buy",
            "limit",
            Decimal("0.1"),
            Decimal("100"),
            skip_order_index_query=True,
        )

        self.assertIsNone(result)


class LighterMarketCloseSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_slippage_applies_to_provided_close_price(self):
        rest = LighterRest.__new__(LighterRest)
        rest.config = {"market_order_slippage": "0.01"}
        rest.get_orderbook = AsyncMock()

        sell_price = await rest._calculate_slippage_protection_price(
            "ETH", "sell", Decimal("100")
        )
        buy_price = await rest._calculate_slippage_protection_price(
            "ETH", "buy", Decimal("100")
        )

        self.assertEqual(sell_price, Decimal("99.00"))
        self.assertEqual(buy_price, Decimal("101.00"))
        rest.get_orderbook.assert_not_awaited()

    async def test_market_protection_rounding_never_widens_slippage(self):
        rest = LighterRest.__new__(LighterRest)
        market_info = {
            "market_index": 0,
            "price_decimals": 2,
            "price_multiplier": Decimal("100"),
            "size_decimals": 3,
            "size_multiplier": Decimal("1000"),
        }

        buy = rest._convert_market_order_params(
            market_info, Decimal("1"), Decimal("100.006"), "buy"
        )
        sell = rest._convert_market_order_params(
            market_info, Decimal("1"), Decimal("99.994"), "sell"
        )

        self.assertEqual(buy["avg_execution_price"], 10000)
        self.assertEqual(sell["avg_execution_price"], 10000)


class LighterAdapterCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cancel_does_not_query_removed_order(self):
        adapter = LighterAdapter.__new__(LighterAdapter)
        adapter._normalize_symbol = MagicMock(return_value="BTC")
        adapter._rest = SimpleNamespace(
            cancel_order=AsyncMock(return_value=True)
        )
        adapter.get_order = AsyncMock(
            side_effect=AssertionError("post-cancel lookup must not run")
        )

        result = await adapter.cancel_order("123", "BTC")

        adapter._rest.cancel_order.assert_awaited_once_with("BTC", "123")
        adapter.get_order.assert_not_awaited()
        self.assertEqual(result.status, OrderStatus.CANCELED)

    async def test_cancel_all_uses_canonical_order_id(self):
        adapter = LighterAdapter.__new__(LighterAdapter)
        adapter.get_open_orders = AsyncMock(
            return_value=[SimpleNamespace(id="12", symbol="ETH")]
        )
        cancelled = SimpleNamespace(id="12", symbol="ETH")
        adapter.cancel_order = AsyncMock(return_value=cancelled)

        result = await adapter.cancel_all_orders("ETH")

        adapter.cancel_order.assert_awaited_once_with("12", "ETH")
        self.assertEqual(result, [cancelled])


if __name__ == "__main__":
    unittest.main()
