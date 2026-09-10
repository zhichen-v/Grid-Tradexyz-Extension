"""Offline public-contract checks: no adapter construction, signer or network."""

import unittest
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D, ROUND_DOWN
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, call

from core.services.market_maker_v2.domain import Side, WorkingOrder
from core.services.market_maker_v2.lighter_runtime import (
    LighterAccountPort, LighterMarketData, LighterReadError,
)
from core.services.market_maker_v2.session_ledger import SessionLedger
from core.services.market_maker_v2.api_budget import ApiBudgetUnavailable


ADDRESS = "0x" + "1" * 40


class Clock:
    now = 1.0

    def monotonic(self):
        return self.now


def trade(identifier="1", *, side="buy", price="100", size="1", role="maker", order="10", realized="0"):
    rate = D("0.0001") if role == "maker" else D("0.0003")
    amount, price = D(size), D(price)
    return NS(id=identifier, order_id=order, symbol="BTC", side=side,
              amount=amount, price=price, cost=amount * price,
              fee=dict(role=role, rate=rate, tick=int(rate * 1000000),
                       cost=amount * price * rate, currency="USDG"),
              raw_data=dict(timestamp=int(identifier) * 1000,
                            trade_sequence=int(identifier), integrator_fee_tick=0, realized_pnl=D(realized)))


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

    async def test_confirmation_handoff_serves_one_sync_and_cash_bracket_without_restamping(self):
        self.port.stream = ReadStream(self.adapter, self.clock)
        await self.start()
        self.adapter.get_open_orders = AsyncMock(wraps=self.adapter.get_open_orders)
        await self.port.read_confirmation_orders("BTC")
        await self.port.read_execution_orders("BTC")
        self.adapter.get_open_orders.assert_awaited_once()
        await self.port.snapshot()
        self.assertEqual(self.adapter.get_open_orders.await_count, 2)
        await self.port.read_execution_orders("BTC")
        self.assertEqual(self.adapter.get_open_orders.await_count, 2)
        for invalidate in ("mutation", "expiry", "cycle"):
            await self.port.read_confirmation_orders("BTC")
            before = self.adapter.get_open_orders.await_count
            if invalidate == "mutation":
                self.generation += 1
            elif invalidate == "expiry":
                self.clock.now += 3.001
            else:
                self.port.begin_quote_cycle()
            await self.port.read_execution_orders("BTC")
            self.assertEqual(self.adapter.get_open_orders.await_count, before + 1)

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
        events = []
        initial = await self.port.snapshot()
        self.ledger = SessionLedger(initial, telemetry=NS(emit=events.append))
        self.port.attach_ledger(self.ledger)
        self.clock.now = 2
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.clock.now = 3
        await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=3).maker_fill_count, 1)
        self.adapter.trades += [trade("2", side="sell", price="101", size="0.4", realized="0.4"),
                                trade("3", side="sell", price="101", size="0.6", realized="0.6")]
        self.adapter.position.position = "0"
        self.adapter.account.collateral = "100.9799"
        final = await self.port.snapshot()
        report = self.ledger.finalize(final, now=3)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, D("0.9799"))
        self.assertEqual(report.maker_fill_count, 3)
        fills = [event.fill for event in events if hasattr(event, "fill")]
        self.assertEqual([fill.source_timestamp_ms for fill in fills], [1000, 2000, 3000])
        self.assertEqual([fill.observed_monotonic for fill in fills], [2, 3, 3])

    async def test_delayed_inventory_uses_prior_cash_proof_for_risk_age_only(self):
        self.port.stream = ReadStream(self.adapter, self.clock)
        await self.start()
        self.clock.now = 5
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=5).inventory_age, D("0"))
        self.assertEqual(self.port.inventory_age_bound(5), D("4"))
        self.clock.now = 6
        await self.port.snapshot()
        self.assertEqual(self.port.inventory_age_bound(6), D("5"))
        self.clock.now = 7
        self.adapter.trades += [trade("2", side="sell", size="2")]
        self.adapter.position.sign = -1
        self.adapter.account.collateral = "99.97"
        await self.port.snapshot()
        self.assertEqual(self.port.inventory_age_bound(7), D("1"))
        self.clock.now = 8
        self.adapter.trades += [trade("3")]
        self.adapter.position.position = "0"
        self.adapter.account.collateral = "99.96"
        await self.port.snapshot()
        self.assertEqual(self.port.inventory_age_bound(8), D("0"))

    async def test_unified_mode_is_not_reinterpreted_as_classic_collateral(self):
        self.adapter.unified()
        self.adapter.account.assets[0].margin_balance = "50"
        with self.assertRaisesRegex(LighterReadError, "Unified cash and collateral mismatch"):
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
        self.adapter.trades += [trade("2", side="sell", price="100.1001", size="0.01", role="taker", order="11", realized="0.001")]
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

    async def test_scaled_short_partial_close_uses_exchange_cash_and_reconciles_exactly(self):
        baseline = D("297.931743512682")
        self.adapter.unified(str(baseline))
        self.adapter.fees.update(maker_fee_rate=D("0.00012"), taker_fee_rate=D("0.00035"))
        self.port.stream = ReadStream(self.adapter, self.clock)
        await self.start()
        cash, position = baseline, D("0")
        rows = [("sell", "0.00040", "77590.9", "0", "maker"),
                ("sell", "0.00020", "77602.1", "0", "maker"),
                ("buy", "0.00040", "77577.7", "0.006774", "maker"),
                # Synthetic closing evidence at the later manual fill's price;
                # this fixture does not attribute the real manual fill to the runner.
                ("buy", "0.00020", "77513.5", "0.016226", "taker")]
        for index, (side, size, price, realized, role) in enumerate(rows, 1):
            self.clock.now += 1
            order = "11" if role == "taker" else "10"
            if role == "taker":
                self.flatten[order] = "exit-1"
            row = trade(str(index), side=side, size=size, price=price,
                        realized=realized, role=role, order=order)
            rate = self.adapter.fees[f"{role}_fee_rate"]
            row.fee.update(rate=rate, tick=int(rate * 1000000), cost=row.cost * rate)
            self.adapter.trades.append(row)
            position += D(size) * (1 if side == "buy" else -1)
            self.adapter.position.position, self.adapter.position.sign = str(abs(position)), -1
            cash += D(realized) - row.fee["cost"]
            self.adapter.unified_cash(cash)
            current = await self.port.snapshot()
            self.assertEqual(current.equity, cash)
            self.assertEqual(current.position, position)
            if index == 3:
                report = self.ledger.snapshot(now=self.clock.now)
                self.assertEqual(report.realized_gross_pnl, D("0.006774"))
                self.assertEqual(report.realized_net_pnl, D("-0.00253654320"))
                exit_account = await self.port.snapshot(allow_unreconciled_cash=True)
                self.assertEqual(exit_account.position, D("-0.00020"))
        await self.port.snapshot()  # Immutable duplicate fills remain idempotent.
        report = self.ledger.finalize(current, now=self.clock.now)
        self.assertTrue(report.complete)
        self.assertEqual(report.all_in_net_pnl, D("0.00826351180"))
        self.assertEqual((report.maker_fill_count, report.taker_fill_count), (3, 1))

    async def test_exit_only_proof_does_not_evaluate_unreconciled_cash_arithmetic(self):
        await self.start()
        snapshot = self.ledger.snapshot
        self.ledger.snapshot = Mock(side_effect=lambda **kwargs: replace(snapshot(**kwargs),
            realized_net_pnl=D("0.006773333333333333333333333333")))
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        current = await self.port.snapshot(allow_unreconciled_cash=True)
        self.assertTrue(current.authenticated)
        self.assertEqual((current.position, current.open_order_count), (D("0"), 0))
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()  # Exit proof cannot authorize normal quotes.

    async def test_fill_realization_is_required_finite_and_immutable(self):
        for invalid in (None, "NaN", "Infinity", 0.1):
            row = trade()
            row.raw_data["realized_pnl"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(LighterReadError):
                self.port._fill(row, 2)
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.adapter.trades[0].raw_data["realized_pnl"] = D("0.000001")
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()

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
        self.adapter.trades += [trade("2", side="sell", price="99", role="taker", order="11", realized="-1")]
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
            remaining=D("0.4"), amount=D("0.4"), filled=D("0"), price=D("99"),
            raw_data={"order_info": NS(reduce_only=False)})]
        self.adapter.account.total_order_count = self.adapter.position.open_order_count = 1
        await self.port.snapshot()
        self.assertEqual(self.port.latest_orders, (WorkingOrder("10", Side.BUY, D("0.4"), D("99")),))

    async def test_partial_working_fill_cannot_hide_behind_consistently_lagging_cash_and_counter(self):
        self.port.stream = ReadStream(self.adapter, self.clock)
        await self.start()
        self.adapter.orders = [NS(symbol="BTC", id="10", side="buy", status="partially_filled",
            amount=D("1"), filled=D("0.4"), remaining=D("0.6"), price=D("100"),
            raw_data={"order_info": NS(reduce_only=False)})]
        self.adapter.account.total_order_count = self.adapter.position.open_order_count = 1
        with self.assertRaisesRegex(LighterReadError, "active order fills"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
        self.adapter.trades = [trade(size="0.4")]
        self.adapter.position.position = "0.4"
        self.adapter.account.collateral = "99.996"
        current = await self.port.snapshot()
        self.assertEqual(current.position, D("0.4"))
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.adapter.orders[0].amount = self.adapter.orders[0].remaining = D("1e28")
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()  # Rounding must not hide the inconsistent remaining amount.

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

    async def test_conditional_read_refusal_precedes_query_and_preserves_cache_age(self):
        methods = {"fees": "get_account_fee_and_funding", "settlement": "get_settlement_asset",
                   "trades": "get_account_trades", "terminal_history": "get_order_history"}
        for kind, method in methods.items():
            with self.subTest(kind=kind):
                await self.asyncSetUp()
                self.adapter.unified()
                await self.start()
                fees_at, settlement_at = self.port._fees_at, self.port._settlement_at
                fees_cache, settlement = self.port._fees_cache, self.port._settlement
                if kind in {"fees", "settlement"}:
                    self.clock.now = 9  # Expire the real eight-second metadata TTL.
                else:
                    self.adapter.trades = [trade()]
                    self.adapter.position.position = "1"
                    self.adapter.unified_cash("99.99000056076")
                if kind == "terminal_history":
                    self.terminal.add("10")
                    self.adapter.get_order_history = AsyncMock(return_value=[
                        NS(id="10", symbol="BTC", status="filled", amount=D("1"), filled=D("1"))])
                else:
                    setattr(self.adapter, method, AsyncMock(wraps=getattr(self.adapter, method)))

                def admit(value):
                    if value == kind:
                        raise ApiBudgetUnavailable("fixture conditional read denied")

                self.port.before_read = Mock(side_effect=admit)
                with self.assertRaises(ApiBudgetUnavailable):
                    await self.port.snapshot()
                getattr(self.adapter, method).assert_not_awaited()
                self.assertEqual(self.port.before_read.call_args_list.count(call(kind)), 1)
                self.assertIsNone(self.port.aligned_book)
                self.assertIsNone(self.port._cash_cache)
                self.assertEqual(self.port._stream_count, 0)
                if kind == "fees":
                    self.assertIs(self.port._fees_cache, fees_cache)
                    self.assertEqual(self.port._fees_at, fees_at)
                if kind == "settlement":
                    self.assertIs(self.port._settlement, settlement)
                    self.assertEqual(self.port._settlement_at, settlement_at)
                if kind == "terminal_history":
                    self.assertNotIn("10", self.port._terminal_proofs)
                    self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
                # A denied late history read may have accepted the fill already;
                # completing the proof must not accept that fill a second time.
                self.port.before_read = Mock()
                current = await self.port.snapshot()
                self.assertTrue(current.authenticated)
                getattr(self.adapter, method).assert_awaited_once()
                self.assertEqual(self.ledger.snapshot(now=self.clock.now).maker_fill_count,
                                 int(kind in {"trades", "terminal_history"}))

    async def test_cached_reads_skip_conditional_admission_and_settlement_keeps_request_start(self):
        self.adapter.unified()
        await self.start()
        self.port.before_read = Mock()
        await self.port.snapshot(allow_cash_reuse=True)
        for kind in ("fees", "settlement", "trades", "terminal_history"):
            self.assertNotIn(call(kind), self.port.before_read.call_args_list)
        original = self.adapter.get_settlement_asset

        async def delayed_settlement():
            self.clock.now += 0.5
            return await original()

        self.clock.now = 9
        self.adapter.get_settlement_asset = delayed_settlement
        await self.port.snapshot()
        self.assertEqual(self.port._settlement_at, 9)
        self.assertEqual(self.port.before_read.call_args_list.count(call("fees")), 1)
        self.assertEqual(self.port.before_read.call_args_list.count(call("settlement")), 1)

    async def test_forensic_double_reads_each_admit_without_claiming_stream_audit_bound(self):
        self.adapter.unified()
        await self.start()
        self.port.stream = None
        self.port.before_read = Mock()
        await self.port.snapshot()
        self.assertNotIn(call("audit"), self.port.before_read.call_args_list)
        for kind, count in (("trades", 2), ("fees", 2), ("settlement", 1)):
            self.assertEqual(self.port.before_read.call_args_list.count(call(kind)), count)

    async def test_cached_inputs_keep_source_time_and_reduce_read_weight(self):
        self.adapter.unified()
        for name in ("get_balances", "get_account_trades", "get_account_fee_and_funding", "get_settlement_asset"):
            setattr(self.adapter, name, AsyncMock(wraps=getattr(self.adapter, name)))
        await self.start()
        second = await self.port.snapshot()
        self.assertEqual((second.observed_monotonic, second.inputs_observed_monotonic,
                          second.terms_observed_monotonic), (2, 2, 1))
        self.clock.now = 9
        third = await self.port.snapshot()
        self.assertEqual(third.inputs_observed_monotonic, 9)
        self.assertEqual(self.adapter.get_balances.await_count, 3)
        self.assertEqual(self.adapter.get_account_trades.await_count, 1)
        self.assertEqual(self.adapter.get_account_fee_and_funding.await_count, 2)
        self.assertEqual(self.adapter.get_settlement_asset.await_count, 2)
        self.assertFalse(second.fresh(12.001))

    async def test_normal_terms_cache_has_separate_age_without_extending_cash_freshness(self):
        for name in ("get_balances", "get_account_fee_and_funding", "get_settlement_asset"):
            setattr(self.adapter, name, AsyncMock(wraps=getattr(self.adapter, name)))
        await self.start()
        self.clock.now = 20
        current = await self.port.snapshot(allow_metadata_cache=True)
        self.assertEqual((current.observed_monotonic, current.inputs_observed_monotonic,
                          current.terms_observed_monotonic), (20, 20, 1))
        self.assertTrue(current.fresh(20))
        self.assertFalse(current.fresh(30.001))  # Fresh terms cannot renew cash/order truth.
        self.adapter.get_account_fee_and_funding.assert_awaited_once()
        self.adapter.get_settlement_asset.assert_awaited_once()
        self.assertEqual(self.adapter.get_balances.await_count, 2)
        self.clock.now = 28.999
        last_cached = await self.port.snapshot(allow_metadata_cache=True)
        self.assertEqual(last_cached.terms_observed_monotonic, 1)
        self.assertFalse(last_cached.fresh(31.001))  # Fresh cash cannot renew terms.
        self.clock.now = 29  # Refresh before the 30-second proof limit.
        refreshed = await self.port.snapshot(allow_metadata_cache=True)
        self.assertEqual(refreshed.terms_observed_monotonic, 29)
        self.assertEqual(self.adapter.get_account_fee_and_funding.await_count, 2)
        self.assertEqual(self.adapter.get_settlement_asset.await_count, 2)

    async def test_exit_and_replaced_transport_do_not_inherit_normal_terms_ttl(self):
        for boundary in ("exit", "new_stream"):
            with self.subTest(boundary=boundary):
                await self.asyncSetUp()
                await self.start()
                self.adapter.get_account_fee_and_funding = AsyncMock(wraps=self.adapter.get_account_fee_and_funding)
                self.adapter.get_settlement_asset = AsyncMock(wraps=self.adapter.get_settlement_asset)
                self.clock.now = 12
                cached = await self.port.snapshot(allow_metadata_cache=True)
                self.assertEqual(cached.terms_observed_monotonic, 1)
                if boundary == "new_stream":
                    self.port.stream = ReadStream(self.adapter, self.clock)
                current = await self.port.snapshot(allow_metadata_cache=boundary == "new_stream")
                self.assertEqual(current.terms_observed_monotonic, 12)
                self.adapter.get_account_fee_and_funding.assert_awaited_once()
                self.adapter.get_settlement_asset.assert_awaited_once()

    async def test_new_fill_fee_increase_refreshes_terms_within_original_audit(self):
        await self.start()
        self.adapter.fees["maker_fee_rate"] = D("0.0002")
        fill = trade()
        fill.fee.update(rate=D("0.0002"), tick=200, cost=D("0.02"))
        self.adapter.trades = [fill]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.98"
        self.adapter.get_account_fee_and_funding = AsyncMock(wraps=self.adapter.get_account_fee_and_funding)
        self.port.before_read = Mock()
        current = await self.port.snapshot(allow_metadata_cache=True)
        self.assertEqual(current.maker_fee_rate, D("0.0002"))
        self.assertEqual(self.ledger.snapshot(now=self.clock.now).maker_fee, D("0.02"))
        self.adapter.get_account_fee_and_funding.assert_awaited_once()
        self.assertIn(call("funding_refresh"), self.port.before_read.call_args_list)
        self.assertIn(call("retry"), self.port.before_read.call_args_list)

    async def test_fresh_fee_discount_does_not_reject_earlier_fill_or_exit_proof(self):
        for normal in (False, True):
            with self.subTest(normal=normal):
                await self.asyncSetUp()
                await self.start()
                fill = trade()
                fill.fee.update(rate=D("0.00012"), tick=120, cost=D("0.012"))
                self.adapter.trades = [fill]
                self.adapter.position.position = "1"
                self.adapter.account.collateral = "99.988"
                self.adapter.get_account_fee_and_funding = AsyncMock(wraps=self.adapter.get_account_fee_and_funding)
                current = await self.port.snapshot(allow_metadata_cache=normal,
                                                   allow_unreconciled_cash=not normal)
                self.assertEqual((current.position, current.maker_fee_rate), (D("1"), D("0.0001")))
                self.assertEqual(self.ledger.snapshot(now=2).maker_fee, D("0.012"))
                self.adapter.get_account_fee_and_funding.assert_awaited_once()

    async def test_cash_reuse_requires_new_full_proofs_and_keeps_original_eight_second_age(self):
        self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
        self.port.stream.request_snapshot = AsyncMock(wraps=self.port.stream.request_snapshot)
        await self.start()
        self.port.begin_quote_cycle()
        current = await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(current.observed_monotonic, 1)
        self.assertEqual(self.adapter.get_balances.await_count, 1)
        self.assertEqual(self.port.stream.request_snapshot.await_count, 2)
        self.clock.now = 8.999
        current = await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(current.observed_monotonic, 1)
        self.assertEqual(self.adapter.get_balances.await_count, 1)
        self.clock.now = 9
        current = await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(current.observed_monotonic, 9)
        self.assertEqual(self.adapter.get_balances.await_count, 2)
        self.clock.now = 10
        current = await self.port.snapshot()  # Exit/post-mutation/final callers default fresh.
        self.assertEqual(current.observed_monotonic, 10)
        self.assertEqual(self.adapter.get_balances.await_count, 3)

    async def test_cached_cash_cannot_hide_roundtrip_activity_or_unrealized_change(self):
        await self.start()
        self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
        self.adapter.trades = [trade(), trade("2", side="sell", order="11")]
        self.adapter.account.collateral = "99.98"
        current = await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(current.observed_monotonic, 2)
        self.adapter.get_balances.assert_awaited_once()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 2)
        self.clock.now = 3
        self.adapter.trades.append(trade("3"))
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.97"
        await self.port.snapshot()
        self.clock.now = 4
        self.adapter.position.unrealized_pnl = "0.5"
        before = self.adapter.get_balances.await_count
        current = await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(current.equity, D("100.47"))
        self.assertEqual(current.observed_monotonic, 4)
        self.assertEqual(self.adapter.get_balances.await_count, before + 1)

    async def test_unified_nonflat_reuses_cash_not_valuation_time_and_keeps_fresh_price_stop(self):
        from core.services.market_maker_v2.domain import ExecutionHealth, ExecutionSnapshot, MarketStateSnapshot, StrategyState
        from core.services.market_maker_v2.inventory_governor import InventoryGovernor
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                await self.asyncSetUp()
                self.adapter.unified("100")
                await self.start()
                self.adapter.trades = [trade(side=side)]
                self.adapter.position.position, self.adapter.position.sign = "1", 1 if side == "buy" else -1
                self.adapter.position.unrealized_pnl = "1"
                self.adapter.unified_cash("99.99")
                original = await self.port.snapshot()
                self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
                self.clock.now = 3
                self.adapter.position.unrealized_pnl = "-5"
                self.adapter.unified_cash("99.99")
                current = await self.port.snapshot(allow_cash_reuse=True)
                self.assertEqual((current.equity, current.unrealized_pnl, current.observed_monotonic),
                                 (original.equity, D("1"), 2))
                self.adapter.get_balances.assert_not_awaited()
                governor = InventoryGovernor(order_size=D("1"), soft_limit=D("1"), hard_limit=D("2"),
                    stop_loss_usdg=D("1"), max_hold_seconds=60, cooldown_seconds=5,
                    max_session_loss_usdg=D("10"), session_started_monotonic=1,
                    session_deadline_monotonic=100, ioc_slippage_ticks=2)
                bid = D("95") if side == "buy" else D("105")
                market = MarketStateSnapshot("BTC", 3, bid, bid + D("0.1"),
                    D("0.1"), D("0.1"), D("0.1"), True)
                execution = ExecutionSnapshot(ExecutionHealth.HEALTHY, 0, False, "BTC", 3, ())
                risk = governor.evaluate(market, current, self.ledger.snapshot(now=3), execution, now=3)
                self.assertIs(risk.state, StrategyState.FLATTENING)
                self.clock.now = 10  # Exactly eight seconds: fresh REST is mandatory.
                current = await self.port.snapshot(allow_cash_reuse=True)
                self.assertEqual((current.observed_monotonic, current.unrealized_pnl), (10, D("-5")))
                self.adapter.get_balances.assert_awaited_once()
                await self.port.snapshot()  # Exit/final defaults do not reuse even this new proof.
                self.assertEqual(self.adapter.get_balances.await_count, 2)

    async def test_unified_nonflat_cash_and_position_core_changes_invalidate_cache(self):
        for change in ("cash", "entry", "sign", "margin", "count"):
            with self.subTest(change=change):
                await self.asyncSetUp()
                self.adapter.unified("100")
                await self.start()
                self.adapter.trades = [trade()]
                self.adapter.position.position = "1"
                self.adapter.unified_cash("99.99")
                await self.port.snapshot()
                self.clock.now = 3
                self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
                if change == "cash":
                    self.adapter.unified_cash("99.99000000001")
                elif change == "entry":
                    self.adapter.position.avg_entry_price = "101"
                elif change == "sign":
                    self.adapter.position.sign = -1
                elif change == "margin":
                    self.adapter.position.initial_margin_fraction = "200"
                else:
                    self.adapter.trades += [trade("2", side="sell", order="11"), trade("3")]
                    self.adapter.unified_cash("99.97")
                if change in {"entry", "count"}:
                    current = await self.port.snapshot(allow_cash_reuse=True)
                    self.assertEqual(current.observed_monotonic, 3)
                else:
                    with self.assertRaises(LighterReadError):
                        await self.port.snapshot(allow_cash_reuse=True)
                self.adapter.get_balances.assert_awaited_once()
                self.assertEqual(self.ledger.snapshot(now=3).external_transfers, D("0"))

    async def test_generation_error_and_missing_generation_prevent_cash_reuse(self):
        await self.start()
        self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
        self.generation += 1
        await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(self.adapter.get_balances.await_count, 1)
        self.adapter.orders = [NS(symbol="ETH", id="10")]
        with self.assertRaises(LighterReadError):
            await self.port.snapshot(allow_cash_reuse=True)
        self.adapter.orders = []
        await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(self.adapter.get_balances.await_count, 2)
        self.generation = None
        await self.port.snapshot(allow_cash_reuse=True)
        await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(self.adapter.get_balances.await_count, 4)

    async def test_visible_partial_fill_reads_fresh_cash_without_extra_stream_audit(self):
        await self.start()
        self.adapter.orders = [NS(symbol="BTC", id="10", side="buy", status="open",
            amount=D("1"), remaining=D("1"), filled=D("0"), price=D("100"),
            raw_data={"order_info": NS(reduce_only=False)})]
        self.adapter.account.total_order_count = self.adapter.position.open_order_count = 1
        await self.port.snapshot()
        self.clock.now = 3
        self.adapter.orders[0].remaining = D("0.6")
        self.adapter.orders[0].filled = D("0.4")
        self.adapter.orders[0].status = "partially_filled"
        self.adapter.trades = [trade(size="0.4")]
        self.adapter.position.position = "0.4"
        self.adapter.account.collateral = "99.996"
        self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
        self.port.stream.request_snapshot = AsyncMock(wraps=self.port.stream.request_snapshot)
        current = await self.port.snapshot(allow_cash_reuse=True)
        self.assertEqual(current.position, D("0.4"))
        self.assertEqual(current.observed_monotonic, 3)
        self.adapter.get_balances.assert_awaited_once()
        self.port.stream.request_snapshot.assert_awaited_once()

    async def test_settled_funding_with_stale_stream_source_refreshes_once_at_full_precision(self):
        self.adapter.unified("100.00000056076")
        await self.start()
        original_snapshot = self.port.stream.request_snapshot
        async def unchanged(channel):
            result = await original_snapshot(channel)
            result["funding_histories"] = {}
            return result
        self.port.stream.request_snapshot = unchanged
        await self.port.snapshot()  # Establish the unchanged source before payment.
        amount = D("-0.0003840091200")
        self.adapter.fees["fundings"] = ({"id": "67904", "timestamp": 1788692400000, "change": amount},)
        self.adapter.unified_cash(D("100.00000056076") + amount)
        self.adapter.get_account_fee_and_funding = AsyncMock(wraps=self.adapter.get_account_fee_and_funding)
        self.port.before_read = Mock()
        current = await self.port.snapshot()
        self.assertEqual(current.equity, D("100.00000056076") + amount)
        self.assertEqual(self.ledger.snapshot(now=2).funding, amount)
        self.adapter.get_account_fee_and_funding.assert_awaited_once()
        self.assertEqual(self.port.before_read.call_args_list.count(call("retry")), 1)
        await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).funding, amount)

    async def test_exit_cash_gap_does_not_change_ledger_or_relax_position_proof(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.adapter.account.collateral = "99.98999999999"
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()
        exited = await self.port.snapshot(allow_unreconciled_cash=True)
        self.assertEqual((exited.position, exited.equity), (D("1"), D("99.98999999999")))
        self.assertEqual(self.ledger.snapshot(now=2).realized_net_pnl, D("-0.01"))
        self.assertEqual(self.ledger.snapshot(now=2).funding, D("0"))
        with self.assertRaises(LighterReadError):
            await self.port.snapshot()  # An exit observation cannot authorize new risk.
        self.adapter.position.position = "1.1"
        with self.assertRaises(LighterReadError):
            await self.port.snapshot(allow_unreconciled_cash=True)

    async def test_changed_funding_fee_cache_is_shared_across_one_history_retry(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.985"
        self.adapter.fees["fundings"] = ({"id": "5", "timestamp": 1000, "change": D("-0.005")},)
        self.adapter.get_account_fee_and_funding = AsyncMock(wraps=self.adapter.get_account_fee_and_funding)
        self.adapter.get_account_trades = AsyncMock(side_effect=[[], [trade()]])
        await self.port.snapshot()
        self.adapter.get_account_fee_and_funding.assert_awaited_once()
        self.assertEqual(self.ledger.snapshot(now=2).funding, D("-0.005"))

    async def test_exact_fill_and_cash_precede_counter_then_counter_catches_up(self):
        self.adapter.unified("100.00000056076")
        await self.start()
        stream_snapshot = self.port.stream.request_snapshot
        counter = 0

        async def delayed_counter(channel):
            snapshot = await stream_snapshot(channel)
            snapshot["total_trades_count"] = counter
            return snapshot

        self.port.stream.request_snapshot = delayed_counter
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.unified_cash("99.99000056076")
        self.adapter.get_account_trades = AsyncMock(wraps=self.adapter.get_account_trades)
        self.port.before_read = Mock()

        current = await self.port.snapshot()

        self.assertEqual(current.position, D("1"))
        self.assertEqual(current.equity, D("99.99000056076"))
        self.adapter.get_account_trades.assert_awaited_once()
        self.assertNotIn(call("retry"), self.port.before_read.call_args_list)
        self.assertEqual(self.port._stream_count, 0)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        counter = 1
        await self.port.snapshot()
        await self.port.snapshot()
        self.assertEqual(self.port._stream_count, 1)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.assertEqual(self.ledger.snapshot(now=2).ledger_position, D("1"))
        self.assertEqual(self.adapter.get_account_trades.await_count, 2)

    async def test_unchanged_counter_cannot_skip_history_on_bounded_retry(self):
        for arrives in (True, False):
            with self.subTest(arrives=arrives):
                await self.asyncSetUp()
                await self.start()
                stream_snapshot = self.port.stream.request_snapshot

                async def delayed_counter(channel):
                    snapshot = await stream_snapshot(channel)
                    snapshot["total_trades_count"] = 0
                    return snapshot

                self.port.stream.request_snapshot = delayed_counter
                self.adapter.trades = [trade()]
                self.adapter.position.position = "1"
                self.adapter.account.collateral = "99.99"
                self.adapter.get_account_trades = AsyncMock(side_effect=[[], [trade()] if arrives else []])
                self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
                self.port.before_read = Mock()

                if arrives:
                    current = await self.port.snapshot()
                    self.assertEqual(current.position, D("1"))
                else:
                    with self.assertRaisesRegex(LighterReadError, "account fills and position disagree"):
                        await self.port.snapshot()

                self.assertEqual(self.adapter.get_account_trades.await_count, 2)
                self.assertEqual(self.adapter.get_balances.await_count, 2)
                self.assertEqual(self.port.before_read.call_args_list.count(call("retry")), 1)
                self.assertEqual(self.port._stream_count, 0)
                self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, int(arrives))

    async def test_mark_pnl_and_order_counts_alone_do_not_refresh_trade_history(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        await self.port.snapshot()
        self.adapter.get_account_trades = AsyncMock(wraps=self.adapter.get_account_trades)
        self.adapter.position.unrealized_pnl = "0.5"

        self.port.before_read = Mock()
        marked = await self.port.snapshot(allow_cash_reuse=True)

        self.assertEqual(marked.equity, D("100.49"))
        self.adapter.get_account_trades.assert_not_awaited()
        self.assertNotIn(call("retry"), self.port.before_read.call_args_list)
        await self.port.snapshot(allow_cash_reuse=True)
        await self.port.snapshot()
        self.adapter.get_account_trades.assert_not_awaited()
        self.adapter.orders = [NS(symbol="BTC", id="11", side="sell", status="open",
            amount=D("1"), remaining=D("1"), filled=D("0"), price=D("101"),
            raw_data={"order_info": NS(reduce_only=False)})]
        self.adapter.account.total_order_count = self.adapter.position.open_order_count = 1

        working = await self.port.snapshot()

        self.assertEqual(working.open_order_ids, ("11",))
        self.adapter.get_account_trades.assert_not_awaited()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

    async def test_classic_cash_roundtrip_precedes_unchanged_counter(self):
        await self.start()
        activity = self.port.stream.request_snapshot
        async def lagged(channel):
            row = await activity(channel)
            row["total_trades_count"] = 0
            return row
        self.port.stream.request_snapshot = lagged
        self.adapter.trades = [trade(), trade("2", side="sell", order="11")]
        self.adapter.account.collateral = "99.98"
        current = await self.port.snapshot()
        self.assertEqual(current.position, D("0"))
        self.assertEqual(current.equity, D("99.98"))
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 2)

    async def test_terminal_fill_ahead_of_account_and_counter_forces_one_history_read(self):
        await self.start()
        self.terminal.add("10")
        async def terminal(*args, **kwargs):
            self.adapter.trades = [trade()]
            self.adapter.position.position = "1"
            self.adapter.account.collateral = "99.99"
            return [NS(id="10", symbol="BTC", status="filled", amount=D("1"), filled=D("1"))]
        self.adapter.get_order_history = AsyncMock(side_effect=terminal)
        self.adapter.get_account_trades = AsyncMock(wraps=self.adapter.get_account_trades)
        self.port.before_read = Mock()
        current = await self.port.snapshot()
        self.assertEqual(current.position, D("1"))
        self.adapter.get_account_trades.assert_awaited_once()
        self.assertEqual(self.port.before_read.call_args_list.count(call("retry")), 1)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

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

    async def test_later_fill_retries_frozen_cash_once_and_cannot_escape_position_proof(self):
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
        current = await self.port.snapshot()
        self.assertEqual(current.position, D("1"))
        self.assertEqual(reads, 4)  # Both rejected bookends are replaced, not mixed.
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.adapter.get_open_orders = orders
        await self.port.snapshot()
        original = self.adapter.get_account_trades
        self.adapter.trades.append(trade("2", side="sell", price="101", realized="1"))
        self.adapter.position.position = "0"
        self.adapter.account.collateral = "100.9799"
        async def extra_fill(*args, **kwargs):
            rows = await original(*args, **kwargs)
            return rows + [trade("3")]
        self.adapter.get_account_trades = extra_fill
        with self.assertRaisesRegex(LighterReadError, "account fills and position disagree"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

    async def test_nonflat_valuation_summary_does_not_relax_exact_cash_or_flat_proof(self):
        self.adapter.unified()
        await self.start()
        cash = D("99.99000056076")
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.unified_cash(cash)
        # Nonflat with zero serialized PnL is still nonflat. A valuation
        # summary is not a same-mark/same-rounding cash reconciliation proof.
        self.adapter.account.total_asset_value = self.adapter.account.cross_asset_value = "99.990001"
        current = await self.port.snapshot()
        self.assertEqual(current.position, D("1"))
        self.assertEqual(current.equity, cash)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

        self.adapter.account.assets[0].margin_balance = str(cash + D("0.00000000001"))
        with self.assertRaisesRegex(LighterReadError, "unattributed account cashflow or equity mismatch"):
            await self.port.snapshot()
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

        self.adapter.trades.append(trade("2", side="sell", order="11"))
        self.adapter.position.position = "0"
        self.adapter.unified_cash(cash - D(".01"))
        self.adapter.account.total_asset_value = self.adapter.account.cross_asset_value = "99.980001"
        with self.assertRaisesRegex(LighterReadError, "Unified exclusive valuation summary mismatch"):
            await self.port.snapshot()

    async def test_one_history_lag_retries_before_ledger_ingestion(self):
        await self.start()
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        original = self.adapter.get_balances
        async def balances():
            self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
            return await original()
        self.adapter.get_balances = AsyncMock(side_effect=balances)
        self.adapter.get_account_trades = AsyncMock(side_effect=[[], [trade()]])
        current = await self.port.snapshot()
        self.assertEqual(current.position, D("1"))
        self.assertEqual(self.adapter.get_balances.await_count, 2)
        self.assertEqual(self.adapter.get_account_trades.await_count, 2)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.assertEqual(self.port._stream_count, 1)

    async def test_read_retry_keeps_one_deadline_generation_and_attempt_bound(self):
        for cause, reads in (("persistent", 2), ("deadline", 1), ("mutation", 1), ("invalid", 1),
                             ("quota_retry", 1), ("quota_audit", 0), ("quota_orders", 0)):
            with self.subTest(cause=cause):
                await self.asyncSetUp()
                await self.start()
                def admit(kind):
                    if cause == "quota_" + kind:
                        raise RuntimeError("read budget unavailable")
                self.port.before_read = admit
                original = self.port.stream.request_snapshot
                async def changed(channel):
                    row = await original(channel)
                    row["positions"]["1"]["position"] = "1"
                    if cause == "deadline":
                        self.clock.now += 10
                    elif cause == "mutation":
                        self.generation += 1
                    elif cause == "invalid":
                        row["total_trades_count"] = -1
                    return row
                self.port.stream.request_snapshot = AsyncMock(side_effect=changed)
                self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
                with self.assertRaises(LighterReadError):
                    await self.port.snapshot()
                self.assertEqual(self.adapter.get_balances.await_count, reads)
                self.assertEqual(self.ledger.snapshot(now=self.clock.now).maker_fill_count, 0)
                self.assertIsNone(self.port.aligned_book)
                self.assertEqual(self.port._stream_count, 0)

    async def test_terminal_history_lag_retries_full_audit_without_duplicate_fill(self):
        await self.start()
        self.terminal.add("10")
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        terminal = NS(id="10", symbol="BTC", status="filled", amount=D("1"), filled=D("1"))
        self.adapter.get_order_history = AsyncMock(side_effect=[[], [terminal]])
        self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
        self.adapter.get_account_trades = AsyncMock(wraps=self.adapter.get_account_trades)
        self.adapter.get_account_fee_and_funding = AsyncMock(wraps=self.adapter.get_account_fee_and_funding)
        self.port.before_read = Mock()

        current = await self.port.snapshot()

        self.assertEqual(current.position, D("1"))
        self.assertEqual(self.adapter.get_order_history.await_count, 2)
        self.assertEqual(self.adapter.get_balances.await_count, 2)
        self.assertEqual(self.adapter.get_account_trades.await_count, 2)
        self.adapter.get_account_fee_and_funding.assert_not_awaited()
        self.assertEqual(self.port.before_read.call_args_list.count(call("retry")), 1)
        self.assertEqual(self.port._stream_count, 1)
        self.assertEqual(self.port._stream_ids, frozenset({"1"}))
        await self.port.snapshot()
        report = self.ledger.snapshot(now=2)
        self.assertEqual(report.maker_fill_count, 1)
        self.assertEqual(report.ledger_position, D("1"))
        self.assertEqual(self.adapter.get_order_history.await_count, 2)

    async def test_trade_counter_ahead_of_matching_old_positions_retries_once(self):
        await self.start()
        self.adapter.trades = [trade()]
        balances = self.adapter.get_balances
        reads = 0

        async def catch_up_position():
            nonlocal reads
            reads += 1
            self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 0)
            if reads == 2:
                self.adapter.position.position = "1"
                self.adapter.account.collateral = "99.99"
            return await balances()

        self.adapter.get_balances = AsyncMock(side_effect=catch_up_position)
        self.port.before_read = Mock()
        current = await self.port.snapshot()

        self.assertEqual(current.position, D("1"))
        self.assertEqual(reads, 2)
        self.assertEqual(self.port.before_read.call_args_list.count(call("retry")), 1)
        self.assertEqual(self.port._stream_count, 1)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)

    async def test_persistent_terminal_history_lag_has_one_retry_without_checkpoint(self):
        await self.start()
        self.terminal.add("10")
        self.adapter.trades = [trade()]
        self.adapter.position.position = "1"
        self.adapter.account.collateral = "99.99"
        self.adapter.get_order_history = AsyncMock(return_value=[])
        self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
        self.port.before_read = Mock()

        with self.assertRaisesRegex(LighterReadError, "exact terminal order proof unavailable"):
            await self.port.snapshot()

        self.assertEqual(self.adapter.get_order_history.await_count, 2)
        self.assertEqual(self.adapter.get_balances.await_count, 2)
        self.assertEqual(self.port.before_read.call_args_list.count(call("retry")), 1)
        self.assertEqual(self.port._stream_count, 0)
        self.assertEqual(self.port._stream_ids, frozenset())
        self.assertIsNone(self.port.aligned_book)
        self.assertEqual(self.ledger.snapshot(now=2).maker_fill_count, 1)
        self.assertEqual(self.ledger.snapshot(now=2).ledger_position, D("1"))

    async def test_terminal_history_retry_preserves_original_deadline_and_generation(self):
        for cause in ("deadline", "generation"):
            with self.subTest(cause=cause):
                await self.asyncSetUp()
                await self.start()
                self.terminal.add("10")
                self.adapter.trades = [trade()]
                self.adapter.position.position = "1"
                self.adapter.account.collateral = "99.99"

                async def late_history(*args, **kwargs):
                    if cause == "deadline":
                        self.clock.now += 10
                    else:
                        self.generation += 1
                    return []

                self.adapter.get_order_history = AsyncMock(side_effect=late_history)
                self.adapter.get_balances = AsyncMock(wraps=self.adapter.get_balances)
                self.port.before_read = Mock()
                with self.assertRaises(LighterReadError):
                    await self.port.snapshot()

                self.adapter.get_order_history.assert_awaited_once()
                self.adapter.get_balances.assert_awaited_once()
                self.assertNotIn(call("retry"), self.port.before_read.call_args_list)
                self.assertEqual(self.port._stream_count, 0)
                self.assertEqual(self.ledger.snapshot(now=self.clock.now).maker_fill_count, 1)

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
