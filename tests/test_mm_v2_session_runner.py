"""Bounded runner acceptance with real V2 providers and frozen order manager; no I/O."""

import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from core.services.market_maker_v2.config import (
    ConfigError, FlattenConfig, InventoryConfig, MarketMakerV2Config, QuoteConfig, SessionConfig,
)
from core.services.market_maker_v2.domain import BoundedExitReport, ExitStatus, QuotePlan
from core.services.market_maker_v2 import orchestrator
from test_mm_v2_lighter_runtime import Adapter, ADDRESS, Clock, trade


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

    async def disconnect(self):
        self.disconnections += 1

    def enable_market_maker_cancellation_outcomes(self):
        pass

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

    async def test_unauthorized_live_rejected_before_connection(self):
        with self.assertRaises((ConfigError, ValueError)):
            session = self.session(dry=False)
            await session.run(asyncio.Event())
        self.assertEqual((self.adapter.connections, self.adapter.creates, self.adapter.cancels), (0, 0, 0))

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


if __name__ == "__main__":
    unittest.main()
