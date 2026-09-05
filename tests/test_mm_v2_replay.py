"""The eight §11.2 scenario replays: synthetic fills, never an economic GO."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS
import unittest

from core.adapters.exchanges.models import OrderSide, OrderStatus
from core.services.market_maker_v2.domain import BoundedExitReport, QuotePlan
from core.services.market_maker_v2.orchestrator import VolumeSession
from test_mm_v2_session_runner import ADDRESS, RuntimeAdapter, RuntimeClock, config


class ReplayAdapter(RuntimeAdapter):
    """Only scenario mechanics extend the existing offline exchange fixture."""

    def __init__(self):
        super().__init__()
        self.partial_ioc = False
        self.cancel_race = False
        self.ioc_fills = []
        self.race_ids = []

    def move(self, mid):
        mid = D(mid)
        self.book.bids = [NS(price=mid - 1, size=D("5"))]
        self.book.asks = [NS(price=mid + 1, size=D("5"))]
        position = D(self.position.position) * self.position.sign
        self.position.unrealized_pnl = str(position * (mid - D(self.position.avg_entry_price)))

    def fill(self, order, *, role="maker", price=None):
        original = order
        before = D(self.position.position) * self.position.sign
        if role == "taker":
            price = (self.book.asks[0].price if order.side == OrderSide.BUY else self.book.bids[0].price)
            if self.partial_ioc:
                self.partial_ioc = False
                order = replace(order, amount=order.amount / 2, remaining=order.remaining / 2)
        super().fill(order, role=role, price=price)
        if order.amount != original.amount:
            self.history[order.id] = replace(self.history[order.id], amount=original.amount,
                filled=order.amount, remaining=original.amount - order.amount, status=OrderStatus.CANCELED)
        if role == "taker":
            after = D(self.position.position) * self.position.sign
            self.ioc_fills.append((before, after, original.price, order.remaining, order.side))
        self.move((self.book.bids[0].price + self.book.asks[0].price) / 2)

    async def cancel_order(self, identifier, symbol):
        order = self.history[identifier]
        if self.cancel_race and order.side == OrderSide.BUY:
            self.cancel_race = False
            self.race_ids.append(identifier)
            self.fill(order)
            return self.history[identifier]
        return await super().cancel_order(identifier, symbol)


class ScenarioReplayTests(unittest.IsolatedAsyncioTestCase):
    async def replay(self, hook, *, duration=6, size="0.1", passive=0, stop_loss="2", skew="2"):
        self.adapter, self.clock, self.events = ReplayAdapter(), RuntimeClock(), []
        self.adapter.clock = self.clock
        cfg = config(dry=False, duration=duration)
        cfg = replace(cfg, quote=replace(cfg.quote, order_size=D(size)),
            inventory=replace(cfg.inventory, soft_limit=D("0.15"), hard_limit=D("0.4"), skew_bps_at_hard=D(skew)),
            flatten=replace(cfg.flatten, passive_grace_seconds=passive, stop_loss_usdg=D(stop_loss)),
            session=replace(cfg.session, cooldown_seconds=1))
        step = 0
        async def advance(seconds):
            nonlocal step
            await hook(step)
            step += 1
            self.clock.now += float(seconds)
            await asyncio.sleep(0)
        session = VolumeSession(cfg, self.adapter, account_index=7, expected_l1_address=ADDRESS,
            authorize_bounded_flatten=True, telemetry=NS(emit=self.events.append),
            clock=self.clock, sleep=advance)
        return await session.run(asyncio.Event())

    def fill_side(self, side):
        orders = [row for row in self.adapter.orders if row.side == side]
        self.assertTrue(orders, "scenario requires a real accepted maker quote")
        self.adapter.fill(orders[0])

    def assert_flat_complete(self, result):
        self.assertTrue(result.completed, result.failure)
        self.assertTrue(result.report.complete)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertEqual(self.adapter.disconnections, 1)
        self.assertEqual(result.report.all_in_net_pnl,
            result.report.realized_gross_pnl - result.report.maker_fee - result.report.taker_fee + result.report.funding)

    async def test_calm_mean_reverting_continues_quoting_after_natural_flat(self):
        async def hook(step):
            if step == 0:
                self.fill_side(OrderSide.BUY)
                self.adapter.move("101")
            elif step == 1:
                self.fill_side(OrderSide.SELL)
                self.adapter.move("100")
        result = await self.replay(hook, duration=12)
        self.assert_flat_complete(result)
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (2, 0))
        self.assertGreater(result.report.quote_uptime_seconds, D("2"))
        self.assertGreater(result.report.two_sided_quote_seconds, D("0"))
        self.assertEqual(result.report.quote_uptime_seconds,
                         result.report.buy_quote_seconds + result.report.sell_quote_seconds
                         - result.report.two_sided_quote_seconds)
        self.assertGreater(sum(isinstance(event, QuotePlan) and bool(event.quotes) for event in self.events), 2)

    async def adverse(self, opening, mid):
        skewed = []
        async def hook(step):
            if step == 0:
                self.fill_side(opening)
            elif step == 1:
                skewed.extend(self.adapter.orders)
                # The fixture has no public matching engine. Confirm withdrawal
                # of the increasing quote before jumping the external book through it.
                for row in self.adapter.orders[:]:
                    if row.side == opening:
                        await self.adapter.cancel_order(row.id, "BTC")
                self.adapter.move(mid)
        result = await self.replay(hook, duration=8, size="0.2", passive=1, stop_loss="0.2", skew="4")
        self.assert_flat_complete(result)
        self.assertTrue(skewed)
        self.assertTrue(all(row.amount <= D("0.1") for row in skewed if row.side == opening))
        reducing = OrderSide.BUY if opening == OrderSide.SELL else OrderSide.SELL
        self.assertTrue(any(row.side == reducing and row.price == D("100") for row in skewed))
        passive = [row for row in self.adapter.created_records if row[2] and row[3] == "POST_ONLY"]
        self.assertTrue(passive)
        self.assertTrue(all(row[1] == reducing for row in passive))
        self.assertEqual(result.report.taker_fill_count, 1)
        self.assertLess(result.report.all_in_net_pnl, D("0"))
        self.assertGreater(result.report.forced_flatten_loss, D("0"))

    async def test_short_then_one_way_rise_skews_reduces_and_accepts_exit_loss(self):
        await self.adverse(OrderSide.SELL, "103")

    async def test_long_then_one_way_fall_skews_reduces_and_accepts_exit_loss(self):
        await self.adverse(OrderSide.BUY, "97")

    async def test_oscillating_high_fill_has_whole_session_accounting(self):
        async def hook(step):
            if step < 8:
                self.fill_side(OrderSide.BUY if step % 2 == 0 else OrderSide.SELL)
                self.adapter.move("101" if step % 2 == 0 else "100")
        result = await self.replay(hook, duration=27)
        self.assert_flat_complete(result)
        self.assertEqual((result.report.maker_fill_count, result.report.taker_fill_count), (8, 0))
        self.assertGreater(result.report.maker_turnover_total, D("70"))
        self.assertGreater(result.report.maker_turnover_per_quote_hour, D("0"))
        self.assertEqual(result.report.forced_flatten_loss, D("0"))

    async def test_stale_book_stops_quotes_and_flat_postflight_needs_no_price(self):
        created = []
        async def hook(step):
            if step == 0:
                created.append(self.adapter.creates)
                self.adapter.book_age = 4
        result = await self.replay(hook)
        self.assertFalse(result.completed)
        self.assertEqual(self.adapter.creates, created[0])
        self.assertGreater(self.adapter.cancels, 0)
        self.assertEqual((result.final_account.position, result.final_account.open_order_ids), (D("0"), ()))
        self.assertNotIn("IOC", self.adapter.created_tifs)

    async def test_cancel_fill_race_counts_fill_once_and_exits_its_residual(self):
        async def hook(step):
            if step == 0:
                self.adapter.cancel_race = True
        result = await self.replay(hook, duration=2)
        self.assert_flat_complete(result)
        self.assertEqual(len(self.adapter.race_ids), 1)
        self.assertEqual(result.report.maker_fill_count, 1)
        self.assertEqual(result.report.taker_fill_count, 1)
        self.assertEqual(sum(row.order_id == self.adapter.race_ids[0] for row in self.adapter.trades), 1)

    async def test_session_deadline_nonflat_does_not_wait_for_natural_flat(self):
        async def hook(step):
            if step == 0:
                self.fill_side(OrderSide.SELL)
        result = await self.replay(hook, duration=2)
        self.assert_flat_complete(result)
        self.assertEqual(result.report.taker_fill_count, 1)
        self.assertLess(result.report.duration_seconds, D("32"))
        self.assertLess(result.report.all_in_net_pnl, D("0"))

    async def test_ioc_partial_residual_uses_remaining_size_and_original_price_cap(self):
        async def hook(step):
            if step == 0:
                self.fill_side(OrderSide.BUY)
                self.adapter.partial_ioc = True
        result = await self.replay(hook, duration=2, size="0.2")
        self.assert_flat_complete(result)
        self.assertEqual(result.report.taker_fill_count, 2)
        first, second = self.adapter.ioc_fills
        self.assertEqual((first[0], first[1], second[0], second[1]), (D("0.2"), D("0.1"), D("0.1"), D("0")))
        self.assertEqual(first[2], second[2])
        exits = [event for event in self.events if isinstance(event, BoundedExitReport) and event.attempts]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].attempts, 2)
        self.assertLess(result.report.duration_seconds, D("32"))


if __name__ == "__main__":
    unittest.main()
