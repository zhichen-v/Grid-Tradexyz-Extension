"""Offline public-contract checks: no adapter construction, signer or network."""

import unittest
from copy import deepcopy
from decimal import Decimal as D, ROUND_DOWN
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
        self.position = NS(symbol="BTC", market_id=1, allocated_margin="0", position="0", sign=1, avg_entry_price="100",
                           unrealized_pnl="0", margin_mode=0, initial_margin_fraction="100",
                           pending_order_count=0, open_order_count=0)
        self.account = NS(account_index=7, index=7, l1_address=ADDRESS, collateral="100",
                          account_trading_mode=0, shares=[], assets=[], pending_order_count=0,
                          total_order_count=0, positions=[self.position])
        self.book = NS(symbol="BTC", bids=[NS(price=D("99"), size=D("5"))],
                       asks=[NS(price=D("101"), size=D("5"))])
        self.metadata = NS(symbols=[dict(symbol="BTC", status="active", price_decimals=0,
                          size_decimals=1, min_base_amount="0.1", min_quote_amount="5")])
        self.settlement = dict(symbol="USDG", asset_id=3, decimals=6,
                               index_price=D("1"), loan_to_value=D("1"))

    async def get_account_trades(self, symbol, limit):
        return self.trades[:]

    async def get_account_fee_and_funding(self, symbol, limit):
        return self.fees.copy()

    async def get_open_orders(self, symbol=None):
        return self.orders[:]

    async def get_balances(self):
        return [NS(currency="USDG", total=D(self.account.collateral),
                   raw_data={"account": self.account})]

    def unified(self, cash="100.00000056076"):
        self.account.account_trading_mode = 1
        self.account.pending_unlocks = []
        self.account.total_isolated_order_count = 0
        self.position.allocated_margin = "0"
        self.account.assets = [NS(symbol="USDG", asset_id=3, balance="0", locked_balance="0",
                                  margin_balance=cash, margin_mode="enabled")]
        self.settlement = dict(symbol="USDG", asset_id=3, decimals=6,
                               index_price=D("1"), loan_to_value=D("1"))
        self.unified_cash(cash)

    def unified_cash(self, cash):
        self.account.assets[0].margin_balance = str(cash)
        self.account.collateral = str(D(cash).quantize(D("0.000001"), rounding=ROUND_DOWN))
        equity = (D(cash) + D(self.position.unrealized_pnl)).quantize(D("0.000001"), rounding=ROUND_DOWN)
        self.account.total_asset_value = self.account.cross_asset_value = str(equity)

    async def get_settlement_asset(self):
        return self.settlement.copy()

    async def get_exchange_info(self):
        return self.metadata

    async def get_orderbook(self, symbol, limit):
        return self.book


class ReadStream:
    """Fresh protocol-shaped snapshots, not a second simulated account ledger."""
    def __init__(self, adapter, clock):
        self.adapter, self.clock = adapter, clock
        self.transport_healthy = True

    async def request_snapshot(self, channel):
        if not self.transport_healthy:
            raise RuntimeError("fixture transport unavailable")
        self.assert_channel(channel)
        account = self.adapter.account
        return deepcopy(dict(account=account.account_index, total_trades_count=len(self.adapter.trades),
            assets={str(row.asset_id): vars(row) for row in account.assets},
            positions={str(row.market_id): vars(row) for row in account.positions},
            shares=account.shares, funding_histories=list(self.adapter.fees["fundings"])))

    @staticmethod
    def assert_channel(channel):
        if channel != "account_all":
            raise ValueError("unsupported fixture channel")

    def book_snapshot(self):
        if getattr(self.adapter, "read_failure", False):
            raise RuntimeError("secret-provider-detail")
        bids = [(row.price, row.size) for row in self.adapter.book.bids]
        asks = [(row.price, row.size) for row in self.adapter.book.asks]
        for row in self.adapter.orders:
            (bids if row.side.value == "buy" else asks).append((row.price, row.remaining))
        return dict(bids=bids, asks=asks,
                    received_monotonic=self.clock.monotonic() - getattr(self.adapter, "book_age", 0))


class LighterAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter, self.clock = Adapter(), Clock()
        self.flatten = {}
        self.generation = 0
        self.terminal = set()
        self.port = LighterAccountPort(self.adapter, "BTC", self.clock,
            account_index=7, expected_l1_address=ADDRESS,
            known_order_ids=lambda: {"10", "11"}, flatten_id_for=self.flatten.get,
            terminal_order_ids=lambda: self.terminal, mutation_generation=lambda: self.generation)

    async def test_order_observations_are_shared_once_without_repeating_bookends(self):
        self.port.stream = ReadStream(self.adapter, self.clock)
        await self.start()
        self.port.begin_quote_cycle()
        self.adapter.get_open_orders = AsyncMock(wraps=self.adapter.get_open_orders)
        await self.port.read_execution_orders("BTC")
        await self.port.snapshot()
        await self.port.read_execution_orders("BTC")
        self.assertEqual(self.adapter.get_open_orders.await_count, 2)
        await self.port.read_execution_orders("BTC")
        self.assertEqual(self.adapter.get_open_orders.await_count, 3)

    async def test_mutation_expiry_new_cycle_and_failed_audit_discard_order_handoff(self):
        self.port.stream = ReadStream(self.adapter, self.clock)
        await self.start()
        self.adapter.get_open_orders = AsyncMock(wraps=self.adapter.get_open_orders)
        for cause in ("mutation", "expiry", "cycle", "failure"):
            with self.subTest(cause=cause):
                await self.port.snapshot()
                if cause == "mutation":
                    self.generation += 1
                elif cause == "expiry":
                    self.clock.now += 4
                elif cause == "cycle":
                    self.port.begin_quote_cycle()
                else:
                    original = self.adapter.account.collateral
                    self.adapter.account.collateral = "99"
                    with self.assertRaises(LighterReadError):
                        await self.port.snapshot()
                    self.adapter.account.collateral = original
                before = self.adapter.get_open_orders.await_count
                await self.port.read_execution_orders("BTC")
                self.assertEqual(self.adapter.get_open_orders.await_count, before + 1)

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

    async def test_unified_mode_is_not_reinterpreted_as_classic_collateral(self):
        self.adapter.unified()
        self.adapter.account.assets[0].margin_balance = "50"
        with self.assertRaisesRegex(LighterReadError, "Unified cash and summary mismatch"):
            await self.port.snapshot()
        # Raw backend exceptions remain redacted; only code-owned refusals survive.
        self.adapter.get_balances = AsyncMock(side_effect=RuntimeError("secret-provider-detail"))
        with self.assertRaisesRegex(LighterReadError, "^authenticated account audit unavailable$"):
            await self.port.snapshot()

    async def test_unified_full_precision_cash_actual_fees_funding_and_equity(self):
        self.adapter.unified()
        await self.start()
        baseline = D("100.00000056076")
        self.assertEqual(self.port._baseline.equity, baseline)
        self.adapter.trades = [trade(price="100.0001", size="0.01")]
        self.adapter.position.position = "0.01"
        self.adapter.position.unrealized_pnl = "-0.000003"
        cash = baseline - self.adapter.trades[0].fee["cost"]
        self.adapter.unified_cash(cash)
        current = await self.port.snapshot()
        self.assertEqual(current.equity, cash - D("0.000003"))
        self.clock.now = 3
        self.flatten["11"] = "exit-1"
        self.adapter.trades += [trade("2", side="sell", price="100.1001", size="0.01", role="taker", order="11")]
        self.adapter.fees["fundings"] = ({"id": "9", "timestamp": 2100, "change": D("-0.00000001")},)
        self.adapter.position.position = self.adapter.position.unrealized_pnl = "0"
        cash += D("0.001") - self.adapter.trades[1].fee["cost"] - D("0.00000001")
        self.adapter.unified_cash(cash)
        final = await self.port.snapshot()
        await self.port.snapshot()
        report = self.ledger.finalize(final, now=3)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, cash - baseline)
        self.assertEqual(report.maker_fill_count, 1)
        self.assertEqual(report.taker_fill_count, 1)

    async def test_unified_sub_quantum_unattributed_cash_is_not_tolerated(self):
        self.adapter.unified()
        await self.start()
        self.adapter.unified_cash("100.00000056077")
        with self.assertRaisesRegex(LighterReadError, "unattributed account cashflow"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).external_transfers, D("0"))

    async def test_unified_precision_loss_cannot_hide_cash_changes(self):
        for cash in ("100.000000560760000000000000001", "100.000000560760000000000000009"):
            await self.asyncSetUp()
            self.adapter.unified(cash)
            with self.assertRaises(LighterReadError):
                await self.port.snapshot()

    async def test_unified_exclusivity_metadata_summaries_and_mode_fail_closed(self):
        cases = [("asset", "symbol", "USDC"), ("asset", "asset_id", 4),
                 ("asset", "asset_id", True), ("asset", "balance", "0.1"),
                 ("asset", "locked_balance", "0.1"), ("asset", "margin_mode", "disabled"),
                 ("asset", "margin_balance", "NaN"), ("asset", "margin_balance", "-1"),
                 ("account", "assets", []), ("account", "pending_unlocks", [NS()]),
                 ("account", "shares", [NS()]), ("account", "total_isolated_order_count", 1),
                 ("account", "total_asset_value", "100.000001"),
                 ("account", "cross_asset_value", "100.000001"),
                 ("account", "collateral", "100.000001"),
                 ("position", "allocated_margin", "0.1"), ("position", "unrealized_pnl", "0.1")]
        for target, key, value in cases:
            with self.subTest(target=target, key=key):
                await self.asyncSetUp()
                self.adapter.unified()
                obj = self.adapter.account.assets[0] if target == "asset" else getattr(self.adapter, target)
                setattr(obj, key, value)
                with self.assertRaises(LighterReadError):
                    await self.port.snapshot()
        for key, value in (("decimals", 5), ("index_price", D("0.99")), ("loan_to_value", D("0.9"))):
            await self.asyncSetUp()
            self.adapter.unified()
            await self.start()
            self.adapter.settlement[key] = value
            with self.assertRaises(LighterReadError):
                await self.port.snapshot()
        await self.asyncSetUp()
        self.adapter.unified()
        await self.start()
        self.adapter.account.account_trading_mode = 0
        with self.assertRaisesRegex(LighterReadError, "changed account trading mode"):
            await self.port.snapshot()

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
        self.adapter.get_order_history = AsyncMock(return_value=[NS(id="10", symbol="BTC",
            status="filled", amount=D("1"), filled=D("1"))])
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        calls = self.adapter.get_order_history.await_count
        self.clock.now = 3
        await self.port.snapshot()
        self.assertEqual(self.adapter.get_order_history.await_count, calls)

    async def test_terminal_cancel_partial_amount_and_exact_identity_required(self):
        await self.start()
        self.terminal.add("10")
        self.adapter.get_order_history = AsyncMock(return_value=[NS(id="10", symbol="BTC",
            status="canceled", amount=D("1"), filled=D("0.4"))])
        self.adapter.trades = [trade(size="0.4")]
        self.adapter.position.position = "0.4"
        self.adapter.account.collateral = "99.996"
        await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.terminal.add("11")
        self.adapter.get_order_history.return_value = [NS(id="foreign", symbol="BTC", status="canceled",
                                                        amount=D("1"), filled=D("0"))]
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()

    async def test_two_terminal_orders_share_one_exact_history_window(self):
        await self.start()
        self.terminal.update({"10", "11"})
        rows = [NS(id=identifier, symbol="BTC", status="canceled", amount=D("1"), filled=D("0"))
                for identifier in ("10", "11")]
        self.adapter.get_order_history = AsyncMock(return_value=[rows[0], rows[0]])
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        self.adapter.get_order_history.reset_mock()
        self.adapter.get_order_history.return_value = rows
        await self.port.snapshot()
        self.adapter.get_order_history.assert_awaited_once_with("BTC", limit=100)
        await self.port.snapshot()
        self.adapter.get_order_history.assert_awaited_once()


class LighterStreamAccountTests(unittest.IsolatedAsyncioTestCase):
    """Run the exact ledger contract against the stream bracket as well as REST."""

    async def asyncSetUp(self):
        await LighterAccountTests.asyncSetUp(self)
        self.port.stream = ReadStream(self.adapter, self.clock)

    start = LighterAccountTests.start
    test_actual_fills = LighterAccountTests.test_actual_fills_partial_roundtrip_duplicates_and_final_equity
    test_unified_fees_and_funding = LighterAccountTests.test_unified_full_precision_cash_actual_fees_funding_and_equity
    test_terminal_fill_proof = LighterAccountTests.test_terminal_fill_proof_blocks_fully_lagging_flat_rest
    test_unknown_orders_and_fills = LighterAccountTests.test_unknown_order_and_fill_rejected_before_ledger

    async def test_cached_inputs_keep_source_time_and_reduce_read_weight(self):
        self.adapter.unified()
        for name in ("get_balances", "get_account_trades", "get_account_fee_and_funding", "get_settlement_asset"):
            setattr(self.adapter, name, AsyncMock(wraps=getattr(self.adapter, name)))
        await self.start()
        second = await self.port.snapshot()
        self.assertEqual((second.observed_monotonic, second.inputs_observed_monotonic), (2, 1))
        self.clock.now = 9
        third = await self.port.snapshot()
        self.assertEqual(third.inputs_observed_monotonic, 9)
        self.assertEqual(self.adapter.get_balances.await_count, 3)
        self.assertEqual(self.adapter.get_account_trades.await_count, 1)
        self.assertEqual(self.adapter.get_account_fee_and_funding.await_count, 2)
        self.assertEqual(self.adapter.get_settlement_asset.await_count, 2)
        self.assertFalse(second.fresh(12))

    async def test_counter_mismatch_does_not_advance_checkpoint_or_ledger(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.get_account_trades = AsyncMock(return_value=[])
        with self.assertRaisesRegex(LighterReadError, "account trade count and history disagree"):
            await self.port.snapshot()
        self.assertEqual(self.port._stream_count, 0)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        self.assertEqual(self.port._fees_at, float("-inf"))

    async def test_activity_or_position_change_inside_bracket_is_rejected(self):
        await self.start()
        original = self.port.stream.request_snapshot
        async def changed(channel):
            row = await original(channel)
            row["positions"]["1"]["position"] = "1"
            return row
        self.port.stream.request_snapshot = changed
        with self.assertRaisesRegex(LighterReadError, "account changed during stream/REST bracket"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)

    async def test_later_fill_cannot_rewrite_cash_snapshot_or_escape_counter_bound(self):
        await self.start()
        orders = self.adapter.get_open_orders
        reads = 0
        async def fill_after_cash(symbol=None):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.adapter.trades = [trade()]
                self.adapter.position.position = "1"
                self.adapter.account.collateral = "99.99"
            return await orders()
        self.adapter.get_open_orders = fill_after_cash
        with self.assertRaisesRegex(LighterReadError, "account changed during stream/REST bracket"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        self.adapter.get_open_orders = orders
        await self.port.snapshot()
        original = self.adapter.get_account_trades
        self.adapter.trades.append(trade("2", side="sell", price="101"))
        self.adapter.position.position = "0"
        self.adapter.account.collateral = "100.9799"
        async def extra_fill(*args, **kwargs):
            rows = await original(*args, **kwargs)
            return rows + [trade("3")]
        self.adapter.get_account_trades = extra_fill
        with self.assertRaisesRegex(LighterReadError, "account trade count and history disagree"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

    async def test_book_outside_order_watermarks_cannot_authorize_but_flat_proof_survives(self):
        await self.start()
        self.port.stream.order_nonce = 10
        self.port.stream.book_at_or_after = AsyncMock(return_value={"nonce": 11})
        current = await self.port.snapshot()
        self.assertTrue(current.authenticated)
        self.assertIsNone(self.port.aligned_book)
        self.port.stream.book_at_or_after.side_effect = RuntimeError("book unavailable")
        current = await self.port.snapshot()
        self.assertTrue(current.authenticated)
        self.assertEqual(current.open_order_ids, ())
        self.assertIsNone(self.port.aligned_book)

    async def test_decreasing_counter_is_rejected(self):
        self.adapter.trades = [trade()]
        await self.start()
        self.adapter.trades = []
        with self.assertRaisesRegex(LighterReadError, "invalid account activity counter"):
            await self.port.snapshot()

    async def test_failed_equity_audit_does_not_poison_trade_counter_or_double_fill(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        with self.assertRaisesRegex(LighterReadError, "unattributed account cashflow"):
            await self.port.snapshot()
        self.assertEqual(self.port._stream_count, 0)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.adapter.account.collateral = "99.99"
        self.clock.now = 3
        current = await self.port.snapshot()
        await self.port.snapshot()
        self.assertEqual(current.position, D("1"))
        self.assertEqual(self.port._stream_count, 1)
        self.assertEqual(self.ledger.snapshot(now=3).maker_fill_count, 1)


class LighterMarketTests(unittest.IsolatedAsyncioTestCase):
    async def test_aligned_packet_rechecks_source_health_before_cached_quote_reuse(self):
        adapter, clock = Adapter(), Clock()
        stream = ReadStream(adapter, clock)
        packet = stream.book_snapshot() | {"timestamp": 1000}
        stream.book_at_or_after = AsyncMock(return_value=packet)
        stream.check_book_source = Mock()
        market = LighterMarketData(adapter, "BTC", clock, aligned_book=lambda: packet)
        market.stream = stream
        await market.initialize()
        await market.refresh()
        stream.check_book_source.assert_called_once_with(1000)
        stream.check_book_source.side_effect = RuntimeError("book source became invalid")
        with self.assertRaises(LighterReadError):
            await market.refresh()
        with self.assertRaises(LighterReadError):
            market.snapshot()

    async def test_same_stream_book_can_be_read_twice_without_restamping(self):
        adapter, clock = Adapter(), Clock()
        market = LighterMarketData(adapter, "BTC", clock)
        market.stream = ReadStream(adapter, clock)
        await market.initialize()
        first = await market.refresh()
        second = await market.refresh()
        self.assertIs(second, first)
        self.assertEqual(second.observed_monotonic, 1)
        clock.now = 5
        adapter.book_age = 4
        with self.assertRaises(LighterReadError):
            await market.refresh()

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
    async def test_public_settlement_asset_precision_validation_and_sanitized_errors(self):
        from core.adapters.exchanges.adapters.lighter import LighterAdapter
        adapter = LighterAdapter.__new__(LighterAdapter)
        async def call_api(name, operation):
            return await operation()
        asset = dict(symbol="USDG", asset_id=3, decimals=6, index_price="1",
                     loan_to_value="1", margin_mode="enabled")
        api = NS(asset_details=AsyncMock(return_value=NS(asset_details=[NS(**asset)])))
        adapter._rest = NS(network="robinhood", order_api=api, _call_api=call_api,
                           _require_success_response=Mock())
        result = await adapter.get_settlement_asset()
        self.assertEqual(result, dict(symbol="USDG", asset_id=3, decimals=6,
                                      index_price=D("1"), loan_to_value=D("1")))
        api.asset_details.assert_awaited_once_with()
        adapter._rest._require_success_response.assert_called_once()
        for update in ({"symbol": "USDC"}, {"asset_id": True}, {"asset_id": -1},
                       {"decimals": True}, {"decimals": -1}, {"decimals": 19},
                       {"index_price": "0.99"}, {"index_price": "NaN"}, {"index_price": 1.0},
                       {"loan_to_value": "0.9"}, {"loan_to_value": "Infinity"},
                       {"margin_mode": "disabled"}):
            with self.subTest(update=update):
                api.asset_details.return_value = NS(asset_details=[NS(**(asset | update))])
                with self.assertRaisesRegex(RuntimeError, "^settlement asset metadata unavailable$"):
                    await adapter.get_settlement_asset()
        for rows in ([], None, [NS(**asset), NS(**asset)],
                     [NS(**asset), NS(**(asset | {"symbol": "OTHER"}))]):
            api.asset_details.return_value = NS(asset_details=rows)
            with self.assertRaises(RuntimeError):
                await adapter.get_settlement_asset()
        api.asset_details.side_effect = RuntimeError("private-upstream-details")
        with self.assertRaisesRegex(RuntimeError, "^settlement asset metadata unavailable$"):
            await adapter.get_settlement_asset()
        adapter._rest.network = "mainnet"
        api.asset_details.reset_mock()
        with self.assertRaises(RuntimeError):
            await adapter.get_settlement_asset()
        api.asset_details.assert_not_awaited()

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
