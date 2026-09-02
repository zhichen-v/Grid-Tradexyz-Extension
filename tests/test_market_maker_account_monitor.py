import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.adapters.exchanges.models import OrderSide, PositionSide
from core.services.market_maker.account_monitor import (
    AccountAuditError,
    MarketMakerAccountMonitor,
    SessionEconomics,
)
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.models import (
    EpisodePolicyObservation,
    ExitBindingConstraint,
    InventoryExitStage,
    OrderIntentKind,
    OrderIntentMetadata,
)


def trade(
    trade_id: str,
    order_id: str,
    side: OrderSide,
    amount: str,
    turnover: str,
    gross: str,
    *,
    role: str = "maker",
    fee_rate: str = "0.000120",
    integrator_fee_tick=0,
    timestamp: int = 1,
):
    rate = Decimal(fee_rate)
    return SimpleNamespace(
        id=trade_id,
        order_id=order_id,
        side=side,
        amount=Decimal(amount),
        cost=Decimal(turnover),
        fee={
            "role": role,
            "rate": rate,
            "cost": Decimal(turnover) * rate,
        },
        raw_data={
            "realized_pnl": Decimal(gross),
            "timestamp": timestamp,
            "trade_type": "trade",
            "integrator_fee_tick": integrator_fee_tick,
        },
    )


def position(
    size: str,
    side: PositionSide,
    *,
    leverage: str = "1",
    initial_margin_fraction: str = "100",
    margin_mode: str = "cross",
    unrealized_pnl: str = "0",
):
    return SimpleNamespace(
        symbol="BTC",
        size=Decimal(size),
        side=side,
        leverage=Decimal(leverage),
        margin_mode=SimpleNamespace(value=margin_mode),
        unrealized_pnl=Decimal(unrealized_pnl),
        raw_data={
            "position_info": SimpleNamespace(
                initial_margin_fraction=initial_margin_fraction
            )
        },
    )


class MarketMakerAccountMonitorTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **overrides) -> MarketMakerConfig:
        values = {
            "symbol": "BTC",
            "order_size": Decimal("0.2"),
            "max_position": Decimal("0.4"),
            "maker_fee_rate": Decimal("0.000120"),
            "account_audit_interval_seconds": 15,
            "max_session_drawdown": Decimal("5"),
            "economic_min_fills": 2,
            "min_completed_net_turnover_bps": Decimal("0.10"),
            "require_flat_start": True,
        }
        values.update(overrides)
        return MarketMakerConfig(**values)

    def adapter(self, *, equity: str = "300"):
        return SimpleNamespace(
            get_open_orders=AsyncMock(return_value=[]),
            get_account_trades=AsyncMock(return_value=[]),
            get_positions=AsyncMock(return_value=[]),
            get_balances=AsyncMock(
                return_value=[
                    SimpleNamespace(currency="USDG", total=Decimal(equity))
                ]
            ),
        )

    def monitor(self, adapter, config=None):
        self.now = 100.0

        async def no_wait(_seconds: float) -> None:
            return None

        return MarketMakerAccountMonitor(
            adapter,
            config or self.config(),
            monotonic=lambda: self.now,
            sleep=no_wait,
        )

    def active_unwind_config(self, **overrides) -> MarketMakerConfig:
        values = {
            "ping_pong_enabled": True,
            "soft_exit_after_seconds": 10,
            "soft_exit_net_turnover_bps": Decimal("-0.5"),
            "max_session_loss_for_maker_exit": Decimal("0.10"),
            "active_unwind_enabled": True,
            "active_unwind_after_seconds": 30,
            "active_unwind_loss_trigger": Decimal("0.20"),
            "active_unwind_max_slippage_ticks": 2,
            "max_episode_loss_for_unwind": Decimal("0.30"),
            "max_session_loss_for_unwind": Decimal("0.40"),
            "taker_fee_rate": Decimal("0.0004"),
        }
        values.update(overrides)
        return self.config(**values)

    async def test_audit_snapshot_binds_open_order_count(self) -> None:
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_open_orders.return_value = [
            SimpleNamespace(id="managed", symbol="BTC")
        ]

        await monitor.audit({"managed"})

        snapshot = monitor.snapshot(self.now)
        self.assertTrue(snapshot["last_audit_authenticated"])
        self.assertEqual(snapshot["audited_open_order_count"], 1)

    async def test_evidence_audit_detects_order_appearing_mid_read(self) -> None:
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_open_orders.side_effect = [
            [],
            [SimpleNamespace(id="external", symbol="BTC")],
        ]

        with self.assertRaisesRegex(AccountAuditError, "unmanaged open order"):
            await monitor.audit(set(), confirm_open_orders=True)

        snapshot = monitor.snapshot(self.now)
        self.assertFalse(snapshot["last_audit_authenticated"])
        self.assertIsNone(snapshot["audited_open_order_count"])

    async def test_authorized_active_unwind_taker_is_separately_attributed(self):
        config = self.active_unwind_config()
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        fills = [
            trade(
                "1", "maker-open", OrderSide.BUY, "0.2", "20", "0",
                timestamp=1,
            ),
            trade(
                "2", "active-close", OrderSide.SELL, "0.2", "19.9", "-0.1",
                role="taker", fee_rate="0.0004", timestamp=2,
            ),
        ]
        adapter.get_account_trades.return_value = fills
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.88964"))
        ]

        await monitor.audit(
            {"maker-open"},
            active_unwind_order_ids={"active-close"},
            order_intent_contexts={
                "maker-open": OrderIntentMetadata(
                    kind=OrderIntentKind.BASE_ENTRY,
                    revision=1,
                    inventory_episode_id=1,
                    authenticated_episode_sequence=1,
                ),
                "active-close": OrderIntentMetadata(
                    kind=OrderIntentKind.ACTIVE_EXIT,
                    revision=1,
                    inventory_episode_id=1,
                    authenticated_episode_sequence=1,
                    exit_stage=InventoryExitStage.ACTIVE_IOC,
                    policy_decision_id=2,
                    binding_constraint=(
                        ExitBindingConstraint.ACTIVE_SLIPPAGE
                    ),
                ),
            },
            episode_policy_observation=EpisodePolicyObservation(
                authenticated_episode_sequence=1,
                entered_inventory_hold=False,
                active_attempts=1,
                max_unlocked_episode_loss=Decimal("0"),
            ),
        )

        snapshot = monitor.snapshot(self.now)
        self.assertEqual(snapshot["unique_maker_fills"], 1)
        self.assertEqual(snapshot["unique_taker_fills"], 1)
        self.assertEqual(snapshot["completed_fills"], 1)
        self.assertEqual(snapshot["active_unwind_turnover"], Decimal("19.9"))
        self.assertEqual(
            snapshot["active_unwind_exact_fee"], Decimal("0.00796")
        )
        self.assertEqual(snapshot["ledger_position"], Decimal("0"))
        self.assertEqual(snapshot["episode_flat_success"], 1)
        self.assertEqual(snapshot["episode_active_unwind_flat"], 1)
        self.assertEqual(snapshot["episode_sequence"], 1)
        self.assertEqual(
            snapshot["completed_episode_ledger"],
            [
                {
                    "session_id": monitor.economics.session_id,
                    "episode_sequence": 1,
                    "opened_at": 1,
                    "closed_at": 2,
                    "maker_fills": 1,
                    "entry_side": "buy",
                    "turnover": Decimal("39.9"),
                    "gross": Decimal("-0.1"),
                    "exact_fee": Decimal("0.01036"),
                    "maker_fee": Decimal("0.0024"),
                    "taker_fee": Decimal("0.00796"),
                    "net_ex_funding": Decimal("-0.11036"),
                    "active_unwind_used": True,
                    "close_type": "active_unwind_flat",
                    "entry_vwap": Decimal("100"),
                    "exit_vwap": Decimal("99.5"),
                    "quantity": Decimal("0.2"),
                    "inventory_duration_seconds": Decimal("1"),
                    "final_exit_stage": "active_ioc",
                    "final_binding_constraint": "active_slippage",
                    "surplus_spent": Decimal("0"),
                    "passive_loss_used": Decimal("0"),
                    "max_unlocked_episode_loss": Decimal("0"),
                    "entered_inventory_hold": False,
                    "active_attempts": 1,
                    "close_policy_coverage": True,
                }
            ],
        )
        self.assertEqual(snapshot["policy_context_missing_count"], 0)

    async def test_snapshot_exposes_same_generation_position_and_unrealized(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()

        baseline = monitor.snapshot(self.now)
        self.assertEqual(baseline["audited_position"], Decimal("0"))
        self.assertEqual(
            baseline["audited_unrealized_pnl"], Decimal("0")
        )

        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "20", "0")
        ]
        adapter.get_positions.return_value = [
            position(
                "0.2",
                PositionSide.LONG,
                unrealized_pnl="-1.25",
            )
        ]
        await monitor.audit({"buy"})

        audited = monitor.snapshot(self.now)
        self.assertEqual(audited["audited_position"], Decimal("0.2"))
        self.assertEqual(
            audited["audited_unrealized_pnl"], Decimal("-1.25")
        )

    def test_active_unwind_requires_strict_inventory_reduction(self):
        cases = (
            (
                "flat",
                "0",
                OrderSide.BUY,
                "0.1",
                "0.1",
                "requires nonzero inventory",
            ),
            (
                "long increase",
                "0.2",
                OrderSide.BUY,
                "0.1",
                "0.3",
                "direction does not reduce inventory",
            ),
            (
                "short increase",
                "-0.2",
                OrderSide.SELL,
                "0.1",
                "-0.3",
                "direction does not reduce inventory",
            ),
            (
                "long flip",
                "0.2",
                OrderSide.SELL,
                "0.3",
                "-0.1",
                "must not flip inventory",
            ),
            (
                "short flip",
                "-0.2",
                OrderSide.BUY,
                "0.3",
                "0.1",
                "must not flip inventory",
            ),
        )
        for name, prior, side, amount, current, message in cases:
            with self.subTest(name=name):
                economics = SessionEconomics(
                    self.active_unwind_config(),
                    baseline_equity=Decimal("300"),
                    ledger_position=Decimal(prior),
                )
                before = economics.snapshot()
                with self.assertRaisesRegex(AccountAuditError, message):
                    economics.apply(
                        [
                            trade(
                                "1",
                                "active-close",
                                side,
                                amount,
                                "10",
                                "0",
                                role="taker",
                                fee_rate="0.0004",
                            )
                        ],
                        current_position=Decimal(current),
                        current_equity=Decimal("300"),
                        managed_order_ids={"active-close"},
                        active_unwind_order_ids={"active-close"},
                    )

                self.assertEqual(economics.snapshot(), before)
                self.assertEqual(economics.seen_trade_ids, set())

    def test_active_unwind_partial_fill_reduces_long_and_short_inventory(self):
        for prior, side, current in (
            ("0.2", OrderSide.SELL, "0.1"),
            ("-0.2", OrderSide.BUY, "-0.1"),
        ):
            with self.subTest(prior=prior):
                economics = SessionEconomics(
                    self.active_unwind_config(),
                    baseline_equity=Decimal("300"),
                    ledger_position=Decimal(prior),
                )
                economics.apply(
                    [
                        trade(
                            "1",
                            "active-close",
                            side,
                            "0.1",
                            "10",
                            "0",
                            role="taker",
                            fee_rate="0.0004",
                        )
                    ],
                    current_position=Decimal(current),
                    current_equity=Decimal("300"),
                    managed_order_ids={"active-close"},
                    active_unwind_order_ids={"active-close"},
                )

                self.assertEqual(economics.ledger_position, Decimal(current))
                self.assertEqual(economics.unique_taker_fills, 1)
                self.assertEqual(economics.seen_trade_ids, {"1"})

    def test_partial_active_then_maker_flat_reports_final_close_lane(self):
        economics = SessionEconomics(
            self.active_unwind_config(), baseline_equity=Decimal("300")
        )
        economics.apply(
            [
                trade(
                    "1", "maker-open", OrderSide.BUY, "0.2", "20", "0",
                    timestamp=1,
                ),
                trade(
                    "2",
                    "active-partial",
                    OrderSide.SELL,
                    "0.1",
                    "10",
                    "0",
                    role="taker",
                    fee_rate="0.0004",
                    timestamp=2,
                ),
                trade(
                    "3", "maker-close", OrderSide.SELL, "0.1", "10.1", "0.1",
                    timestamp=3,
                ),
            ],
            current_position=Decimal("0"),
            current_equity=Decimal("300.092388"),
            managed_order_ids={"maker-open", "maker-close"},
            active_unwind_order_ids={"active-partial"},
        )

        snapshot = economics.snapshot()
        self.assertEqual(snapshot["episode_flat_success"], 1)
        self.assertEqual(snapshot["episode_active_unwind_flat"], 0)
        self.assertEqual(
            snapshot["completed_episode_ledger"],
            [
                {
                    "session_id": economics.session_id,
                    "episode_sequence": 1,
                    "opened_at": 1,
                    "closed_at": 3,
                    "maker_fills": 2,
                    "entry_side": "buy",
                    "turnover": Decimal("40.1"),
                    "gross": Decimal("0.1"),
                    "exact_fee": Decimal("0.007612"),
                    "maker_fee": Decimal("0.003612"),
                    "taker_fee": Decimal("0.004"),
                    "net_ex_funding": Decimal("0.092388"),
                    "active_unwind_used": True,
                    "close_type": "maker_flat",
                    "entry_vwap": Decimal("100"),
                    "exit_vwap": Decimal("100.5"),
                    "quantity": Decimal("0.2"),
                    "inventory_duration_seconds": Decimal("2"),
                    "final_exit_stage": None,
                    "final_binding_constraint": None,
                    "surplus_spent": None,
                    "passive_loss_used": None,
                    "max_unlocked_episode_loss": None,
                    "entered_inventory_hold": None,
                    "active_attempts": 1,
                    "close_policy_coverage": False,
                }
            ],
        )

    async def test_unattributed_or_wrong_fee_taker_remains_fatal(self):
        for active_ids, fee_rate, message in (
            (set(), "0.0004", "non-maker"),
            ({"active-close"}, "0.0005", "taker fee changed"),
        ):
            with self.subTest(message=message):
                config = self.active_unwind_config()
                adapter = self.adapter()
                monitor = self.monitor(adapter, config)
                await monitor.initialize()
                adapter.get_account_trades.return_value = [
                    trade(
                        "1",
                        "active-close",
                        OrderSide.BUY,
                        "0.2",
                        "20",
                        "0",
                        role="taker",
                        fee_rate=fee_rate,
                    )
                ]
                adapter.get_positions.return_value = [
                    position("0.2", PositionSide.LONG)
                ]
                with self.assertRaisesRegex(AccountAuditError, message):
                    await monitor.audit(
                        {"active-close"},
                        active_unwind_order_ids=active_ids,
                    )

    async def test_completed_maker_round_trip_reports_exact_economics(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        fills = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100.04",
                "0.04",
                timestamp=2,
            ),
        ]
        adapter.get_account_trades.return_value = fills
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("300.0159952"))
        ]
        self.now += 900

        await monitor.audit({"buy", "sell"})

        snapshot = monitor.snapshot(self.now)
        account_net = Decimal("0.04") - (
            Decimal("100") + Decimal("100.04")
        ) * Decimal("0.000120")
        self.assertEqual(
            snapshot["economic_state"], "fee_and_equity_gate_go"
        )
        self.assertEqual(snapshot["unique_maker_fills"], 2)
        self.assertEqual(snapshot["completed_round_trips"], 1)
        self.assertEqual(snapshot["completed_turnover"], Decimal("200.04"))
        self.assertEqual(snapshot["completed_net_ex_funding"], account_net)
        self.assertEqual(snapshot["open_episode_turnover"], Decimal("0"))
        self.assertEqual(snapshot["open_episode_net_ex_funding"], Decimal("0"))
        self.assertEqual(
            snapshot["completed_net_turnover_bps"],
            account_net / Decimal("200.04") * Decimal("10000"),
        )
        self.assertEqual(snapshot["unattributed_flat_cashflow"], Decimal("0"))
        self.assertEqual(snapshot["episode_flat_success"], 1)
        self.assertEqual(snapshot["episode_active_unwind_flat"], 0)
        self.assertEqual(
            snapshot["completed_episode_ledger"],
            [
                {
                    "session_id": monitor.economics.session_id,
                    "episode_sequence": 1,
                    "opened_at": 1,
                    "closed_at": 2,
                    "maker_fills": 2,
                    "entry_side": "buy",
                    "turnover": Decimal("200.04"),
                    "gross": Decimal("0.04"),
                    "exact_fee": Decimal("0.0240048"),
                    "maker_fee": Decimal("0.0240048"),
                    "taker_fee": Decimal("0"),
                    "net_ex_funding": Decimal("0.0159952"),
                    "active_unwind_used": False,
                    "close_type": "maker_flat",
                    "entry_vwap": Decimal("500"),
                    "exit_vwap": Decimal("500.2"),
                    "quantity": Decimal("0.2"),
                    "inventory_duration_seconds": Decimal("1"),
                    "final_exit_stage": None,
                    "final_binding_constraint": None,
                    "surplus_spent": None,
                    "passive_loss_used": None,
                    "max_unlocked_episode_loss": None,
                    "entered_inventory_hold": None,
                    "active_attempts": 0,
                    "close_policy_coverage": False,
                }
            ],
        )
        self.assertEqual(
            snapshot["maker_turnover_per_wall_hour"], Decimal("800.16")
        )
        self.assertEqual(snapshot["maker_fills_per_wall_hour"], Decimal("8"))

    async def test_bounded_session_loss_recovers_to_full_economic_go(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.10"),
            dry_run=True,
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        first_round = [
            trade("1", "buy-1", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell-1",
                OrderSide.SELL,
                "0.2",
                "100",
                "0",
                timestamp=2,
            ),
        ]
        adapter.get_account_trades.return_value = first_round
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.976"))
        ]

        await monitor.audit({"buy-1", "sell-1", "buy-2", "sell-2"})

        recovering = monitor.snapshot(self.now)
        self.assertEqual(
            recovering["economic_state"], "bounded_economic_recovery"
        )
        self.assertEqual(
            recovering["session_loss_for_maker_exit"], Decimal("0.024")
        )
        self.assertEqual(
            recovering["remaining_session_loss_for_maker_exit"],
            Decimal("0.076"),
        )
        self.assertEqual(monitor.state, "healthy")

        adapter.get_account_trades.return_value = [
            *first_round,
            trade("3", "buy-2", OrderSide.BUY, "0.2", "100", "0", timestamp=3),
            trade(
                "4",
                "sell-2",
                OrderSide.SELL,
                "0.2",
                "100",
                "0.08",
                timestamp=4,
            ),
        ]
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("300.032"))
        ]

        await monitor.audit({"buy-1", "sell-1", "buy-2", "sell-2"})

        recovered = monitor.snapshot(self.now)
        self.assertEqual(
            recovered["economic_state"], "fee_and_equity_gate_go"
        )
        self.assertEqual(recovered["completed_net_ex_funding"], Decimal("0.032"))
        self.assertEqual(recovered["session_loss_for_maker_exit"], Decimal("0"))

    async def test_live_economic_failure_does_not_enter_bounded_recovery(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.10"),
            dry_run=False,
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade("2", "sell", OrderSide.SELL, "0.2", "100", "0", timestamp=2),
        ]

        with self.assertRaisesRegex(
            AccountAuditError, "completed gross does not cover exact fees"
        ):
            await monitor.audit({"buy", "sell"})

        self.assertEqual(monitor.economics.economic_state, "no_go")
        self.assertEqual(monitor.state, "hard_stop")

    async def test_live_economic_stop_waits_for_authenticated_flat(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.10"),
            dry_run=False,
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        first_page = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade("2", "sell", OrderSide.SELL, "0.2", "100", "0", timestamp=2),
            trade(
                "3", "current-buy", OrderSide.BUY, "0.1", "50", "0", timestamp=3
            ),
        ]
        adapter.get_account_trades.return_value = first_page
        adapter.get_positions.return_value = [
            position("0.1", PositionSide.LONG)
        ]

        await monitor.audit({"buy", "sell", "current-buy", "current-sell"})

        pending = monitor.snapshot(self.now)
        self.assertEqual(
            pending["economic_state"], "economic_stop_pending_flat"
        )
        self.assertIn("waiting for authenticated flat", pending["economic_reason"])
        self.assertEqual(monitor.state, "healthy")

        reopened_page = [
            *first_page,
            trade(
                "4",
                "current-sell",
                OrderSide.SELL,
                "0.1",
                "50",
                "-0.2",
                timestamp=4,
            ),
            trade(
                "5",
                "reopen-buy",
                OrderSide.BUY,
                "0.1",
                "50",
                "0",
                timestamp=5,
            ),
        ]
        adapter.get_account_trades.return_value = reopened_page
        await monitor.audit(
            {"buy", "sell", "current-buy", "current-sell", "reopen-buy"}
        )

        still_pending = monitor.snapshot(self.now)
        self.assertEqual(
            still_pending["economic_state"], "economic_stop_pending_flat"
        )
        self.assertIn(
            "completed gross does not cover exact fees",
            still_pending["economic_reason"],
        )
        self.assertNotIn("session loss exceeded", still_pending["economic_reason"])

        adapter.get_account_trades.return_value = [
            *reopened_page,
            trade(
                "6",
                "reopen-sell",
                OrderSide.SELL,
                "0.1",
                "50",
                "1",
                timestamp=6,
            ),
        ]
        adapter.get_positions.return_value = []
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("300.97"))
        ]

        with self.assertRaisesRegex(
            AccountAuditError, "completed gross does not cover exact fees"
        ):
            await monitor.audit(
                {
                    "buy",
                    "sell",
                    "current-buy",
                    "current-sell",
                    "reopen-buy",
                    "reopen-sell",
                }
            )

        stopped = monitor.snapshot(self.now)
        self.assertEqual(stopped["economic_state"], "no_go")
        self.assertEqual(stopped["audited_position"], Decimal("0"))
        self.assertEqual(stopped["episode_flat_success"], 3)
        self.assertEqual(monitor.state, "hard_stop")

    async def test_live_session_loss_stop_waits_for_authenticated_flat(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.01"),
            dry_run=False,
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        open_page = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade("2", "sell", OrderSide.SELL, "0.2", "100", "0", timestamp=2),
            trade(
                "3", "current-buy", OrderSide.BUY, "0.1", "50", "0", timestamp=3
            ),
        ]
        managed_ids = {"buy", "sell", "current-buy", "current-sell"}
        adapter.get_account_trades.return_value = open_page
        adapter.get_positions.return_value = [
            position("0.1", PositionSide.LONG)
        ]

        await monitor.audit(managed_ids)

        pending = monitor.snapshot(self.now)
        self.assertEqual(
            pending["economic_state"], "economic_stop_pending_flat"
        )
        self.assertIn("session loss exceeded", pending["economic_reason"])
        self.assertEqual(monitor.state, "healthy")

        adapter.get_account_trades.return_value = [
            *open_page,
            trade(
                "4",
                "current-sell",
                OrderSide.SELL,
                "0.1",
                "50",
                "0",
                timestamp=4,
            ),
        ]
        adapter.get_positions.return_value = []

        with self.assertRaisesRegex(AccountAuditError, "session loss exceeded"):
            await monitor.audit(managed_ids)

        self.assertEqual(monitor.economics.economic_state, "no_go")
        self.assertEqual(monitor.state, "hard_stop")

    async def test_bounded_session_loss_exact_limit_is_allowed_but_excess_stops(
        self,
    ) -> None:
        fills = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100",
                "0",
                timestamp=2,
            ),
        ]
        for budget, should_stop in (
            (Decimal("0.024"), False),
            (Decimal("0.023999"), True),
        ):
            with self.subTest(budget=budget):
                config = self.config(
                    soft_exit_after_seconds=120,
                    soft_exit_net_turnover_bps=Decimal("-0.5"),
                    max_session_loss_for_maker_exit=budget,
                )
                adapter = self.adapter(equity="299.976")
                monitor = self.monitor(adapter, config)
                await monitor.initialize()
                adapter.get_account_trades.return_value = fills
                if should_stop:
                    with self.assertRaisesRegex(
                        AccountAuditError, "session loss exceeded"
                    ):
                        await monitor.audit({"buy", "sell"})
                    self.assertEqual(monitor.state, "hard_stop")
                else:
                    await monitor.audit({"buy", "sell"})
                    self.assertEqual(
                        monitor.economics.economic_state,
                        "bounded_economic_recovery",
                    )

    async def test_disabled_active_unwind_budget_cannot_relax_economic_no_go(
        self,
    ) -> None:
        config = self.config(
            max_session_loss_for_maker_exit=Decimal("0"),
            active_unwind_enabled=False,
            max_session_loss_for_unwind=Decimal("0.40"),
        )
        adapter = self.adapter(equity="299.976")
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100",
                "0",
                timestamp=2,
            ),
        ]

        with self.assertRaisesRegex(
            AccountAuditError, "completed gross does not cover exact fees"
        ):
            await monitor.audit({"buy", "sell"})

        self.assertEqual(monitor.state, "hard_stop")

    async def test_bounded_session_loss_uses_worse_flat_equity_evidence(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.10"),
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.89"))
        ]
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100",
                "0.03",
                timestamp=2,
            ),
        ]

        with self.assertRaisesRegex(AccountAuditError, "session loss exceeded"):
            await monitor.audit({"buy", "sell"})

        self.assertEqual(
            monitor.economics.session_loss_for_maker_exit, Decimal("0.11")
        )

    async def test_drawdown_stop_publishes_authenticated_flat_snapshot(self):
        config = self.config(
            economic_min_fills=10,
            max_session_drawdown=Decimal("0.10"),
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.89"))
        ]
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade("2", "sell", OrderSide.SELL, "0.2", "100", "0", timestamp=2),
        ]

        with self.assertRaisesRegex(AccountAuditError, "max_session_drawdown"):
            await monitor.audit({"buy", "sell"})

        stopped = monitor.snapshot(self.now)
        self.assertEqual(stopped["state"], "hard_stop")
        self.assertTrue(stopped["last_audit_authenticated"])
        self.assertEqual(stopped["audited_position"], Decimal("0"))
        self.assertEqual(stopped["ledger_position"], Decimal("0"))

    async def test_flat_equity_loss_remains_reserved_during_next_episode(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.10"),
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        completed = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100",
                "0.074",
                timestamp=2,
            ),
        ]
        adapter.get_account_trades.return_value = completed
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.91"))
        ]

        await monitor.audit({"buy", "sell", "next-buy"})

        flat = monitor.snapshot(self.now)
        self.assertEqual(flat["completed_net_ex_funding"], Decimal("0.050"))
        self.assertEqual(flat["last_flat_equity_change"], Decimal("-0.09"))
        self.assertEqual(flat["session_loss_for_maker_exit"], Decimal("0.09"))
        self.assertEqual(
            flat["remaining_session_loss_for_maker_exit"], Decimal("0.01")
        )

        adapter.get_account_trades.return_value = [
            *completed,
            trade(
                "3",
                "next-buy",
                OrderSide.BUY,
                "0.2",
                "20",
                "0",
                timestamp=3,
            ),
        ]
        adapter.get_positions.return_value = [
            position("0.2", PositionSide.LONG)
        ]
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.80"))
        ]

        await monitor.audit({"buy", "sell", "next-buy"})

        nonflat = monitor.snapshot(self.now)
        self.assertEqual(
            nonflat["last_flat_equity_change"], Decimal("-0.09")
        )
        self.assertEqual(
            nonflat["session_loss_for_maker_exit"], Decimal("0.09")
        )
        self.assertEqual(
            nonflat["remaining_session_loss_for_maker_exit"], Decimal("0.01")
        )

    async def test_coalesced_close_and_reopen_marks_flat_evidence_stale(self):
        config = self.config(
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            max_session_loss_for_maker_exit=Decimal("0.10"),
        )
        adapter = self.adapter()
        monitor = self.monitor(adapter, config)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100",
                "0.074",
                timestamp=2,
            ),
            trade(
                "3",
                "next-buy",
                OrderSide.BUY,
                "0.2",
                "20",
                "0",
                timestamp=3,
            ),
        ]
        adapter.get_positions.return_value = [
            position("0.2", PositionSide.LONG)
        ]

        await monitor.audit({"buy", "sell", "next-buy"})

        snapshot = monitor.snapshot(self.now)
        self.assertEqual(snapshot["completed_fills"], 2)
        self.assertEqual(snapshot["last_flat_completed_fills"], 0)
        self.assertIsNone(snapshot["flat_equity_change"])

    async def test_opening_fill_remains_incomplete_without_false_no_go(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0")
        ]
        adapter.get_positions.return_value = [position("0.2", PositionSide.LONG)]

        await monitor.audit({"buy"})

        snapshot = monitor.snapshot(self.now)
        self.assertEqual(snapshot["economic_state"], "incomplete_nonflat")
        self.assertEqual(snapshot["completed_fills"], 0)
        self.assertEqual(snapshot["turnover"], Decimal("100"))
        self.assertEqual(snapshot["open_episode_turnover"], Decimal("100"))
        self.assertEqual(
            snapshot["open_episode_net_ex_funding"], Decimal("-0.012")
        )

    async def test_null_integrator_fee_requires_local_zero_fee_signing_proof(self):
        adapter = self.adapter()
        adapter.managed_order_integrator_fee_tick = False
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade(
                "1",
                "buy",
                OrderSide.BUY,
                "0.2",
                "100",
                "0",
                integrator_fee_tick=None,
            )
        ]
        adapter.get_positions.return_value = [position("0.2", PositionSide.LONG)]

        with self.assertRaisesRegex(AccountAuditError, "integrator fee"):
            await monitor.audit({"buy"})

    async def test_null_integrator_fee_still_requires_managed_order_attribution(self):
        adapter = self.adapter()
        adapter.managed_order_integrator_fee_tick = 0
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade(
                "1",
                "foreign-order",
                OrderSide.BUY,
                "0.2",
                "100",
                "0",
                integrator_fee_tick=None,
            )
        ]
        adapter.get_positions.return_value = [position("0.2", PositionSide.LONG)]

        with self.assertRaisesRegex(AccountAuditError, "not attributable"):
            await monitor.audit(set())

    async def test_null_integrator_fee_is_accepted_for_explicit_zero_fee_orders(self):
        adapter = self.adapter()
        adapter.managed_order_integrator_fee_tick = 0
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade(
                "1",
                "buy",
                OrderSide.BUY,
                "0.2",
                "100",
                "0",
                integrator_fee_tick=None,
            )
        ]
        adapter.get_positions.return_value = [position("0.2", PositionSide.LONG)]

        await monitor.audit({"buy"})

        self.assertEqual(monitor.economics.unique_maker_fills, 1)

    async def test_historical_null_integrator_fee_only_seeds_the_baseline(self):
        adapter = self.adapter()
        adapter.get_account_trades.return_value = [
            trade(
                "historical",
                "old-order",
                OrderSide.BUY,
                "0.2",
                "100",
                "0",
                integrator_fee_tick=None,
            )
        ]
        monitor = self.monitor(adapter)

        await monitor.initialize()

        self.assertEqual(monitor.economics.seen_trade_ids, {"historical"})

    async def test_partial_fill_can_cross_inventory_before_returning_flat(self):
        adapter = self.adapter()
        monitor = self.monitor(
            adapter,
            self.config(economic_min_fills=3),
        )
        await monitor.initialize()
        first_page = [
            trade("1", "buy", OrderSide.BUY, "0.1", "10", "0", timestamp=1),
            trade(
                "2", "sell", OrderSide.SELL, "0.2", "20.02", "0.01", timestamp=2
            ),
        ]
        adapter.get_account_trades.return_value = first_page
        adapter.get_positions.return_value = [
            position("0.1", PositionSide.SHORT)
        ]

        await monitor.audit({"buy", "sell"})

        self.assertEqual(monitor.economics.ledger_position, Decimal("-0.1"))
        self.assertEqual(monitor.economics.economic_state, "incomplete_nonflat")

        final_page = first_page + [
            trade(
                "3",
                "buy-2",
                OrderSide.BUY,
                "0.1",
                "9.99",
                "0.02",
                timestamp=3,
            )
        ]
        exact_fee = Decimal("40.01") * Decimal("0.000120")
        adapter.get_account_trades.return_value = final_page
        adapter.get_positions.return_value = []
        adapter.get_balances.return_value = [
            SimpleNamespace(
                currency="USDG",
                total=Decimal("300.03") - exact_fee,
            )
        ]

        await monitor.audit({"buy", "sell", "buy-2"})

        self.assertEqual(monitor.economics.completed_round_trips, 1)
        self.assertEqual(monitor.economics.completed_fills, 3)
        self.assertEqual(
            monitor.economics.economic_state, "fee_and_equity_gate_go"
        )

    async def test_economics_accumulates_to_eight_fills_without_duplicates(self):
        adapter = self.adapter()
        historical = SimpleNamespace(id="historical")
        adapter.get_account_trades.return_value = [historical]
        monitor = self.monitor(adapter, self.config(economic_min_fills=8))
        await monitor.initialize()
        fills = []
        managed_ids = set()
        completed_net = Decimal("0")
        round_net = Decimal("0.01") - Decimal("20.01") * Decimal(
            "0.000120"
        )

        for episode in range(4):
            buy_id = f"buy-{episode}"
            sell_id = f"sell-{episode}"
            managed_ids.update({buy_id, sell_id})
            fills.extend(
                (
                    trade(
                        str(episode * 2 + 1),
                        buy_id,
                        OrderSide.BUY,
                        "0.1",
                        "10",
                        "0",
                        timestamp=episode * 2 + 1,
                    ),
                    trade(
                        str(episode * 2 + 2),
                        sell_id,
                        OrderSide.SELL,
                        "0.1",
                        "10.01",
                        "0.01",
                        timestamp=episode * 2 + 2,
                    ),
                )
            )
            completed_net += round_net
            adapter.get_account_trades.return_value = [historical, *fills]
            adapter.get_balances.return_value = [
                SimpleNamespace(
                    currency="USDG", total=Decimal("300") + completed_net
                )
            ]

            await monitor.audit(managed_ids)

            expected = (
                "fee_and_equity_gate_go" if episode == 3 else "collecting"
            )
            self.assertEqual(monitor.economics.economic_state, expected)

        await monitor.audit(managed_ids)
        self.assertEqual(monitor.economics.unique_maker_fills, 8)
        self.assertEqual(monitor.economics.completed_fills, 8)

        opening = trade(
            "9", "buy-open", OrderSide.BUY, "0.1", "10", "0", timestamp=9
        )
        adapter.get_account_trades.return_value = [historical, *fills, opening]
        adapter.get_positions.return_value = [
            position("0.1", PositionSide.LONG)
        ]
        adapter.get_balances.return_value = [
            SimpleNamespace(
                currency="USDG",
                total=(
                    Decimal("300")
                    + completed_net
                    - Decimal("10") * Decimal("0.000120")
                ),
            )
        ]

        await monitor.audit({*managed_ids, "buy-open"})

        self.assertEqual(
            monitor.economics.economic_state,
            "fee_gate_pass_equity_pending_flat",
        )
        self.assertIsNone(monitor.economics.flat_equity_turnover_bps)
        self.assertEqual(monitor.economics.completed_fills, 8)

    def test_completed_episode_ledger_is_bounded_without_losing_counters(self):
        economics = SessionEconomics(
            self.config(
                maker_fee_rate=Decimal("0"),
                min_completed_net_turnover_bps=Decimal("0"),
            ),
            baseline_equity=Decimal("300"),
        )
        for episode in range(101):
            buy_id = f"buy-{episode}"
            sell_id = f"sell-{episode}"
            economics.apply(
                [
                    trade(
                        str(episode * 2 + 1),
                        buy_id,
                        OrderSide.BUY,
                        "0.1",
                        "1",
                        "0",
                        fee_rate="0",
                        timestamp=episode * 2 + 1,
                    ),
                    trade(
                        str(episode * 2 + 2),
                        sell_id,
                        OrderSide.SELL,
                        "0.1",
                        "1",
                        "0",
                        fee_rate="0",
                        timestamp=episode * 2 + 2,
                    ),
                ],
                current_position=Decimal("0"),
                current_equity=Decimal("300"),
                managed_order_ids={buy_id, sell_id},
            )

        snapshot = economics.snapshot()
        self.assertEqual(len(snapshot["completed_episode_ledger"]), 100)
        self.assertEqual(snapshot["episode_flat_success"], 101)
        self.assertEqual(snapshot["episode_active_unwind_flat"], 0)

    def test_order_id_reuse_across_inventory_episodes_fails_closed(self):
        economics = SessionEconomics(
            self.config(
                maker_fee_rate=Decimal("0"),
                min_completed_net_turnover_bps=Decimal("0"),
            ),
            baseline_equity=Decimal("300"),
        )
        economics.apply(
            [
                trade(
                    "1",
                    "shared-order",
                    OrderSide.BUY,
                    "0.1",
                    "1",
                    "0",
                    fee_rate="0",
                    timestamp=1,
                ),
                trade(
                    "2",
                    "close-1",
                    OrderSide.SELL,
                    "0.1",
                    "1",
                    "0",
                    fee_rate="0",
                    timestamp=2,
                ),
            ],
            current_position=Decimal("0"),
            current_equity=Decimal("300"),
            managed_order_ids={"shared-order", "close-1"},
            order_intent_contexts={
                "shared-order": OrderIntentMetadata(
                    kind=OrderIntentKind.BASE_ENTRY,
                    revision=1,
                    inventory_episode_id=1,
                    authenticated_episode_sequence=1,
                )
            },
        )
        before = economics.snapshot()

        with self.assertRaisesRegex(
            AccountAuditError,
            "order id was reused across inventory episodes",
        ):
            economics.apply(
                [
                    trade(
                        "3",
                        "shared-order",
                        OrderSide.BUY,
                        "0.1",
                        "1",
                        "0",
                        fee_rate="0",
                        timestamp=3,
                    )
                ],
                current_position=Decimal("0.1"),
                current_equity=Decimal("300"),
                managed_order_ids={"shared-order"},
                order_intent_contexts={
                    "shared-order": OrderIntentMetadata(
                        kind=OrderIntentKind.BASE_ENTRY,
                        revision=2,
                        inventory_episode_id=2,
                        authenticated_episode_sequence=2,
                    )
                },
            )

        self.assertEqual(economics.snapshot(), before)
        self.assertEqual(economics.seen_trade_ids, {"1", "2"})

    def test_long_run_attribution_supports_four_thousand_distinct_orders(self):
        economics = SessionEconomics(
            self.config(
                maker_fee_rate=Decimal("0"),
                min_completed_net_turnover_bps=Decimal("0"),
            ),
            baseline_equity=Decimal("300"),
        )
        first_episode = None
        for episode in range(2000):
            buy_sequence = episode * 2 + 1
            sell_sequence = buy_sequence + 1
            fills = [
                trade(
                    str(buy_sequence),
                    f"buy-{episode}",
                    OrderSide.BUY,
                    "0.1",
                    "1",
                    "0",
                    fee_rate="0",
                    timestamp=buy_sequence,
                ),
                trade(
                    str(sell_sequence),
                    f"sell-{episode}",
                    OrderSide.SELL,
                    "0.1",
                    "1",
                    "0",
                    fee_rate="0",
                    timestamp=sell_sequence,
                ),
            ]
            if first_episode is None:
                first_episode = fills
            economics.apply(
                fills,
                current_position=Decimal("0"),
                current_equity=Decimal("300"),
                managed_order_ids={fill.order_id for fill in fills},
            )

        before_replay = economics.snapshot()
        economics.apply(
            first_episode,
            current_position=Decimal("0"),
            current_equity=Decimal("300"),
            managed_order_ids={fill.order_id for fill in first_episode},
        )
        snapshot = economics.snapshot()

        self.assertEqual(snapshot, before_replay)
        self.assertEqual(snapshot["unique_maker_fills"], 4000)
        self.assertEqual(snapshot["completed_round_trips"], 2000)
        self.assertEqual(len(economics.seen_trade_ids), 4000)
        self.assertEqual(len(economics._order_role_bindings), 4000)
        self.assertEqual(snapshot["seen_trade_id_registry_size"], 4000)
        self.assertEqual(snapshot["seen_trade_id_evictions"], 0)
        self.assertEqual(snapshot["order_role_binding_registry_size"], 4000)
        self.assertEqual(snapshot["order_role_binding_evictions"], 0)
        self.assertEqual(len(snapshot["completed_episode_ledger"]), 100)
        self.assertEqual(
            {
                episode["session_id"]
                for episode in snapshot["completed_episode_ledger"]
            },
            {economics.session_id},
        )
        self.assertEqual(
            [
                episode["episode_sequence"]
                for episode in snapshot["completed_episode_ledger"]
            ],
            list(range(1901, 2001)),
        )
        self.assertEqual(
            snapshot["completed_episode_ledger"][-1],
            {
                "session_id": economics.session_id,
                "episode_sequence": 2000,
                "opened_at": 3999,
                "closed_at": 4000,
                "maker_fills": 2,
                "entry_side": "buy",
                "turnover": Decimal("2"),
                "gross": Decimal("0"),
                "exact_fee": Decimal("0"),
                "maker_fee": Decimal("0"),
                "taker_fee": Decimal("0"),
                "net_ex_funding": Decimal("0"),
                "active_unwind_used": False,
                "close_type": "maker_flat",
                "entry_vwap": Decimal("10"),
                "exit_vwap": Decimal("10"),
                "quantity": Decimal("0.1"),
                "inventory_duration_seconds": Decimal("1"),
                "final_exit_stage": None,
                "final_binding_constraint": None,
                "surplus_spent": None,
                "passive_loss_used": None,
                "max_unlocked_episode_loss": None,
                "entered_inventory_hold": None,
                "active_attempts": 0,
                "close_policy_coverage": False,
            },
        )

    async def test_completed_fee_failure_is_not_hidden_by_open_tail(self):
        cases = (("0", "cover exact fees"), ("0.025", "threshold"))
        for close_gross, message in cases:
            with self.subTest(close_gross=close_gross):
                adapter = self.adapter()
                monitor = self.monitor(adapter)
                await monitor.initialize()
                adapter.get_account_trades.return_value = [
                    trade(
                        "1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1
                    ),
                    trade(
                        "2",
                        "sell",
                        OrderSide.SELL,
                        "0.2",
                        "100",
                        close_gross,
                        timestamp=2,
                    ),
                    trade(
                        "3",
                        "buy-open",
                        OrderSide.BUY,
                        "0.1",
                        "50",
                        "0",
                        timestamp=3,
                    ),
                ]
                adapter.get_positions.return_value = [
                    position("0.1", PositionSide.LONG)
                ]

                with self.assertRaisesRegex(AccountAuditError, message):
                    await monitor.audit({"buy", "sell", "buy-open"})

                self.assertEqual(monitor.economics.ledger_position, Decimal("0.1"))
                self.assertEqual(monitor.economics.completed_fills, 2)
                self.assertEqual(monitor.economics.economic_state, "no_go")

    async def test_pending_flat_equity_transition_uses_exact_threshold(self):
        for equity_delta, should_go in (
            (Decimal("0.003"), True),
            (Decimal("0.002999999"), False),
        ):
            with self.subTest(equity_delta=equity_delta):
                adapter = self.adapter()
                monitor = self.monitor(adapter)
                await monitor.initialize()
                completed_and_open = [
                    trade(
                        "1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1
                    ),
                    trade(
                        "2",
                        "sell",
                        OrderSide.SELL,
                        "0.2",
                        "100",
                        "0.026",
                        timestamp=2,
                    ),
                    trade(
                        "3",
                        "buy-open",
                        OrderSide.BUY,
                        "0.1",
                        "50",
                        "0",
                        timestamp=3,
                    ),
                ]
                managed_ids = {"buy", "sell", "buy-open", "sell-close"}
                adapter.get_account_trades.return_value = completed_and_open
                adapter.get_positions.return_value = [
                    position("0.1", PositionSide.LONG)
                ]

                await monitor.audit(managed_ids)

                self.assertEqual(
                    monitor.economics.economic_state,
                    "fee_gate_pass_equity_pending_flat",
                )
                adapter.get_account_trades.return_value = [
                    *completed_and_open,
                    trade(
                        "4",
                        "sell-close",
                        OrderSide.SELL,
                        "0.1",
                        "50",
                        "0.013",
                        timestamp=4,
                    ),
                ]
                adapter.get_positions.return_value = []
                adapter.get_balances.return_value = [
                    SimpleNamespace(
                        currency="USDG", total=Decimal("300") + equity_delta
                    )
                ]

                if should_go:
                    await monitor.audit(managed_ids)
                    self.assertEqual(
                        monitor.economics.economic_state,
                        "fee_and_equity_gate_go",
                    )
                    self.assertEqual(
                        monitor.economics.flat_equity_turnover_bps,
                        Decimal("0.10"),
                    )
                else:
                    with self.assertRaisesRegex(AccountAuditError, "account-value"):
                        await monitor.audit(managed_ids)

    async def test_completed_net_threshold_is_decimal_exact(self):
        for close_gross, should_go in (
            (Decimal("0.026"), True),
            (Decimal("0.025999999"), False),
        ):
            with self.subTest(close_gross=close_gross):
                adapter = self.adapter()
                monitor = self.monitor(adapter)
                await monitor.initialize()
                adapter.get_account_trades.return_value = [
                    trade("1", "buy", OrderSide.BUY, "0.2", "100", "0"),
                    trade(
                        "2",
                        "sell",
                        OrderSide.SELL,
                        "0.2",
                        "100",
                        str(close_gross),
                        timestamp=2,
                    ),
                ]
                net = close_gross - Decimal("200") * Decimal("0.000120")
                adapter.get_balances.return_value = [
                    SimpleNamespace(
                        currency="USDG", total=Decimal("300") + net
                    )
                ]

                if should_go:
                    await monitor.audit({"buy", "sell"})
                    self.assertEqual(
                        monitor.economics.completed_net_turnover_bps,
                        Decimal("0.10"),
                    )
                else:
                    with self.assertRaisesRegex(AccountAuditError, "threshold"):
                        await monitor.audit({"buy", "sell"})

    async def test_same_timestamp_trades_use_numeric_sequence_order(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter, self.config(economic_min_fills=4))
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("11", "sell-2", OrderSide.SELL, "0.2", "100", "0", timestamp=1),
            trade("10", "sell-1", OrderSide.SELL, "0.2", "100", "0.04", timestamp=1),
            trade("2", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
        ]
        adapter.get_positions.return_value = [position("0.2", PositionSide.SHORT)]

        await monitor.audit({"buy", "sell-1", "sell-2"})

        snapshot = monitor.snapshot(self.now)
        self.assertEqual(snapshot["completed_round_trips"], 1)
        self.assertEqual(snapshot["completed_fills"], 2)
        self.assertEqual(snapshot["ledger_position"], Decimal("-0.2"))

    async def test_full_trade_page_is_rejected_before_history_can_be_lost(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        fills = []
        known_ids = set()
        for index in range(100):
            order_id = f"order-{index}"
            known_ids.add(order_id)
            fills.append(
                trade(
                    str(index + 1),
                    order_id,
                    OrderSide.BUY if index % 2 == 0 else OrderSide.SELL,
                    "0.2",
                    "100",
                    "0.03" if index % 2 else "0",
                    timestamp=index + 1,
                )
            )
        adapter.get_account_trades.return_value = fills

        with self.assertRaisesRegex(AccountAuditError, "window exhausted"):
            await monitor.audit(known_ids)

    async def test_completed_fee_shortfall_triggers_hard_stop(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100.01",
                "0.01",
                timestamp=2,
            ),
        ]

        with self.assertRaisesRegex(AccountAuditError, "does not cover"):
            await monitor.audit({"buy", "sell"})

        self.assertEqual(monitor.snapshot(self.now)["economic_state"], "no_go")
        self.assertEqual(monitor.state, "hard_stop")

    async def test_flat_account_value_gate_catches_non_trade_costs(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0", timestamp=1),
            trade(
                "2",
                "sell",
                OrderSide.SELL,
                "0.2",
                "100.04",
                "0.04",
                timestamp=2,
            ),
        ]
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("299.99"))
        ]

        with self.assertRaisesRegex(AccountAuditError, "account-value"):
            await monitor.audit({"buy", "sell"})

        self.assertEqual(monitor.state, "hard_stop")

    async def test_taker_or_unattributed_trade_triggers_hard_stop(self):
        for role, known_ids, message in (
            ("taker", {"buy"}, "non-maker"),
            ("maker", set(), "not attributable"),
        ):
            with self.subTest(role=role):
                adapter = self.adapter()
                monitor = self.monitor(adapter)
                await monitor.initialize()
                adapter.get_account_trades.return_value = [
                    trade(
                        "1",
                        "buy",
                        OrderSide.BUY,
                        "0.2",
                        "100",
                        "0",
                        role=role,
                    )
                ]
                adapter.get_positions.return_value = [
                    position("0.2", PositionSide.LONG)
                ]

                with self.assertRaisesRegex(AccountAuditError, message):
                    await monitor.audit(known_ids)

    async def test_read_fault_has_bounded_recovery(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_open_orders.side_effect = [OSError("dns"), []]

        await monitor.audit(set())

        self.assertEqual(adapter.get_open_orders.await_count, 3)
        self.assertEqual(monitor.total_read_failures, 1)
        self.assertEqual(monitor.state, "healthy")

    async def test_five_transient_read_faults_recover_on_sixth_attempt(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        sleeps: list[float] = []

        async def record_sleep(seconds):
            sleeps.append(seconds)

        monitor._sleep = record_sleep
        adapter.get_open_orders.side_effect = [
            OSError("gateway") for _ in range(5)
        ] + [[]]

        await monitor.audit(set())

        self.assertEqual(adapter.get_open_orders.await_count, 7)
        self.assertEqual(monitor.total_read_failures, 5)
        self.assertEqual(monitor.state, "healthy")
        self.assertEqual(sleeps, [1.0, 1.0, 1.0, 1.0, 1.0])

    async def test_persistent_read_fault_hard_stops_after_six_attempts(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        sleeps: list[float] = []

        async def record_sleep(seconds):
            sleeps.append(seconds)

        monitor._sleep = record_sleep
        adapter.get_open_orders.side_effect = OSError("dns unavailable")

        with self.assertRaisesRegex(AccountAuditError, "dns unavailable"):
            await monitor.audit(set())

        self.assertEqual(adapter.get_open_orders.await_count, 7)
        self.assertEqual(monitor.total_read_failures, 6)
        self.assertEqual(monitor.state, "hard_stop")
        self.assertEqual(sleeps, [1.0, 1.0, 1.0, 1.0, 1.0])

    async def test_actual_non_target_order_is_a_hard_stop(self):
        adapter = self.adapter()
        adapter.get_open_orders.return_value = [
            SimpleNamespace(id="x", symbol="ETH")
        ]
        monitor = self.monitor(adapter)

        with self.assertRaisesRegex(AccountAuditError, "non-BTC"):
            await monitor.initialize()
        self.assertEqual(monitor.state, "hard_stop")

    async def test_nonflat_start_is_reported_as_hard_stop(self):
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            position("0.2", PositionSide.LONG)
        ]
        monitor = self.monitor(adapter)

        with self.assertRaisesRegex(AccountAuditError, "flat starting"):
            await monitor.initialize()

        self.assertEqual(monitor.state, "hard_stop")
        self.assertIn("flat starting", monitor.reason)

    async def test_drawdown_limit_is_enforced_by_main_monitor(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_balances.return_value = [
            SimpleNamespace(currency="USDG", total=Decimal("294.99"))
        ]

        with self.assertRaisesRegex(AccountAuditError, "drawdown"):
            await monitor.audit(set())

    async def test_drawdown_stops_at_exact_mark_to_market_limit(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_positions.return_value = [
            position(
                "0.2",
                PositionSide.LONG,
                unrealized_pnl="-5",
            )
        ]
        adapter.get_account_trades.return_value = [
            trade("1", "buy", OrderSide.BUY, "0.2", "100", "0")
        ]

        with self.assertRaisesRegex(AccountAuditError, "drawdown"):
            await monitor.audit({"buy"})

    async def test_position_requires_one_x_cross(self):
        for leverage, initial_margin_fraction, margin_mode, message in (
            ("2", "50", "cross", "1x"),
            ("1", "51", "cross", "1x"),
            ("1", "100", "isolated", "cross"),
        ):
            with self.subTest(
                leverage=leverage,
                initial_margin_fraction=initial_margin_fraction,
                margin_mode=margin_mode,
            ):
                adapter = self.adapter()
                monitor = self.monitor(adapter)
                await monitor.initialize()
                adapter.get_positions.return_value = [
                    position(
                        "0.2",
                        PositionSide.LONG,
                        leverage=leverage,
                        initial_margin_fraction=initial_margin_fraction,
                        margin_mode=margin_mode,
                    )
                ]

                with self.assertRaisesRegex(AccountAuditError, message):
                    await monitor.audit(set())


if __name__ == "__main__":
    unittest.main()
