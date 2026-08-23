import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_base import LighterBase
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.models import OrderSide, OrderStatus, PositionSide


class LighterSignerConfigTests(unittest.TestCase):
    @patch("lighter.SignerClient")
    def test_signer_indices_from_string_config_are_converted_to_ints(
        self, signer_client
    ):
        LighterBase(
            {
                "network": "robinhood",
                "api_key_private_key": "test-only",
                "account_index": "65",
                "api_key_index": "4",
            }
        )

        signer_client.assert_called_once()
        kwargs = signer_client.call_args.kwargs
        self.assertEqual(kwargs["account_index"], 65)
        self.assertIsInstance(kwargs["account_index"], int)
        self.assertEqual(kwargs["api_private_keys"], {4: "test-only"})
        self.assertIsInstance(next(iter(kwargs["api_private_keys"])), int)

    @patch("lighter.SignerClient")
    def test_invalid_signer_indices_fail_before_sdk_client_creation(
        self, signer_client
    ):
        invalid_values = (
            ("account_index", True),
            ("account_index", "1.5"),
            ("account_index", "-1"),
            ("api_key_index", "255"),
        )

        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                config = {
                    "network": "robinhood",
                    "api_key_private_key": "test-only",
                    "account_index": "65",
                    "api_key_index": "4",
                    field: value,
                }
                with self.assertRaisesRegex(ValueError, field):
                    LighterBase(config)

        signer_client.assert_not_called()


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
            trades=AsyncMock(),
        )
        rest.account_api = SimpleNamespace(account=AsyncMock())
        rest.network = "robinhood"
        rest.get_market_index = MagicMock(return_value=1)
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

    async def test_account_trades_preserve_account_side_role_fee_and_pnl(self):
        rest = self.make_rest()
        rest.order_api.trades.return_value = SimpleNamespace(
            code=200,
            trades=[
                SimpleNamespace(
                    trade_id=1,
                    trade_id_str="trade-1",
                    type="trade",
                    market_id=1,
                    size="0.00020",
                    price="77000",
                    usd_amount="15.400000",
                    ask_id=11,
                    ask_id_str="ask-11",
                    bid_id=12,
                    bid_id_str="bid-12",
                    ask_account_id=123,
                    bid_account_id=999,
                    is_maker_ask=True,
                    maker_fee=120,
                    taker_fee=None,
                    integrator_maker_fee=0,
                    ask_account_pnl="0.010000",
                    bid_account_pnl="0",
                    timestamp=1_800_000_000_000,
                )
            ],
        )

        trades = await rest.get_account_trades("BTC")

        self.assertEqual(len(trades), 1)
        self.assertIs(trades[0].side, OrderSide.SELL)
        self.assertEqual(trades[0].order_id, "ask-11")
        self.assertEqual(trades[0].fee["role"], "maker")
        self.assertEqual(trades[0].fee["rate"], Decimal("0.00012"))
        self.assertEqual(trades[0].fee["cost"], Decimal("0.00184800000"))
        self.assertEqual(trades[0].raw_data["trade_sequence"], 1)
        self.assertEqual(trades[0].raw_data["realized_pnl"], Decimal("0.010000"))
        rest.order_api.trades.assert_awaited_once_with(
            sort_by="timestamp",
            limit=100,
            authorization="token",
            market_id=1,
            market_type="perp",
            account_index=123,
            sort_dir="desc",
            aggregate=False,
        )

    async def test_account_trades_reject_self_trade_attribution(self):
        rest = self.make_rest()
        rest.order_api.trades.return_value = SimpleNamespace(
            code=200,
            trades=[
                SimpleNamespace(
                    ask_account_id=123,
                    bid_account_id=123,
                )
            ],
        )

        with self.assertRaisesRegex(RuntimeError, "self-trade"):
            await rest.get_account_trades("BTC")

    async def test_account_trades_treat_null_opening_pnl_as_zero(self):
        rest = self.make_rest()
        rest.order_api.trades.return_value = SimpleNamespace(
            code=200,
            trades=[
                SimpleNamespace(
                    trade_id=2,
                    trade_id_str="trade-2",
                    type="trade",
                    size="0.00020",
                    price="77000",
                    usd_amount="15.400000",
                    ask_id_str="ask-12",
                    ask_account_id=123,
                    bid_account_id=999,
                    is_maker_ask=True,
                    maker_fee=120,
                    integrator_maker_fee=0,
                    ask_account_pnl=None,
                    timestamp=1_800_000_000_001,
                )
            ],
        )

        trades = await rest.get_account_trades("BTC")

        self.assertEqual(trades[0].raw_data["realized_pnl"], Decimal("0"))

    async def test_account_trades_classify_both_sides_and_roles_exactly(self):
        cases = (
            (999, 123, False, "maker", 120, OrderSide.BUY),
            (999, 123, True, "taker", 350, OrderSide.BUY),
            (123, 999, True, "maker", 120, OrderSide.SELL),
            (123, 999, False, "taker", 350, OrderSide.SELL),
        )
        for ask_account, bid_account, maker_ask, role, tick, side in cases:
            with self.subTest(role=role, side=side):
                rest = self.make_rest()
                rest.order_api.trades.return_value = SimpleNamespace(
                    code=200,
                    trades=[
                        SimpleNamespace(
                            trade_id=3,
                            trade_id_str="trade-3",
                            type="trade",
                            size="0.00020",
                            price="77000",
                            usd_amount="15.400000",
                            ask_id_str="ask-13",
                            bid_id_str="bid-13",
                            ask_account_id=ask_account,
                            bid_account_id=bid_account,
                            is_maker_ask=maker_ask,
                            maker_fee=120,
                            taker_fee=350,
                            integrator_maker_fee=0,
                            integrator_taker_fee=0,
                            ask_account_pnl=None,
                            bid_account_pnl=None,
                            timestamp=1_800_000_000_002,
                        )
                    ],
                )

                parsed = await rest.get_account_trades("BTC")

                self.assertIs(parsed[0].side, side)
                self.assertEqual(parsed[0].fee["role"], role)
                self.assertEqual(parsed[0].fee["tick"], tick)

    async def test_account_trades_reject_missing_role_or_nonzero_integrator_fee(self):
        for role_value, integrator_fee, message in (
            (None, 0, "maker/taker"),
            (True, 1, "integrator fee"),
        ):
            with self.subTest(message=message):
                rest = self.make_rest()
                rest.order_api.trades.return_value = SimpleNamespace(
                    code=200,
                    trades=[
                        SimpleNamespace(
                            trade_id=4,
                            trade_id_str="trade-4",
                            type="trade",
                            size="0.00020",
                            price="77000",
                            usd_amount="15.400000",
                            ask_id_str="ask-14",
                            ask_account_id=123,
                            bid_account_id=999,
                            is_maker_ask=role_value,
                            maker_fee=120,
                            ask_account_pnl=None,
                            integrator_maker_fee=integrator_fee,
                            timestamp=1_800_000_000_003,
                        )
                    ],
                )

                with self.assertRaisesRegex(RuntimeError, message):
                    await rest.get_account_trades("BTC")

    async def test_account_trades_preserve_null_integrator_fee_as_unverified(self):
        rest = self.make_rest()
        rest.order_api.trades.return_value = SimpleNamespace(
            code=200,
            trades=[
                SimpleNamespace(
                    trade_id=4,
                    trade_id_str="trade-4",
                    type="trade",
                    size="0.00020",
                    price="77000",
                    usd_amount="15.400000",
                    ask_id_str="ask-14",
                    ask_account_id=123,
                    bid_account_id=999,
                    is_maker_ask=True,
                    maker_fee=120,
                    ask_account_pnl=None,
                    integrator_maker_fee=None,
                    timestamp=1_800_000_000_003,
                )
            ],
        )

        trades = await rest.get_account_trades("BTC")

        self.assertIsNone(trades[0].raw_data["integrator_fee_tick"])

    async def test_safety_cancellation_bypasses_existing_read_cooldown(self):
        rest = self.make_rest()
        rest._request_lock = asyncio.Lock()
        rest._next_request_at = asyncio.get_running_loop().time() + 30
        rest._rate_limit_failures = 1
        rest._request_interval = 0.05
        rest._max_rate_limit_delay = 30.0
        request = AsyncMock(return_value="cancelled")

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            result = await rest._call_api(
                "order cancellation", request, retry_on_429=False
            )

        self.assertEqual(result, "cancelled")
        request.assert_awaited_once_with()
        sleep.assert_not_awaited()

    async def test_safety_scope_bypasses_cooldown_for_terminal_reads(self):
        rest = self.make_rest()
        rest._request_lock = asyncio.Lock()
        rest._next_request_at = asyncio.get_running_loop().time() + 30
        rest._rate_limit_failures = 1
        rest._request_interval = 0.05
        rest._max_rate_limit_delay = 30.0
        request = AsyncMock(return_value="proof")

        rest.begin_safety_requests()
        try:
            with patch(
                "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep:
                result = await rest._call_api("order history query", request)
        finally:
            rest.end_safety_requests()

        self.assertEqual(result, "proof")
        sleep.assert_not_awaited()
        self.assertEqual(rest._safety_request_depth, 0)

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

    async def test_cancel_reconciliation_outlasts_stale_active_snapshot(self):
        rest = self.make_rest()
        rest.CANCELLATION_RECONCILIATION_ATTEMPTS = 2
        rest.CANCELLATION_RECONCILIATION_DELAY = 0
        active = SimpleNamespace(id="123", client_id="client-123")
        canceled = SimpleNamespace(
            id="123",
            client_id="client-123",
            status=OrderStatus.CANCELED,
        )
        rest.get_open_orders = AsyncMock(side_effect=([active], []))
        rest.get_order_history = AsyncMock(side_effect=([], [canceled]))

        result = await rest._reconcile_cancellation("BTC", "123")

        self.assertIs(result, True)
        self.assertEqual(rest.get_open_orders.await_count, 2)
        self.assertEqual(rest.get_order_history.await_count, 2)

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

        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            await rest.get_positions()

    async def test_positions_legitimate_empty_account(self):
        rest = self.make_rest()
        account = SimpleNamespace(account_index=123, positions=[])
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
            code=200,
            accounts=[SimpleNamespace(account_index=123, positions=[position])],
        )

        positions = await rest.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].side, PositionSide.SHORT)
        self.assertEqual(positions[0].size, Decimal("0.078"))

    async def test_positions_rejects_unknown_sdk_sign(self):
        rest = self.make_rest()
        position = SimpleNamespace(symbol="ETH", position="0.1", sign=2)
        rest.account_api.account.return_value = SimpleNamespace(
            code=200,
            accounts=[SimpleNamespace(account_index=123, positions=[position])],
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
                    accounts=[
                        SimpleNamespace(account_index=123, positions=[position])
                    ],
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
        account = SimpleNamespace(
            account_index=123,
            available_balance="0",
            collateral="0",
        )
        rest.account_api.account.return_value = SimpleNamespace(
            code=200, accounts=[account]
        )

        balances = await rest.get_account_balance()

        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].currency, "USDG")
        self.assertEqual(balances[0].total, Decimal("0"))

    async def test_account_queries_reject_wrong_identity_or_missing_collateral(self):
        for account, message in (
            (
                SimpleNamespace(
                    account_index=999,
                    available_balance="1",
                    collateral="1",
                ),
                "different account",
            ),
            (
                SimpleNamespace(account_index=123, available_balance="1"),
                "collateral",
            ),
        ):
            with self.subTest(message=message):
                rest = self.make_rest()
                rest.account_api.account.return_value = SimpleNamespace(
                    code=200, accounts=[account]
                )

                with self.assertRaisesRegex(RuntimeError, message):
                    await rest.get_account_balance()

    async def test_positions_reject_untrusted_risk_fields(self):
        valid = {
            "symbol": "BTC",
            "position": "0.2",
            "sign": 1,
            "avg_entry_price": "70000",
            "unrealized_pnl": "0",
            "realized_pnl": "0",
            "initial_margin_fraction": "100",
            "margin_mode": 0,
            "allocated_margin": "14",
            "liquidation_price": "0",
        }
        cases = (
            ({"unrealized_pnl": None}, "unrealized"),
            ({"initial_margin_fraction": None}, "margin fraction"),
            ({"initial_margin_fraction": "101"}, "margin fraction"),
            ({"margin_mode": None}, "margin mode"),
            ({"margin_mode": 2}, "margin mode"),
        )
        for overrides, message in cases:
            with self.subTest(message=message):
                rest = self.make_rest()
                values = dict(valid)
                values.update(overrides)
                rest.account_api.account.return_value = SimpleNamespace(
                    code=200,
                    accounts=[
                        SimpleNamespace(
                            account_index=123,
                            positions=[SimpleNamespace(**values)],
                        )
                    ],
                )

                with self.assertRaisesRegex(RuntimeError, message):
                    await rest.get_positions()

    async def test_positions_accept_exchange_imf_precision(self):
        for margin_fraction, expected_leverage in (("33.33", 3), ("6.66", 15)):
            with self.subTest(margin_fraction=margin_fraction):
                rest = self.make_rest()
                position = SimpleNamespace(
                    symbol="BTC",
                    position="0.2",
                    sign=1,
                    avg_entry_price="70000",
                    unrealized_pnl="0",
                    realized_pnl="0",
                    initial_margin_fraction=margin_fraction,
                    margin_mode=0,
                    allocated_margin="14",
                    liquidation_price="0",
                )
                rest.account_api.account.return_value = SimpleNamespace(
                    code=200,
                    accounts=[
                        SimpleNamespace(account_index=123, positions=[position])
                    ],
                )

                parsed = await rest.get_positions()

                self.assertEqual(parsed[0].leverage, expected_leverage)

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
