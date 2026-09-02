import unittest
from dataclasses import replace
from decimal import Decimal

from core.adapters.exchanges.models import OrderBookLevel, OrderSide
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.inventory_unwind import InventoryEpisodeExecutor
from core.services.market_maker.models import (
    ExitBindingConstraint,
    InventoryExitStage,
    MarketMetadata,
    MarketSnapshot,
    OrderIntentKind,
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
            "completed_turnover": Decimal("0"),
            "completed_net_ex_funding": Decimal("0"),
            "last_flat_equity_change": Decimal("0"),
            "open_episode_turnover": Decimal("20"),
            "open_episode_net_ex_funding": Decimal("-0.0024"),
            "current_drawdown": Decimal("0.05"),
            "baseline_equity": Decimal("300"),
            "current_equity": Decimal("299.95"),
            "audited_position": Decimal(position),
            "audited_unrealized_pnl": Decimal("-0.05"),
            "episode_sequence": 1,
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
        soft_exit_latched: bool | None = None,
        entry: str = "100",
        coordinator_unrealized_pnl: str = "0",
        metadata: MarketMetadata | None = None,
        normal_passive_price: Decimal | None = None,
    ):
        selected_market = market or self.market()
        return executor.evaluate(
            position=self.position(
                position,
                entry=entry,
                unrealized_pnl=coordinator_unrealized_pnl,
            ),
            market=selected_market,
            metadata=metadata or self.metadata,
            account_snapshot=account or self.account(position),
            now_monotonic=now,
            soft_exit_latched=(
                position != "0"
                if soft_exit_latched is None
                else soft_exit_latched
            ),
            stranded_soft_exit=stranded,
            authenticated_flat=authenticated_flat,
            active_unwind_pending=pending,
            normal_passive_price=(
                normal_passive_price
                if normal_passive_price is not None
                else selected_market.best_ask
                if Decimal(position) > 0
                else selected_market.best_bid
                if Decimal(position) < 0
                else None
            ),
        )

    def gate_c_config(self, **overrides) -> MarketMakerConfig:
        values = {
            "order_size": Decimal("0.00020"),
            "max_position": Decimal("0.00040"),
            "active_unwind_enabled": False,
            "soft_exit_after_seconds": 120,
            "active_unwind_after_seconds": 180,
            "min_completed_net_turnover_bps": Decimal("0.02"),
            "soft_exit_surplus_reserve_bps": Decimal("0.02"),
            "max_episode_loss_for_unwind": Decimal("0.075"),
            "max_session_loss_for_unwind": Decimal("0.10"),
            "max_session_loss_for_maker_exit": Decimal("0"),
        }
        values.update(overrides)
        return replace(self.config, **values)

    def gate_c_account(self, **overrides) -> dict:
        values = {
            "completed_turnover": Decimal("123.670880"),
            "completed_net_ex_funding": Decimal("0.00795949440"),
            "last_flat_equity_change": Decimal("0.00795949440"),
            "open_episode_turnover": Decimal("15.441480"),
            "open_episode_net_ex_funding": Decimal("-0.001852977600"),
            "current_drawdown": Decimal("0"),
            "current_equity": Decimal("300"),
            "audited_unrealized_pnl": Decimal("0"),
        }
        values.update(overrides)
        return self.account("-0.00020", **values)

    def gate_c_metadata(self) -> MarketMetadata:
        return replace(
            self.metadata,
            size_decimals=5,
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00001"),
        )

    def gate_c_decision(
        self,
        *,
        config: MarketMakerConfig | None = None,
        account: dict | None = None,
        now: float = 180,
        soft_exit_latched: bool = True,
        stranded: bool = True,
    ):
        executor = InventoryEpisodeExecutor(config or self.gate_c_config())
        market = self.market("77230.0", "77230.2")
        snapshot = account or self.gate_c_account()
        metadata = self.gate_c_metadata()
        self.evaluate(
            executor,
            now=0,
            position="-0.00020",
            entry="77207.4",
            market=market,
            account=snapshot,
            metadata=metadata,
            stranded=False,
            soft_exit_latched=False,
        )
        decision = self.evaluate(
            executor,
            now=now,
            position="-0.00020",
            entry="77207.4",
            market=market,
            account=snapshot,
            metadata=metadata,
            normal_passive_price=Decimal("77230.0"),
            soft_exit_latched=soft_exit_latched,
            stranded=stranded,
        )
        return decision, metadata

    def test_passive_budget_unlock_suppresses_then_chases_normal_band(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        early = self.evaluate(executor, now=0)
        self.assertEqual(early.state, "unwind_blocked")
        self.assertFalse(early.suppress_passive)
        self.assertEqual(early.passive_order.price, Decimal("99.9"))

        still_blocked = self.evaluate(executor, now=15)
        self.assertEqual(still_blocked.state, "unwind_blocked")
        self.assertFalse(still_blocked.suppress_passive)
        self.assertEqual(
            still_blocked.passive_order.price, Decimal("100.3")
        )
        self.assertEqual(
            still_blocked.unlocked_episode_loss, Decimal("0.0750")
        )

        reachable = self.evaluate(executor, now=25)
        self.assertFalse(reachable.suppress_passive)
        self.assertEqual(reachable.passive_order.side, OrderSide.BUY)
        self.assertEqual(reachable.passive_order.price, Decimal("100.5"))
        self.assertTrue(reachable.passive_order.reduce_only)

    def test_sub_min_residual_uses_executable_reduce_only_amount(self) -> None:
        metadata = replace(
            self.metadata,
            size_decimals=5,
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("10"),
        )
        base_config = replace(
            self.config,
            order_size=Decimal("0.00020"),
            max_position=Decimal("0.00040"),
        )
        market = self.market("77230.0", "77230.2")
        account = self.account(
            "-0.00010",
            open_episode_turnover=Decimal("7.72074"),
            open_episode_net_ex_funding=Decimal("-0.0009264888"),
            audited_unrealized_pnl=Decimal("0"),
        )

        passive = InventoryEpisodeExecutor(
            replace(base_config, active_unwind_enabled=False)
        )
        passive_decision = self.evaluate(
            passive,
            now=15,
            position="-0.00010",
            entry="77207.4",
            market=market,
            account=account,
            metadata=metadata,
            normal_passive_price=Decimal("77230.0"),
            soft_exit_latched=True,
        )
        self.assertIsNotNone(passive_decision.passive_order)
        self.assertEqual(
            passive_decision.passive_order.amount, Decimal("0.00020")
        )
        self.assertTrue(passive_decision.passive_order.reduce_only)

        active = InventoryEpisodeExecutor(base_config)
        self.evaluate(
            active,
            now=0,
            position="-0.00010",
            entry="77207.4",
            market=market,
            account=account,
            metadata=metadata,
            soft_exit_latched=False,
        )
        active_decision = self.evaluate(
            active,
            now=30,
            position="-0.00010",
            entry="77207.4",
            market=market,
            account=account,
            metadata=metadata,
            soft_exit_latched=False,
        )
        self.assertIsNotNone(active_decision.active_order)
        self.assertEqual(
            active_decision.active_order.amount, Decimal("0.00020")
        )
        self.assertTrue(active_decision.active_order.reduce_only)

    def test_active_off_gate_c_session_boundary_is_exact_to_one_tick(
        self,
    ) -> None:
        decision, metadata = self.gate_c_decision()

        self.assertEqual(decision.state, "unwind_blocked")
        self.assertIsNone(decision.active_order)
        self.assertIsNotNone(decision.passive_order)
        self.assertEqual(decision.passive_order.side, OrderSide.BUY)
        self.assertEqual(decision.passive_order.amount, Decimal("0.00020"))
        self.assertTrue(decision.passive_order.reduce_only)
        boundary = decision.passive_order.price
        self.assertEqual(boundary, Decimal("77225.8"))

        quantity = Decimal("0.00020")
        entry = Decimal("77207.4")
        maker_fee = Decimal("0.000120")
        minimum_rate = Decimal("0.02") / Decimal("10000")
        completed_turnover = Decimal("123.670880")
        completed_net = Decimal("0.00795949440")
        open_turnover = Decimal("15.441480")
        open_net = Decimal("-0.001852977600")
        reserve = (
            Decimal("0.02") / Decimal("10000") * completed_turnover
        )

        def projected_net(price: Decimal) -> Decimal:
            return (
                completed_net
                + open_net
                + quantity * (entry - price)
                - quantity * price * maker_fee
            )

        def required_floor(price: Decimal) -> Decimal:
            final_turnover = (
                completed_turnover + open_turnover + quantity * price
            )
            return minimum_rate * final_turnover + reserve

        self.assertGreaterEqual(
            projected_net(boundary), required_floor(boundary)
        )
        next_tick = boundary + metadata.price_tick
        self.assertLess(
            projected_net(next_tick), required_floor(next_tick)
        )

    def test_long_surplus_nonpositive_denominator_fails_closed(self) -> None:
        account = self.account(
            "0.2",
            completed_turnover=Decimal("100"),
            completed_net_ex_funding=Decimal("101"),
            last_flat_equity_change=Decimal("101"),
            open_episode_turnover=Decimal("20"),
            open_episode_net_ex_funding=Decimal("0"),
            current_drawdown=Decimal("0"),
            current_equity=Decimal("300"),
            audited_unrealized_pnl=Decimal("0"),
        )
        for name, minimum_bps in (
            ("zero", "9998.8"),
            ("negative", "9998.9"),
        ):
            with self.subTest(name=name):
                executor = InventoryEpisodeExecutor(
                    replace(
                        self.config,
                        active_unwind_enabled=False,
                        max_session_loss_for_maker_exit=Decimal("0"),
                        min_completed_net_turnover_bps=Decimal(minimum_bps),
                    )
                )
                self.evaluate(
                    executor,
                    now=0,
                    position="0.2",
                    account=account,
                    stranded=False,
                    soft_exit_latched=False,
                )

                decision = self.evaluate(
                    executor,
                    now=15,
                    position="0.2",
                    account=account,
                    soft_exit_latched=True,
                )

                self.assertEqual(decision.state, "blocked")
                self.assertTrue(decision.blocked)
                self.assertTrue(decision.suppress_passive)
                self.assertIsNone(decision.passive_order)
                self.assertIn("denominator", decision.reason)

    def test_gate_c_episode_cap_is_exact_to_one_tick(self) -> None:
        config = self.gate_c_config(
            max_episode_loss_for_unwind=Decimal("0.005")
        )
        decision, metadata = self.gate_c_decision(config=config)

        self.assertIsNotNone(decision.passive_order)
        boundary = decision.passive_order.price
        self.assertEqual(boundary, Decimal("77213.8"))

        quantity = Decimal("0.00020")
        entry = Decimal("77207.4")
        maker_fee = Decimal("0.000120")
        open_net = Decimal("-0.001852977600")

        def projected_episode_net(price: Decimal) -> Decimal:
            return (
                open_net
                + quantity * (entry - price)
                - quantity * price * maker_fee
            )

        self.assertGreaterEqual(
            projected_episode_net(boundary), Decimal("-0.005")
        )
        self.assertLess(
            projected_episode_net(boundary + metadata.price_tick),
            Decimal("-0.005"),
        )

    def test_untrusted_economics_never_relaxes_passive_boundary(self) -> None:
        cases = {
            "missing turnover": self.gate_c_account(),
            "stale audit": self.gate_c_account(age_seconds=21.0),
        }
        cases["missing turnover"].pop("completed_turnover")

        for name, account in cases.items():
            with self.subTest(name=name):
                decision, _ = self.gate_c_decision(account=account)

                self.assertIsNone(decision.passive_order)
                self.assertIsNone(decision.active_order)
                self.assertTrue(decision.suppress_passive)
                self.assertEqual(decision.state, "passive_wait")

    def test_passive_budget_does_not_progress_before_soft_exit_latch(self) -> None:
        before_latch, _ = self.gate_c_decision(
            now=150,
            stranded=False,
            soft_exit_latched=False,
        )

        self.assertEqual(before_latch.unlocked_episode_loss, Decimal("0"))
        self.assertIsNone(before_latch.passive_order)
        self.assertIsNone(before_latch.active_order)

    def test_loss_and_time_barriers_create_bounded_long_short_ioc_intents(self) -> None:
        short = InventoryEpisodeExecutor(self.config)
        self.evaluate(short, now=0, soft_exit_latched=False)
        loss_hit = self.evaluate(
            short,
            now=5,
            market=self.market("100.7", "100.8"),
            soft_exit_latched=False,
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
            soft_exit_latched=False,
        )
        timed = self.evaluate(
            long,
            now=30,
            position="0.2",
            market=self.market("99.9", "100.1"),
            account=self.account("0.2"),
            soft_exit_latched=False,
        )
        self.assertEqual(timed.trigger, "time")
        self.assertEqual(timed.active_order.side, OrderSide.SELL)
        self.assertEqual(timed.active_order.price, Decimal("99.7"))
        self.assertTrue(timed.active_order.reduce_only)

    def test_profit_exit_remains_when_no_loss_or_time_barrier_fires(self) -> None:
        decision = self.evaluate(
            InventoryEpisodeExecutor(self.config),
            now=0,
            soft_exit_latched=False,
        )

        self.assertEqual(decision.state, "profit_exit")
        self.assertEqual(decision.reason, "strict fee-aware profit exit")
        self.assertIsNone(decision.trigger)
        self.assertIsNone(decision.active_order)

    def test_pre_latch_untrusted_economics_waits_until_time_barrier(self) -> None:
        cases = (
            ("missing", {"state": "healthy"}, 30),
            ("stale", self.account("-0.2", age_seconds=21.0), 31),
        )
        for name, account, barrier_now in cases:
            with self.subTest(name=name):
                executor = InventoryEpisodeExecutor(self.config)
                waiting = self.evaluate(
                    executor,
                    now=0,
                    account=account,
                    stranded=False,
                    soft_exit_latched=False,
                )
                blocked = self.evaluate(
                    executor,
                    now=barrier_now,
                    account=account,
                    stranded=False,
                    soft_exit_latched=False,
                )

                self.assertEqual(waiting.state, "passive_wait")
                self.assertFalse(waiting.blocked)
                self.assertTrue(waiting.suppress_passive)
                self.assertIn("unavailable", waiting.reason)
                self.assertEqual(blocked.state, "blocked")
                self.assertTrue(blocked.blocked)
                self.assertIn("fresh authenticated economics", blocked.reason)

    def test_pre_latch_unprovable_loss_exit_fails_closed(self) -> None:
        cases = (
            ("invalid entry", {"entry": "0"}, "entry price"),
            (
                "unusable slippage",
                {
                    "position": "0.2",
                    "market": self.market("0.1", "0.2"),
                    "account": self.account("0.2"),
                },
                "slippage price",
            ),
        )
        for name, overrides, reason in cases:
            with self.subTest(name=name):
                decision = self.evaluate(
                    InventoryEpisodeExecutor(self.config),
                    now=0,
                    stranded=False,
                    soft_exit_latched=False,
                    **overrides,
                )

                self.assertEqual(decision.state, "blocked")
                self.assertTrue(decision.blocked)
                self.assertIn(reason, decision.reason)

    def test_caps_attempts_and_authenticated_flat_are_fail_closed(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0)
        blocked = self.evaluate(
            executor,
            now=30,
            market=self.market("101.5", "101.6"),
        )
        self.assertTrue(blocked.blocked)
        self.assertTrue(blocked.budget_blocked)
        self.assertIn("episode loss", blocked.reason)

        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0)
        decision = self.evaluate(executor, now=30)
        self.assertIsNotNone(decision.active_order)
        executor.record_active_attempt()
        executor.record_active_attempt()
        exhausted = self.evaluate(executor, now=31)
        self.assertTrue(exhausted.blocked)
        self.assertFalse(exhausted.budget_blocked)
        self.assertIn("attempt", exhausted.reason)

        pending_flat = self.evaluate(
            executor,
            now=32,
            position="0",
            account=self.account("0"),
        )
        self.assertEqual(pending_flat.state, "flat_pending_audit")
        self.assertIsNotNone(pending_flat.episode_id)
        self.assertEqual(
            pending_flat.snapshot()["completed_episode_execution_history"], []
        )
        confirmed_flat = self.evaluate(
            executor,
            now=33,
            position="0",
            account=self.account("0"),
            authenticated_flat=True,
        )
        self.assertEqual(confirmed_flat.state, "flat")
        self.assertIsNone(confirmed_flat.episode_id)
        self.assertEqual(
            confirmed_flat.snapshot()["completed_episode_execution_history"],
            [
                {
                    "episode_id": 1,
                    "active_attempts": 2,
                    "last_active_trigger": "time",
                }
            ],
        )

    def test_completed_episode_history_records_loss_trigger(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0, soft_exit_latched=False)
        ready = self.evaluate(
            executor,
            now=5,
            market=self.market("100.7", "100.8"),
            soft_exit_latched=False,
        )
        self.assertEqual(ready.trigger, "loss")

        executor.record_active_attempt()
        completed = self.evaluate(
            executor,
            now=6,
            position="0",
            account=self.account("0"),
            authenticated_flat=True,
        )

        self.assertEqual(
            completed.snapshot()["completed_episode_execution_history"],
            [
                {
                    "episode_id": 1,
                    "active_attempts": 1,
                    "last_active_trigger": "loss",
                }
            ],
        )

    def test_audit_can_finalize_execution_history_before_runtime_stop(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0, soft_exit_latched=False)
        ready = self.evaluate(
            executor,
            now=5,
            market=self.market("100.7", "100.8"),
            soft_exit_latched=False,
        )
        self.assertEqual(ready.trigger, "loss")
        executor.record_active_attempt()

        self.assertTrue(executor.record_authenticated_flat())
        self.assertFalse(executor.record_authenticated_flat())
        published = executor.execution_snapshot()
        self.assertEqual(published["state"], "flat")
        self.assertIsNone(published["trigger"])
        self.assertIsNone(published["order_lane"])
        self.assertFalse(published["blocked"])
        completed = self.evaluate(
            executor,
            now=6,
            position="0",
            account=self.account("0"),
            authenticated_flat=True,
        )
        self.assertEqual(
            completed.snapshot()["completed_episode_execution_history"],
            [
                {
                    "episode_id": 1,
                    "active_attempts": 1,
                    "last_active_trigger": "loss",
                }
            ],
        )

    def test_completed_episode_history_is_bounded_and_resets_with_session(
        self,
    ) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        history = []
        for episode in range(101):
            self.evaluate(
                executor,
                now=float(episode * 2),
                soft_exit_latched=False,
            )
            completed = self.evaluate(
                executor,
                now=float(episode * 2 + 1),
                position="0",
                account=self.account("0"),
                authenticated_flat=True,
            )
            history = completed.snapshot()[
                "completed_episode_execution_history"
            ]

        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["episode_id"], 2)
        self.assertEqual(history[-1]["episode_id"], 101)
        self.assertEqual(history[-1]["active_attempts"], 0)
        self.assertIsNone(history[-1]["last_active_trigger"])

        executor.reset_session()
        reset = self.evaluate(
            executor,
            now=203,
            position="0",
            account=self.account("0"),
            authenticated_flat=True,
        )
        self.assertEqual(
            reset.snapshot()["completed_episode_execution_history"], []
        )

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

    def test_flat_position_allows_absent_unrealized_pnl_only_when_flat(
        self,
    ) -> None:
        flat = InventoryEpisodeExecutor(self.config).evaluate(
            position=replace(self.position("0"), unrealized_pnl=None),
            market=self.market(),
            metadata=self.metadata,
            account_snapshot=self.account("0"),
            now_monotonic=0,
            soft_exit_latched=False,
            stranded_soft_exit=False,
            authenticated_flat=True,
            active_unwind_pending=False,
            normal_passive_price=None,
        )
        self.assertEqual(flat.state, "flat")
        self.assertFalse(flat.blocked)

        nonflat = InventoryEpisodeExecutor(self.config).evaluate(
            position=replace(self.position("-0.2"), unrealized_pnl=None),
            market=self.market(),
            metadata=self.metadata,
            account_snapshot=self.account("-0.2"),
            now_monotonic=0,
            soft_exit_latched=True,
            stranded_soft_exit=True,
            authenticated_flat=False,
            active_unwind_pending=False,
            normal_passive_price=self.market().best_bid,
        )
        self.assertTrue(nonflat.blocked)
        self.assertIn("unrealized pnl", nonflat.reason)

        for invalid in (Decimal("NaN"), "garbage"):
            with self.subTest(flat_invalid_unrealized_pnl=invalid):
                decision = InventoryEpisodeExecutor(self.config).evaluate(
                    position=replace(
                        self.position("0"), unrealized_pnl=invalid
                    ),
                    market=self.market(),
                    metadata=self.metadata,
                    account_snapshot=self.account("0"),
                    now_monotonic=0,
                    soft_exit_latched=False,
                    stranded_soft_exit=False,
                    authenticated_flat=True,
                    active_unwind_pending=False,
                    normal_passive_price=None,
                )
                self.assertTrue(decision.blocked)
                self.assertIn("unrealized pnl", decision.reason)

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

    def test_policy_ids_stages_and_passive_intent_are_deterministic(
        self,
    ) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        strict = self.evaluate(
            executor,
            now=0,
            stranded=False,
            soft_exit_latched=False,
        )
        hold = self.evaluate(executor, now=1)
        bounded = self.evaluate(executor, now=25)

        self.assertEqual(
            [
                strict.policy_decision_id,
                hold.policy_decision_id,
                bounded.policy_decision_id,
            ],
            [1, 2, 3],
        )
        self.assertEqual(strict.authenticated_episode_sequence, 1)
        self.assertEqual(strict.exit_stage, InventoryExitStage.STRICT_PROFIT)
        self.assertEqual(
            strict.binding_constraint,
            ExitBindingConstraint.NORMAL_PASSIVE,
        )
        self.assertEqual(hold.exit_stage, InventoryExitStage.INVENTORY_HOLD)
        self.assertTrue(hold.entered_inventory_hold)
        self.assertFalse(hold.reachable_now)
        self.assertEqual(
            hold.binding_constraint,
            ExitBindingConstraint.EPISODE_CAP,
        )
        self.assertEqual(
            bounded.exit_stage,
            InventoryExitStage.BOUNDED_PASSIVE_LOSS,
        )
        self.assertTrue(bounded.reachable_now)
        self.assertEqual(
            bounded.binding_constraint,
            ExitBindingConstraint.NORMAL_PASSIVE,
        )
        self.assertTrue(bounded.entered_inventory_hold)
        self.assertEqual(
            bounded.passive_order.intent.kind,
            OrderIntentKind.PASSIVE_EXIT,
        )
        self.assertTrue(bounded.passive_order.intent.entered_inventory_hold)
        self.assertEqual(bounded.passive_order.intent.revision, 3)
        self.assertEqual(bounded.passive_order.intent.policy_decision_id, 3)
        self.assertEqual(bounded.passive_order.intent.inventory_episode_id, 1)
        self.assertEqual(
            bounded.passive_order.intent.authenticated_episode_sequence,
            1,
        )
        self.assertTrue(executor.record_authenticated_flat())
        transitions = executor.execution_snapshot()[
            "stage_transition_history"
        ]
        self.assertEqual(
            [item["transition_sequence"] for item in transitions],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            transitions[-1]["policy_decision_id"],
            bounded.policy_decision_id,
        )

    def test_authenticated_episode_sequence_is_bound_to_exit_intent(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)

        decision = self.evaluate(
            executor,
            now=20,
            account=self.account("-0.2", episode_sequence=9),
        )

        self.assertEqual(decision.authenticated_episode_sequence, 9)
        self.assertIsNotNone(decision.passive_order)
        self.assertEqual(
            decision.passive_order.intent.authenticated_episode_sequence,
            9,
        )

    def test_missing_or_invalid_episode_sequence_suppresses_exit(self) -> None:
        for sequence in (None, 0, True):
            with self.subTest(sequence=sequence):
                decision = self.evaluate(
                    InventoryEpisodeExecutor(self.config),
                    now=20,
                    account=self.account(
                        "-0.2", episode_sequence=sequence
                    ),
                )

                self.assertEqual(decision.state, "passive_wait")
                self.assertTrue(decision.suppress_passive)
                self.assertIsNone(decision.passive_order)
                self.assertIsNone(
                    decision.authenticated_episode_sequence
                )

    def test_episode_sequence_drift_blocks_before_flat(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        first = self.evaluate(
            executor,
            now=20,
            account=self.account("-0.2", episode_sequence=4),
        )

        drift = self.evaluate(
            executor,
            now=21,
            account=self.account("-0.2", episode_sequence=5),
        )

        self.assertEqual(first.authenticated_episode_sequence, 4)
        self.assertTrue(drift.blocked)
        self.assertTrue(drift.suppress_passive)
        self.assertIsNone(drift.authenticated_episode_sequence)
        self.assertIn("changed before flat", drift.reason)

    def test_surplus_boundary_has_typed_hold_attribution(self) -> None:
        decision, _ = self.gate_c_decision()

        self.assertEqual(decision.exit_stage, InventoryExitStage.INVENTORY_HOLD)
        self.assertEqual(
            decision.binding_constraint,
            ExitBindingConstraint.SESSION_SURPLUS,
        )
        self.assertEqual(
            decision.selected_exit_price, decision.passive_order.price
        )
        self.assertGreater(
            decision.available_completed_surplus, decision.surplus_reserve
        )
        self.assertEqual(
            decision.passive_order.intent.exit_stage,
            InventoryExitStage.INVENTORY_HOLD,
        )

    def test_active_ioc_has_typed_policy_attribution(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        self.evaluate(executor, now=0, soft_exit_latched=False)
        decision = self.evaluate(executor, now=30, soft_exit_latched=False)

        self.assertEqual(decision.exit_stage, InventoryExitStage.ACTIVE_IOC)
        self.assertEqual(
            decision.binding_constraint,
            ExitBindingConstraint.ACTIVE_SLIPPAGE,
        )
        self.assertEqual(
            decision.active_order.intent.kind, OrderIntentKind.ACTIVE_EXIT
        )
        self.assertEqual(
            decision.active_order.intent.policy_decision_id,
            decision.policy_decision_id,
        )
        self.assertEqual(
            decision.selected_exit_price, decision.active_order.price
        )
        self.assertIsNotNone(decision.projected_episode_net)
        self.assertIsNotNone(decision.projected_session_net)
        self.assertIsNotNone(decision.projected_drawdown)

    def test_stage_transition_history_is_bounded_per_episode(self) -> None:
        executor = InventoryEpisodeExecutor(self.config)
        decision = None
        for policy_id in range(1, 103):
            decision = self.evaluate(
                executor,
                now=1,
                stranded=False,
                soft_exit_latched=policy_id % 2 == 0,
            )

        history = decision.snapshot()["stage_transition_history"]
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["policy_decision_id"], 3)
        self.assertEqual(history[-1]["policy_decision_id"], 102)
        self.assertTrue(all(item["episode_id"] == 1 for item in history))


if __name__ == "__main__":
    unittest.main()
