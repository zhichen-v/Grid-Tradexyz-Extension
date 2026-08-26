import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.adapters.exchanges.models import OrderSide, PositionSide
from core.services.market_maker.account_monitor import (
    AccountAuditError,
    MarketMakerAccountMonitor,
)
from core.services.market_maker.config import MarketMakerConfig


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
        self.assertEqual(
            snapshot["maker_turnover_per_wall_hour"], Decimal("800.16")
        )
        self.assertEqual(snapshot["maker_fills_per_wall_hour"], Decimal("8"))

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

    async def test_four_transient_read_faults_recover_on_fifth_attempt(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        sleeps: list[float] = []

        async def record_sleep(seconds):
            sleeps.append(seconds)

        monitor._sleep = record_sleep
        adapter.get_open_orders.side_effect = [
            OSError("gateway") for _ in range(4)
        ] + [[]]

        await monitor.audit(set())

        self.assertEqual(adapter.get_open_orders.await_count, 6)
        self.assertEqual(monitor.total_read_failures, 4)
        self.assertEqual(monitor.state, "healthy")
        self.assertEqual(sleeps, [1.0, 1.0, 1.0, 1.0])

    async def test_persistent_read_fault_hard_stops_after_five_attempts(self):
        adapter = self.adapter()
        monitor = self.monitor(adapter)
        await monitor.initialize()
        adapter.get_open_orders.side_effect = OSError("dns unavailable")

        with self.assertRaisesRegex(AccountAuditError, "dns unavailable"):
            await monitor.audit(set())

        self.assertEqual(adapter.get_open_orders.await_count, 6)
        self.assertEqual(monitor.total_read_failures, 5)
        self.assertEqual(monitor.state, "hard_stop")

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
