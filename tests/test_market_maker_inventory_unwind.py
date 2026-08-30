import unittest
from dataclasses import replace
from decimal import Decimal

from core.adapters.exchanges.models import OrderBookLevel, OrderSide
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.inventory_unwind import InventoryEpisodeExecutor
from core.services.market_maker.models import (
    MarketMetadata,
    MarketSnapshot,
    PositionSnapshot,
)


class InventoryEpisodeExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MarketMakerConfig(
            symbol="BTC",
            order_size="0.2",
            max_position="1",
            maker_fee_rate="0.00012",
            taker_fee_rate="0.0004",
            ping_pong_enabled=True,
            soft_exit_after_seconds=10,
            soft_exit_net_turnover_bps="-0.5",
            min_completed_net_turnover_bps="0.1",
            active_unwind_enabled=True,
            active_unwind_after_seconds=30,
            active_unwind_loss_trigger="0.20",
            active_unwind_max_slippage_ticks=2,
            active_unwind_max_attempts=2,
            active_unwind_confirmation_timeout_seconds=5,
            max_episode_loss_for_unwind="0.30",
            max_session_loss_for_unwind="0.40",
            max_session_loss_for_maker_exit="0.15",
            account_audit_interval_seconds=15,
            max_session_drawdown="0.50",
            require_flat_start=True,
        )
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("0"),
        )

    @staticmethod
    def position(
        size: str,
        *,
        entry: str = "100",
        unrealized_pnl: str = "0",
    ) -> PositionSnapshot:
        signed = Decimal(size)
        return PositionSnapshot(
            symbol="BTC",
            signed_size=signed,
            entry_price=Decimal(entry) if signed else None,
            unrealized_pnl=Decimal(unrealized_pnl),
            received_monotonic=0,
        )

    @staticmethod
    def market(bid: str = "100.5", ask: str = "100.6") -> MarketSnapshot:
        best_bid = Decimal(bid)
        best_ask = Decimal(ask)
        return MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(best_bid, Decimal("1")),),
            asks=(OrderBookLevel(best_ask, Decimal("1")),),
            best_bid=best_bid,
            best_ask=best_ask,
            exchange_timestamp=None,
            received_monotonic=0,
        )

    @staticmethod
    def account(position: str, **overrides):
        snapshot = {
            "state": "healthy",
            "age_seconds": 0.5,
            "ledger_position": Decimal(position),
            "completed_net_ex_funding": Decimal("0"),
            "last_flat_equity_change": Decimal("0"),
            "open_episode_net_ex_funding": Decimal("-0.0024"),
            "current_drawdown": Decimal("0.05"),
            "baseline_equity": Decimal("300"),
            "current_equity": Decimal("299.95"),
            "audited_position": Decimal(position),
            "audited_unrealized_pnl": Decimal("-0.05"),
        }
        snapshot.update(overrides)
        return snapshot

    def evaluate(
        self,
        executor: InventoryEpisodeExecutor,
        *,
        now: float,
        position: str = "-0.2",
        market: MarketSnapshot | None = None,
        account: dict | None = None,
        stranded: bool = True,
        authenticated_flat: bool = False,
        pending: bool = False,
        coordinator_unrealized_pnl: str = "0",
    ):
        return executor.evaluate(
            position=self.position(
                position,
                unrealized_pnl=coordinator_unrealized_pnl,
            ),
            market=market or self.market(),
            metadata=self.metadata,
            account_snapshot=account or self.account(position),
            now_monotonic=now,
            soft_exit_latched=position != "0",
            stranded_soft_exit=stranded,
            authenticated_flat=authenticated_flat,
            active_unwind_pending=pending,
            normal_passive_price=(
                (market or self.market()).best_ask
                if Decimal(position) > 0
                else (market or self.market()).best_bid
                if Decimal(position) < 0
                else None
            ),
        )

    def test_passive_budget_unlock_suppresses_then_chases_normal_band(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        early = self.evaluate(executor, now=0)
        self.assertTrue(early.suppress_passive)
        self.assertIsNone(early.passive_order)

        still_blocked = self.evaluate(executor, now=15)
        self.assertTrue(still_blocked.suppress_passive)
        self.assertEqual(
            still_blocked.unlocked_episode_loss, Decimal("0.0375")
        )

        reachable = self.evaluate(executor, now=25)
        self.assertFalse(reachable.suppress_passive)
        self.assertEqual(reachable.passive_order.side, OrderSide.BUY)
        self.assertEqual(reachable.passive_order.price, Decimal("100.5"))
        self.assertTrue(reachable.passive_order.reduce_only)

    def test_loss_and_time_barriers_create_bounded_long_short_ioc_intents(self) -> None:
        short = InventoryEpisodeExecutor(self.config)
        self.evaluate(short, now=0)
        loss_hit = self.evaluate(
            short,
            now=5,
            market=self.market("100.7", "100.8"),
        )
        self.assertEqual(loss_hit.trigger, "loss")
        self.assertEqual(loss_hit.active_order.side, OrderSide.BUY)
        self.assertEqual(loss_hit.active_order.price, Decimal("101.0"))
        self.assertEqual(loss_hit.active_order.amount, Decimal("0.2"))

        long = InventoryEpisodeExecutor(self.config)
        self.evaluate(
            long,
            now=0,
            position="0.2",
            market=self.market("99.9", "100.1"),
            account=self.account("0.2"),
        )
        timed = self.evaluate(
            long,
            now=30,
            position="0.2",
            market=self.market("99.9", "100.1"),
            account=self.account("0.2"),
        )
        self.assertEqual(timed.trigger, "time")
        self.assertEqual(timed.active_order.side, OrderSide.SELL)
        self.assertEqual(timed.active_order.price, Decimal("99.7"))
        self.assertTrue(timed.active_order.reduce_only)

    def test_caps_attempts_and_authenticated_flat_are_fail_closed(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0)
        blocked = self.evaluate(
            executor,
            now=30,
            market=self.market("101.5", "101.6"),
        )
        self.assertTrue(blocked.blocked)
        self.assertIn("episode loss", blocked.reason)

        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0)
        decision = self.evaluate(executor, now=30)
        self.assertIsNotNone(decision.active_order)
        executor.record_active_attempt()
        executor.record_active_attempt()
        exhausted = self.evaluate(executor, now=31)
        self.assertTrue(exhausted.blocked)
        self.assertIn("attempt", exhausted.reason)

        pending_flat = self.evaluate(
            executor,
            now=32,
            position="0",
            account=self.account("0"),
        )
        self.assertEqual(pending_flat.state, "flat_pending_audit")
        self.assertIsNotNone(pending_flat.episode_id)
        confirmed_flat = self.evaluate(
            executor,
            now=33,
            position="0",
            account=self.account("0"),
            authenticated_flat=True,
        )
        self.assertEqual(confirmed_flat.state, "flat")
        self.assertIsNone(confirmed_flat.episode_id)

    def test_stale_economics_and_pre_trigger_caps_do_not_mutate(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        early = self.evaluate(
            executor,
            now=0,
            account=self.account(
                "-0.2",
                completed_net_ex_funding=Decimal("-0.35"),
                last_flat_equity_change=Decimal("-0.35"),
            ),
        )
        self.assertFalse(early.blocked)
        self.assertIsNone(early.active_order)

        stale = self.evaluate(
            executor,
            now=30,
            account=self.account("-0.2", age_seconds=21.0),
        )
        self.assertTrue(stale.blocked)
        self.assertIn("fresh authenticated economics", stale.reason)

    def test_uses_same_generation_audited_unrealized_pnl(self) -> None:
        account = self.account(
            "-0.2", audited_unrealized_pnl=Decimal("-0.05")
        )
        high_executor = InventoryEpisodeExecutor(self.config)
        low_executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(high_executor, now=0, account=account)
        self.evaluate(low_executor, now=0, account=account)
        high = self.evaluate(
            high_executor,
            now=30,
            account=account,
            coordinator_unrealized_pnl="25",
        )
        low = self.evaluate(
            low_executor,
            now=30,
            account=account,
            coordinator_unrealized_pnl="-25",
        )

        self.assertEqual(high.state, "active_ready")
        self.assertEqual(high.active_order, low.active_order)
        self.assertEqual(
            high.projected_episode_loss, low.projected_episode_loss
        )
        self.assertEqual(
            high.projected_session_loss, low.projected_session_loss
        )

    def test_missing_or_mismatched_audited_truth_fails_closed(self) -> None:
        cases = {
            "missing position": {"audited_position": None},
            "missing unrealized": {"audited_unrealized_pnl": None},
            "position mismatch": {"audited_position": Decimal("0.2")},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                executor = InventoryEpisodeExecutor(self.config)
                self.evaluate(executor, now=0)
                decision = self.evaluate(
                    executor,
                    now=30,
                    account=self.account("-0.2", **overrides),
                )
                self.assertTrue(decision.blocked)
                self.assertIn(
                    "fresh authenticated economics", decision.reason
                )

    def test_decision_snapshot_exposes_executable_order_intent(self) -> None:
        passive_executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(passive_executor, now=0)
        passive = self.evaluate(passive_executor, now=25).snapshot()
        self.assertEqual(passive["order_lane"], "passive_post_only")
        self.assertEqual(passive["order_side"], "buy")
        self.assertEqual(passive["order_price"], Decimal("100.5"))
        self.assertEqual(passive["order_amount"], Decimal("0.2"))
        self.assertTrue(passive["order_reduce_only"])
        self.assertEqual(passive["order_time_in_force"], "POST_ONLY")

        active_executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(active_executor, now=0)
        active = self.evaluate(active_executor, now=30).snapshot()
        self.assertEqual(active["order_lane"], "active_ioc")
        self.assertEqual(active["order_side"], "buy")
        self.assertEqual(active["order_price"], Decimal("100.8"))
        self.assertEqual(active["order_amount"], Decimal("0.2"))
        self.assertTrue(active["order_reduce_only"])
        self.assertEqual(active["order_time_in_force"], "IOC")

    def test_invalid_position_market_and_metadata_fail_closed(self) -> None:
        cases = (
            (
                self.position("1.1"),
                self.market(),
                self.metadata,
                "inputs",
            ),
            (
                self.position("-0.2"),
                self.market("100.6", "100.6"),
                self.metadata,
                "market",
            ),
            (
                self.position("-0.2"),
                self.market(),
                replace(self.metadata, price_tick=Decimal("0")),
                "metadata",
            ),
        )
        for position, market, metadata, reason in cases:
            with self.subTest(reason=reason):
                decision = InventoryEpisodeExecutor(self.config).evaluate(
                    position=position,
                    market=market,
                    metadata=metadata,
                    account_snapshot=self.account("-0.2"),
                    now_monotonic=0,
                    soft_exit_latched=True,
                    stranded_soft_exit=True,
                    authenticated_flat=False,
                    active_unwind_pending=False,
                    normal_passive_price=market.best_bid,
                )
                self.assertTrue(decision.blocked)
                self.assertIn(reason, decision.reason)


if __name__ == "__main__":
    unittest.main()
