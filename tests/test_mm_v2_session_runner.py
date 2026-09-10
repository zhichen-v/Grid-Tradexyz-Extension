"""Bounded runner acceptance with real V2 providers and frozen order manager; no I/O."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS
import unittest
import time
import warnings
from unittest.mock import patch

from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from core.services.market_maker_v2.config import (
    ConfigError, FlattenConfig, InventoryConfig, MarketMakerV2Config, QuoteConfig, SessionConfig,
)
from core.services.market_maker_v2.domain import BoundedExitReport, ExitStatus, QuotePlan, FailureDiagnostic
from core.services.market_maker_v2.api_budget import ApiBudgetUnavailable
from core.services.market_maker_v2 import orchestrator
from test_mm_v2_lighter_runtime import Adapter, ADDRESS, Clock, ReadStream, trade


def config(*, dry=True, duration=2):
    return MarketMakerV2Config("BTC", "fee_neutral_volume_v1",
        QuoteConfig(D("0.1"), D("0"), D("0"), 1, 1000),
        InventoryConfig(D("0.4"), D("1"), D("2")),
        FlattenConfig(60, D("2"), 0, 2),
        SessionConfig(duration, D("10"), 0), dry)


class RuntimeAdapter(Adapter):
    supports_definitive_pre_send_failure = True
    supports_definitive_submission_rejection = True

    def __init__(self):
        super().__init__()
        self.connections = self.disconnections = self.creates = self.cancels = 0
        self.history = {}
        self.read_failure = False
        self.created_tifs = []
        self.created_records = []

    async def connect(self):
        self.connections += 1
        return True

    async def authenticate(self):
        return True

    async def open_read_stream(self, symbol, *, clock=None):
        return ReadStream(self, NS(monotonic=clock) if clock else self.clock)

    async def disconnect(self):
        self.disconnections += 1

    def enable_market_maker_cancellation_outcomes(self):
        pass

    def set_market_maker_confirmation_reader(self, reader):
        self.confirmation_reader = reader

    def get_unresolved_submissions(self):
        return []

    def get_unresolved_cancellations(self):
        return []

    def begin_safety_requests(self):
        pass

    def end_safety_requests(self):
        pass

    async def subscribe_orders(self, callback):
        self.callback = callback

    async def get_order(self, identifier, symbol):
        return self.history[identifier]

    async def get_open_orders(self, symbol=None):
        return self.orders[:]

    async def get_order_history(self, symbol=None, since=None, limit=None):
        return list(self.history.values())

    def get_terminal_cancellation_outcome(self, identifier, symbol):
        return self.history.get(identifier)

    def confirm_terminal_cancellation_outcome(self, order):
        return True

    async def create_order(self, symbol, side, order_type, amount, price=None, params=None, **kwargs):
        self.creates += 1
        params = params or {}
        identifier = str(self.creates + 9)
        self.created_tifs.append(params.get("time_in_force"))
        self.created_records.append((self.clock.now, side, params.get("reduce_only", False),
                                     params.get("time_in_force"), price))
        order = OrderData(id=identifier, client_id=str(params.get("client_order_id", identifier)),
            symbol=symbol, side=side, type=OrderType.LIMIT, amount=amount, price=price,
            filled=D("0"), remaining=amount, cost=D("0"), average=None,
            status=OrderStatus.OPEN, timestamp=datetime.now(), updated=None, fee=None,
            trades=[], params=params,
            raw_data={"order_info": NS(reduce_only=params.get("reduce_only", False))})
        self.orders.append(order)
        self.history[identifier] = order
        self._counts()
        if params.get("time_in_force") == "IOC":
            self.fill(order, role="taker", price=D("99"))
        if getattr(self, "confirmation_reader", None):
            await self.confirmation_reader(symbol)
        return self.history[identifier]

    def _counts(self):
        self.account.total_order_count = self.position.open_order_count = len(self.orders)

    async def cancel_order(self, identifier, symbol):
        self.cancels += 1
        order = self.history[identifier]
        terminal = replace(order, status=OrderStatus.CANCELED)
        self.orders = [row for row in self.orders if row.id != identifier]
        self.history[identifier] = terminal
        self._counts()
        return terminal

    def fill(self, order, *, role="maker", price=None):
        price = price if price is not None else order.price
        fill = trade(str(len(self.trades) + 1), side=order.side.value,
                     price=str(price), size=str(order.remaining), role=role, order=order.id)
        old_position = D(self.position.position) * self.position.sign
        signed = order.remaining if order.side == OrderSide.BUY else -order.remaining
        gross = D("0")
        if old_position * signed < 0:
            gross = min(abs(old_position), abs(signed)) * (price - D(self.position.avg_entry_price))
            gross *= 1 if old_position > 0 else -1
        current = old_position + signed
        fill.raw_data["realized_pnl"] = gross
        self.position.position = str(abs(current))
        self.position.sign = 1 if current >= 0 else -1
        if not old_position:
            self.position.avg_entry_price = str(price)
        self.account.collateral = str(D(self.account.collateral) + gross - fill.fee["cost"])
        self.trades.append(fill)
        self.orders = [row for row in self.orders if row.id != order.id]
        self.history[order.id] = replace(order, status=OrderStatus.FILLED, filled=order.amount,
                                         remaining=D("0"), average=price)
        self._counts()

    async def get_orderbook(self, symbol, limit):
        if self.read_failure:
            raise RuntimeError("secret-provider-detail")
        book = NS(symbol="BTC", bids=self.book.bids[:], asks=self.book.asks[:])
        for row in self.orders:
            (book.bids if row.side == OrderSide.BUY else book.asks).append(
                NS(price=row.price, size=row.remaining))
        return book


class RuntimeClock(Clock):
    def monotonic(self):
        self.now += 0.001
        return self.now


class VolumeSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter, self.clock = RuntimeAdapter(), RuntimeClock()
        self.adapter.clock = self.clock
        self.events = []

    def session(self, *, dry=True, duration=2, authorized=False, sleep=None, passive=0):
        async def advance(seconds):
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        configured = config(dry=dry, duration=duration)
        configured = replace(configured, flatten=replace(configured.flatten, passive_grace_seconds=passive))
        return orchestrator.VolumeSession(configured, self.adapter,
            account_index=7, expected_l1_address=ADDRESS, authorize_bounded_flatten=authorized,
            telemetry=NS(emit=self.events.append), clock=self.clock, sleep=sleep or advance)

    async def test_delayed_immutable_book_is_bracketed_by_order_watermarks(self):
        class Buffered(ReadStream):
            engine_nonce = 1
            order_nonce = 1
            deliveries = 0

            def book_snapshot(inner):
                return inner.packet

            def check_book_source(inner, timestamp):
                if self.clock.now * 1000 - timestamp > 3000:
                    raise RuntimeError("fixture source expired")

            async def book_at_or_after(inner, nonce, *, after):
                # Deliver a new WS packet only after the account request. Reads of
                # the buffered packet never restamp it or insert new own orders.
                self.clock.now += 0.05
                await asyncio.sleep(0)
                inner.packet = ReadStream.book_snapshot(inner) | {
                    "nonce": inner.engine_nonce, "timestamp": self.clock.now * 1000}
                inner.deliveries += 1
                return inner.packet

        async def open_stream(symbol, *, clock):
            stream = Buffered(self.adapter, self.clock)
            stream.packet = ReadStream.book_snapshot(stream) | {"nonce": 1, "timestamp": self.clock.now * 1000}
            self.adapter.stream = stream
            return stream
        self.adapter.open_read_stream = open_stream
        original_read = self.adapter.get_open_orders
        async def orders(symbol=None):
            # The confirmation lookup occurs inside create_order, after mutation.
            self.adapter.stream.engine_nonce = 1 + self.adapter.creates + self.adapter.cancels
            self.adapter.stream.order_nonce = self.adapter.stream.engine_nonce
            return await original_read(symbol)
        self.adapter.get_open_orders = orders
        result = await self.session(dry=False, duration=12, authorized=True).run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        self.assertGreater(self.adapter.creates, 2)
        self.assertEqual(self.adapter.creates, self.adapter.cancels)
        self.assertGreater(self.adapter.stream.deliveries, 2)
        self.assertEqual(result.final_account.open_order_ids, ())
        self.assertEqual(result.final_account.position, D("0"))

    async def test_unauthorized_live_rejected_before_connection(self):
        with self.assertRaises((ConfigError, ValueError)):
            session = self.session(dry=False)
            await session.run(asyncio.Event())
        self.assertEqual((self.adapter.connections, self.adapter.creates, self.adapter.cancels), (0, 0, 0))

    async def test_second_ioc_replaces_coherent_pre_prepare_book_handoff(self):
        class Buffered(ReadStream):
            order_nonce = 1

            def book_snapshot(inner):
                return inner.packet

            def check_book_source(inner, timestamp):
                if not 0 <= self.clock.now * 1000 - timestamp <= 3000:
                    raise RuntimeError("fixture source expired")

            def deliver(inner):
                inner.packet = ReadStream.book_snapshot(inner) | {
                    "nonce": inner.order_nonce, "timestamp": self.clock.now * 1000}

            async def book_at_or_after(inner, nonce, *, after):
                if inner.packet["nonce"] < nonce or inner.packet["received_monotonic"] < after:
                    inner.deliver()
                return inner.packet

        async def open_stream(symbol, *, clock):
            stream = Buffered(self.adapter, self.clock)
            stream.deliver()
            self.adapter.stream = stream
            return stream

        session = self.session(dry=False, authorized=True)
        original_start, original_fill = session._start, self.adapter.fill
        maker_filled, ioc_fills, handoffs, intents = [], [], [], []

        def fill(order, *, role="maker", price=None):
            if role == "taker":
                ioc_fills.append(order.id)
                if len(ioc_fills) == 1:
                    # Exact IOC terminal with no fill, as in the failed run.
                    self.adapter.orders.remove(order)
                    self.adapter.history[order.id] = replace(order, status=OrderStatus.CANCELED)
                    self.adapter._counts()
                    return
            original_fill(order, role=role, price=price)

        async def start():
            await original_start()
            original_prepare, original_flatten = session.manager.execute_active_unwind, session.execution.flatten_ioc

            async def prepare(desired, *, prepared_generation=None):
                if prepared_generation is None and len(ioc_fills) == 1 and not handoffs:
                    # A valid same-generation confirmation can arrive just before
                    # prepare completes. Its book must not satisfy the later send.
                    await session.account.read_confirmation_orders("BTC")
                    self.adapter.stream.deliver()
                    handoffs.append(self.adapter.stream.packet["received_monotonic"])
                return await original_prepare(desired, prepared_generation=prepared_generation)

            async def flatten(intent):
                intents.append(intent)
                return await original_flatten(intent)

            session.manager.execute_active_unwind = prepare
            session.execution.flatten_ioc = flatten

        async def advance(seconds):
            buys = [row for row in self.adapter.orders if row.side is OrderSide.BUY]
            if buys and not maker_filled:
                self.adapter.fill(buys[0])
                maker_filled.append(buys[0].id)
            self.clock.now += float(seconds)
            await asyncio.sleep(0)

        self.adapter.open_read_stream, self.adapter.fill = open_stream, fill
        session._start, session.sleep = start, advance
        result = await session.run(asyncio.Event())
        self.assertTrue(handoffs, "the second IOC must encounter the earlier coherent book")
        self.assertTrue(result.completed, (result.failure,
            [event for event in self.events if type(event) is FailureDiagnostic]))
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual(len(ioc_fills), 2)
        self.assertEqual(len(intents), 2)
        self.assertEqual({intent.limit_price for intent in intents}, {D("97")})
        self.assertEqual(len({intent.deadline_monotonic for intent in intents}), 1)
        self.assertEqual({intent.size for intent in intents}, {D("0.1")})
        self.assertLess(self.clock.now, intents[0].deadline_monotonic)
        self.assertGreater(session.market.snapshot().observed_monotonic, handoffs[0])
        self.assertEqual(result.report.taker_fill_count, 1)
        self.assertFalse([event for event in self.events if type(event) is FailureDiagnostic])

    async def test_btc_partial_maker_fill_exits_exact_subminimum_lots(self):
        # Real BTC precision/minima from the 000201 run; exchange I/O stays fake.
        for side in (OrderSide.BUY, OrderSide.SELL):
            for partial_ioc in (False, True):
                with self.subTest(side=side, partial_ioc=partial_ioc):
                    self.setUp()
                    adapter = self.adapter
                    adapter.metadata.symbols[0].update(price_decimals=1, size_decimals=5,
                        min_base_amount="0.00020", min_quote_amount="10")
                    adapter.book.bids[0].price, adapter.book.asks[0].price = D("79590"), D("79600")
                    adapter.unified("298.068939505372")
                    configured = MarketMakerV2Config("BTC", "fee_neutral_volume_v1",
                        QuoteConfig(D("0.00040"), D("0.20"), D("0"), 500, 60000),
                        InventoryConfig(D("0.00040"), D("0.00080"), D("2")),
                        FlattenConfig(180, D("0.15"), 0, 200), SessionConfig(2, D("0.50"), 0), False)
                    original_fill, maker_filled, iocs = adapter.fill, [], []

                    def fill(order, *, role="maker", price=None):
                        size = D("0.00017") if role == "maker" else order.remaining
                        if role == "taker":
                            self.assertFalse(adapter.orders[:-1], "known makers must be canceled before IOC")
                            iocs.append(order)
                            if partial_ioc and len(iocs) == 1:
                                size = D("0.00016")  # The last lot is below both maker minima.
                            price = (adapter.book.bids[0].price if order.side is OrderSide.SELL
                                     else adapter.book.asks[0].price)
                        cash, collateral = D(adapter.account.assets[0].margin_balance), D(adapter.account.collateral)
                        original_fill(replace(order, amount=size, remaining=size), role=role, price=price)
                        adapter.unified_cash(cash + D(adapter.account.collateral) - collateral)
                        remaining = order.amount - size
                        updated = replace(adapter.history[order.id], amount=order.amount, filled=size,
                            remaining=remaining, status=(OrderStatus.OPEN if role == "maker" else
                                OrderStatus.CANCELED if remaining else OrderStatus.FILLED))
                        adapter.history[order.id] = updated
                        if role == "maker":
                            adapter.orders.append(updated)
                        adapter._counts()

                    async def advance(seconds):
                        orders = [row for row in adapter.orders if row.side is side]
                        if orders and not maker_filled:
                            adapter.fill(orders[0])
                            maker_filled.append(orders[0].id)
                        self.clock.now += float(seconds)
                        await asyncio.sleep(0)

                    adapter.fill = fill
                    session = orchestrator.VolumeSession(configured, adapter, account_index=7,
                        expected_l1_address=ADDRESS, authorize_bounded_flatten=True,
                        telemetry=NS(emit=self.events.append), clock=self.clock, sleep=advance)
                    result = await session.run(asyncio.Event())
                    self.assertTrue(result.completed, (result.failure,
                        [event for event in self.events if isinstance(event, FailureDiagnostic)]))
                    self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
                    self.assertEqual([order.amount for order in iocs],
                        [D("0.00017"), D("0.00001")] if partial_ioc else [D("0.00017")])
                    self.assertTrue(all(order.side is not side and order.params["reduce_only"] for order in iocs))
                    self.assertEqual(len({order.price for order in iocs}), 1)
                    self.assertEqual(result.report.maker_fill_count, 1)
                    self.assertEqual(result.report.taker_fill_count, 2 if partial_ioc else 1)
                    self.assertTrue(result.report.complete)
                    self.assertEqual(result.report.equity_reconciliation_difference, D("0"))
                    self.assertFalse([event for event in self.events if isinstance(event, FailureDiagnostic)])

    async def test_live_start_satisfies_real_confirmation_reader_prerequisites(self):
        from core.adapters.exchanges.adapters.lighter import LighterAdapter
        from core.adapters.exchanges.adapters.lighter_rest import LighterRest
        backend = object.__new__(LighterAdapter)
        backend._rest = object.__new__(LighterRest)
        backend._read_stream = object()  # Already-owned stream; no network in this contract.
        self.adapter.enable_market_maker_cancellation_outcomes = backend.enable_market_maker_cancellation_outcomes
        def register(reader):
            backend.set_market_maker_confirmation_reader(reader)
            self.adapter.confirmation_reader = reader
        self.adapter.set_market_maker_confirmation_reader = register
        result = await self.session(dry=False, authorized=True).run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        self.assertGreater(self.adapter.creates, 0)
        self.assertEqual(result.final_account.open_order_ids, ())
        self.assertEqual(result.final_account.position, D("0"))

    async def test_delayed_book_override_reaches_only_dry_stream_and_result(self):
        for authorized in (False, True):
            with self.subTest(authorized=authorized), self.assertRaises(ConfigError):
                orchestrator.VolumeSession(config(dry=False), self.adapter, account_index=7,
                    expected_l1_address=ADDRESS, authorize_bounded_flatten=authorized,
                    allow_delayed_dry_book=True)
        self.assertEqual(self.adapter.connections, 0)
        received = []
        original = self.adapter.open_read_stream
        async def open_stream(symbol, *, clock, allow_delayed_dry_book=False):
            received.append(allow_delayed_dry_book)
            return await original(symbol, clock=clock)
        async def advance(seconds):
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        self.adapter.open_read_stream = open_stream
        session = orchestrator.VolumeSession(config(), self.adapter, account_index=7,
            expected_l1_address=ADDRESS, clock=self.clock, sleep=advance, allow_delayed_dry_book=True)
        result = await session.run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.delayed_dry_book)
        self.assertEqual(received, [True])
        self.assertEqual((self.adapter.creates, self.adapter.cancels), (0, 0))

    def test_default_clock_is_high_resolution_and_shared_with_all_v2_inputs(self):
        session = orchestrator.VolumeSession(config(), self.adapter, account_index=7,
                                             expected_l1_address=ADDRESS)
        self.assertIs(session.clock.monotonic, time.perf_counter)
        self.assertIs(session.account.clock, session.clock)
        self.assertIs(session.market.clock, session.clock)

    async def test_default_dry_quotes_are_simulated_and_deadline_postflight_no_mutations(self):
        result = await self.session().run(asyncio.Event())
        self.assertTrue(result.dry_run)
        self.assertTrue(result.completed, result.failure)
        self.assertEqual((self.adapter.connections, self.adapter.disconnections), (1, 1))
        self.assertEqual((self.adapter.creates, self.adapter.cancels), (0, 0))
        self.assertTrue(any(isinstance(event, QuotePlan) and event.quotes for event in self.events))
        self.assertFalse(result.report.complete)
        self.assertIsNone(result.report.all_in_net_pnl)
        self.assertEqual(result.final_account.open_order_ids, ())

    async def test_preset_stop_still_postflights_without_quotes(self):
        stop = asyncio.Event()
        stop.set()
        result = await self.session().run(stop)
        self.assertTrue(result.completed, result.failure)
        self.assertEqual((self.adapter.creates, self.adapter.cancels), (0, 0))
        self.assertEqual(result.final_account.position, D("0"))

    async def test_authorized_fake_live_deadline_cancels_and_authenticates_flat(self):
        result = await self.session(dry=False, authorized=True).run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        self.assertGreater(self.adapter.creates, 0)
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual(result.final_account.open_order_ids, ())
        self.assertTrue(result.report.complete)
        self.assertEqual(result.report.all_in_net_pnl, D("0"))
        self.assertTrue(all(tif == "POST_ONLY" for tif in self.adapter.created_tifs))

    async def test_flat_unaffordable_minimum_quote_exits_early_with_reason_and_fresh_proof(self):
        configured = config(dry=False, duration=3600)
        # Configuration requires session loss >= inventory stop. Equality leaves
        # no room for the minimum quote's fees/slippage under the strict reserve.
        configured = replace(configured, session=replace(configured.session,
            max_loss_usdg=configured.flatten.stop_loss_usdg))

        async def advance(seconds):
            self.clock.now += float(seconds)
            await asyncio.sleep(0)

        session = orchestrator.VolumeSession(configured, self.adapter,
            account_index=7, expected_l1_address=ADDRESS, authorize_bounded_flatten=True,
            telemetry=NS(emit=self.events.append), clock=self.clock, sleep=advance)
        result = await session.run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertEqual(result.stop_reason, "risk_capacity_exhausted")
        self.assertLess(result.report.duration_seconds, D("10"))
        self.assertEqual(session.governor.session_deadline_monotonic
                         - session.governor.session_started_monotonic, 3600)
        self.assertEqual((self.adapter.creates, self.adapter.cancels), (0, 0))
        self.assertEqual(self.adapter.created_tifs, [])
        self.assertFalse(any(isinstance(event, QuotePlan) and event.quotes for event in self.events))
        exits = [event for event in self.events if isinstance(event, BoundedExitReport)]
        self.assertEqual(len(exits), 1)
        self.assertEqual((exits[0].status, exits[0].attempts), (ExitStatus.FLAT, 0))
        proof = exits[0].final_result.account_snapshot
        self.assertTrue(proof.authenticated)
        self.assertEqual((proof.position, proof.open_order_ids), (D("0"), ()))
        self.assertGreater(proof.observed_monotonic, session.governor.session_started_monotonic)
        self.assertTrue(result.final_account.authenticated)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertGreater(result.final_account.observed_monotonic, proof.observed_monotonic)
        self.assertEqual((self.adapter.connections, self.adapter.disconnections), (1, 1))

    async def test_fake_live_filled_inventory_deadline_ioc_includes_all_cost(self):
        filled = False
        async def fill_then_advance(seconds):
            nonlocal filled
            if not filled:
                buys = [row for row in self.adapter.orders if row.side == OrderSide.BUY]
                if buys:
                    self.adapter.fill(buys[0])
                    filled = True
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        result = await self.session(dry=False, authorized=True, sleep=fill_then_advance).run(asyncio.Event())
        self.assertTrue(filled)
        self.assertTrue(result.completed, result.failure)
        self.assertEqual(result.final_account.position, D("0"))
        self.assertEqual(result.report.maker_fill_count, 1)
        self.assertEqual(result.report.taker_fill_count, 1)
        self.assertEqual(result.report.all_in_net_pnl, -result.report.maker_fee - result.report.taker_fee)
        self.assertIn("IOC", self.adapter.created_tifs)

    async def test_fill_after_manager_sync_with_lagging_counter_continues_and_exits(self):
        session = self.session(dry=False, duration=12, authorized=True)
        original_start = session._start
        original_balances = self.adapter.get_balances
        filled = False

        async def start():
            await original_start()
            original_activity = self.adapter_stream.request_snapshot

            async def activity(channel):
                row = await original_activity(channel)
                row["total_trades_count"] = 0  # Deliberately stale through maker and IOC fills.
                return row

            self.adapter_stream.request_snapshot = activity

        async def balances():
            nonlocal filled
            if self.adapter.creates >= 2 and not filled and not session._budget_exiting:
                self.adapter.fill(next(row for row in self.adapter.orders if row.side is OrderSide.BUY))
                filled = True  # After OM/opening proof, before closing account bookend.
            return await original_balances()

        original_stream = self.adapter.open_read_stream
        async def stream(*args, **kwargs):
            self.adapter_stream = await original_stream(*args, **kwargs)
            return self.adapter_stream

        self.adapter.open_read_stream, self.adapter.get_balances = stream, balances
        session._start = start
        result = await session.run(asyncio.Event())
        self.assertTrue(filled)
        self.assertTrue(result.completed, result.failure)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (1, 1))
        self.assertFalse(any(type(event) is FailureDiagnostic for event in self.events))
        self.assertEqual(session.account._stream_count, 0)
        self.assertEqual(session.account._stream_trade_ahead, 2)

    async def test_normal_missing_terminal_recovers_before_deadline_exit(self):
        await self._normal_missing_terminal(proof_arrives=True)

    async def test_unattributed_cash_stops_quotes_but_preserves_bounded_flat_cleanup(self):
        await self._unattributed_cash_cleanup(in_budget_exit=False)

    async def test_cash_first_seen_during_budget_exit_cannot_authorize_reentry(self):
        await self._unattributed_cash_cleanup(in_budget_exit=True)

    async def _unattributed_cash_cleanup(self, *, in_budget_exit):
        self.adapter.set_market_maker_request_observer = lambda observer, *, enforce_admission: None
        session = self.session(dry=False, duration=90, authorized=True)
        original_balances, original_admit = self.adapter.get_balances, session.account.before_read
        filled, refused, changed = False, False, []
        unexplained = D("0.00000000001")

        def change_cash():
            self.adapter.account.collateral = str(D(self.adapter.account.collateral) - unexplained)
            changed.append((self.clock.now, session._budget_exiting))

        async def advance(seconds):
            nonlocal filled
            if self.adapter.creates == 2 and not filled:
                self.adapter.fill(next(row for row in self.adapter.orders if row.side is OrderSide.BUY))
                filled = True
                if not in_budget_exit:
                    change_cash()
            self.clock.now += float(seconds)
            await asyncio.sleep(0)

        def admit(kind):
            nonlocal refused
            if in_budget_exit and filled and not refused and not session._budget_exiting:
                refused = True
                # Exercise the real admission refusal and its same-session exit path.
                for _ in range(40):
                    session.api_budget.observe("rest", "account")
                original_admit(kind)
                self.fail("synthetic burst must refuse the normal read")
            return original_admit(kind)

        async def balances():
            if in_budget_exit and refused and session._budget_exiting and not changed:
                change_cash()  # This discrepancy was absent from every normal authorization.
            return await original_balances()

        session.sleep, session.account.before_read = advance, admit
        self.adapter.get_balances = balances
        result = await session.run(asyncio.Event())
        self.assertTrue(filled)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][1], in_budget_exit)
        self.assertEqual(session.api_budget.deferrals, int(in_budget_exit))
        self.assertEqual(session.api_budget.account_read_deferrals, 0)
        self.assertFalse(result.completed)
        self.assertIsNone(result.final_account, "the final economic proof must remain strict")
        self.assertFalse(result.report.complete)
        self.assertIsNone(result.report.all_in_net_pnl)
        self.assertEqual((result.report.funding, result.report.external_transfers), (D("0"), D("0")))
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (1, 1))
        self.assertEqual(result.report.ledger_position, D("0"))
        self.assertTrue(result.cleanup_account.authenticated)
        self.assertEqual((result.cleanup_account.position, result.cleanup_account.open_order_ids),
                         (D("0"), ()))
        self.assertEqual(self.adapter.created_tifs, ["POST_ONLY", "POST_ONLY", "IOC"])
        self.assertEqual(self.adapter.created_records[-1][1:4], (OrderSide.SELL, True, "IOC"))
        self.assertLess(self.adapter.created_records[-1][0] - changed[0][0], 30)
        # The budget refusal precedes OM sync, so cleanup also checks the just-filled buy.
        self.assertEqual((self.adapter.cancels, self.adapter.disconnections),
                         (2 if in_budget_exit else 1, 1))
        self.assertEqual((D(self.adapter.position.position), self.adapter.orders), (D("0"), []))
        exits = [event for event in self.events if type(event) is BoundedExitReport]
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0].complete)
        self.assertTrue(any(type(event) is FailureDiagnostic and event.stage == "final_account"
                            for event in self.events))

    async def test_normal_budget_boundaries_clean_wait_and_resume_same_session(self):
        for boundary in ("sync", "first_audit", "filled_second_audit", "one_create", "cancel_proof"):
            with self.subTest(boundary=boundary):
                self.setUp()
                await self._normal_budget_boundary(boundary)

    async def test_conditional_account_reads_preserve_exit_reserve_and_resume(self):
        for boundary in ("fees", "settlement", "trades", "terminal_history"):
            with self.subTest(boundary=boundary):
                self.setUp()
                await self._normal_budget_boundary(boundary)

    async def test_two_bracket_races_clean_and_resume_before_quotes_or_after_one_create(self):
        for stage in ("authorizing_quotes", "reconciling_quotes"):
            with self.subTest(stage=stage):
                self.setUp()
                await self._account_bracket_recovery(stage=stage)

    async def test_bracket_race_cannot_resume_unknown_wire_or_incomplete_cleanup(self):
        for outcome in ("unknown_wire", "persistent"):
            with self.subTest(outcome=outcome):
                self.setUp()
                await self._account_bracket_recovery(stage="authorizing_quotes", outcome=outcome)

    async def _account_bracket_recovery(self, *, stage, outcome="recover"):
        self.adapter.set_market_maker_request_observer = lambda observer, *, enforce_admission: None
        session = self.session(dry=False, duration=25, authorized=True)
        original_stream, original_admit = self.adapter.open_read_stream, session.account.before_read
        injections, retries, preserved = [], [], []
        stale = None

        async def stream(*args, **kwargs):
            result = await original_stream(*args, **kwargs)
            original_activity = result.request_snapshot

            async def activity(channel):
                nonlocal stale
                row = await original_activity(channel)
                creates = 2 if stage == "authorizing_quotes" else 1
                if (not injections and self.adapter.creates == creates
                        and session.phase == stage and not session._budget_exiting):
                    stale = deepcopy(row)
                    preserved.append((session.ledger, session.governor.session_deadline_monotonic,
                                      session.governor.max_session_loss_usdg, self.clock.now,
                                      self.adapter.creates))
                    # The actual known maker fills after frozen REST cash but
                    # before WS account, so the first full bracket disagrees.
                    self.adapter.fill(next(order for order in self.adapter.orders
                                           if order.side is OrderSide.BUY))
                    injections.append(self.clock.now)
                    return await original_activity(channel)
                if len(injections) == 1 and not session._budget_exiting:
                    # Its second full audit sees new REST cash but a still-old
                    # WS snapshot. Neither read may authorize the next quote.
                    injections.append(self.clock.now)
                    if outcome == "unknown_wire":
                        session.manager._mark_submission_uncertain(OrderSide.BUY, "fixture wire unknown")
                        self.adapter.get_unresolved_submissions = lambda: [{"symbol": "BTC"}]
                    return deepcopy(stale)
                if len(injections) == 2 and outcome == "persistent":
                    return deepcopy(stale)
                return row

            result.request_snapshot = activity
            return result

        def admit(kind):
            if kind == "retry" and injections and not session._budget_exiting:
                retries.append(self.clock.now)
            return original_admit(kind)

        self.adapter.open_read_stream, session.account.before_read = stream, admit
        result = await session.run(asyncio.Event())
        self.assertEqual(len(injections), 2)
        self.assertEqual(len(retries), 1, "one audit still has only its original bounded retry")
        self.assertEqual(session.api_budget.deferrals, 0, "data arrival is not an API refusal")
        self.assertIs(session.ledger, preserved[0][0])
        self.assertEqual(session.governor.session_deadline_monotonic, preserved[0][1])
        self.assertEqual(session.governor.max_session_loss_usdg, preserved[0][2])
        self.assertEqual(self.adapter.disconnections, 1)
        exits = [event for event in self.events if type(event) is BoundedExitReport]
        if outcome != "recover":
            self.assertFalse(result.completed)
            self.assertEqual(len(exits), 1)
            self.assertFalse(exits[0].complete)
            self.assertEqual(self.adapter.creates, preserved[0][4])
            self.assertNotIn("IOC", self.adapter.created_tifs)
            self.assertTrue(any(type(event) is FailureDiagnostic for event in self.events))
            self.assertEqual(session.api_budget.account_read_deferrals, int(outcome == "persistent"))
            return
        self.assertTrue(result.completed, result.failure)
        self.assertEqual(session.api_budget.snapshot()["account_read_deferrals"], 1)
        self.assertTrue(all(event.complete for event in exits))
        self.assertEqual(len(exits), 2)
        self.assertLess(exits[0].observed_monotonic - injections[0], 30)
        self.assertFalse([record for record in self.adapter.created_records
                          if injections[0] <= record[0] < exits[0].observed_monotonic
                          and record[3] == "POST_ONLY"])
        self.assertTrue([record for record in self.adapter.created_records
                         if record[0] > exits[0].observed_monotonic and record[3] == "POST_ONLY"])
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (1, 1))
        self.assertEqual(result.report.all_in_net_pnl, -result.report.maker_fee - result.report.taker_fee)
        self.assertFalse(any(type(event) is FailureDiagnostic for event in self.events))

    async def test_budget_refusal_never_recovers_unknown_wire_or_failed_cleanup(self):
        for outcome in ("unknown_wire", "cleanup_failure"):
            with self.subTest(outcome=outcome):
                self.setUp()
                await self._normal_budget_boundary("one_create", outcome=outcome)

    async def _normal_budget_boundary(self, boundary, *, outcome="recover"):
        # Activate production admission; this fixture's one synthetic burst is
        # fault injection, not measured or estimated exchange wire traffic.
        self.adapter.set_market_maker_request_observer = lambda observer, *, enforce_admission: None
        # Metadata now refreshes at ~30s; allow its 60s injected burst to expire
        # before asserting re-entry, without renewing the configured deadline.
        session = self.session(dry=False, duration=120 if boundary in {"fees", "settlement"} else 90,
                               authorized=True)
        original_start, original_admit = session._start, session.account.before_read
        original_available = session.api_budget.scheduled_live_available
        original_cancel = self.adapter.cancel_order
        injected, cooled, filled_id, failed_cancels = [], [], None, []
        reentry = []
        filled_boundaries = {"filled_second_audit", "trades", "terminal_history"}
        conditional_costs = {"fees": 900, "settlement": 300, "trades": 600, "terminal_history": 100}

        async def start():
            await original_start()
            original_authorize = session._authorize

            async def authorize(exposure):
                nonlocal filled_id
                if (boundary in filled_boundaries and not injected and filled_id is None
                        and self.adapter.creates >= 2 and session.phase == "authorizing_quotes"):
                    order = next(row for row in self.adapter.orders if row.side is OrderSide.BUY)
                    filled_id = order.id
                    self.adapter.fill(order)  # After OM sync, before account authorization.
                result = await original_authorize(exposure)
                if cooled:
                    reentry.append((self.clock.now, result.account.authenticated))
                return result

            session._authorize = session.execution.refresh_quote = authorize

        def admit(kind):
            eligible = (session.manager is not None and not session._budget_exiting and not injected)
            hit = eligible and (
                boundary == "sync" and kind == "sync" and session.phase == "syncing_orders"
                or boundary == "first_audit" and kind == "audit" and self.adapter.creates == 0
                or boundary == "filled_second_audit" and kind == "audit" and filled_id is not None
                    and filled_id in session.manager.terminal_order_ids and bool(session.account._trades)
                or boundary == "one_create" and kind == "audit" and self.adapter.creates == 1
                or boundary == "cancel_proof" and kind == "audit" and self.adapter.cancels == 2
                    and not self.adapter.orders and len(session.manager.terminal_order_ids) == 2
                or boundary in {"fees", "settlement"} and kind == boundary
                    and self.adapter.creates >= 2 and session.phase == "authorizing_quotes"
                or boundary == "trades" and kind == boundary and filled_id is not None
                or boundary == "terminal_history" and kind == boundary
                    and filled_id in session.manager.terminal_order_ids)
            if hit:
                injected.append((self.clock.now, self.adapter.creates, session.ledger,
                                 session.governor.session_deadline_monotonic,
                                 session.governor.max_session_loss_usdg))
                if outcome == "unknown_wire":
                    session.manager._mark_submission_uncertain(OrderSide.BUY, "unproven receipt")
                    self.adapter.get_unresolved_submissions = lambda: [{
                        "symbol": "BTC", "client_order_id": "unmatched-submission"}]
                for _ in range(40):
                    session.api_budget.observe("rest", "account")
                try:
                    original_admit(kind)
                except ApiBudgetUnavailable as error:
                    if boundary in conditional_costs:
                        self.assertEqual(error.diagnostic_values["api_next_rest"], D(conditional_costs[boundary]))
                    raise
                self.fail("synthetic burst must refuse the selected normal read")
            return original_admit(kind)

        async def cancel(identifier, symbol):
            if injected and outcome == "cleanup_failure":
                failed_cancels.append(identifier)
                raise RuntimeError("fixture cancellation outcome unavailable")
            return await original_cancel(identifier, symbol)

        async def advance(seconds):
            if session.phase == "api_cooldown":
                self.assertFalse(self.adapter.orders)
                self.assertEqual(D(self.adapter.position.position), D("0"))
                self.assertTrue(session.final_account.authenticated)
                self.assertEqual((session.final_account.position, session.final_account.open_order_ids),
                                 (D("0"), ()))
                self.assertIs(session.ledger, injected[0][2])
                self.assertEqual(session.governor.session_deadline_monotonic, injected[0][3])
                self.assertEqual(session.governor.max_session_loss_usdg, injected[0][4])
                self.assertFalse(original_available({"rest": 6000, "ws": 32, "tx": 4}))
                cooled.append((self.clock.now, self.adapter.creates))
            self.clock.now += float(seconds)
            await asyncio.sleep(0)

        session._start, session.account.before_read, session.sleep = start, admit, advance
        self.adapter.cancel_order = cancel
        result = await session.run(asyncio.Event())
        self.assertEqual(len(injected), 1)
        exits = [event for event in self.events if type(event) is BoundedExitReport]
        faults = [event for event in self.events if type(event) is FailureDiagnostic]
        self.assertEqual(self.adapter.disconnections, 1)
        if outcome != "recover":
            self.assertFalse(result.completed)
            self.assertFalse(cooled or reentry)
            self.assertEqual(self.adapter.creates, injected[0][1])
            self.assertNotIn("IOC", self.adapter.created_tifs)
            self.assertEqual(len(exits), 1, "failed cleanup must not be attempted again")
            self.assertFalse(exits[0].complete)
            self.assertTrue(faults)
            if outcome == "cleanup_failure":
                self.assertFalse([source for fault in faults for source in fault.source
                                  if source.startswith("api_budget:")])
                self.assertFalse([value for fault in faults for value in fault.values
                                  if value.name.startswith(("api_used_", "api_next_"))])
            self.assertEqual(session.api_budget.deferrals, int(outcome == "cleanup_failure"))
            self.assertEqual(len(failed_cancels), int(outcome == "cleanup_failure"))
            return
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertFalse(faults)
        self.assertEqual(session.api_budget.deferrals, 1)
        self.assertTrue(cooled and reentry)
        self.assertTrue(all(authenticated for _, authenticated in reentry))
        self.assertEqual(len({creates for _, creates in cooled}), 1)
        self.assertFalse([row for row in self.adapter.created_records
                          if injected[0][0] < row[0] < cooled[-1][0] and row[3] == "POST_ONLY"])
        resumed = [row for row in self.adapter.created_records
                   if row[0] > cooled[-1][0] and row[3] == "POST_ONLY"]
        self.assertTrue(resumed)
        self.assertLessEqual(reentry[0][0], resumed[0][0])
        self.assertEqual(len(exits), 2)
        self.assertTrue(all(event.complete for event in exits))
        self.assertLess(exits[0].observed_monotonic - injected[0][0], 30)
        self.assertGreaterEqual(result.report.duration_seconds, D("90"))
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertIs(session.ledger, injected[0][2])
        self.assertEqual(session.governor.session_deadline_monotonic, injected[0][3])
        self.assertEqual(session.governor.max_session_loss_usdg, injected[0][4])
        expected_fills = int(boundary in filled_boundaries)
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count),
                         (expected_fills, expected_fills))
        self.assertEqual(result.report.all_in_net_pnl, -result.report.maker_fee - result.report.taker_fee)

    async def test_normal_missing_terminal_never_promotes_unproven_state(self):
        await self._normal_missing_terminal(proof_arrives=False)

    async def _normal_missing_terminal(self, *, proof_arrives):
        session = self.session(dry=False, duration=12, authorized=True)
        original_history = self.adapter.get_order_history
        missing_id, history_reads = None, 0

        async def history(*args, **kwargs):
            nonlocal history_reads
            rows = await original_history(*args, **kwargs)
            if missing_id is not None:
                history_reads += 1
                if history_reads == 1 or not proof_arrives:
                    return [row for row in rows if row.id != missing_id]
            return rows

        async def advance(seconds):
            nonlocal missing_id
            self.clock.now += float(seconds)
            if self.adapter.orders and missing_id is None:
                sold = next(row for row in self.adapter.orders if row.side is OrderSide.SELL)
                self.adapter.fill(sold)
                missing_id = sold.id
            await asyncio.sleep(0)

        self.adapter.get_order_history, session.sleep = history, advance
        result = await session.run(asyncio.Event())
        self.assertIsNotNone(missing_id)
        if proof_arrives:
            self.assertTrue(result.completed, result.failure)
            self.assertGreaterEqual(result.report.duration_seconds, D("12"))
            self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (1, 1))
            self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
            self.assertFalse(any(type(event) is FailureDiagnostic for event in self.events))
            ioc_time = next(row[0] for row in self.adapter.created_records if row[3] == "IOC")
            self.assertGreaterEqual(ioc_time, session.governor.session_deadline_monotonic)
        else:
            self.assertFalse(result.completed)
            self.assertTrue(session.manager.has_uncertain_state)
            self.assertEqual(self.adapter.creates, 2)
            self.assertEqual(self.adapter.cancels, 0)
            self.assertNotIn("IOC", self.adapter.created_tifs)
            self.assertLessEqual(history_reads, 3)

    async def test_delayed_fill_discovery_triggers_hold_exit_without_restarting_age(self):
        filled_at = None
        async def delay_after_fill(seconds):
            nonlocal filled_at
            buys = [row for row in self.adapter.orders if row.side == OrderSide.BUY]
            if filled_at is None and buys:
                self.adapter.fill(buys[0])
                filled_at = self.clock.now
                self.clock.now += 65  # Delivery is delayed past the 60s hold limit.
            else:
                self.clock.now += float(seconds)
            await asyncio.sleep(0)
        result = await self.session(dry=False, duration=90, authorized=True,
                                    sleep=delay_after_fill).run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        ioc = [row for row in self.adapter.created_records if row[3] == "IOC"]
        self.assertEqual(len(ioc), 1)
        self.assertLess(ioc[0][0] - filled_at, 66)
        self.assertEqual(result.report.forced_flatten_count, 1)
        self.assertEqual(result.final_account.position, D("0"))

    async def test_market_failure_stops_and_disconnects_without_false_completion(self):
        async def break_then_advance(seconds):
            self.adapter.read_failure = True
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        result = await self.session(sleep=break_then_advance).run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertFalse(result.report.complete)
        self.assertEqual(self.adapter.disconnections, 1)
        self.assertEqual((self.adapter.creates, self.adapter.cancels), (0, 0))
        self.assertNotIn("secret-provider-detail", str(result.failure))

    async def test_exhausted_exit_is_not_restarted_by_finally_cleanup(self):
        attempts = []
        async def exhausted(execution, market, clock, **kwargs):
            attempts.append(kwargs["flatten_id"])
            return BoundedExitReport(kwargs["flatten_id"], "BTC", clock.monotonic(),
                                     ExitStatus.ATTEMPTS_EXHAUSTED, 3)
        with patch.object(orchestrator, "bounded_exit", exhausted):
            result = await self.session(dry=False, authorized=True).run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertEqual(len(attempts), 1, "cleanup must not grant another three IOC attempts")
        self.assertEqual(self.adapter.disconnections, 1)

    async def test_quote_read_timeout_preserves_known_order_cleanup(self):
        session = self.session(dry=False, duration=20, authorized=True)
        original_start = session._start

        async def start():
            await original_start()
            original_refresh = session.execution.refresh_quote

            async def refresh(exposure):
                if self.adapter.creates:
                    raise TimeoutError("pre-mutation account read timed out")
                return await original_refresh(exposure)

            session.execution.refresh_quote = refresh

        session._start = start
        result = await session.run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual(result.final_account.open_order_ids, ())
        self.assertEqual(result.final_account.position, D("0"))
        self.assertEqual(self.adapter.disconnections, 1)
        self.assertNotIn("IOC", self.adapter.created_tifs)

        faults = [event for event in self.events if type(event) is FailureDiagnostic]
        self.assertEqual(faults[0].stage, "reconciling_quotes")
        self.assertEqual(faults[0].error_type, "TimeoutError")
        self.assertTrue(faults[0].source)
        # The second-side refresh fails after the first quote is already live.
        self.assertEqual(faults[0].order_states, ("buy:live:10",))

    async def test_diagnostic_sink_failure_does_not_interrupt_cleanup(self):
        session = self.session(dry=False, duration=20, authorized=True)
        original_start = session._start

        def emit(event):
            if type(event) is FailureDiagnostic:
                raise OSError("private-sink-detail")
            self.events.append(event)

        async def start():
            await original_start()
            original_refresh = session.execution.refresh_quote

            async def refresh(exposure):
                if self.adapter.creates:
                    raise TimeoutError("private-provider-detail")
                return await original_refresh(exposure)

            session.execution.refresh_quote = refresh

        session._start, session.telemetry = start, NS(emit=emit)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = await session.run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertEqual(self.adapter.cancels, self.adapter.creates)
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual(self.adapter.disconnections, 1)
        self.assertNotIn("private-", repr(self.events))

    async def test_cancel_account_failure_preserves_cause_before_blocked_exit(self):
        session = self.session(dry=False, authorized=True)
        original_snapshot = session.snapshot

        async def snapshot(*, exiting=False):
            if exiting:
                raise ValueError("private-account-detail")
            return await original_snapshot(exiting=exiting)

        session.snapshot = snapshot
        result = await session.run(asyncio.Event())
        faults = [event for event in self.events if type(event) is FailureDiagnostic]
        self.assertFalse(result.completed)
        self.assertEqual((faults[0].stage, faults[0].error_type), ("cancel_managed_orders", "ValueError"))
        self.assertEqual(faults[1].stage, "exit_account")
        self.assertTrue(faults[0].source)
        self.assertNotIn("private-account-detail", repr(self.events))
        self.assertEqual(self.adapter.cancels, self.adapter.creates)
        self.assertNotIn("IOC", self.adapter.created_tifs)
        self.assertEqual(self.adapter.disconnections, 1)

    async def test_exit_reconciles_pending_receipt_before_known_order_cleanup(self):
        from core.services.market_maker_v2.domain import ExecutionStatus
        from core.services.market_maker_v2.execution_models import OrderSlotState

        session = self.session(dry=False, duration=20, authorized=True)
        original_start = session._start
        failed_at = []

        async def start():
            await original_start()
            original_reconcile = session.execution.reconcile_quotes

            async def reconcile(plan):
                result = await original_reconcile(plan)
                # The exchange has the order; only its local receipt is pending.
                for slot in session.manager._slots.values():
                    if slot is not None:
                        slot.state = OrderSlotState.SUBMITTING
                failed_at.append(self.clock.now)
                return replace(result, status=ExecutionStatus.BLOCKED,
                               snapshot=session.execution.snapshot())

            session.execution.reconcile_quotes = reconcile

        session._start = start
        result = await session.run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual(self.adapter.creates, self.adapter.cancels)
        self.assertEqual(result.final_account.open_order_ids, ())
        self.assertEqual(result.final_account.position, D("0"))
        self.assertNotIn("IOC", self.adapter.created_tifs)
        self.assertLess(self.clock.now - failed_at[0], 30)

    async def test_exit_does_not_promote_unknown_submission_to_known_cleanup(self):
        from core.services.market_maker_v2.domain import ExecutionStatus

        session = self.session(dry=False, duration=20, authorized=True)
        original_start = session._start

        async def start():
            await original_start()
            original_reconcile = session.execution.reconcile_quotes

            async def reconcile(plan):
                result = await original_reconcile(plan)
                session.manager._mark_submission_uncertain(OrderSide.BUY, "unproven receipt")
                self.adapter.get_unresolved_submissions = lambda: [{
                    "symbol": "BTC", "client_order_id": "unmatched-submission"}]
                return replace(result, status=ExecutionStatus.BLOCKED,
                               snapshot=session.execution.snapshot())

            session.execution.reconcile_quotes = reconcile

        session._start = start
        result = await session.run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertEqual(self.adapter.cancels, 0)
        self.assertNotIn("IOC", self.adapter.created_tifs)
        self.assertTrue(session.manager.has_uncertain_state)
        self.assertTrue(result.final_account.open_order_ids)

    async def test_exit_recovers_delayed_terminal_proof_without_resubmitting(self):
        await self._exit_after_missing_terminal(proof_arrives=True)

    async def test_exit_stays_blocked_without_terminal_proof(self):
        await self._exit_after_missing_terminal(proof_arrives=False)

    async def _exit_after_missing_terminal(self, *, proof_arrives):
        from core.services.market_maker_v2.domain import ExecutionStatus

        session = self.session(dry=False, duration=20, authorized=True)
        original_start = session._start
        original_history = self.adapter.get_order_history
        missing_id, history_reads, failed_at = None, 0, None

        async def history(*args, **kwargs):
            nonlocal history_reads
            rows = await original_history(*args, **kwargs)
            if missing_id is not None:
                history_reads += 1
                if history_reads == 1 or not proof_arrives:
                    return [row for row in rows if row.id != missing_id]
            return rows

        async def start():
            await original_start()
            original_reconcile = session.execution.reconcile_quotes

            async def reconcile(plan):
                nonlocal missing_id, failed_at
                result = await original_reconcile(plan)
                sold = next(row for row in self.adapter.orders if row.side is OrderSide.SELL)
                self.adapter.fill(sold)
                missing_id, failed_at = sold.id, self.clock.now
                session.account.begin_quote_cycle()
                await session.manager.sync_open_orders()
                self.assertTrue(session.manager.has_uncertain_state)
                return replace(result, status=ExecutionStatus.BLOCKED,
                               snapshot=session.execution.snapshot())

            session.execution.reconcile_quotes = reconcile

        self.adapter.get_order_history = history
        session._start = start
        result = await session.run(asyncio.Event())
        self.assertFalse(result.completed)  # Cleanup does not turn a failed run into success.
        self.assertLess(self.clock.now - failed_at, 30)
        self.assertEqual(self.adapter.created_tifs.count("POST_ONLY"), 2)
        if proof_arrives:
            self.assertEqual(self.adapter.cancels, 1)
            self.assertEqual(self.adapter.created_tifs.count("IOC"), 1)
            self.assertEqual(result.final_account.open_order_ids, ())
            self.assertEqual(result.final_account.position, D("0"))
            self.assertFalse(session.manager.has_uncertain_state)
        else:
            self.assertEqual(history_reads, 2, "one bounded recovery read only")
            self.assertEqual(self.adapter.cancels, 0)
            self.assertNotIn("IOC", self.adapter.created_tifs)
            self.assertTrue(session.manager.has_uncertain_state)
            faults = [event for event in self.events if type(event) is FailureDiagnostic]
            self.assertEqual(faults[0].stage, "reconciling_quotes")
            self.assertEqual(faults[1].stage, "exit_health")
            self.assertTrue(faults[0].uncertain and faults[1].uncertain)
            self.assertIn("sell:uncertain_submission:11", faults[0].order_states)

    async def test_passive_read_refusal_still_cancels_and_exits_in_original_budget(self):
        session = self.session(dry=False, duration=2, authorized=True, passive=10)
        filled = failed = False

        async def advance(seconds):
            nonlocal filled
            self.clock.now += float(seconds)
            if self.adapter.orders and not filled and session._passive_until is None:
                self.adapter.fill(self.adapter.orders[0])
                filled = True
            await asyncio.sleep(0)

        original_snapshot = session.snapshot

        async def snapshot(*, exiting=False):
            nonlocal failed
            if (not failed and session._passive_until is not None
                    and any(row.params.get("reduce_only") for row in self.adapter.orders)):
                failed = True
                raise ValueError("one-shot passive read refusal")
            return await original_snapshot(exiting=exiting)

        session.sleep, session.snapshot = advance, snapshot
        result = await session.run(asyncio.Event())
        self.assertTrue(filled and failed)
        self.assertTrue(result.completed, result.failure)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual(self.adapter.created_tifs.count("IOC"), 1)
        self.assertEqual(result.report.forced_flatten_count, 1)
        self.assertLess(self.adapter.created_records[-1][0], 34)

    async def test_nonzero_passive_grace_only_reduces_then_ioc_inside_original_deadline(self):
        filled = False
        async def fill_then_advance(seconds):
            nonlocal filled
            if not filled:
                buys = [row for row in self.adapter.orders if row.side == OrderSide.BUY]
                if buys:
                    self.adapter.fill(buys[0])
                    filled = True
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        result = await self.session(dry=False, authorized=True, passive=2,
                                    sleep=fill_then_advance).run(asyncio.Event())
        self.assertTrue(result.completed, result.failure)
        passive = [row for row in self.adapter.created_records if row[2] and row[3] == "POST_ONLY"]
        self.assertTrue(passive)
        self.assertTrue(all(row[1] == OrderSide.SELL and row[4] == D("101") for row in passive))
        ioc = [row for row in self.adapter.created_records if row[3] == "IOC"]
        self.assertEqual(len(ioc), 1)
        self.assertGreaterEqual(ioc[0][0] - passive[0][0], 1.8)
        self.assertLess(ioc[0][0], 34)
        self.assertTrue(result.report.complete)
        self.assertEqual(result.report.forced_flatten_count, 1)

    async def test_operator_stop_during_read_keeps_original_exit_deadline(self):
        stop = asyncio.Event()
        original_read = self.adapter.get_account_fee_and_funding
        observed = []
        async def stop_in_read(symbol, limit):
            if self.adapter.creates and not observed:
                stop.set()
                await asyncio.sleep(0)
                observed.append(self.clock.now)
                self.clock.now += 8
            return await original_read(symbol, limit)
        self.adapter.get_account_fee_and_funding = stop_in_read
        original_exit = orchestrator.bounded_exit
        deadlines = []
        async def capture_exit(*args, **kwargs):
            deadlines.append(kwargs["deadline_monotonic"])
            return await original_exit(*args, **kwargs)
        with patch.object(orchestrator, "bounded_exit", capture_exit):
            result = await self.session(dry=False, duration=60, authorized=True).run(stop)
        self.assertTrue(observed)
        self.assertTrue(result.completed, result.failure)
        self.assertEqual(len(deadlines), 1)
        self.assertLessEqual(deadlines[0], observed[0] + 30.01)
        self.assertLess(deadlines[0] - self.clock.now, 23)
        self.assertEqual(result.final_account.open_order_ids, ())

    async def test_live_book_failure_still_cancels_and_proves_already_flat(self):
        async def break_then_advance(seconds):
            self.adapter.read_failure = True
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        result = await self.session(dry=False, authorized=True,
                                    sleep=break_then_advance).run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertGreater(self.adapter.cancels, 0)
        self.assertIsNotNone(result.final_account)
        self.assertEqual(result.final_account.open_order_ids, ())
        self.assertEqual(result.final_account.position, D("0"))
        self.assertTrue(result.report.complete)
        self.assertNotIn("IOC", self.adapter.created_tifs)

    async def test_nonflat_book_failure_never_sends_ioc_without_fresh_price(self):
        async def fill_break_then_advance(seconds):
            buys = [row for row in self.adapter.orders if row.side == OrderSide.BUY]
            if buys:
                self.adapter.fill(buys[0])
            self.adapter.read_failure = True
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        result = await self.session(dry=False, authorized=True,
                                    sleep=fill_break_then_advance).run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertFalse(result.report.complete)
        self.assertNotIn("IOC", self.adapter.created_tifs)
        self.assertEqual(self.adapter.disconnections, 1)

    async def test_transport_loss_uses_exit_only_rest_to_cancel_and_prove_flat(self):
        session = None
        detached = []
        async def close_stream():
            detached.append(True)
        async def fail_then_advance(seconds):
            session.account.stream.transport_healthy = False
            self.adapter.read_failure = True
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        self.adapter.close_read_stream = close_stream
        session = self.session(dry=False, authorized=True, sleep=fail_then_advance)
        result = await session.run(asyncio.Event())
        self.assertFalse(result.completed)
        self.assertEqual(detached, [True])
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertNotIn("IOC", self.adapter.created_tifs)


if __name__ == "__main__":
    unittest.main()
