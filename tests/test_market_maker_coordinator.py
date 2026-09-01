import asyncio
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.adapters.exchanges.models import (
    OrderBookData,
    OrderBookLevel,
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.services.market_maker.account_monitor import AccountAuditError
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.coordinator import MarketMakerCoordinator
from core.services.market_maker.models import (
    DesiredOrder,
    DesiredQuotes,
    MarketMetadata,
    MarketSnapshot,
    PositionSnapshot,
    RuntimeState,
)
from core.services.market_maker.order_manager import (
    MarketMakerOrderManager,
    ReconcileAction,
    ReconcileResult,
)
from core.services.market_maker.risk_manager import RiskDecision
from core.services.market_maker.strategy import SoftExitEconomics


class MarketMakerCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.coordinators: list[MarketMakerCoordinator] = []
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("0"),
        )

    async def asyncTearDown(self) -> None:
        for coordinator in self.coordinators:
            await coordinator.stop()

    def config(self, **overrides) -> MarketMakerConfig:
        values = {
            "symbol": "BTC",
            "order_size": Decimal("0.2"),
            "max_position": Decimal("1"),
            "dry_run": False,
            "refresh_interval_ms": 1000,
            "position_poll_interval_seconds": 60,
            "order_sync_interval_seconds": 60,
            "health_check_interval_seconds": 60,
            "log_status_interval_seconds": 60,
        }
        values.update(overrides)
        return MarketMakerConfig(**values)

    def active_unwind_config(self, **overrides) -> MarketMakerConfig:
        values = {
            "ping_pong_enabled": True,
            "maker_fee_rate": "0.00012",
            "taker_fee_rate": "0.0004",
            "soft_exit_after_seconds": 10,
            "soft_exit_net_turnover_bps": "-0.5",
            "min_completed_net_turnover_bps": "0.1",
            "max_session_loss_for_maker_exit": "0.15",
            "active_unwind_enabled": True,
            "active_unwind_after_seconds": 30,
            "active_unwind_loss_trigger": "0.20",
            "active_unwind_max_slippage_ticks": 2,
            "active_unwind_max_attempts": 2,
            "active_unwind_confirmation_timeout_seconds": 5,
            "max_episode_loss_for_unwind": "0.30",
            "max_session_loss_for_unwind": "0.40",
            "account_audit_interval_seconds": 15,
            "max_session_drawdown": "0.50",
            "require_flat_start": True,
        }
        values.update(overrides)
        return self.config(**values)

    def market(self, received: float | None = None) -> MarketSnapshot:
        return MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("99.9"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.1"), Decimal("1")),),
            best_bid=Decimal("99.9"),
            best_ask=Decimal("100.1"),
            exchange_timestamp=None,
            received_monotonic=self.now if received is None else received,
        )

    def position(
        self,
        received: float | None = None,
        *,
        signed_size: Decimal = Decimal("0"),
    ) -> PositionSnapshot:
        return PositionSnapshot(
            symbol="BTC",
            signed_size=signed_size,
            entry_price=Decimal("100") if signed_size else None,
            unrealized_pnl=Decimal("0"),
            received_monotonic=self.now if received is None else received,
        )

    @staticmethod
    def soft_exit_snapshot(**overrides):
        snapshot = {
            "state": "healthy",
            "age_seconds": 1.0,
            "ledger_position": Decimal("0.2"),
            "completed_turnover": Decimal("100"),
            "completed_net_ex_funding": Decimal("0.003"),
            "completed_fills": 2,
            "last_flat_equity_change": Decimal("0.003"),
            "last_flat_completed_fills": 2,
            "open_episode_turnover": Decimal("20"),
            "open_episode_net_ex_funding": Decimal("-0.0024"),
        }
        snapshot.update(overrides)
        return snapshot

    @staticmethod
    def order(
        order_id: str = "order-1",
        *,
        filled: str = "0",
        status: OrderStatus = OrderStatus.OPEN,
    ) -> OrderData:
        filled_amount = Decimal(filled)
        return OrderData(
            id=order_id,
            client_id=f"client-{order_id}",
            symbol="BTC",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            amount=Decimal("0.2"),
            price=Decimal("99.9"),
            filled=filled_amount,
            remaining=Decimal("0.2") - filled_amount,
            cost=Decimal("0"),
            average=None,
            status=status,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params={},
            raw_data={},
        )

    def risk(self) -> RiskDecision:
        return RiskDecision(
            buy_amount=Decimal("0.2"),
            sell_amount=Decimal("0.2"),
            buy_reduce_only=False,
            sell_reduce_only=False,
            buy_capacity=Decimal("1"),
            sell_capacity=Decimal("1"),
            worst_long=Decimal("0.2"),
            worst_short=Decimal("-0.2"),
            inventory_ratio=Decimal("0"),
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
            safe=True,
        )

    def desired(self) -> DesiredQuotes:
        return DesiredQuotes(
            bid=DesiredOrder(
                OrderSide.BUY,
                Decimal("99.9"),
                Decimal("0.2"),
                False,
                "normal",
            ),
            ask=DesiredOrder(
                OrderSide.SELL,
                Decimal("100.1"),
                Decimal("0.2"),
                False,
                "normal",
            ),
            reference_price=Decimal("100"),
            reservation_price=Decimal("100"),
            half_spread=Decimal("0.1"),
            inventory_ratio=Decimal("0"),
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
        )

    def adapter(self):
        market = {
            "symbol": "BTC",
            "price_decimals": 1,
            "size_decimals": 1,
            "min_base_amount": "0.1",
            "min_quote_amount": "0",
        }
        async def subscribe_orderbook(_symbol, callback):
            await callback(self.market())

        return SimpleNamespace(
            connect=AsyncMock(return_value=True),
            authenticate=AsyncMock(return_value=True),
            get_exchange_info=AsyncMock(
                return_value=SimpleNamespace(markets={"BTC": market})
            ),
            health_check=AsyncMock(return_value={"healthy": True}),
            get_orderbook=AsyncMock(return_value=self.market()),
            get_positions=AsyncMock(return_value=[self.position()]),
            subscribe_orderbook=AsyncMock(side_effect=subscribe_orderbook),
            subscribe_user_data=AsyncMock(),
            subscribe_positions=AsyncMock(),
            unsubscribe=AsyncMock(),
            disconnect=AsyncMock(),
        )

    @staticmethod
    def order_manager():
        return SimpleNamespace(
            initialize=AsyncMock(),
            reconcile=AsyncMock(
                return_value=SimpleNamespace(
                    errors=(), runtime_state=RuntimeState.ACTIVE
                )
            ),
            execute_active_unwind=AsyncMock(),
            handle_order_update=AsyncMock(),
            sync_open_orders=AsyncMock(),
            cancel_managed_orders=AsyncMock(),
            shutdown=AsyncMock(),
            snapshot=Mock(return_value=()),
            known_order_ids=frozenset(),
            active_unwind_order_ids=frozenset(),
            active_unwind_pending=False,
            mutation_generation=0,
            active_unwind_prepared_generation=None,
            pause_reason=None,
            has_uncertain_state=False,
        )

    def coordinator(
        self,
        *,
        adapter=None,
        config=None,
        order_manager=None,
        metadata=None,
        account_monitor=None,
        inventory_executor=None,
    ) -> MarketMakerCoordinator:
        adapter = adapter or self.adapter()
        order_manager = order_manager or self.order_manager()
        coordinator = MarketMakerCoordinator(
            adapter,
            config or self.config(),
            metadata=metadata,
            order_manager=order_manager,
            account_monitor=account_monitor,
            inventory_executor=inventory_executor,
            risk_manager=SimpleNamespace(evaluate=Mock(return_value=self.risk())),
            strategy=SimpleNamespace(
                calculate_quotes=Mock(return_value=self.desired())
            ),
            monotonic=lambda: self.now,
        )
        self.coordinators.append(coordinator)
        return coordinator

    def prepare_running(self, **kwargs) -> MarketMakerCoordinator:
        kwargs.setdefault("metadata", self.metadata)
        coordinator = self.coordinator(**kwargs)
        coordinator._running = True
        coordinator._authenticated = True
        coordinator._exchange_healthy = True
        coordinator._market = self.market()
        coordinator._position = self.position()
        coordinator._transition(RuntimeState.SYNCING)
        return coordinator

    def test_trusted_soft_exit_economics_requires_audited_surplus(self) -> None:
        coordinator = self.coordinator(
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                min_completed_net_turnover_bps=Decimal("0.1"),
                soft_exit_surplus_reserve_bps=Decimal("0.02"),
            )
        )

    @staticmethod
    def active_account_snapshot(position: str) -> dict:
        signed = Decimal(position)
        return {
            "state": "healthy",
            "age_seconds": 0.5,
            "ledger_position": signed,
            "audited_position": signed,
            "audited_unrealized_pnl": Decimal("0"),
            "completed_turnover": Decimal("10"),
            "completed_net_ex_funding": Decimal("0"),
            "completed_fills": 2,
            "last_flat_completed_fills": 2,
            "last_flat_equity_change": Decimal("0"),
            "open_episode_turnover": Decimal("20") if signed else Decimal("0"),
            "open_episode_net_ex_funding": Decimal("-0.0024") if signed else Decimal("0"),
            "current_drawdown": Decimal("0.05"),
            "baseline_equity": Decimal("300"),
            "current_equity": Decimal("299.95") if signed else Decimal("300"),
            "turnover": Decimal("10"),
            "unique_maker_fills": 2,
        }
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.soft_exit_snapshot())
        )

        self.assertEqual(
            coordinator._trusted_soft_exit_economics(self.now),
            SoftExitEconomics(
                completed_turnover=Decimal("100"),
                completed_net=Decimal("0.003"),
                open_turnover=Decimal("20"),
                open_net=Decimal("-0.0024"),
            ),
        )

    def test_loss_budget_accepts_trusted_first_episode_without_surplus(
        self,
    ) -> None:
        config = self.config(
            account_audit_interval_seconds=15,
            max_session_drawdown=Decimal("0.5"),
            max_session_loss_for_maker_exit=Decimal("0.1"),
            require_flat_start=True,
            soft_exit_after_seconds=120,
            soft_exit_net_turnover_bps=Decimal("-0.5"),
            min_completed_net_turnover_bps=Decimal("0.1"),
        )
        coordinator = self.coordinator(config=config)
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(
                return_value=self.soft_exit_snapshot(
                    completed_turnover=Decimal("0"),
                    completed_net_ex_funding=Decimal("0"),
                    completed_fills=0,
                    last_flat_completed_fills=0,
                )
            )
        )

        self.assertEqual(
            coordinator._trusted_soft_exit_economics(self.now),
            SoftExitEconomics(
                completed_turnover=Decimal("0"),
                completed_net=Decimal("0"),
                open_turnover=Decimal("20"),
                open_net=Decimal("-0.0024"),
            ),
        )

        coordinator.config = replace(
            config, max_session_loss_for_maker_exit=Decimal("0")
        )
        self.assertIsNone(coordinator._trusted_soft_exit_economics(self.now))

    def test_loss_budget_rejects_stale_flat_equity_generation(self) -> None:
        coordinator = self.coordinator(
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                max_session_loss_for_maker_exit=Decimal("0.1"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps=Decimal("-0.5"),
            )
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(
                return_value=self.soft_exit_snapshot(
                    completed_fills=4,
                    last_flat_completed_fills=2,
                )
            )
        )

        self.assertIsNone(coordinator._trusted_soft_exit_economics(self.now))

    def test_loss_budget_uses_worse_prior_flat_equity_evidence(self) -> None:
        coordinator = self.coordinator(
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                max_session_loss_for_maker_exit=Decimal("0.1"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps=Decimal("-0.5"),
            )
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(
                return_value=self.soft_exit_snapshot(
                    completed_net_ex_funding=Decimal("0.05"),
                    last_flat_equity_change=Decimal("-0.09"),
                )
            )
        )

        economics = coordinator._trusted_soft_exit_economics(self.now)

        self.assertIsNotNone(economics)
        self.assertEqual(economics.completed_net, Decimal("-0.09"))

    def test_loss_budget_rejects_missing_flat_equity_evidence(self) -> None:
        coordinator = self.coordinator(
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                max_session_loss_for_maker_exit=Decimal("0.1"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                soft_exit_net_turnover_bps=Decimal("-0.5"),
            )
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        snapshot = self.soft_exit_snapshot()
        snapshot.pop("last_flat_equity_change")
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(return_value=snapshot)
        )

        self.assertIsNone(coordinator._trusted_soft_exit_economics(self.now))

    def test_soft_exit_economics_fails_closed_on_untrusted_prerequisites(
        self,
    ) -> None:
        config = self.config(
            account_audit_interval_seconds=15,
            max_session_drawdown=Decimal("0.5"),
            require_flat_start=True,
            soft_exit_after_seconds=120,
            min_completed_net_turnover_bps=Decimal("0.1"),
            soft_exit_surplus_reserve_bps=Decimal("0.02"),
        )
        cases = {
            "dry_run": {"config": replace(config, dry_run=True)},
            "soft_exit_disabled": {
                "config": replace(config, soft_exit_after_seconds=0)
            },
            "monitor_not_initialized": {"initialized": False},
            "monitor_missing": {"monitor": None},
            "position_missing": {"position": None},
            "position_flat": {"position": self.position()},
            "position_non_finite": {
                "position": self.position(signed_size=Decimal("NaN"))
            },
            "position_wrong_type": {
                "position": SimpleNamespace(signed_size="0.2")
            },
            "position_missing_size": {"position": SimpleNamespace()},
        }
        for name, case in cases.items():
            with self.subTest(name=name):
                coordinator = self.coordinator(config=case.get("config", config))
                coordinator._position = case.get(
                    "position", self.position(signed_size=Decimal("0.2"))
                )
                coordinator._account_monitor_initialized = case.get(
                    "initialized", True
                )
                coordinator.account_monitor = case.get(
                    "monitor",
                    SimpleNamespace(
                        snapshot=Mock(return_value=self.soft_exit_snapshot())
                    ),
                )
                self.assertIsNone(
                    coordinator._trusted_soft_exit_economics(self.now)
                )

    def test_soft_exit_economics_fails_closed_on_untrusted_audit_snapshot(
        self,
    ) -> None:
        coordinator = self.coordinator(
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                min_completed_net_turnover_bps=Decimal("0.1"),
                soft_exit_surplus_reserve_bps=Decimal("0.02"),
            )
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        monitor = SimpleNamespace(snapshot=Mock())
        coordinator.account_monitor = monitor
        cases = {
            "not_mapping": None,
            "unhealthy": self.soft_exit_snapshot(state="hard_stop"),
            "missing_age": self.soft_exit_snapshot(age_seconds=None),
            "boolean_age": self.soft_exit_snapshot(age_seconds=True),
            "negative_age": self.soft_exit_snapshot(age_seconds=-0.1),
            "stale": self.soft_exit_snapshot(age_seconds=21.0),
            "non_finite_age": self.soft_exit_snapshot(age_seconds=float("nan")),
            "position_mismatch": self.soft_exit_snapshot(
                ledger_position=Decimal("0.1")
            ),
            "completed_turnover_zero": self.soft_exit_snapshot(
                completed_turnover=Decimal("0")
            ),
            "open_turnover_negative": self.soft_exit_snapshot(
                open_episode_turnover=Decimal("-1")
            ),
            "decimal_missing": self.soft_exit_snapshot(
                open_episode_net_ex_funding=None
            ),
            "decimal_wrong_type": self.soft_exit_snapshot(
                completed_net_ex_funding="0.003"
            ),
            "decimal_non_finite": self.soft_exit_snapshot(
                open_episode_net_ex_funding=Decimal("Infinity")
            ),
            "no_surplus": self.soft_exit_snapshot(
                completed_net_ex_funding=Decimal("0.001")
            ),
            "reserve_consumes_surplus": self.soft_exit_snapshot(
                completed_net_ex_funding=Decimal("0.0011")
            ),
        }
        for name, snapshot in cases.items():
            with self.subTest(name=name):
                monitor.snapshot.side_effect = None
                monitor.snapshot.return_value = snapshot
                self.assertIsNone(
                    coordinator._trusted_soft_exit_economics(self.now)
                )

        monitor.snapshot.side_effect = RuntimeError("snapshot unavailable")
        self.assertIsNone(coordinator._trusted_soft_exit_economics(self.now))

    async def test_cycle_passes_only_trusted_soft_exit_economics(self) -> None:
        coordinator = self.prepare_running(
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                min_completed_net_turnover_bps=Decimal("0.1"),
                soft_exit_surplus_reserve_bps=Decimal("0.02"),
            )
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.soft_exit_snapshot())
        )

        await coordinator.run_one_cycle(force=True)

        economics = coordinator.strategy.calculate_quotes.call_args.kwargs[
            "soft_exit_economics"
        ]
        self.assertIsInstance(economics, SoftExitEconomics)
        self.assertEqual(economics.completed_net, Decimal("0.003"))

    async def test_strategy_hard_stop_cancels_before_reconcile(self) -> None:
        reason = (
            "soft exit is stranded outside the passive market "
            "by the economic gate"
        )
        order_manager = self.order_manager()
        order_manager.cancel_managed_orders.return_value = SimpleNamespace(
            actions=(),
            errors=(),
            fill_observed=False,
            position_refresh_required=False,
        )
        coordinator = self.prepare_running(order_manager=order_manager)
        coordinator.strategy.calculate_quotes.return_value = replace(
            self.desired(),
            bid=None,
            ask=None,
            runtime_state=RuntimeState.PAUSED_ERROR,
            reason=reason,
        )

        with self.assertRaisesRegex(RuntimeError, "strategy hard stop"):
            await coordinator.run_one_cycle(force=True)

        order_manager.cancel_managed_orders.assert_awaited_once_with(reason)
        order_manager.reconcile.assert_not_awaited()
        self.assertIs(coordinator._state, RuntimeState.PAUSED_ERROR)

    async def test_stranded_soft_exit_cancels_before_reconcile(self) -> None:
        reason = (
            "soft exit is stranded outside the normal passive quote "
            "band by the economic gate"
        )
        cases = (
            (
                Decimal("-0.2"),
                replace(
                    self.risk(),
                    buy_amount=Decimal("0.2"),
                    sell_amount=None,
                    buy_reduce_only=True,
                    soft_exit_latched=True,
                    runtime_state=RuntimeState.RISK_REDUCTION,
                ),
                replace(
                    self.desired(),
                    bid=DesiredOrder(
                        OrderSide.BUY,
                        Decimal("77870.0"),
                        Decimal("0.2"),
                        True,
                        "soft_exit_hard_fallback",
                    ),
                    ask=None,
                    reference_price=Decimal("78139.2"),
                    reservation_price=Decimal("78139.2"),
                    half_spread=Decimal("25.0"),
                    reason="soft_exit_hard_fallback",
                    runtime_state=RuntimeState.RISK_REDUCTION,
                ),
            ),
            (
                Decimal("0.2"),
                replace(
                    self.risk(),
                    buy_amount=None,
                    sell_amount=Decimal("0.2"),
                    sell_reduce_only=True,
                    soft_exit_latched=True,
                    runtime_state=RuntimeState.RISK_REDUCTION,
                ),
                replace(
                    self.desired(),
                    bid=None,
                    ask=DesiredOrder(
                        OrderSide.SELL,
                        Decimal("77908.2"),
                        Decimal("0.2"),
                        True,
                        "soft_exit_active",
                    ),
                    reference_price=Decimal("77600.1"),
                    reservation_price=Decimal("77600.1"),
                    half_spread=Decimal("25.0"),
                    reason="soft_exit_active",
                    runtime_state=RuntimeState.RISK_REDUCTION,
                ),
            ),
        )
        for position, risk, desired in cases:
            with self.subTest(position=position):
                order_manager = self.order_manager()
                order_manager.cancel_managed_orders.return_value = (
                    SimpleNamespace(
                        actions=(),
                        errors=(),
                        fill_observed=False,
                        position_refresh_required=False,
                    )
                )
                coordinator = self.prepare_running(order_manager=order_manager)
                coordinator._position = self.position(signed_size=position)
                coordinator.risk_manager.evaluate.return_value = risk
                coordinator.strategy.calculate_quotes.return_value = desired

                with self.assertRaisesRegex(RuntimeError, "strategy hard stop"):
                    await coordinator.run_one_cycle(force=True)

                order_manager.cancel_managed_orders.assert_awaited_once_with(
                    reason
                )
                order_manager.reconcile.assert_not_awaited()
                self.assertIs(coordinator._state, RuntimeState.PAUSED_ERROR)
                self.assertEqual(coordinator.metrics.quote_reason, reason)

    async def test_active_unwind_suppresses_stranded_quote_before_barrier(
        self,
    ) -> None:
        manager = self.order_manager()
        monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.active_account_snapshot("-0.2")),
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            last_audit_monotonic=self.now,
        )
        coordinator = self.prepare_running(
            config=self.active_unwind_config(),
            order_manager=manager,
            account_monitor=monitor,
        )
        coordinator._account_monitor_initialized = True
        coordinator._audited_fill_generation = 0
        coordinator._processed_fill_generation = 0
        coordinator.metrics.account_audit = self.active_account_snapshot("-0.2")
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        coordinator._market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("100.5"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.7"), Decimal("1")),),
            best_bid=Decimal("100.5"),
            best_ask=Decimal("100.7"),
            exchange_timestamp=None,
            received_monotonic=self.now,
        )
        coordinator.risk_manager.evaluate.return_value = replace(
            self.risk(),
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            soft_exit_latched=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
        )
        coordinator.strategy.calculate_quotes.return_value = replace(
            self.desired(),
            bid=DesiredOrder(
                OrderSide.BUY,
                Decimal("99.8"),
                Decimal("0.2"),
                True,
                "soft_exit_hard_fallback",
            ),
            ask=None,
            reference_price=Decimal("100.6"),
            reservation_price=Decimal("100.6"),
            half_spread=Decimal("0.1"),
            runtime_state=RuntimeState.RISK_REDUCTION,
        )

        await coordinator.run_one_cycle(force=True)

        reconciled = manager.reconcile.await_args.args[0]
        self.assertIsNone(reconciled.bid)
        self.assertIsNone(reconciled.ask)
        manager.execute_active_unwind.assert_not_awaited()
        self.assertEqual(
            coordinator.metrics.inventory_unwind["state"], "passive_wait"
        )

    async def test_active_unwind_error_is_terminal_hard_stop(self) -> None:
        manager = self.order_manager()
        manager.execute_active_unwind.return_value = ReconcileResult(
            (),
            RuntimeState.RISK_REDUCTION,
            errors=("active unwind terminal proof is unavailable: timeout",),
        )
        monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.active_account_snapshot("-0.2")),
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            last_audit_monotonic=self.now,
        )
        coordinator = self.prepare_running(
            config=self.active_unwind_config(
                active_unwind_loss_trigger="0.001"
            ),
            order_manager=manager,
            account_monitor=monitor,
        )
        coordinator._account_monitor_initialized = True
        coordinator.metrics.account_audit = self.active_account_snapshot("-0.2")
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        coordinator._market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("100.5"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.7"), Decimal("1")),),
            best_bid=Decimal("100.5"),
            best_ask=Decimal("100.7"),
            exchange_timestamp=None,
            received_monotonic=self.now,
        )
        coordinator.risk_manager.evaluate.return_value = replace(
            self.risk(),
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            soft_exit_latched=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
        )
        coordinator.strategy.calculate_quotes.return_value = replace(
            self.desired(),
            bid=DesiredOrder(
                OrderSide.BUY,
                Decimal("99.8"),
                Decimal("0.2"),
                True,
                "soft_exit_hard_fallback",
            ),
            ask=None,
            reference_price=Decimal("100.6"),
            reservation_price=Decimal("100.6"),
            half_spread=Decimal("0.1"),
            runtime_state=RuntimeState.RISK_REDUCTION,
        )

        with self.assertRaisesRegex(RuntimeError, "active unwind hard stop"):
            await coordinator.run_one_cycle(force=True)

        manager.execute_active_unwind.assert_awaited_once()
        manager.cancel_managed_orders.assert_awaited()
        self.assertIs(coordinator._state, RuntimeState.PAUSED_ORDER_STATE)
        self.assertEqual(
            coordinator.metrics.counters["reconciliation_failure"], 1
        )

    async def test_active_unwind_budget_block_counts_episode_cap(self) -> None:
        manager = self.order_manager()
        unwind = SimpleNamespace(
            blocked=True,
            budget_blocked=True,
            active_order=None,
            passive_order=None,
            suppress_passive=True,
            reason="marketable active unwind exceeds episode loss cap",
            snapshot=Mock(
                return_value={
                    "state": "blocked",
                    "budget_blocked": True,
                }
            ),
        )
        coordinator = self.prepare_running(
            config=self.active_unwind_config(),
            order_manager=manager,
            inventory_executor=SimpleNamespace(evaluate=Mock(return_value=unwind)),
        )
        coordinator._position = self.position(signed_size=Decimal("-0.2"))

        with self.assertRaisesRegex(RuntimeError, "inventory unwind hard stop"):
            await coordinator.run_one_cycle(force=True)

        self.assertEqual(
            coordinator.metrics.counters["episode_cap_blocked"], 1
        )
        self.assertEqual(
            coordinator.metrics.counters["active_unwind_blocks"], 1
        )

    async def test_active_unwind_rearms_and_bypasses_debounce_after_truth_drift(
        self,
    ) -> None:
        manager = self.order_manager()
        prepare_result = ReconcileResult(
            (
                ReconcileAction(
                    OrderSide.BUY,
                    "prepare_active_unwind",
                    "zero symbol orders proved; fresh truth required",
                    success=True,
                ),
            ),
            RuntimeState.RISK_REDUCTION,
            position_refresh_required=True,
        )
        execute_result = ReconcileResult(
            (
                ReconcileAction(
                    OrderSide.BUY,
                    "active_unwind",
                    "active unwind terminal no-fill",
                    success=True,
                ),
            ),
            RuntimeState.RISK_REDUCTION,
        )

        async def execute_active_unwind(_desired, *, prepared_generation=None):
            if prepared_generation is None:
                manager.active_unwind_prepared_generation = 1
                return prepare_result
            self.assertEqual(prepared_generation, 1)
            manager.active_unwind_prepared_generation = None
            return execute_result

        manager.execute_active_unwind.side_effect = execute_active_unwind
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            last_audit_monotonic=self.now,
        )
        adapter = self.adapter()
        coordinator = self.prepare_running(
            adapter=adapter,
            config=self.active_unwind_config(),
            order_manager=manager,
            account_monitor=monitor,
        )
        monitor.snapshot = Mock(
            side_effect=lambda _now: self.active_account_snapshot(
                "-0.2" if coordinator._position is None or coordinator._position.signed_size else "0"
            )
        )
        coordinator._account_monitor_initialized = True
        coordinator.metrics.account_audit = self.active_account_snapshot("-0.2")
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        initial_market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("100.5"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.7"), Decimal("1")),),
            best_bid=Decimal("100.5"),
            best_ask=Decimal("100.7"),
            exchange_timestamp=None,
            received_monotonic=self.now,
        )
        coordinator._market = initial_market
        coordinator.risk_manager.evaluate.return_value = replace(
            self.risk(),
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            soft_exit_latched=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
        )
        coordinator.strategy.calculate_quotes.return_value = replace(
            self.desired(),
            bid=DesiredOrder(
                OrderSide.BUY, Decimal("99.8"), Decimal("0.2"), True, "soft"
            ),
            ask=None,
            reference_price=Decimal("100.6"),
            reservation_price=Decimal("100.6"),
            half_spread=Decimal("0.1"),
            runtime_state=RuntimeState.RISK_REDUCTION,
        )

        await coordinator.run_one_cycle(force=True)
        self.now = 131.0
        coordinator._market = replace(initial_market, received_monotonic=self.now)
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        refreshed_market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("100.7"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.9"), Decimal("1")),),
            best_bid=Decimal("100.7"),
            best_ask=Decimal("100.9"),
            exchange_timestamp=None,
            received_monotonic=self.now,
        )
        adapter.get_orderbook.return_value = refreshed_market
        adapter.get_positions.return_value = [
            self.position(signed_size=Decimal("-0.2"))
        ]

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(manager.execute_active_unwind.await_count, 1)
        monitor.audit.assert_awaited_once()
        await coordinator.on_position(
            self.position(received=self.now, signed_size=Decimal("-0.2"))
        )
        self.assertIsNone(coordinator._active_unwind_truth_token)
        self.now = 132.0
        await coordinator.run_one_cycle(force=True)
        self.assertEqual(manager.execute_active_unwind.await_count, 1)
        self.assertEqual(monitor.audit.await_count, 2)
        self.assertIsNotNone(coordinator._active_unwind_truth_token)

        await coordinator.run_one_cycle()

        intents = [call.args[0] for call in manager.execute_active_unwind.await_args_list]
        self.assertEqual([intent.price for intent in intents], [Decimal("100.9"), Decimal("101.1")])
        self.assertIsNone(
            manager.execute_active_unwind.await_args_list[0].kwargs[
                "prepared_generation"
            ]
        )
        self.assertEqual(
            manager.execute_active_unwind.await_args_list[1].kwargs[
                "prepared_generation"
            ],
            1,
        )
        self.assertEqual(
            coordinator.inventory_executor._active_attempts, 1
        )
        self.assertEqual(
            coordinator.metrics.counters["active_unwind_attempts"], 1
        )

    async def test_cycle_debounce_still_applies_without_active_truth(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._last_cycle_monotonic = self.now

        await coordinator.run_one_cycle()

        manager.reconcile.assert_not_awaited()
        manager.execute_active_unwind.assert_not_awaited()

    async def test_active_unwind_rearm_refresh_failure_fails_closed(self) -> None:
        manager = self.order_manager()
        manager.active_unwind_prepared_generation = 1
        manager.cancel_managed_orders.return_value = ReconcileResult(
            (), RuntimeState.PAUSED_ERROR
        )
        monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.active_account_snapshot("-0.2")),
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            last_audit_monotonic=self.now,
        )
        adapter = self.adapter()
        adapter.get_positions.side_effect = RuntimeError("position unavailable")
        coordinator = self.prepare_running(
            adapter=adapter,
            config=self.active_unwind_config(
                active_unwind_loss_trigger="0.001"
            ),
            order_manager=manager,
            account_monitor=monitor,
        )
        coordinator._account_monitor_initialized = True
        coordinator.metrics.account_audit = self.active_account_snapshot("-0.2")
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        coordinator._market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("100.5"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.7"), Decimal("1")),),
            best_bid=Decimal("100.5"),
            best_ask=Decimal("100.7"),
            exchange_timestamp=None,
            received_monotonic=self.now,
        )
        coordinator.risk_manager.evaluate.return_value = replace(
            self.risk(),
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            soft_exit_latched=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
        )
        coordinator.strategy.calculate_quotes.return_value = replace(
            self.desired(),
            bid=DesiredOrder(
                OrderSide.BUY,
                Decimal("99.8"),
                Decimal("0.2"),
                True,
                "soft",
            ),
            ask=None,
            reference_price=Decimal("100.6"),
            reservation_price=Decimal("100.6"),
            half_spread=Decimal("0.1"),
            runtime_state=RuntimeState.RISK_REDUCTION,
        )

        await coordinator.run_one_cycle(force=True)

        manager.execute_active_unwind.assert_not_awaited()
        adapter.get_positions.assert_awaited_once_with(["BTC"])
        manager.cancel_managed_orders.assert_awaited_once()
        monitor.mark_hard_stop.assert_called_once()
        self.assertIs(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertIsNone(coordinator._active_unwind_truth_token)
        self.assertEqual(coordinator.inventory_executor._active_attempts, 0)
        self.assertEqual(
            coordinator.metrics.counters["active_unwind_attempts"], 0
        )

    def test_active_unwind_truth_token_is_one_shot_and_rejects_drift(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.active_unwind_prepared_generation = 1
        monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.active_account_snapshot("-0.2")),
            last_audit_monotonic=self.now,
        )
        coordinator = self.prepare_running(
            config=self.active_unwind_config(),
            order_manager=manager,
            account_monitor=monitor,
        )
        coordinator._account_monitor_initialized = True
        coordinator._audited_fill_generation = 0
        coordinator._processed_fill_generation = 0
        coordinator._position = self.position(signed_size=Decimal("-0.2"))

        self.assertTrue(coordinator._arm_active_unwind_truth(7))
        self.assertEqual(coordinator._consume_active_unwind_truth(7, self.now), 1)
        self.assertIsNone(coordinator._consume_active_unwind_truth(7, self.now))

        self.assertTrue(coordinator._arm_active_unwind_truth(7))
        manager.mutation_generation += 1
        self.assertIsNone(coordinator._consume_active_unwind_truth(7, self.now))

    async def test_exact_active_terminal_is_audited_with_taker_role_ids(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"ioc-1"})
        manager.active_unwind_order_ids = frozenset({"ioc-1"})
        manager.execute_active_unwind.return_value = ReconcileResult(
            (
                ReconcileAction(
                    OrderSide.BUY,
                    "active_unwind",
                    "active unwind filled",
                    Decimal("100.9"),
                    Decimal("0.2"),
                    True,
                    True,
                ),
            ),
            RuntimeState.RISK_REDUCTION,
            position_refresh_required=True,
            fill_observed=True,
        )
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            last_audit_monotonic=self.now,
        )
        adapter = self.adapter()
        coordinator = self.prepare_running(
            adapter=adapter,
            config=self.active_unwind_config(),
            order_manager=manager,
            account_monitor=monitor,
        )
        monitor.snapshot = Mock(
            side_effect=lambda _now: self.active_account_snapshot(
                "-0.2" if coordinator._position is None or coordinator._position.signed_size else "0"
            )
        )
        coordinator._account_monitor_initialized = True
        coordinator.metrics.account_audit = self.active_account_snapshot("-0.2")
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        coordinator._market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("100.5"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.7"), Decimal("1")),),
            best_bid=Decimal("100.5"),
            best_ask=Decimal("100.7"),
            exchange_timestamp=None,
            received_monotonic=self.now,
        )
        coordinator.risk_manager.evaluate.return_value = replace(
            self.risk(),
            buy_amount=Decimal("0.2"),
            sell_amount=None,
            buy_reduce_only=True,
            soft_exit_latched=True,
            runtime_state=RuntimeState.RISK_REDUCTION,
        )
        coordinator.strategy.calculate_quotes.return_value = replace(
            self.desired(),
            bid=DesiredOrder(
                OrderSide.BUY, Decimal("99.8"), Decimal("0.2"), True, "soft"
            ),
            ask=None,
            reference_price=Decimal("100.6"),
            reservation_price=Decimal("100.6"),
            half_spread=Decimal("0.1"),
            runtime_state=RuntimeState.RISK_REDUCTION,
        )

        await coordinator.run_one_cycle(force=True)
        self.now = 131.0
        coordinator._market = replace(
            coordinator._market, received_monotonic=self.now
        )
        coordinator._position = self.position(signed_size=Decimal("-0.2"))
        adapter.get_orderbook.return_value = replace(
            coordinator._market, received_monotonic=self.now
        )
        adapter.get_positions.return_value = [self.position(signed_size=Decimal("0"))]

        await coordinator.run_one_cycle(force=True)

        monitor.audit.assert_awaited_once_with(
            {"ioc-1"}, active_unwind_order_ids=frozenset({"ioc-1"})
        )
        self.assertEqual(coordinator._processed_fill_generation, 1)
        self.assertEqual(coordinator._audited_fill_generation, 1)
        self.assertEqual(coordinator.metrics.counters["active_unwind_success"], 1)

    async def test_stranded_soft_exit_guard_preserves_normal_band(self) -> None:
        cases = (
            (False, OrderSide.BUY, Decimal("-0.2"), Decimal("70")),
            (False, OrderSide.SELL, Decimal("0.2"), Decimal("130")),
            (True, OrderSide.BUY, Decimal("-0.2"), Decimal("89.9")),
            (True, OrderSide.SELL, Decimal("0.2"), Decimal("110.1")),
        )
        for soft_exit_latched, side, position, price in cases:
            with self.subTest(
                soft_exit_latched=soft_exit_latched,
                side=side,
                price=price,
            ):
                is_buy = side is OrderSide.BUY
                order_manager = self.order_manager()
                coordinator = self.prepare_running(order_manager=order_manager)
                coordinator._position = self.position(signed_size=position)
                coordinator.risk_manager.evaluate.return_value = replace(
                    self.risk(),
                    buy_amount=Decimal("0.2") if is_buy else None,
                    sell_amount=None if is_buy else Decimal("0.2"),
                    buy_reduce_only=is_buy,
                    sell_reduce_only=not is_buy,
                    soft_exit_latched=soft_exit_latched,
                    runtime_state=RuntimeState.RISK_REDUCTION,
                )
                order = DesiredOrder(
                    side,
                    price,
                    Decimal("0.2"),
                    True,
                    "timed reduction",
                )
                coordinator.strategy.calculate_quotes.return_value = replace(
                    self.desired(),
                    bid=order if is_buy else None,
                    ask=None if is_buy else order,
                    half_spread=Decimal("10"),
                    runtime_state=RuntimeState.RISK_REDUCTION,
                )

                await coordinator.run_one_cycle(force=True)

                order_manager.cancel_managed_orders.assert_not_awaited()
                order_manager.reconcile.assert_awaited_once()

    async def test_ping_pong_waits_for_authenticated_flat_checkpoint(
        self,
    ) -> None:
        coordinator = self.prepare_running(
            config=self.config(
                ping_pong_enabled=True,
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
            )
        )
        coordinator._account_monitor_initialized = True
        coordinator._processed_fill_generation = 1
        coordinator._audited_fill_generation = 0
        coordinator.metrics.account_audit["ledger_position"] = Decimal("0")

        await coordinator.run_one_cycle(force=True)

        self.assertFalse(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )

        coordinator._audited_fill_generation = 1
        coordinator.metrics.account_audit["ledger_position"] = Decimal("0.2")
        await coordinator.run_one_cycle(force=True)

        self.assertFalse(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )

        coordinator.metrics.account_audit["ledger_position"] = Decimal("0")
        await coordinator.run_one_cycle(force=True)

        self.assertTrue(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )

    async def test_entry_admission_reserves_full_lot_active_exit(self) -> None:
        snapshot = self.active_account_snapshot("0")
        snapshot["remaining_session_loss_for_unwind"] = Decimal("0.29")
        monitor = SimpleNamespace(snapshot=Mock(return_value=snapshot))
        coordinator = self.prepare_running(
            config=self.active_unwind_config(),
            account_monitor=monitor,
        )
        coordinator._account_monitor_initialized = True
        coordinator.metrics.account_audit["ledger_position"] = Decimal("0")

        await coordinator.run_one_cycle(force=True)
        await coordinator.run_one_cycle(force=True)

        self.assertFalse(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )
        self.assertEqual(
            coordinator.metrics.account_audit["reserved_worst_case_exit_cost"],
            Decimal("0.30"),
        )
        self.assertEqual(
            coordinator.metrics.account_audit["entry_admission"], "blocked"
        )
        self.assertEqual(
            coordinator.metrics.counters["episode_cap_blocked"], 1
        )

        snapshot["remaining_session_loss_for_unwind"] = Decimal("0.30")
        await coordinator.run_one_cycle(force=True)

        self.assertTrue(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )
        self.assertEqual(
            coordinator.metrics.account_audit["entry_admission"], "allowed"
        )

        snapshot["current_drawdown"] = Decimal("0.20")
        await coordinator.run_one_cycle(force=True)

        self.assertFalse(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )
        self.assertEqual(
            coordinator.metrics.account_audit["entry_admission_reason"],
            "remaining drawdown cannot fund a full-lot stop",
        )

    async def test_entry_admission_fails_closed_without_fresh_economics(
        self,
    ) -> None:
        coordinator = self.prepare_running(config=self.active_unwind_config())
        coordinator._account_monitor_initialized = True
        coordinator.metrics.account_audit["ledger_position"] = Decimal("0")

        await coordinator.run_one_cycle(force=True)

        self.assertFalse(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )
        self.assertEqual(
            coordinator.metrics.account_audit["entry_admission_reason"],
            "entry admission requires fresh authenticated economics",
        )
        self.assertEqual(
            coordinator.metrics.counters["episode_cap_blocked"], 0
        )

        snapshot = self.active_account_snapshot("0")
        snapshot["remaining_session_loss_for_unwind"] = Decimal("0.29")
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(return_value=snapshot)
        )
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(
            coordinator.metrics.counters["episode_cap_blocked"], 1
        )

    async def test_entry_admission_does_not_block_zero_mutation_dry_run(
        self,
    ) -> None:
        coordinator = self.prepare_running(
            config=self.active_unwind_config(dry_run=True)
        )

        await coordinator.run_one_cycle(force=True)

        self.assertTrue(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )
        self.assertEqual(
            coordinator.metrics.account_audit["entry_admission_reason"],
            "dry run has no exchange mutations",
        )
        self.assertEqual(
            coordinator.metrics.counters["episode_cap_blocked"], 0
        )

    async def test_economic_stop_pending_forces_inventory_exit(self) -> None:
        coordinator = self.prepare_running()
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator.metrics.account_audit["economic_state"] = (
            "economic_stop_pending_flat"
        )

        await coordinator.run_one_cycle(force=True)

        self.assertTrue(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "force_inventory_exit"
            ]
        )
        self.assertFalse(
            coordinator.risk_manager.evaluate.call_args.kwargs[
                "allow_new_episode"
            ]
        )

    def test_maker_turnover_rate_excludes_active_unwind_turnover(self) -> None:
        monitor = SimpleNamespace(
            snapshot=Mock(
                return_value={
                    "maker_turnover": Decimal("10"),
                    "turnover": Decimal("25"),
                    "unique_maker_fills": 2,
                }
            )
        )
        coordinator = self.coordinator(account_monitor=monitor)
        coordinator._eligible_quote_seconds = 3600

        coordinator._update_account_audit_metrics()

        self.assertEqual(
            coordinator.metrics.account_audit[
                "maker_turnover_per_eligible_hour"
            ],
            Decimal("10"),
        )

    def test_side_markouts_capture_horizons_and_excursions(self) -> None:
        metrics = self.coordinator().metrics
        for order_id, side in (("buy-1", "buy"), ("sell-1", "sell")):
            metrics.record_maker_fill_markout(
                order_id=order_id,
                side=side,
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=0.0,
                mid=Decimal("100"),
                source="websocket_order_update",
                terminal=True,
            )

        for now, mid in (
            (1.0, Decimal("99")),
            (5.0, Decimal("101")),
            (15.0, Decimal("98")),
            (60.0, Decimal("102")),
        ):
            metrics.update_fill_markouts(now=now, mid=mid)

        snapshot = metrics.snapshot(60.0)
        buy, sell = snapshot["fill_markouts"]
        self.assertEqual(buy["markout_1s_bps"], Decimal("-100"))
        self.assertEqual(buy["markout_60s_bps"], Decimal("200"))
        self.assertEqual(buy["mae_bps"], Decimal("-200"))
        self.assertEqual(buy["mfe_bps"], Decimal("200"))
        self.assertEqual(sell["markout_60s_bps"], Decimal("-200"))
        self.assertEqual(
            snapshot["side_markout_summary"]["buy"]["60s"],
            {"count": 1, "mean_bps": Decimal("200")},
        )
        self.assertEqual(
            snapshot["side_markout_summary"]["sell"]["60s"],
            {"count": 1, "mean_bps": Decimal("-200")},
        )

    def test_markouts_use_incremental_partial_fill_price(self) -> None:
        metrics = self.coordinator().metrics

        first = metrics.record_maker_fill_markout(
            order_id="buy-partial",
            side="buy",
            cumulative_filled=Decimal("0.1"),
            cumulative_cost=Decimal("10"),
            average_price=None,
            now=0.0,
            mid=Decimal("100"),
            source="websocket_order_update",
            terminal=False,
        )
        second = metrics.record_maker_fill_markout(
            order_id="buy-partial",
            side="buy",
            cumulative_filled=Decimal("0.2"),
            cumulative_cost=Decimal("20.2"),
            average_price=None,
            now=1.0,
            mid=Decimal("100"),
            source="reconciliation",
            terminal=True,
        )
        duplicate = metrics.record_maker_fill_markout(
            order_id="buy-partial",
            side="buy",
            cumulative_filled=Decimal("0.2"),
            cumulative_cost=Decimal("20.2"),
            average_price=None,
            now=2.0,
            mid=Decimal("100"),
            source="reconciliation",
            terminal=True,
        )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertFalse(duplicate)
        first_event, second_event = metrics.fill_markouts
        self.assertEqual(first_event["fill_amount"], Decimal("0.1"))
        self.assertEqual(first_event["fill_price"], Decimal("100"))
        self.assertEqual(second_event["fill_amount"], Decimal("0.1"))
        self.assertEqual(second_event["fill_price"], Decimal("102"))
        self.assertEqual(
            second_event["observation_source"], "reconciliation"
        )

    def test_live_partial_progress_survives_terminal_history_churn(self) -> None:
        metrics = self.coordinator().metrics
        metrics.record_maker_fill_markout(
            order_id="live-partial",
            side="buy",
            cumulative_filled=Decimal("0.1"),
            cumulative_cost=Decimal("10"),
            average_price=None,
            now=0.0,
            mid=Decimal("100"),
            source="websocket_order_update",
            terminal=False,
        )
        for index in range(501):
            metrics.record_maker_fill_markout(
                order_id=f"terminal-{index}",
                side="buy",
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=float(index + 1),
                mid=Decimal("100"),
                source="reconciliation",
                terminal=True,
            )

        recorded = metrics.record_maker_fill_markout(
            order_id="live-partial",
            side="buy",
            cumulative_filled=Decimal("0.2"),
            cumulative_cost=Decimal("20.2"),
            average_price=None,
            now=503.0,
            mid=Decimal("100"),
            source="reconciliation",
            terminal=True,
        )

        self.assertTrue(recorded)
        self.assertLessEqual(len(metrics._maker_fill_progress), 500)
        self.assertEqual(metrics.fill_markouts[-1]["fill_amount"], Decimal("0.1"))
        self.assertEqual(metrics.fill_markouts[-1]["fill_price"], Decimal("102"))

    def test_terminal_without_new_fill_releases_progress_pin(self) -> None:
        metrics = self.coordinator().metrics
        for index in range(501):
            order_id = f"partial-cancel-{index}"
            metrics.record_maker_fill_markout(
                order_id=order_id,
                side="buy",
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=float(index),
                mid=Decimal("100"),
                source="websocket_order_update",
                terminal=False,
            )
            duplicate_terminal = metrics.record_maker_fill_markout(
                order_id=order_id,
                side="buy",
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=float(index) + 0.5,
                mid=Decimal("100"),
                source="reconciliation",
                terminal=True,
            )
            self.assertFalse(duplicate_terminal)

        self.assertLessEqual(len(metrics._maker_fill_progress), 500)
        self.assertEqual(metrics._maker_fill_open_ids, set())

    async def test_reconciliation_fill_is_forwarded_to_markouts(self) -> None:
        observed = replace(
            self.order("reconciled-fill", filled="0.2", status=OrderStatus.FILLED),
            cost=Decimal("20"),
            average=Decimal("100"),
        )
        manager = self.order_manager()
        manager.reconcile.return_value = ReconcileResult(
            actions=(),
            runtime_state=RuntimeState.ACTIVE,
            position_refresh_required=True,
            fill_observed=True,
            observed_fill_orders=(observed,),
        )
        coordinator = self.prepare_running(order_manager=manager)

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(len(coordinator.metrics.fill_markouts), 1)
        event = coordinator.metrics.fill_markouts[0]
        self.assertEqual(event["order_id"], "reconciled-fill")
        self.assertEqual(event["fill_price"], Decimal("100"))
        self.assertEqual(event["observation_source"], "reconciliation")

    async def test_soft_exit_waits_for_audit_after_processed_fill_updates(
        self,
    ) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            self.position(signed_size=Decimal("0.2"))
        ]
        coordinator = self.prepare_running(
            adapter=adapter,
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                min_completed_net_turnover_bps=Decimal("0.1"),
                soft_exit_surplus_reserve_bps=Decimal("0.02"),
            ),
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            audit=AsyncMock(),
            snapshot=Mock(return_value=self.soft_exit_snapshot()),
        )
        coordinator.order_manager.handle_order_update.side_effect = [True, True]
        sell_fill = replace(
            self.order("sell-fill", filled="0.1"),
            side=OrderSide.SELL,
        )
        buy_fill = replace(
            self.order("buy-fill", filled="0.1"),
            side=OrderSide.BUY,
        )

        await coordinator.on_order_update(sell_fill)
        await coordinator.on_order_update(buy_fill)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator._processed_fill_generation, 2)
        self.assertEqual(coordinator._audited_fill_generation, 0)
        self.assertIsNone(
            coordinator.strategy.calculate_quotes.call_args.kwargs[
                "soft_exit_economics"
            ]
        )

        await coordinator.audit_account_once()
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator._audited_fill_generation, 2)
        economics = coordinator.strategy.calculate_quotes.call_args.kwargs[
            "soft_exit_economics"
        ]
        self.assertIsInstance(economics, SoftExitEconomics)
        self.assertEqual(economics.completed_net, Decimal("0.003"))

    async def test_reconcile_fill_invalidates_same_position_audit(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            self.position(signed_size=Decimal("0.2"))
        ]
        manager = self.order_manager()
        manager.reconcile.return_value = ReconcileResult(
            (),
            RuntimeState.ACTIVE,
            position_refresh_required=True,
            fill_observed=True,
        )
        coordinator = self.prepare_running(
            adapter=adapter,
            order_manager=manager,
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                min_completed_net_turnover_bps=Decimal("0.1"),
                soft_exit_surplus_reserve_bps=Decimal("0.02"),
            ),
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.soft_exit_snapshot())
        )

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(
            coordinator.position_snapshot.signed_size, Decimal("0.2")
        )
        self.assertEqual(coordinator._processed_fill_generation, 1)
        self.assertEqual(coordinator._audited_fill_generation, 0)
        self.assertIsNone(coordinator._trusted_soft_exit_economics(self.now))

    async def test_reprice_refresh_without_fill_keeps_audit_trusted(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            self.position(signed_size=Decimal("0.2"))
        ]
        manager = self.order_manager()
        manager.reconcile.return_value = ReconcileResult(
            (),
            RuntimeState.ACTIVE,
            position_refresh_required=True,
            fill_observed=False,
        )
        coordinator = self.prepare_running(
            adapter=adapter,
            order_manager=manager,
            config=self.config(
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
                soft_exit_after_seconds=120,
                min_completed_net_turnover_bps=Decimal("0.1"),
                soft_exit_surplus_reserve_bps=Decimal("0.02"),
            ),
        )
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator._account_monitor_initialized = True
        coordinator.account_monitor = SimpleNamespace(
            snapshot=Mock(return_value=self.soft_exit_snapshot())
        )

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator._processed_fill_generation, 0)
        self.assertEqual(coordinator._audited_fill_generation, 0)
        self.assertIsInstance(
            coordinator._trusted_soft_exit_economics(self.now),
            SoftExitEconomics,
        )

    async def test_startup_loads_data_subscribes_then_becomes_active(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)

        await coordinator.start()

        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
        self.assertEqual(coordinator.metadata, self.metadata)
        adapter.connect.assert_awaited_once_with()
        adapter.authenticate.assert_awaited_once_with()
        adapter.get_exchange_info.assert_awaited_once_with()
        self.assertEqual(adapter.health_check.await_count, 2)
        adapter.get_orderbook.assert_awaited_once_with("BTC")
        adapter.get_positions.assert_awaited_once_with(["BTC"])
        manager.initialize.assert_awaited_once_with()
        adapter.subscribe_orderbook.assert_awaited_once_with(
            "BTC", coordinator.on_orderbook
        )
        adapter.subscribe_user_data.assert_awaited_once_with(
            coordinator.on_order_update
        )
        adapter.subscribe_positions.assert_awaited_once_with(
            coordinator.on_position
        )
        manager.reconcile.assert_awaited_once()

    async def test_live_abort_after_auth_runs_early_final_account_audit(self) -> None:
        events = []
        adapter = self.adapter()
        adapter.disconnect.side_effect = lambda: events.append("disconnect")
        monitor = SimpleNamespace(
            initialize=AsyncMock(side_effect=lambda: events.append("initialize")),
            audit=AsyncMock(side_effect=lambda _ids: events.append("audit")),
            snapshot=Mock(return_value={"state": "healthy"}),
        )

        async def emit(snapshot):
            events.append("status")
            self.assertEqual(snapshot["event"], "market_maker_final_account_audit")

        callback = AsyncMock(side_effect=emit)
        coordinator = self.coordinator(
            adapter=adapter,
            config=self.config(
                account_audit_interval_seconds=1,
                max_session_drawdown=Decimal("1"),
                require_flat_start=True,
            ),
        )
        coordinator.order_manager = None
        coordinator._status_callback = callback

        async def authenticate():
            coordinator.request_stop()
            return True

        adapter.authenticate.side_effect = authenticate
        with patch(
            "core.services.market_maker.coordinator.MarketMakerAccountMonitor",
            return_value=monitor,
        ) as monitor_factory:
            await coordinator.start()

        monitor_factory.assert_called_once()
        monitor.initialize.assert_awaited_once_with()
        monitor.audit.assert_awaited_once_with(set())
        adapter.get_exchange_info.assert_not_awaited()
        self.assertEqual(events, ["initialize", "audit", "status", "disconnect"])
        self.assertEqual(coordinator.state, RuntimeState.STOPPED)

    async def test_live_exception_after_auth_runs_early_final_account_audit(
        self,
    ) -> None:
        adapter = self.adapter()
        adapter.get_exchange_info.side_effect = RuntimeError("metadata unavailable")
        monitor = SimpleNamespace(
            initialize=AsyncMock(),
            audit=AsyncMock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        callback = AsyncMock()
        coordinator = self.coordinator(
            adapter=adapter,
            config=self.config(
                account_audit_interval_seconds=1,
                max_session_drawdown=Decimal("1"),
                require_flat_start=True,
            ),
        )
        coordinator.order_manager = None
        coordinator._status_callback = callback

        with patch(
            "core.services.market_maker.coordinator.MarketMakerAccountMonitor",
            return_value=monitor,
        ):
            with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
                await coordinator.start()

        monitor.initialize.assert_awaited_once_with()
        monitor.audit.assert_awaited_once_with(set())
        final_snapshot = callback.await_args.args[0]
        self.assertEqual(final_snapshot["event"], "market_maker_final_account_audit")
        adapter.disconnect.assert_awaited_once_with()

    async def test_retry_after_early_startup_failure_reinitializes_account_monitor(
        self,
    ) -> None:
        adapter = self.adapter()
        exchange_info = adapter.get_exchange_info.return_value
        adapter.get_exchange_info.side_effect = [
            RuntimeError("metadata unavailable"),
            exchange_info,
        ]
        manager = self.order_manager()
        initialization_versions = []

        async def initialize():
            initialization_versions.append(len(initialization_versions) + 1)

        monitor = SimpleNamespace(
            initialize=AsyncMock(side_effect=initialize),
            audit=AsyncMock(),
            snapshot=Mock(
                side_effect=lambda _now: {
                    "state": "healthy",
                    "baseline_equity": Decimal(
                        str(initialization_versions[-1])
                    ),
                }
            ),
        )
        coordinator = self.coordinator(
            adapter=adapter,
            config=self.config(
                account_audit_interval_seconds=60,
                max_session_drawdown=Decimal("1"),
                require_flat_start=True,
            ),
            order_manager=manager,
        )
        coordinator.account_monitor = monitor

        with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            await coordinator.start()

        self.assertEqual(initialization_versions, [1])
        self.assertTrue(coordinator._account_monitor_initialized)

        await coordinator.start()

        self.assertEqual(initialization_versions, [1, 2])
        self.assertTrue(coordinator._account_monitor_initialized)
        self.assertEqual(
            coordinator.metrics.account_audit["baseline_equity"],
            Decimal("2"),
        )
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)

    async def test_early_account_audit_is_not_added_before_auth_or_in_dry_run(
        self,
    ) -> None:
        cases = (
            ("unauthenticated", False, False, "authentication failed"),
            ("dry-run", True, True, "metadata unavailable"),
        )
        for name, dry_run, authenticated, reason in cases:
            with self.subTest(name=name):
                adapter = self.adapter()
                adapter.authenticate.return_value = authenticated
                if authenticated:
                    adapter.get_exchange_info.side_effect = RuntimeError(reason)
                callback = AsyncMock()
                coordinator = self.coordinator(
                    adapter=adapter,
                    config=self.config(
                        dry_run=dry_run,
                        account_audit_interval_seconds=1,
                        max_session_drawdown=Decimal("1"),
                        require_flat_start=True,
                    ),
                )
                coordinator.order_manager = None
                coordinator._status_callback = callback

                with patch(
                    "core.services.market_maker.coordinator.MarketMakerAccountMonitor"
                ) as monitor_factory:
                    with self.assertRaisesRegex(RuntimeError, reason):
                        await coordinator.start()

                monitor_factory.assert_not_called()
                callback.assert_not_awaited()

    async def test_injected_monitor_is_not_used_without_successful_auth(self) -> None:
        for name, auth_error in (
            ("false", None),
            ("exception", RuntimeError("auth unavailable")),
        ):
            with self.subTest(name=name):
                adapter = self.adapter()
                if auth_error is None:
                    adapter.authenticate.return_value = False
                    expected = "authentication failed"
                else:
                    adapter.authenticate.side_effect = auth_error
                    expected = "auth unavailable"
                monitor = SimpleNamespace(
                    initialize=AsyncMock(),
                    audit=AsyncMock(),
                    snapshot=Mock(return_value={"state": "healthy"}),
                )
                callback = AsyncMock()
                coordinator = self.coordinator(
                    adapter=adapter,
                    config=self.config(
                        account_audit_interval_seconds=1,
                        max_session_drawdown=Decimal("1"),
                        require_flat_start=True,
                    ),
                )
                coordinator.order_manager = None
                coordinator.account_monitor = monitor
                coordinator._status_callback = callback

                with self.assertRaisesRegex(RuntimeError, expected):
                    await coordinator.start()

                monitor.initialize.assert_not_awaited()
                monitor.audit.assert_not_awaited()
                callback.assert_not_awaited()
                adapter.disconnect.assert_awaited_once_with()

    async def test_startup_reads_position_after_order_initialization(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        position_after_cancel = self.position(signed_size=Decimal("0.2"))

        async def initialize() -> None:
            adapter.get_positions.return_value = [position_after_cancel]

        manager.initialize.side_effect = initialize
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)

        await coordinator.start()

        adapter.get_positions.assert_awaited_once_with(["BTC"])
        evaluated_position = coordinator.risk_manager.evaluate.call_args.args[0]
        self.assertEqual(evaluated_position.signed_size, Decimal("0.2"))

    async def test_startup_waits_for_private_subscription_health(self) -> None:
        adapter = self.adapter()
        adapter.health_check.side_effect = (
            {"healthy": True},
            {"healthy": False},
            {"healthy": False},
            {"healthy": True},
        )
        manager = self.order_manager()

        async def reconcile(*_args):
            self.assertEqual(adapter.health_check.await_count, 4)
            return SimpleNamespace(
                actions=(), errors=(), runtime_state=RuntimeState.ACTIVE
            )

        manager.reconcile.side_effect = reconcile
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await asyncio.sleep(0)

        coordinator._sleep = sleep

        await coordinator.start()

        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
        self.assertEqual(adapter.health_check.await_count, 4)
        self.assertEqual(sleeps, [2.0, 2.0])

    async def test_startup_without_position_does_not_quote(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = None
        manager = self.order_manager()
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)

        await coordinator.start()

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_POSITION)
        manager.reconcile.assert_not_awaited()
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_startup_requires_a_fresh_websocket_book(self) -> None:
        adapter = self.adapter()
        adapter.subscribe_orderbook.side_effect = None
        manager = self.order_manager()
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)
        real_wait_for = asyncio.wait_for
        calls = 0

        async def timeout(awaitable, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                awaitable.close()
                raise TimeoutError
            return await real_wait_for(awaitable, **_kwargs)

        with patch(
            "core.services.market_maker.coordinator.asyncio.wait_for",
            side_effect=timeout,
        ):
            with self.assertRaisesRegex(RuntimeError, "websocket order book"):
                await coordinator.start()

        manager.reconcile.assert_not_awaited()
        manager.shutdown.assert_awaited_once_with()
        adapter.unsubscribe.assert_awaited_once_with()
        adapter.disconnect.assert_awaited_once_with()

    async def test_empty_rest_position_is_confirmed_flat(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = []
        manager = self.order_manager()
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)

        await coordinator.start()

        self.assertEqual(
            coordinator.position_snapshot.signed_size, Decimal("0")
        )
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
        manager.reconcile.assert_awaited_once()

    async def test_callbacks_only_cache_and_one_event_coalesces_updates(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)

        for _ in range(100):
            await coordinator.on_orderbook(self.market())
        await coordinator.on_position(self.position())
        await coordinator.on_order_update(self.order())

        manager.handle_order_update.assert_not_awaited()
        manager.reconcile.assert_not_awaited()
        manager.cancel_managed_orders.assert_not_awaited()
        self.assertTrue(coordinator.quote_event.is_set())

        self.assertTrue(await coordinator.process_quote_event())
        self.assertFalse(await coordinator.process_quote_event())
        manager.handle_order_update.assert_awaited_once()
        manager.reconcile.assert_awaited_once()

    async def test_invalid_target_book_immediately_fails_closed(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        invalid = OrderBookData(
            symbol="BTC",
            bids=[OrderBookLevel(Decimal("100.2"), Decimal("1"))],
            asks=[OrderBookLevel(Decimal("100.1"), Decimal("1"))],
            timestamp=datetime.now(),
            nonce=None,
        )

        await coordinator.on_orderbook(invalid)

        self.assertIsNone(coordinator.market_snapshot)
        self.assertTrue(coordinator.quote_event.is_set())
        self.assertTrue(await coordinator.process_quote_event())
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_DATA)
        manager.cancel_managed_orders.assert_awaited_once()
        self.assertEqual(
            coordinator.metrics.counters["invalid_book_updates"], 1
        )

    async def test_wrong_symbol_book_does_not_invalidate_market(self) -> None:
        coordinator = self.prepare_running()
        original = coordinator.market_snapshot
        wrong_symbol = OrderBookData(
            symbol="ETH",
            bids=[OrderBookLevel(Decimal("99.9"), Decimal("1"))],
            asks=[OrderBookLevel(Decimal("100.1"), Decimal("1"))],
            timestamp=datetime.now(),
            nonce=None,
        )

        await coordinator.on_orderbook(wrong_symbol)

        self.assertIs(coordinator.market_snapshot, original)
        self.assertFalse(coordinator.quote_event.is_set())

    def test_normalize_market_uses_positive_price_extrema_for_bbo(self) -> None:
        bids = (
            OrderBookLevel(Decimal("90"), Decimal("1")),
            OrderBookLevel(Decimal("200"), Decimal("0")),
            OrderBookLevel(Decimal("NaN"), Decimal("1")),
            OrderBookLevel(Decimal("99.8"), Decimal("Infinity")),
            OrderBookLevel(Decimal("99.9"), Decimal("1")),
        )
        asks = (
            OrderBookLevel(Decimal("110"), Decimal("1")),
            OrderBookLevel(Decimal("50"), Decimal("0")),
            OrderBookLevel(Decimal("Infinity"), Decimal("1")),
            OrderBookLevel(Decimal("100.2"), Decimal("NaN")),
            OrderBookLevel(Decimal("100.1"), Decimal("1")),
        )
        books = (
            OrderBookData(
                symbol="BTC",
                bids=list(bids),
                asks=list(asks),
                timestamp=datetime.now(),
                nonce=None,
            ),
            MarketSnapshot(
                symbol="BTC",
                bids=bids,
                asks=asks,
                best_bid=Decimal("90"),
                best_ask=Decimal("110"),
                exchange_timestamp=None,
                received_monotonic=self.now - 1,
            ),
        )
        coordinator = self.coordinator()

        for book in books:
            with self.subTest(book_type=type(book).__name__):
                normalized = coordinator._normalize_market(book)

                self.assertEqual(normalized.best_bid, Decimal("99.9"))
                self.assertEqual(normalized.best_ask, Decimal("100.1"))
                self.assertEqual(len(normalized.bids), 2)
                self.assertEqual(len(normalized.asks), 2)
                self.assertEqual(normalized.received_monotonic, self.now)

    def test_normalize_market_rejects_empty_or_crossed_valid_book(self) -> None:
        coordinator = self.coordinator()
        books = (
            OrderBookData(
                symbol="BTC",
                bids=[OrderBookLevel(Decimal("99.9"), Decimal("0"))],
                asks=[OrderBookLevel(Decimal("100.1"), Decimal("1"))],
                timestamp=datetime.now(),
                nonce=None,
            ),
            MarketSnapshot(
                symbol="BTC",
                bids=(OrderBookLevel(Decimal("100.2"), Decimal("1")),),
                asks=(OrderBookLevel(Decimal("100.1"), Decimal("1")),),
                best_bid=Decimal("99.9"),
                best_ask=Decimal("100.1"),
                exchange_timestamp=None,
                received_monotonic=self.now,
            ),
        )

        for book in books:
            with self.subTest(book_type=type(book).__name__):
                with self.assertRaisesRegex(ValueError, "empty|crossed"):
                    coordinator._normalize_market(book)

    async def test_cycle_uses_order_manager_confirmed_order_ids(self) -> None:
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"fast-fill"})
        coordinator = self.prepare_running(order_manager=manager)

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(
            coordinator._managed_order_id_snapshot(), {"fast-fill"}
        )

    async def test_post_only_terminal_requires_fresh_book_and_cooldown(
        self,
    ) -> None:
        adapter = self.adapter()
        config = self.config(refresh_interval_ms=1000)
        attempts = 0

        async def create(
            _symbol, side, _order_type, amount, price, params
        ):
            nonlocal attempts
            attempts += 1
            return replace(
                self.order(
                    f"order-{attempts}",
                    status=(
                        OrderStatus.CANCELED
                        if attempts == 1
                        else OrderStatus.OPEN
                    ),
                ),
                side=side,
                amount=amount,
                price=price,
                remaining=amount,
                params=params,
                raw_data=(
                    {"post_only_canceled": True}
                    if attempts == 1
                    else {}
                ),
            )

        async def cancel(order_id, _symbol):
            side = (
                OrderSide.BUY
                if str(order_id) == "order-2"
                else OrderSide.SELL
            )
            return replace(
                self.order(str(order_id), status=OrderStatus.CANCELED),
                side=side,
                params={"cancel_terminal": True},
            )

        adapter.create_order = AsyncMock(side_effect=create)
        adapter.cancel_order = AsyncMock(side_effect=cancel)
        adapter.cancel_all_orders = AsyncMock(return_value=[])
        adapter.get_open_orders = AsyncMock(return_value=[])
        adapter.get_order_history = AsyncMock(return_value=[])
        adapter.get_unresolved_submissions = Mock(return_value=[])
        adapter.resolve_unresolved_submissions = AsyncMock(return_value=[])
        manager = MarketMakerOrderManager(
            adapter, config, self.metadata, monotonic=lambda: self.now
        )
        coordinator = self.prepare_running(
            adapter=adapter, config=config, order_manager=manager
        )
        adapter.get_orderbook.reset_mock()

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(adapter.create_order.await_count, 1)
        adapter.get_orderbook.assert_awaited_once_with("BTC")
        self.assertEqual(
            coordinator.metrics.counters["post_only_cancellations"], 1
        )
        self.assertEqual(
            coordinator.metrics.counters["reconciliation_failure"], 0
        )
        self.assertEqual(
            coordinator.metrics.counters["reconciliation_success"], 1
        )

        await coordinator.run_one_cycle(force=True)
        self.assertEqual(adapter.create_order.await_count, 1)

        self.now += config.refresh_interval_ms / 1000
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(adapter.create_order.await_count, 2)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(adapter.create_order.await_count, 3)
        self.assertEqual(adapter.get_orderbook.await_count, 1)

    async def test_post_only_book_refresh_failure_stays_fail_closed(
        self,
    ) -> None:
        adapter = self.adapter()
        config = self.config(refresh_interval_ms=1000)

        async def create(
            _symbol, side, _order_type, amount, price, params
        ):
            return replace(
                self.order("1" if side is OrderSide.BUY else "2"),
                side=side,
                amount=amount,
                price=price,
                remaining=amount,
                params=params,
            )

        async def cancel(order_id, _symbol):
            side = (
                OrderSide.BUY if str(order_id) == "1" else OrderSide.SELL
            )
            return replace(
                self.order(str(order_id), status=OrderStatus.CANCELED),
                side=side,
                params={"cancel_terminal": True},
            )

        adapter.create_order = AsyncMock(side_effect=create)
        adapter.cancel_order = AsyncMock(side_effect=cancel)
        adapter.cancel_all_orders = AsyncMock(return_value=[])
        adapter.get_open_orders = AsyncMock(return_value=[])
        adapter.get_order_history = AsyncMock(return_value=[])
        adapter.get_unresolved_submissions = Mock(return_value=[])
        adapter.resolve_unresolved_submissions = AsyncMock(return_value=[])
        manager = MarketMakerOrderManager(
            adapter, config, self.metadata, monotonic=lambda: self.now
        )
        coordinator = self.prepare_running(
            adapter=adapter, config=config, order_manager=manager
        )
        await coordinator.run_one_cycle(force=True)
        await coordinator.run_one_cycle(force=True)
        adapter.get_orderbook.side_effect = RuntimeError("book unavailable")

        await coordinator.on_order_update(
            replace(
                self.order("1", status=OrderStatus.CANCELED),
                raw_data={"post_only_canceled": True},
            )
        )
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_DATA)
        self.assertEqual(adapter.create_order.await_count, 2)
        adapter.cancel_order.assert_awaited_once_with("2", "BTC")
        self.assertTrue(manager.post_only_book_refresh_required)
        self.assertEqual(
            coordinator.metrics.counters["post_only_cancellations"], 1
        )
        self.assertEqual(
            coordinator.metrics.counters["reconciliation_failure"], 0
        )

    async def test_post_only_refresh_preserves_error_cooldown(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        manager.consume_post_only_cancellations = Mock(
            return_value=(1, 1)
        )
        manager.post_only_book_refresh_required = True

        def acknowledge(_generation: int) -> None:
            manager.post_only_book_refresh_required = False

        manager.acknowledge_post_only_book_refresh = Mock(
            side_effect=acknowledge
        )
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )
        coordinator._transition(RuntimeState.PAUSED_ERROR, "cooldown")
        coordinator._error_paused_until = self.now + 60
        deadline = coordinator._error_paused_until

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertEqual(coordinator._error_paused_until, deadline)
        adapter.get_orderbook.assert_awaited_once_with("BTC")
        adapter.health_check.assert_not_awaited()
        manager.reconcile.assert_not_awaited()

    async def test_post_only_refresh_preserves_full_stale_recovery_gate(
        self,
    ) -> None:
        for paused_state in (
            RuntimeState.PAUSED_DATA,
            RuntimeState.PAUSED_POSITION,
        ):
            with self.subTest(paused_state=paused_state):
                adapter = self.adapter()
                manager = self.order_manager()
                manager.consume_post_only_cancellations = Mock(
                    side_effect=((1, 1), (0, 1))
                )
                manager.post_only_book_refresh_required = True

                def acknowledge(_generation: int) -> None:
                    manager.post_only_book_refresh_required = False

                manager.acknowledge_post_only_book_refresh = Mock(
                    side_effect=acknowledge
                )
                coordinator = self.prepare_running(
                    adapter=adapter, order_manager=manager
                )
                coordinator._transition(paused_state, "stale cache")

                await coordinator.run_one_cycle(force=True)

                self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
                adapter.health_check.assert_awaited_once_with()
                self.assertEqual(adapter.get_orderbook.await_count, 2)
                adapter.get_positions.assert_awaited_once_with(["BTC"])
                manager.sync_open_orders.assert_awaited_once_with()
                manager.reconcile.assert_awaited_once_with(
                    self.desired(), self.risk()
                )

    async def test_queued_post_only_terminal_precedes_second_create(
        self,
    ) -> None:
        adapter = self.adapter()
        config = self.config(refresh_interval_ms=1000)
        created: dict[str, OrderData] = {}
        coordinator: MarketMakerCoordinator

        async def create(
            _symbol, side, _order_type, amount, price, params
        ):
            order_id = str(len(created) + 1)
            order = replace(
                self.order(order_id),
                side=side,
                amount=amount,
                price=price,
                remaining=amount,
                params=params,
            )
            created[order_id] = order
            if len(created) == 1:
                await coordinator.on_order_update(
                    replace(
                        order,
                        status=OrderStatus.CANCELED,
                        raw_data={"post_only_canceled": True},
                    )
                )
            return order

        async def cancel(order_id, _symbol):
            order = created[str(order_id)]
            return replace(
                order,
                status=OrderStatus.CANCELED,
                params={"cancel_terminal": True},
            )

        adapter.create_order = AsyncMock(side_effect=create)
        adapter.cancel_order = AsyncMock(side_effect=cancel)
        adapter.cancel_all_orders = AsyncMock(return_value=[])
        adapter.get_open_orders = AsyncMock(return_value=[])
        adapter.get_order_history = AsyncMock(return_value=[])
        adapter.get_unresolved_submissions = Mock(return_value=[])
        adapter.resolve_unresolved_submissions = AsyncMock(return_value=[])
        manager = MarketMakerOrderManager(
            adapter, config, self.metadata, monotonic=lambda: self.now
        )
        coordinator = self.prepare_running(
            adapter=adapter, config=config, order_manager=manager
        )
        adapter.get_orderbook.reset_mock()

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(adapter.create_order.await_count, 1)
        self.assertEqual(len(coordinator._pending_orders), 1)
        adapter.get_orderbook.assert_not_awaited()

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(adapter.create_order.await_count, 1)
        self.assertFalse(coordinator._pending_orders)
        adapter.get_orderbook.assert_awaited_once_with("BTC")

        self.now += config.refresh_interval_ms / 1000
        await coordinator.run_one_cycle(force=True)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(adapter.create_order.await_count, 3)

    async def test_other_symbol_order_callback_is_ignored(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        order = self.order("other-symbol")
        order.symbol = "ETH"

        await coordinator.on_order_update(order)

        self.assertFalse(coordinator.quote_event.is_set())
        self.assertFalse(coordinator._pending_orders)
        self.assertEqual(
            coordinator.metrics.counters["ignored_other_symbol_orders"], 1
        )

    async def test_concurrent_cycle_requests_never_reconcile_concurrently(self) -> None:
        manager = self.order_manager()
        active = 0
        maximum = 0

        async def reconcile(*_args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return SimpleNamespace(errors=(), runtime_state=RuntimeState.ACTIVE)

        manager.reconcile.side_effect = reconcile
        coordinator = self.prepare_running(order_manager=manager)

        await asyncio.gather(
            *(coordinator.run_one_cycle(force=True) for _ in range(5))
        )

        self.assertEqual(maximum, 1)
        self.assertEqual(manager.reconcile.await_count, 5)

    async def test_stale_book_fails_closed(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._market = self.market(
            self.now - coordinator.config.stale_book_seconds - 1
        )

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_DATA)
        manager.cancel_managed_orders.assert_awaited_once()
        manager.reconcile.assert_not_awaited()

    async def test_stale_position_fails_closed(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._position = self.position(
            self.now - coordinator.config.stale_position_seconds - 1
        )

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_POSITION)
        manager.cancel_managed_orders.assert_awaited_once()
        manager.reconcile.assert_not_awaited()

    async def test_stale_cache_recovery_requires_full_rest_resync(self) -> None:
        for stale_source in ("book", "position"):
            with self.subTest(stale_source=stale_source):
                adapter = self.adapter()
                manager = self.order_manager()
                coordinator = self.prepare_running(
                    adapter=adapter, order_manager=manager
                )
                if stale_source == "book":
                    coordinator._market = self.market(
                        self.now - coordinator.config.stale_book_seconds - 1
                    )
                else:
                    coordinator._position = self.position(
                        self.now - coordinator.config.stale_position_seconds - 1
                    )

                await coordinator.run_one_cycle(force=True)
                expected_pause = (
                    RuntimeState.PAUSED_DATA
                    if stale_source == "book"
                    else RuntimeState.PAUSED_POSITION
                )
                self.assertEqual(coordinator.state, expected_pause)

                adapter.health_check.reset_mock()
                adapter.get_orderbook.reset_mock()
                adapter.get_positions.reset_mock()
                manager.sync_open_orders.reset_mock()
                manager.reconcile.reset_mock()
                coordinator._error_streaks.update(
                    {
                        "health": 1,
                        "position": 1,
                        "orders": 1,
                        "reconcile": 1,
                        "cancel": 1,
                    }
                )
                coordinator.metrics.consecutive_errors = 1

                async def reconcile(*_args):
                    self.assertEqual(adapter.health_check.await_count, 1)
                    self.assertEqual(adapter.get_orderbook.await_count, 1)
                    self.assertEqual(adapter.get_positions.await_count, 1)
                    self.assertEqual(manager.sync_open_orders.await_count, 1)
                    self.assertEqual(
                        coordinator._error_streaks["reconcile"], 1
                    )
                    self.assertEqual(coordinator._error_streaks["cancel"], 1)
                    for recovered_source in ("health", "position", "orders"):
                        self.assertEqual(
                            coordinator._error_streaks[recovered_source], 0
                        )
                    self.assertEqual(coordinator.metrics.consecutive_errors, 1)
                    return SimpleNamespace(
                        errors=(), runtime_state=RuntimeState.ACTIVE
                    )

                manager.reconcile.side_effect = reconcile
                if stale_source == "book":
                    await coordinator.on_orderbook(self.market())
                else:
                    await coordinator.on_position(self.position())

                await coordinator.run_one_cycle(force=True)

                self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
                manager.reconcile.assert_awaited_once()

    async def test_stale_recovery_failure_remains_fail_closed(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._position = self.position(
            self.now - coordinator.config.stale_position_seconds - 1
        )
        await coordinator.run_one_cycle(force=True)

        manager.reconcile.reset_mock()
        manager.sync_open_orders.side_effect = RuntimeError("orders unavailable")
        await coordinator.on_position(self.position())
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertEqual(manager.cancel_managed_orders.await_count, 2)
        manager.reconcile.assert_not_awaited()

        await coordinator.run_one_cycle(force=True)
        self.assertEqual(manager.sync_open_orders.await_count, 1)
        manager.reconcile.assert_not_awaited()

    async def test_stale_recovery_does_not_quote_with_uncertain_state(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.has_uncertain_state = True
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._transition(RuntimeState.PAUSED_DATA, "stale book")

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ORDER_STATE)
        manager.sync_open_orders.assert_not_awaited()
        manager.reconcile.assert_not_awaited()

    async def test_stale_recovery_rejects_invalid_rest_book(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )
        coordinator._market = self.market(
            self.now - coordinator.config.stale_book_seconds - 1
        )
        await coordinator.run_one_cycle(force=True)

        manager.reconcile.reset_mock()
        adapter.get_orderbook.return_value = replace(
            self.market(), symbol="ETH"
        )
        await coordinator.on_orderbook(self.market())
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertEqual(manager.cancel_managed_orders.await_count, 2)
        manager.sync_open_orders.assert_not_awaited()
        manager.reconcile.assert_not_awaited()

    async def test_unhealthy_exchange_fails_closed(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._exchange_healthy = False

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_EXCHANGE)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_health_recovery_stays_syncing_until_a_cycle_succeeds(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._exchange_healthy = False
        coordinator._transition(RuntimeState.PAUSED_EXCHANGE, "down")

        self.assertTrue(await coordinator.poll_health_once())
        self.assertEqual(coordinator.state, RuntimeState.SYNCING)

        await coordinator.run_one_cycle(force=True)
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
        manager.sync_open_orders.assert_awaited_once()

    async def test_market_quality_recovery_uses_fresh_cache_without_rest_churn(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        coordinator = self.prepare_running(
            adapter=adapter,
            order_manager=manager,
        )
        coordinator._transition(RuntimeState.PAUSED_MARKET, "spread too wide")
        adapter.health_check.reset_mock()
        adapter.get_orderbook.reset_mock()
        adapter.get_positions.reset_mock()
        manager.sync_open_orders.reset_mock()

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
        adapter.health_check.assert_not_awaited()
        adapter.get_orderbook.assert_not_awaited()
        adapter.get_positions.assert_not_awaited()
        manager.sync_open_orders.assert_not_awaited()
        manager.reconcile.assert_awaited_once()

    async def test_health_metrics_use_cumulative_reconnect_count(self) -> None:
        adapter = self.adapter()
        adapter.health_check.return_value = {
            "healthy": True,
            "websocket": {
                "reconnect_attempts": 1,
                "reconnect_count": 7,
            },
        }
        coordinator = self.prepare_running(adapter=adapter)

        self.assertTrue(await coordinator.poll_health_once())

        self.assertEqual(coordinator.metrics.ws_reconnect_count, 7)

    async def test_health_recovery_failure_cannot_reuse_old_position(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.side_effect = RuntimeError("position down")
        manager = self.order_manager()
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )
        coordinator._exchange_healthy = False
        coordinator._transition(RuntimeState.PAUSED_EXCHANGE, "down")

        self.assertFalse(await coordinator.poll_health_once())
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_EXCHANGE)
        manager.reconcile.assert_not_awaited()
        manager.cancel_managed_orders.assert_awaited()

    async def test_repeated_poll_errors_pause_and_cancel(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.side_effect = RuntimeError("REST down")
        manager = self.order_manager()
        coordinator = self.prepare_running(
            adapter=adapter,
            config=self.config(max_consecutive_errors=2),
            order_manager=manager,
        )

        self.assertFalse(await coordinator.poll_position_once())
        self.assertFalse(await coordinator.poll_position_once())

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_stop_is_idempotent_and_unsubscribes_all_streams(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator._subscribed = True

        await coordinator.stop()
        await coordinator.stop()

        self.assertEqual(coordinator.state, RuntimeState.STOPPED)
        manager.shutdown.assert_awaited_once_with()
        adapter.unsubscribe.assert_awaited_once_with()
        adapter.disconnect.assert_awaited_once_with()

    async def test_stop_audits_final_account_state_before_disconnect(self) -> None:
        events = []
        adapter = self.adapter()
        adapter.unsubscribe.side_effect = lambda: events.append("unsubscribe")
        adapter.disconnect.side_effect = lambda: events.append("disconnect")
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"filled-during-shutdown"})
        manager.shutdown.side_effect = lambda: events.append("shutdown")

        async def audit(managed_order_ids):
            events.append("audit")
            self.assertEqual(managed_order_ids, {"filled-during-shutdown"})

        final_snapshot = {
            "state": "healthy",
            "ledger_position": Decimal("0.1"),
        }
        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(return_value=final_snapshot),
        )
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        coordinator._subscribed = True

        with self.assertLogs(
            "core.services.market_maker.coordinator", level="INFO"
        ) as captured:
            await coordinator.stop()

        self.assertEqual(events, ["shutdown", "audit", "unsubscribe", "disconnect"])
        self.assertEqual(
            coordinator.metrics.account_audit["ledger_position"], Decimal("0.1")
        )
        self.assertTrue(
            any("market_maker_final_account_audit" in line for line in captured.output)
        )

    async def test_stop_emits_final_audit_through_status_callback(self) -> None:
        events = []
        adapter = self.adapter()
        adapter.unsubscribe.side_effect = lambda: events.append("unsubscribe")
        adapter.disconnect.side_effect = lambda: events.append("disconnect")
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"final-fill"})
        manager.shutdown.side_effect = lambda: events.append("shutdown")

        async def audit(_managed_order_ids):
            events.append("audit")

        async def emit(snapshot):
            events.append("status")
            self.assertEqual(snapshot["event"], "market_maker_final_account_audit")
            self.assertEqual(snapshot["account_audit"]["state"], "healthy")

        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        callback = AsyncMock(side_effect=emit)
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        coordinator._status_callback = callback
        coordinator._subscribed = True

        with patch("core.services.market_maker.coordinator.logger.info"):
            await coordinator.stop()

        callback.assert_awaited_once()
        self.assertEqual(events, ["shutdown", "audit", "status", "unsubscribe", "disconnect"])

    async def test_final_status_callback_failure_does_not_skip_disconnect(self) -> None:
        adapter = self.adapter()
        adapter.close_position = AsyncMock()
        manager = self.order_manager()
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        callback = AsyncMock(side_effect=RuntimeError("final status callback failed"))
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        coordinator._status_callback = callback
        coordinator._subscribed = True

        with self.assertRaisesRegex(RuntimeError, "final status callback failed"):
            await coordinator.stop()

        monitor.audit.assert_awaited_once()
        callback.assert_awaited_once()
        adapter.unsubscribe.assert_awaited_once_with()
        adapter.disconnect.assert_awaited_once_with()
        adapter.close_position.assert_not_awaited()
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_stop_fails_closed_when_final_account_audit_fails(self) -> None:
        adapter = self.adapter()
        adapter.close_position = AsyncMock()
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"final-order"})
        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=AccountAuditError("final audit failed")),
            snapshot=Mock(
                return_value={"state": "hard_stop", "reason": "final audit failed"}
            ),
        )
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        coordinator._subscribed = True

        with self.assertLogs(
            "core.services.market_maker.coordinator", level="INFO"
        ) as captured:
            with self.assertRaisesRegex(AccountAuditError, "final audit failed"):
                await coordinator.stop()

        monitor.audit.assert_awaited_once_with({"final-order"})
        adapter.unsubscribe.assert_awaited_once_with()
        adapter.disconnect.assert_awaited_once_with()
        adapter.close_position.assert_not_awaited()
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertEqual(coordinator.metrics.account_audit["state"], "hard_stop")
        self.assertTrue(
            any("market_maker_final_account_audit" in line for line in captured.output)
        )

    async def test_stop_audits_and_aggregates_when_order_shutdown_fails(self) -> None:
        events = []
        adapter = self.adapter()
        adapter.close_position = AsyncMock()
        adapter.unsubscribe.side_effect = lambda: events.append("unsubscribe")
        adapter.disconnect.side_effect = lambda: events.append("disconnect")
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"shutdown-fill"})

        async def shutdown():
            events.append("shutdown")
            raise RuntimeError("order shutdown failed")

        async def audit(_managed_order_ids):
            events.append("audit")
            raise AccountAuditError("final audit failed")

        manager.shutdown.side_effect = shutdown
        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(
                return_value={"state": "hard_stop", "reason": "final audit failed"}
            ),
        )
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        coordinator._subscribed = True

        with self.assertLogs(
            "core.services.market_maker.coordinator", level="INFO"
        ):
            with self.assertRaisesRegex(
                RuntimeError, "order shutdown failed.*final audit failed"
            ):
                await coordinator.stop()

        monitor.audit.assert_awaited_once_with({"shutdown-fill"})
        self.assertEqual(events, ["shutdown", "audit", "unsubscribe", "disconnect"])
        adapter.close_position.assert_not_awaited()
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_stop_preserves_fatal_and_cleanup_errors(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        manager.shutdown.side_effect = RuntimeError("shutdown failed")
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator._fatal_exception = RuntimeError("worker failed")

        with self.assertRaisesRegex(
            RuntimeError, "worker failed.*shutdown failed"
        ):
            await coordinator.stop()

        self.assertRegex(
            str(coordinator.fatal_exception), "worker failed.*shutdown failed"
        )
        adapter.disconnect.assert_awaited_once_with()
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_dry_run_stop_skips_final_account_audit(self) -> None:
        manager = self.order_manager()
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        callback = AsyncMock()
        coordinator = self.prepare_running(
            config=self.config(dry_run=True), order_manager=manager
        )
        coordinator.account_monitor = monitor
        coordinator._status_callback = callback

        await coordinator.stop()

        manager.shutdown.assert_awaited_once_with()
        monitor.audit.assert_not_awaited()
        callback.assert_not_awaited()

    async def test_stop_requested_during_start_prevents_first_reconcile(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def subscribe_positions(_callback):
            entered.set()
            await release.wait()

        adapter.subscribe_positions.side_effect = subscribe_positions
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)
        start_task = asyncio.create_task(coordinator.start())
        await entered.wait()
        stop_task = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0)
        release.set()

        await asyncio.gather(start_task, stop_task)

        manager.reconcile.assert_not_awaited()
        manager.shutdown.assert_awaited_once()
        self.assertEqual(coordinator.state, RuntimeState.STOPPED)

    async def test_stop_cancels_blocked_cycle_before_shutdown(self) -> None:
        manager = self.order_manager()
        entered = asyncio.Event()
        blocker = asyncio.Event()

        async def reconcile(*_args):
            entered.set()
            await blocker.wait()

        manager.reconcile.side_effect = reconcile
        coordinator = self.prepare_running(order_manager=manager)
        task = asyncio.create_task(
            coordinator.run_one_cycle(force=True), name="blocked-cycle"
        )
        coordinator._tasks.append(task)
        await entered.wait()

        await asyncio.wait_for(coordinator.stop(), timeout=1)

        self.assertTrue(task.cancelled())
        manager.shutdown.assert_awaited_once()
        self.assertEqual(coordinator.state, RuntimeState.STOPPED)

    async def test_stop_bounds_cycle_lock_wait_and_still_disconnects(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        coordinator = self.prepare_running(
            adapter=adapter,
            order_manager=manager,
        )
        coordinator._subscribed = True
        await coordinator._cycle_lock.acquire()
        real_wait_for = asyncio.wait_for

        async def timeout_shutdown(awaitable, timeout):
            code = getattr(awaitable, "cr_code", None)
            if code is not None and code.co_name == "_shutdown_order_manager_locked":
                awaitable.close()
                raise TimeoutError
            return await real_wait_for(awaitable, timeout)

        try:
            with patch(
                "core.services.market_maker.coordinator.asyncio.wait_for",
                new=timeout_shutdown,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "shutdown did not finish within"
                ):
                    await coordinator.stop()
        finally:
            coordinator._cycle_lock.release()

        manager.shutdown.assert_not_awaited()
        adapter.unsubscribe.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()
        self.assertTrue(coordinator._stopped_event.is_set())
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_stop_audits_after_order_shutdown_timeout(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"timeout-fill"})
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        real_wait_for = asyncio.wait_for

        async def timeout_shutdown(awaitable, timeout):
            code = getattr(awaitable, "cr_code", None)
            if code is not None and code.co_name == "_shutdown_order_manager_locked":
                awaitable.close()
                raise TimeoutError
            return await real_wait_for(awaitable, timeout)

        with patch(
            "core.services.market_maker.coordinator.asyncio.wait_for",
            new=timeout_shutdown,
        ):
            with self.assertRaisesRegex(RuntimeError, "shutdown did not finish within"):
                await coordinator.stop()

        monitor.audit.assert_awaited_once_with({"timeout-fill"})
        adapter.disconnect.assert_awaited_once_with()
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_cancelled_stop_waits_for_cleanup_then_reraises(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def shutdown():
            entered.set()
            await release.wait()

        manager.shutdown.side_effect = shutdown
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator._subscribed = True
        task = asyncio.create_task(coordinator.stop())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task

        adapter.unsubscribe.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()
        self.assertTrue(coordinator._stopped_event.is_set())
        self.assertEqual(coordinator.state, RuntimeState.STOPPED)

    async def test_cancelled_stop_surfaces_cleanup_failure(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def shutdown():
            entered.set()
            await release.wait()
            raise RuntimeError("cancel confirmation failed")

        manager.shutdown.side_effect = shutdown
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator._subscribed = True
        task = asyncio.create_task(coordinator.stop())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()

        with self.assertRaisesRegex(RuntimeError, "cancel confirmation failed"):
            await task

        adapter.unsubscribe.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()
        self.assertTrue(coordinator._stopped_event.is_set())
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_cancelled_start_surfaces_cleanup_failure(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        entered = asyncio.Event()

        async def subscribe_positions(_callback):
            entered.set()
            await asyncio.Event().wait()

        adapter.subscribe_positions.side_effect = subscribe_positions
        manager.shutdown.side_effect = RuntimeError("startup cancel failed")
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)
        task = asyncio.create_task(coordinator.start())
        await entered.wait()
        task.cancel()

        with self.assertRaisesRegex(RuntimeError, "startup cancel failed"):
            await task

        adapter.unsubscribe.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()
        self.assertTrue(coordinator._stopped_event.is_set())
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_startup_stop_runs_final_audit_after_first_reconcile(self) -> None:
        events = []
        adapter = self.adapter()
        adapter.unsubscribe.side_effect = lambda: events.append("unsubscribe")
        adapter.disconnect.side_effect = lambda: events.append("disconnect")
        manager = self.order_manager()
        manager.shutdown.side_effect = lambda: events.append("shutdown")

        async def audit(_managed_order_ids):
            events.append("audit")

        monitor = SimpleNamespace(
            initialize=AsyncMock(),
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        coordinator = self.coordinator(
            adapter=adapter,
            config=self.config(
                account_audit_interval_seconds=1,
                max_session_drawdown=Decimal("1"),
                require_flat_start=True,
            ),
            order_manager=manager,
        )
        coordinator.account_monitor = monitor

        async def reconcile(*_args):
            manager.known_order_ids = frozenset({"startup-fill"})
            coordinator.request_stop()
            return SimpleNamespace(
                actions=(), errors=(), runtime_state=RuntimeState.ACTIVE
            )

        manager.reconcile.side_effect = reconcile

        await coordinator.start()
        await coordinator.stop()

        monitor.audit.assert_awaited_once_with({"startup-fill"})
        self.assertEqual(events, ["shutdown", "audit", "unsubscribe", "disconnect"])
        self.assertEqual(coordinator.state, RuntimeState.STOPPED)

    async def test_critical_failure_blocks_new_cycles_before_shutdown(self) -> None:
        manager = self.order_manager()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def shutdown():
            entered.set()
            await release.wait()

        manager.shutdown.side_effect = shutdown
        coordinator = self.prepare_running(order_manager=manager)

        async def explode() -> None:
            raise RuntimeError("boom")

        task = asyncio.create_task(explode(), name="broken-loop")
        coordinator._tasks.append(task)
        task.add_done_callback(coordinator._task_done)
        await entered.wait()

        await coordinator.run_one_cycle(force=True)
        manager.reconcile.assert_not_awaited()
        release.set()
        with self.assertRaises(RuntimeError):
            await coordinator.wait()

    async def test_critical_task_exception_triggers_abnormal_stop(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator._subscribed = True

        async def explode() -> None:
            raise RuntimeError("boom")

        task = asyncio.create_task(explode(), name="broken-loop")
        coordinator._tasks.append(task)
        task.add_done_callback(coordinator._task_done)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(20):
            if coordinator.state is RuntimeState.PAUSED_ERROR:
                break
            await asyncio.sleep(0)

        self.assertFalse(coordinator.running)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        manager.shutdown.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()

    async def test_account_audit_failure_stops_main_and_runs_cleanup(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"resting"})
        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=RuntimeError("account hard stop")),
            snapshot=Mock(
                return_value={"state": "hard_stop", "reason": "account hard stop"}
            ),
        )
        coordinator = self.prepare_running(adapter=adapter, order_manager=manager)
        coordinator.account_monitor = monitor
        coordinator._subscribed = True

        task = asyncio.create_task(
            coordinator.audit_account_once(), name="market-maker-account-audit"
        )
        coordinator._tasks.append(task)
        task.add_done_callback(coordinator._task_done)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(20):
            if coordinator.state is RuntimeState.PAUSED_ERROR:
                break
            await asyncio.sleep(0)

        self.assertFalse(coordinator.running)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertEqual(coordinator.metrics.account_audit["state"], "hard_stop")
        self.assertEqual(
            [item.args for item in monitor.audit.await_args_list],
            [({"resting"},), ({"resting"},)],
        )
        manager.shutdown.assert_awaited_once()
        adapter.unsubscribe.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()

    async def test_order_sync_and_audit_share_fresh_confirmed_ids(self) -> None:
        manager = self.order_manager()
        manager.known_order_ids = frozenset()

        async def sync_orders():
            manager.known_order_ids = frozenset({"reconciled-order"})
            return False

        manager.sync_open_orders.side_effect = sync_orders
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        coordinator = self.prepare_running(order_manager=manager)
        coordinator.account_monitor = monitor

        self.assertTrue(await coordinator.sync_open_orders_once())
        await coordinator.audit_account_once()

        monitor.audit.assert_awaited_once_with({"reconciled-order"})

    async def test_dry_run_cycle_waits_for_account_audit_then_resumes(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        manager = self.order_manager()
        manager.known_order_ids = frozenset({"resting"})

        async def audit(managed_order_ids):
            self.assertEqual(managed_order_ids, {"resting"})
            entered.set()
            await release.wait()

        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        coordinator = self.prepare_running(
            order_manager=manager,
            config=self.config(
                dry_run=True,
                account_audit_interval_seconds=15,
                account_audit_timeout_seconds=15,
                max_session_drawdown=Decimal("1"),
                require_flat_start=True,
            ),
        )
        coordinator.account_monitor = monitor

        audit_task = asyncio.create_task(coordinator.audit_account_once())
        await entered.wait()
        cycle_task = asyncio.create_task(coordinator.run_one_cycle(force=True))
        await asyncio.sleep(0)

        manager.reconcile.assert_not_awaited()
        manager.cancel_managed_orders.assert_not_awaited()

        release.set()
        await asyncio.gather(audit_task, cycle_task)

        manager.reconcile.assert_awaited_once()
        manager.cancel_managed_orders.assert_not_awaited()
        self.assertTrue(coordinator.running)
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)

    async def test_pending_economic_stop_is_published_before_waiting_cycle(
        self,
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        snapshot = self.active_account_snapshot("0.2")
        snapshot["economic_state"] = "collecting"

        async def audit(_managed_order_ids):
            snapshot["economic_state"] = "economic_stop_pending_flat"
            snapshot["economic_reason"] = "fee gate failed; waiting for flat"
            entered.set()
            await release.wait()

        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(side_effect=lambda _now: dict(snapshot)),
        )
        coordinator = self.prepare_running(account_monitor=monitor)
        coordinator._position = self.position(signed_size=Decimal("0.2"))
        coordinator.metrics.account_audit = {
            "state": "healthy",
            "economic_state": "collecting",
        }

        audit_task = asyncio.create_task(coordinator.audit_account_once())
        await entered.wait()
        cycle_task = asyncio.create_task(coordinator.run_one_cycle(force=True))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(audit_task, cycle_task)

        evaluation = coordinator.risk_manager.evaluate.call_args.kwargs
        self.assertFalse(evaluation["allow_new_episode"])
        self.assertTrue(evaluation["force_inventory_exit"])

    async def test_failed_audit_blocks_waiting_cycle_before_risk(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        snapshot = self.active_account_snapshot("0")
        snapshot["economic_state"] = "collecting"

        async def audit(_managed_order_ids):
            snapshot["state"] = "hard_stop"
            snapshot["economic_state"] = "no_go"
            snapshot["economic_reason"] = "authenticated economic no-go"
            entered.set()
            await release.wait()
            raise AccountAuditError("authenticated economic no-go")

        monitor = SimpleNamespace(
            audit=AsyncMock(side_effect=audit),
            snapshot=Mock(side_effect=lambda _now: dict(snapshot)),
        )
        manager = self.order_manager()
        execution_snapshot = {
            "episode_id": None,
            "state": "flat",
            "reason": "authenticated flat checkpoint",
            "trigger": None,
            "suppress_passive": False,
            "blocked": False,
            "active_attempts": 0,
            "episode_active_attempts": 0,
            "last_active_trigger": None,
            "order_lane": None,
            "order_side": None,
            "order_price": None,
            "order_amount": None,
            "completed_episode_execution_history": [
                {
                    "episode_id": 1,
                    "active_attempts": 1,
                    "last_active_trigger": "loss",
                }
            ],
        }
        executor = SimpleNamespace(
            record_authenticated_flat=Mock(),
            execution_snapshot=Mock(return_value=execution_snapshot),
        )
        coordinator = self.prepare_running(
            account_monitor=monitor,
            order_manager=manager,
            inventory_executor=executor,
        )
        coordinator.metrics.account_audit = {
            "state": "healthy",
            "economic_state": "collecting",
        }
        coordinator.metrics.inventory_unwind = {
            "state": "active_ready",
            "trigger": "loss",
            "order_lane": "active_ioc",
            "blocked": True,
        }
        coordinator._position = None

        audit_task = asyncio.create_task(coordinator.audit_account_once())
        await entered.wait()
        cycle_task = asyncio.create_task(coordinator.run_one_cycle(force=True))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(
            audit_task, cycle_task, return_exceptions=True
        )
        monitor.audit.side_effect = None

        self.assertIsInstance(results[0], AccountAuditError)
        self.assertFalse(coordinator.running)
        coordinator.risk_manager.evaluate.assert_not_called()
        manager.reconcile.assert_not_awaited()
        executor.record_authenticated_flat.assert_called_once_with()
        self.assertEqual(
            coordinator.metrics.inventory_unwind[
                "completed_episode_execution_history"
            ],
            execution_snapshot["completed_episode_execution_history"],
        )
        self.assertIsNone(coordinator.metrics.inventory_unwind["trigger"])
        self.assertIsNone(coordinator.metrics.inventory_unwind["order_lane"])
        self.assertFalse(coordinator.metrics.inventory_unwind["blocked"])

    async def test_published_economic_no_go_fails_closed_before_risk(self) -> None:
        manager = self.order_manager()
        coordinator = self.prepare_running(order_manager=manager)
        coordinator.metrics.account_audit = {
            "state": "hard_stop",
            "economic_state": "no_go",
            "economic_reason": "authenticated economic no-go",
        }

        await coordinator.run_one_cycle(force=True)

        coordinator.risk_manager.evaluate.assert_not_called()
        manager.reconcile.assert_not_awaited()
        manager.cancel_managed_orders.assert_awaited_once()
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

    async def test_account_audit_timeout_marks_hard_stop(self) -> None:
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        coordinator = self.prepare_running()
        coordinator.account_monitor = monitor

        async def timeout(awaitable, **_kwargs):
            awaitable.close()
            raise TimeoutError

        with patch(
            "core.services.market_maker.coordinator.asyncio.wait_for",
            new=timeout,
        ):
            with self.assertRaisesRegex(AccountAuditError, "timed out"):
                await coordinator.audit_account_once()

        monitor.mark_hard_stop.assert_called_once_with("account audit timed out")

    async def test_account_audit_timeout_includes_cycle_lock_wait(self) -> None:
        monitor = SimpleNamespace(
            audit=AsyncMock(),
            mark_hard_stop=Mock(),
            snapshot=Mock(return_value={"state": "healthy"}),
        )
        coordinator = self.prepare_running()
        coordinator.account_monitor = monitor
        await coordinator._cycle_lock.acquire()

        async def timeout(awaitable, **_kwargs):
            awaitable.close()
            raise TimeoutError

        try:
            with patch(
                "core.services.market_maker.coordinator.asyncio.wait_for",
                new=timeout,
            ):
                with self.assertRaisesRegex(AccountAuditError, "timed out"):
                    await coordinator.audit_account_once()
        finally:
            coordinator._cycle_lock.release()

        monitor.audit.assert_not_awaited()
        monitor.mark_hard_stop.assert_called_once_with("account audit timed out")

    async def test_disabled_account_audit_does_not_start_worker(self) -> None:
        coordinator = self.coordinator(
            config=self.config(account_audit_interval_seconds=0)
        )
        coordinator.account_monitor = SimpleNamespace()

        coordinator._start_tasks()
        task_names = {task.get_name() for task in coordinator._tasks}
        await asyncio.gather(*coordinator._tasks)
        coordinator._tasks.clear()

        self.assertNotIn("market-maker-account-audit", task_names)

    async def test_eligible_quote_seconds_exclude_paused_time(self) -> None:
        coordinator = self.prepare_running()
        coordinator._transition(RuntimeState.ACTIVE)
        self.now += 60

        active = await coordinator.emit_status_once()
        coordinator._transition(RuntimeState.PAUSED_MARKET)
        self.now += 40
        paused = await coordinator.emit_status_once()

        self.assertEqual(active["eligible_quote_seconds"], 60)
        self.assertEqual(paused["eligible_quote_seconds"], 60)

    async def test_unknown_order_update_pauses_and_cancels_in_cycle(self) -> None:
        manager = self.order_manager()

        async def mark_unknown(_order) -> None:
            manager.pause_reason = "unknown open order"

        manager.handle_order_update.side_effect = mark_unknown
        coordinator = self.prepare_running(order_manager=manager)
        await coordinator.on_order_update(self.order("foreign"))

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ORDER_STATE)
        manager.cancel_managed_orders.assert_awaited_once()
        manager.reconcile.assert_not_awaited()

    async def test_rest_sync_unknown_order_increments_metric(self) -> None:
        manager = self.order_manager()
        manager.has_unknown_order_state = True
        manager.pause_reason = "unknown open orders: foreign"
        coordinator = self.prepare_running(order_manager=manager)

        healthy = await coordinator.sync_open_orders_once()

        self.assertFalse(healthy)
        self.assertEqual(coordinator.metrics.counters["unknown_orders"], 1)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_rest_sync_uncertain_state_without_pause_stays_closed(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.has_uncertain_state = True
        coordinator = self.prepare_running(order_manager=manager)

        healthy = await coordinator.sync_open_orders_once()

        self.assertFalse(healthy)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ORDER_STATE)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_rest_sync_fill_refreshes_position_before_quoting(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            PositionSnapshot(
                symbol="BTC",
                signed_size=Decimal("0.2"),
                entry_price=Decimal("99.9"),
                unrealized_pnl=Decimal("0"),
                received_monotonic=self.now,
            )
        ]
        manager = self.order_manager()
        manager.sync_open_orders.return_value = True
        observed = replace(
            self.order("rest-fill", filled="0.2", status=OrderStatus.FILLED),
            cost=Decimal("20"),
            average=Decimal("100"),
        )
        manager.last_sync_result = ReconcileResult(
            actions=(),
            runtime_state=RuntimeState.SYNCING,
            position_refresh_required=True,
            fill_observed=True,
            observed_fill_orders=(observed,),
        )
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )

        self.assertTrue(await coordinator.sync_open_orders_once())

        adapter.get_positions.assert_awaited_once_with(["BTC"])
        self.assertEqual(
            coordinator.position_snapshot.signed_size, Decimal("0.2")
        )
        self.assertEqual(len(coordinator.metrics.fill_markouts), 1)
        self.assertEqual(
            coordinator.metrics.fill_markouts[0]["observation_source"],
            "rest_open_order_sync",
        )
        self.assertIn(
            "rest_open_order_sync",
            coordinator.metrics.snapshot(self.now)["fill_markout_coverage"][
                "sources"
            ],
        )

    async def test_rest_sync_refreshes_fill_before_unknown_order_pause(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            self.position(signed_size=Decimal("0.2"))
        ]
        manager = self.order_manager()
        manager.sync_open_orders.return_value = True
        manager.has_unknown_order_state = True
        manager.pause_reason = "unknown open orders: foreign"
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )

        self.assertFalse(await coordinator.sync_open_orders_once())

        adapter.get_positions.assert_awaited_once_with(["BTC"])
        self.assertEqual(
            coordinator.position_snapshot.signed_size, Decimal("0.2")
        )
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ORDER_STATE)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_rest_sync_fill_position_failure_fails_closed(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.side_effect = RuntimeError("position unavailable")
        manager = self.order_manager()
        manager.sync_open_orders.return_value = True
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )

        self.assertFalse(await coordinator.sync_open_orders_once())

        self.assertIsNone(coordinator.position_snapshot)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_POSITION)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_rest_sync_error_fails_closed_immediately(self) -> None:
        manager = self.order_manager()
        manager.sync_open_orders.side_effect = RuntimeError("REST unavailable")
        coordinator = self.prepare_running(order_manager=manager)

        self.assertFalse(await coordinator.sync_open_orders_once())

        self.assertIsNone(coordinator.position_snapshot)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ORDER_STATE)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_fill_update_refreshes_rest_position_before_risk(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            PositionSnapshot(
                symbol="BTC",
                signed_size=Decimal("0.1"),
                entry_price=Decimal("99.9"),
                unrealized_pnl=Decimal("0"),
                received_monotonic=self.now,
            )
        ]
        manager = self.order_manager()
        manager.handle_order_update.return_value = True
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )
        await coordinator.on_order_update(self.order(filled="0.1"))

        await coordinator.run_one_cycle(force=True)

        adapter.get_positions.assert_awaited_once_with(["BTC"])
        evaluated_position = coordinator.risk_manager.evaluate.call_args.args[0]
        self.assertEqual(evaluated_position.signed_size, Decimal("0.1"))
        self.assertEqual(
            coordinator.metrics.counters["position_refresh_after_order_update"],
            1,
        )

    async def test_duplicate_full_fill_callback_is_counted_once(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        manager.handle_order_update.side_effect = [True, False]
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )
        fill = replace(
            self.order("filled", filled="0.2"),
            status=OrderStatus.FILLED,
        )

        await coordinator.on_order_update(fill)
        await coordinator.on_order_update(fill)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator._processed_fill_generation, 1)
        self.assertEqual(coordinator.metrics.counters["full_fills"], 1)
        self.assertEqual(coordinator.metrics.counters["partial_fills"], 0)
        adapter.get_positions.assert_awaited_once_with(["BTC"])

    async def test_unproven_full_fill_callback_is_not_counted(self) -> None:
        manager = self.order_manager()
        manager.handle_order_update.return_value = False
        coordinator = self.prepare_running(order_manager=manager)
        fill = replace(
            self.order("unknown-fill", filled="0.2"),
            status=OrderStatus.FILLED,
        )

        await coordinator.on_order_update(fill)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator._processed_fill_generation, 0)
        self.assertEqual(coordinator.metrics.counters["full_fills"], 0)
        self.assertEqual(coordinator.metrics.counters["partial_fills"], 0)

    async def test_reconcile_visible_fill_refreshes_position(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = [
            self.position(signed_size=Decimal("0.1"))
        ]
        manager = self.order_manager()
        manager.reconcile.return_value = SimpleNamespace(
            actions=(),
            errors=(),
            runtime_state=RuntimeState.ACTIVE,
            position_refresh_required=True,
        )
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )

        await coordinator.run_one_cycle(force=True)

        adapter.get_positions.assert_awaited_once_with(["BTC"])
        self.assertEqual(
            coordinator.position_snapshot.signed_size, Decimal("0.1")
        )

    async def test_partial_subscription_failure_unsubscribes_everything(self) -> None:
        adapter = self.adapter()
        adapter.subscribe_user_data.side_effect = RuntimeError("subscribe failed")
        manager = self.order_manager()
        coordinator = self.coordinator(adapter=adapter, order_manager=manager)

        with self.assertRaisesRegex(RuntimeError, "subscribe failed"):
            await coordinator.start()

        adapter.unsubscribe.assert_awaited_once_with()
        adapter.disconnect.assert_awaited_once_with()
        manager.shutdown.assert_awaited_once_with()

    async def test_reconcile_error_limit_performs_fail_closed_cancel(self) -> None:
        manager = self.order_manager()
        manager.reconcile.return_value = SimpleNamespace(
            errors=("mutation failed",), runtime_state=RuntimeState.ACTIVE
        )
        coordinator = self.prepare_running(
            config=self.config(max_consecutive_errors=2),
            order_manager=manager,
        )

        await coordinator.run_one_cycle(force=True)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_error_cooldown_requires_full_resync_before_recovery(self) -> None:
        adapter = self.adapter()
        manager = self.order_manager()
        manager.reconcile.return_value = SimpleNamespace(
            errors=("mutation failed",), runtime_state=RuntimeState.ACTIVE
        )
        coordinator = self.prepare_running(
            adapter=adapter,
            config=self.config(
                max_consecutive_errors=1, error_cooldown_seconds=5
            ),
            order_manager=manager,
        )

        await coordinator.run_one_cycle(force=True)
        manager.reconcile.return_value = SimpleNamespace(
            errors=(), runtime_state=RuntimeState.ACTIVE
        )
        await coordinator.run_one_cycle(force=True)
        self.assertEqual(manager.reconcile.await_count, 1)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)

        self.now += 6
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(manager.reconcile.await_count, 2)
        adapter.health_check.assert_awaited_once()
        adapter.get_orderbook.assert_awaited_once_with("BTC")
        adapter.get_positions.assert_awaited_once_with(["BTC"])
        manager.sync_open_orders.assert_awaited_once()
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)

    async def test_error_cooldown_does_not_retry_cancel_or_extend_deadline(self) -> None:
        manager = self.order_manager()
        manager.cancel_managed_orders.return_value = SimpleNamespace(
            actions=(), errors=("cancel rejected: http_429",)
        )
        coordinator = self.prepare_running(
            config=self.config(
                max_consecutive_errors=5, error_cooldown_seconds=5
            ),
            order_manager=manager,
        )

        await coordinator._fail_closed(
            RuntimeState.PAUSED_ERROR, "reconciliation failed"
        )
        deadline = coordinator._error_paused_until
        self.assertEqual(deadline, 105.0)

        for now in (101.0, 102.0, 103.0, 104.0):
            self.now = now
            await coordinator.run_one_cycle(force=True)

        manager.cancel_managed_orders.assert_awaited_once()
        self.assertEqual(coordinator._error_paused_until, deadline)

        coordinator._recover_from_error_pause = AsyncMock(return_value=False)
        self.now = deadline
        await coordinator.run_one_cycle(force=True)
        coordinator._recover_from_error_pause.assert_awaited_once_with()

    async def test_error_recovery_refreshes_position_after_visible_fill(self) -> None:
        adapter = self.adapter()
        adapter.get_positions.side_effect = (
            [self.position()],
            [self.position(signed_size=Decimal("0.8"))],
        )
        manager = self.order_manager()
        manager.sync_open_orders.return_value = True
        coordinator = self.prepare_running(
            adapter=adapter, order_manager=manager
        )

        recovered = await coordinator._recover_from_error_pause()

        self.assertTrue(recovered)
        self.assertEqual(adapter.get_positions.await_count, 2)
        self.assertEqual(
            coordinator.position_snapshot.signed_size, Decimal("0.8")
        )

    async def test_error_recovery_rejects_uncertain_state_without_pause(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.has_uncertain_state = True
        coordinator = self.prepare_running(order_manager=manager)

        recovered = await coordinator._recover_from_error_pause()

        self.assertFalse(recovered)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_periodic_position_change_requires_full_rest_resync(
        self,
    ) -> None:
        adapter = self.adapter()
        adapter.get_positions.return_value = []
        manager = self.order_manager()
        coordinator = self.prepare_running(
            adapter=adapter,
            order_manager=manager,
        )
        coordinator._position = self.position(
            signed_size=Decimal("-0.2")
        )

        self.assertFalse(await coordinator.poll_position_once())

        self.assertEqual(
            coordinator.position_snapshot.signed_size,
            Decimal("-0.2"),
        )
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_POSITION)
        manager.cancel_managed_orders.assert_awaited_once()

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.position_snapshot.signed_size, Decimal("0"))
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
        self.assertEqual(adapter.get_positions.await_count, 2)
        manager.sync_open_orders.assert_awaited_once()
        manager.reconcile.assert_awaited_once()

    async def test_error_recovery_cannot_create_with_adapter_registry_pending(
        self,
    ) -> None:
        adapter = self.adapter()
        adapter.create_order = AsyncMock()
        adapter.cancel_order = AsyncMock()
        adapter.cancel_all_orders = AsyncMock()
        adapter.get_open_orders = AsyncMock(return_value=[])
        adapter.get_order_history = AsyncMock(return_value=[])
        adapter.get_unresolved_submissions = Mock(
            return_value=[{"client_order_id": "client-1"}]
        )
        adapter.resolve_unresolved_submissions = AsyncMock(return_value=[])
        config = self.config(
            max_consecutive_errors=1, error_cooldown_seconds=5
        )
        manager = MarketMakerOrderManager(
            adapter, config, self.metadata, monotonic=lambda: self.now
        )
        coordinator = self.prepare_running(
            adapter=adapter, config=config, order_manager=manager
        )
        coordinator._transition(RuntimeState.PAUSED_ERROR, "mutation failed")
        coordinator._error_paused_until = self.now

        await coordinator.run_one_cycle(force=True)
        state = coordinator.state
        adapter.get_unresolved_submissions.return_value = []

        adapter.resolve_unresolved_submissions.assert_awaited_once()
        adapter.create_order.assert_not_awaited()
        self.assertEqual(state, RuntimeState.PAUSED_ERROR)

    async def test_successful_position_poll_does_not_clear_reconcile_errors(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.reconcile.return_value = SimpleNamespace(
            errors=("mutation failed",), runtime_state=RuntimeState.ACTIVE
        )
        coordinator = self.prepare_running(
            config=self.config(max_consecutive_errors=2),
            order_manager=manager,
        )

        await coordinator.run_one_cycle(force=True)
        await coordinator.poll_position_once()
        self.assertEqual(coordinator.metrics.consecutive_errors, 1)
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        manager.cancel_managed_orders.assert_awaited_once()

    async def test_callbacks_are_ignored_after_stop(self) -> None:
        coordinator = self.prepare_running()
        original = coordinator.market_snapshot
        coordinator._running = False
        coordinator._transition(RuntimeState.STOPPED)
        coordinator.quote_event.clear()

        await coordinator.on_orderbook(self.market(received=self.now + 1))
        await coordinator.on_order_update(self.order("late"))

        self.assertIs(coordinator.market_snapshot, original)
        self.assertFalse(coordinator.quote_event.is_set())
        self.assertFalse(coordinator._pending_orders)

    async def test_metrics_capture_market_quote_and_exposure(self) -> None:
        manager = self.order_manager()
        manager.unresolved_cancellation_count = 1
        manager.resolved_ambiguous_cancellations = 2
        coordinator = self.prepare_running(order_manager=manager)

        await coordinator.run_one_cycle(force=True)
        snapshot = await coordinator.emit_status_once()

        self.assertEqual(snapshot["mid"], Decimal("100"))
        self.assertEqual(snapshot["raw_spread_ticks"], Decimal("2"))
        self.assertEqual(snapshot["raw_spread_bps"], Decimal("20"))
        self.assertEqual(snapshot["reservation_price"], Decimal("100"))
        self.assertEqual(snapshot["quote_spread_ticks"], Decimal("2"))
        self.assertEqual(snapshot["quote_spread_bps"], Decimal("20"))
        self.assertEqual(snapshot["quote_reason"], "normal")
        self.assertEqual(snapshot["max_position_utilization"], Decimal("0.2"))
        self.assertEqual(snapshot["counters"]["reconciliation_success"], 1)
        self.assertEqual(snapshot["counters"]["unresolved_cancellations"], 1)
        self.assertEqual(
            snapshot["counters"]["resolved_ambiguous_cancellations"], 2
        )

    async def test_paused_status_uses_current_position_and_live_orders(
        self,
    ) -> None:
        manager = self.order_manager()
        manager.snapshot.return_value = (
            SimpleNamespace(
                side=OrderSide.SELL,
                price=Decimal("100.1"),
                remaining=Decimal("0.2"),
            ),
        )
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._transition(
            RuntimeState.PAUSED_ORDER_STATE,
            "order submission outcome is uncertain",
        )
        self.now = 110.0
        coordinator._market = self.market(received=109.0)
        coordinator._position = self.position(
            received=108.0,
            signed_size=Decimal("-0.4"),
        )
        coordinator.metrics.signed_position = Decimal("-0.2")
        coordinator.metrics.live_ask = Decimal("999")

        snapshot = await coordinator.emit_status_once()

        self.assertEqual(snapshot["signed_position"], Decimal("-0.4"))
        self.assertEqual(snapshot["book_age_seconds"], 1.0)
        self.assertEqual(snapshot["position_age_seconds"], 2.0)
        self.assertIsNone(snapshot["live_bid"])
        self.assertEqual(snapshot["live_ask"], Decimal("100.1"))
        self.assertEqual(snapshot["live_sell_remaining"], Decimal("0.2"))
        manager.reconcile.assert_not_awaited()

    async def test_mutation_success_metrics_are_per_action(self) -> None:
        coordinator = self.prepare_running()
        coordinator.order_manager.reconcile.return_value = SimpleNamespace(
            actions=(
                ReconcileAction(
                    OrderSide.BUY, "place", "confirmed", success=True
                ),
                ReconcileAction(
                    OrderSide.SELL, "place", "uncertain", success=None
                ),
            ),
            errors=("create outcome uncertain: http_429",),
            runtime_state=RuntimeState.PAUSED_ORDER_STATE,
        )

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.metrics.counters["create_attempts"], 2)
        self.assertEqual(coordinator.metrics.counters["create_success"], 1)
        self.assertEqual(
            coordinator.metrics.counters["ambiguous_submissions"], 1
        )
        self.assertEqual(coordinator.metrics.counters["http_429"], 1)
        self.assertEqual(coordinator.state, RuntimeState.PAUSED_ERROR)
        self.assertEqual(coordinator._error_paused_until, 105.0)
        coordinator.order_manager.cancel_managed_orders.assert_awaited_once()

        self.now += 1
        await coordinator.run_one_cycle(force=True)
        coordinator.order_manager.reconcile.assert_awaited_once()
        coordinator.order_manager.cancel_managed_orders.assert_awaited_once()

    async def test_definitive_not_sent_action_is_retryable_not_reconcile_failure(
        self,
    ) -> None:
        coordinator = self.prepare_running()
        coordinator.order_manager.reconcile.return_value = SimpleNamespace(
            actions=(
                ReconcileAction(
                    OrderSide.BUY,
                    "place",
                    "DNS resolution failed before send",
                    success=False,
                ),
            ),
            errors=(),
            runtime_state=RuntimeState.ACTIVE,
        )

        await coordinator.run_one_cycle(force=True)

        counters = coordinator.metrics.counters
        self.assertEqual(counters["create_attempts"], 1)
        self.assertEqual(counters["create_success"], 0)
        self.assertEqual(counters["ambiguous_submissions"], 0)
        self.assertEqual(counters["reconciliation_failure"], 0)
        self.assertEqual(counters["reconciliation_success"], 1)
        self.assertEqual(coordinator.state, RuntimeState.ACTIVE)

    async def test_fail_closed_cancel_records_exact_action_metrics(self) -> None:
        manager = self.order_manager()
        manager.cancel_managed_orders.return_value = SimpleNamespace(
            actions=(
                ReconcileAction(
                    OrderSide.BUY, "cancel", "stale book", success=True
                ),
            ),
            errors=(),
            position_refresh_required=True,
        )
        coordinator = self.prepare_running(order_manager=manager)
        coordinator._market = self.market(
            self.now - coordinator.config.stale_book_seconds - 1
        )

        await coordinator.run_one_cycle(force=True)

        self.assertEqual(coordinator.metrics.counters["cancel_attempts"], 1)
        self.assertEqual(coordinator.metrics.counters["cancel_success"], 1)
        self.assertIsNone(coordinator.position_snapshot)
        self.assertTrue(coordinator._position_refresh_required)

    async def test_dry_run_plans_are_visible_in_status_counters(self) -> None:
        manager = self.order_manager()
        manager.reconcile.return_value = SimpleNamespace(
            actions=(
                ReconcileAction(OrderSide.BUY, "would_place", "dry run"),
                ReconcileAction(OrderSide.SELL, "would_cancel", "dry run"),
            ),
            errors=(),
            runtime_state=RuntimeState.ACTIVE,
        )
        coordinator = self.prepare_running(order_manager=manager)

        await coordinator.run_one_cycle(force=True)
        snapshot = await coordinator.emit_status_once()

        self.assertEqual(snapshot["counters"]["would_place"], 1)
        self.assertEqual(snapshot["counters"]["would_cancel"], 1)


if __name__ == "__main__":
    unittest.main()
