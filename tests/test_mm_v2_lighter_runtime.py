"""Offline public-contract checks: no adapter construction, signer or network."""

import unittest
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from core.services.market_maker_v2.domain import Side, WorkingOrder
from core.services.market_maker_v2.lighter_runtime import (
    LighterAccountPort, LighterMarketData, LighterReadError,
)
from core.services.market_maker_v2.session_ledger import SessionLedger


ADDRESS = "0x" + "1" * 40


class Clock:
    now = 1.0

    def monotonic(self):
        return self.now


def trade(identifier="1", *, side="buy", price="100", size="1", role="maker", order="10"):
    rate = D("0.0001") if role == "maker" else D("0.0003")
    amount, price = D(size), D(price)
    return NS(id=identifier, order_id=order, symbol="BTC", side=side,
              amount=amount, price=price, cost=amount * price,
              fee=dict(role=role, rate=rate, tick=int(rate * 1000000),
                       cost=amount * price * rate, currency="USDG"),
              raw_data=dict(timestamp=int(identifier) * 1000,
                            trade_sequence=int(identifier), integrator_fee_tick=0))


class Adapter:
    managed_order_integrator_fee_tick = 0

    def __init__(self):
        self.trades, self.orders = [], []
        self.fees = dict(maker_fee_rate=D("0.0001"), taker_fee_rate=D("0.0003"), fundings=())
        self.position = NS(symbol="BTC", position="0", sign=1, avg_entry_price="100",
                           unrealized_pnl="0", margin_mode=0, initial_margin_fraction="100",
                           pending_order_count=0, open_order_count=0)
        self.account = NS(account_index=7, index=7, l1_address=ADDRESS, collateral="100",
                          account_trading_mode=0, shares=[], assets=[], pending_order_count=0,
                          total_order_count=0, positions=[self.position])
        self.book = NS(symbol="BTC", bids=[NS(price=D("99"), size=D("5"))],
                       asks=[NS(price=D("101"), size=D("5"))])
        self.metadata = NS(symbols=[dict(symbol="BTC", status="active", price_decimals=0,
                          size_decimals=1, min_base_amount="0.1", min_quote_amount="5")])

    async def get_account_trades(self, symbol, limit):
        return self.trades[:]

    async def get_account_fee_and_funding(self, symbol, limit):
        return self.fees.copy()

    async def get_open_orders(self):
        return self.orders[:]

    async def get_balances(self):
        return [NS(currency="USDG", total=D(self.account.collateral),
                   raw_data={"account": self.account})]

    async def get_exchange_info(self):
        return self.metadata

    async def get_orderbook(self, symbol, limit):
        return self.book


class LighterAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter, self.clock = Adapter(), Clock()
        self.flatten = {}
        self.terminal = set()
        self.port = LighterAccountPort(self.adapter, "BTC", self.clock,
            account_index=7, expected_l1_address=ADDRESS,
            known_order_ids=lambda: {"10", "11"}, flatten_id_for=self.flatten.get,
            terminal_order_ids=lambda: self.terminal)

    async def start(self):
        initial = await self.port.snapshot()
        self.ledger = SessionLedger(initial)
        self.port.attach_ledger(self.ledger)
        self.clock.now = 2

    async def test_baseline_fee_auth_identity_and_historical_fills(self):
        self.adapter.trades = [trade()]
        await self.start()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        current = await self.port.snapshot()
        self.assertTrue(current.authenticated)
        self.assertEqual(current.maker_fee_rate, D("0.0001"))
        self.adapter.account.l1_address = "0x" + "2" * 40
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()

    async def test_actual_fills_partial_roundtrip_duplicates_and_final_equity(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.clock.now = 3
        await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=3).maker_fill_count, 1)
        self.adapter.trades += [trade("2", side="sell", price="101", size="0.4"),
                                trade("3", side="sell", price="101", size="0.6")]
        self.adapter.position.position = "0"
        self.adapter.account.collateral = "100.9799"
        final = await self.port.snapshot()
        report = self.ledger.finalize(final, now=3)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, D("0.9799"))
        self.assertEqual(report.maker_fill_count, 3)

    async def test_taker_ioc_is_attributed_and_all_fees_ingested_before_snapshot(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.flatten["11"] = "exit-1"
        self.clock.now = 3
        self.adapter.trades += [trade("2", side="sell", price="99", role="taker", order="11")]
        self.adapter.position.position = "0"
        self.adapter.account.collateral = "98.9603"
        final = await self.port.snapshot()
        report = self.ledger.finalize(final, now=3)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, D("-1.0397"))
        self.assertEqual(report.taker_fee, D("0.0297"))

    async def test_funding_is_realized_signed_cashflow_not_discount(self):
        await self.start()
        self.adapter.fees["fundings"] = ({"id": "5", "timestamp": 1000, "change": D("-0.005")},)
        self.adapter.account.collateral = "99.995"
        account = await self.port.snapshot()
        await self.port.snapshot()
        report = self.ledger.finalize(account, now=2)
        self.assertEqual(report.funding, D("-0.005"))
        self.assertEqual(report.all_in_net_pnl, D("-0.005"))

    async def test_unsupported_account_states_fail_closed(self):
        mutations = [lambda: setattr(self.adapter.account, "account_trading_mode", 1),
                     lambda: setattr(self.adapter.position, "initial_margin_fraction", "33.33"),
                     lambda: setattr(self.adapter.position, "margin_mode", 1),
                     lambda: setattr(self.adapter.account, "pending_order_count", 1),
                     lambda: setattr(self.adapter.account, "positions", []),
                     lambda: setattr(self.adapter.position, "position", "1")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                await self.asyncSetUp()
                mutation()
                with self.assertRaises(LighterReadError):
                    await self.port.snapshot()

    async def test_unknown_order_and_fill_rejected_before_ledger(self):
        await self.start()
        self.adapter.trades = [trade(order="unknown")]
        self.adapter.position.position = "1"
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        self.adapter.trades = []
        self.adapter.orders = [NS(symbol="ETH", id="10")]
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()

    async def test_exact_authenticated_working_order_snapshot(self):
        await self.start()
        self.adapter.orders = [NS(symbol="BTC", id="10", side="buy", status="open",
            remaining=D("0.4"), price=D("99"), raw_data={"order_info": NS(reduce_only=False)})]
        self.adapter.account.total_order_count = self.adapter.position.open_order_count = 1
        await self.port.snapshot()
        self.assertEqual(self.port.latest_orders, (WorkingOrder("10", Side.BUY, D("0.4"), D("99")),))

    async def test_changing_trade_window_or_bad_actual_fee_is_rejected(self):
        await self.start()
        self.adapter.get_account_trades = AsyncMock(side_effect=[[], [trade()]])
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.adapter.get_account_trades = AsyncMock(return_value=[trade()])
        self.adapter.get_account_trades.return_value[0].fee["cost"] = D("0")
        self.adapter.position.position = "1"
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)

    async def test_unattributed_cashflow_does_not_recredit_fills_or_break_idempotency(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "109.99"
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.clock.now = 3
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        report = self.ledger.snapshot(now=3)
        self.assertEqual(report.maker_fill_count, 1)
        self.assertFalse(report.failed)
        self.assertEqual(report.external_transfers, D("0"))

    async def test_lost_history_window_and_old_new_id_fail_closed(self):
        self.adapter.trades = [trade("101")]
        await self.start()
        self.adapter.trades = [trade(str(i)) for i in range(102, 202)]
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.adapter.trades = [trade("100")]
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()

    async def test_terminal_fill_proof_blocks_fully_lagging_flat_rest(self):
        await self.start()
        self.terminal.add("10")
        self.adapter.get_order = AsyncMock(return_value=NS(id="10", symbol="BTC",
            status="filled", amount=D("1"), filled=D("1")))
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        calls = self.adapter.get_order.await_count
        self.clock.now = 3
        await self.port.snapshot()
        self.assertEqual(self.adapter.get_order.await_count, calls)

    async def test_terminal_cancel_partial_amount_and_exact_identity_required(self):
        await self.start()
        self.terminal.add("10")
        self.adapter.get_order = AsyncMock(return_value=NS(id="10", symbol="BTC",
            status="canceled", amount=D("1"), filled=D("0.4")))
        self.adapter.trades = [trade(size="0.4")]
        self.adapter.position.position = "0.4"
        self.adapter.account.collateral = "99.996"
        await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.terminal.add("11")
        self.adapter.get_order.return_value = NS(id="foreign", symbol="BTC", status="canceled",
                                               amount=D("1"), filled=D("0"))
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()


class LighterMarketTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_duplicate_price_own_exclusion_and_stale_failure(self):
        adapter, clock = Adapter(), Clock()
        own = (WorkingOrder("10", Side.BUY, D("5"), D("99")),)
        market = LighterMarketData(adapter, "BTC", clock, working_orders=lambda: own)
        adapter.book.bids += [NS(price=D("99"), size=D("2"))]
        await market.initialize()
        result = await market.refresh()
        self.assertEqual(result.external_bid, D("99"))
        self.assertEqual(result.microprice, D("99") + D("2") * D("2") / D("7"))
        self.assertEqual(market.min_quote_amount, D("5"))
        clock.now = 5
        with self.assertRaises(LighterReadError):
            market.snapshot()
        adapter.book = None
        with self.assertRaises(LighterReadError):
            await market.refresh()
        with self.assertRaises(LighterReadError):
            market.snapshot()


class LighterReadBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_signer_public_read_bridge_fee_ticks_and_funding(self):
        from core.adapters.exchanges.adapters.lighter import LighterAdapter
        adapter = LighterAdapter.__new__(LighterAdapter)
        async def call_api(name, operation):
            return await operation()
        adapter._rest = NS(signer_client=NS(create_auth_token_with_expiry=Mock(return_value=("secret-token", None))),
            api_key_index=2, account_index=7, get_market_index=lambda symbol: 1,
            _call_api=call_api, _require_success_response=lambda *args: None,
            account_api=NS(account_limits=AsyncMock(return_value=NS(current_maker_fee_tick=40,
                current_taker_fee_tick=280)), position_funding=AsyncMock(return_value=NS(position_fundings=[
                    NS(funding_id=9, timestamp=1000, market_id=1, change="-0.1", discount="0.05")]))))
        adapter._normalize_symbol = lambda symbol: symbol
        result = await adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(result["maker_fee_rate"], D("0.00004"))
        self.assertEqual(result["fundings"], ({"id": "9", "timestamp": 1000, "change": D("-0.1")},))
        self.assertNotIn("secret-token", repr(result))
        adapter._rest.account_api.account_limits.side_effect = RuntimeError("secret-token")
        with self.assertRaisesRegex(RuntimeError, "^authenticated fee/funding read unavailable$"):
            await adapter.get_account_fee_and_funding("BTC")


if __name__ == "__main__":
    unittest.main()
