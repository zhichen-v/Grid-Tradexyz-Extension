import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.adapters.exchanges.models import (
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.controllers.base import (
    QuoteControllerDecision,
    SideQuoteAdjustment,
)
from core.services.market_maker.coordinator import MarketMakerCoordinator
from core.services.market_maker.models import (
    DesiredOrder,
    DesiredQuotes,
    MarketMetadata,
    MarketSnapshot,
    OrderBookLevel,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)
from core.services.market_maker.order_manager import MarketMakerOrderManager
from core.services.market_maker.risk_manager import RiskDecision


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class _Features:
    def to_dict(self) -> dict:
        return {"health": "ready"}


class _Controller:
    def __init__(self, features: _Features) -> None:
        self.features = features
        self.bid_extra = 0
        self.ask_extra = 0
        self.bid_blocked = False
        self.ask_blocked = False
        self.decision_id = 0

    def evaluate(self, _context) -> QuoteControllerDecision:
        self.decision_id += 1
        return QuoteControllerDecision(
            mode="active",
            controller="test_controller",
            ready=True,
            reason="test",
            decision_id=self.decision_id,
            bid=SideQuoteAdjustment(
                extra_spread_ticks=self.bid_extra,
                blocked=self.bid_blocked,
            ),
            ask=SideQuoteAdjustment(
                extra_spread_ticks=self.ask_extra,
                blocked=self.ask_blocked,
            ),
            features=self.features,
        )


class MarketMakerControllerOrderLifecycleTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("0"),
        )
        self.market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("99.9"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("100.1"), Decimal("1")),),
            best_bid=Decimal("99.9"),
            best_ask=Decimal("100.1"),
            exchange_timestamp=None,
            received_monotonic=self.clock.value,
        )
        self.position = PositionSnapshot(
            symbol="BTC",
            signed_size=Decimal("0"),
            entry_price=None,
            unrealized_pnl=Decimal("0"),
            received_monotonic=self.clock.value,
        )
        self.features = _Features()
        self.controller = _Controller(self.features)
        self.created = 0
        self.terminal_cancel = True
        self.events: list[tuple[str, str]] = []
        self.adapter = SimpleNamespace(
            create_order=AsyncMock(side_effect=self._create_order),
            cancel_order=AsyncMock(side_effect=self._cancel_order),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[]),
            get_positions=AsyncMock(side_effect=self._positions),
            get_unresolved_submissions=Mock(return_value=[]),
            get_unresolved_cancellations=Mock(return_value=[]),
            get_terminal_cancellation_outcome=Mock(return_value=None),
            confirm_terminal_cancellation_outcome=Mock(return_value=True),
            resolve_unresolved_submissions=AsyncMock(return_value=[]),
        )

    def config(self, **overrides) -> MarketMakerConfig:
        values = {
            "symbol": "BTC",
            "order_size": "0.2",
            "max_position": "1",
            "min_profit_buffer_bps": "0",
            "quote_controller_mode": "active",
            "toxicity_widen_start_ticks": "1",
            "toxicity_max_extra_spread_ticks": 3,
            "dry_run": False,
        }
        values.update(overrides)
        return MarketMakerConfig(**values)

    @staticmethod
    def desired(*, both_sides: bool = False) -> DesiredQuotes:
        return DesiredQuotes(
            bid=DesiredOrder(
                OrderSide.BUY,
                Decimal("99.9"),
                Decimal("0.2"),
                False,
                "test",
            ),
            ask=(
                DesiredOrder(
                    OrderSide.SELL,
                    Decimal("100.1"),
                    Decimal("0.2"),
                    False,
                    "test",
                )
                if both_sides
                else None
            ),
            reference_price=Decimal("100"),
            reservation_price=Decimal("100"),
            half_spread=Decimal("0.1"),
            inventory_ratio=Decimal("0"),
            runtime_state=RuntimeState.ACTIVE,
            reason="test",
        )

    @staticmethod
    def risk() -> RiskDecision:
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
            reason="test",
            safe=True,
        )

    async def _positions(self, _symbols):
        return [
            PositionSnapshot(
                symbol="BTC",
                signed_size=Decimal("0"),
                entry_price=None,
                unrealized_pnl=Decimal("0"),
                received_monotonic=self.clock.value,
            )
        ]

    async def _create_order(
        self, symbol, side, _order_type, amount, price, params
    ) -> OrderData:
        self.created += 1
        order_id = str(self.created)
        self.events.append(("create", order_id))
        return self.order(
            order_id,
            side,
            price=price,
            amount=amount,
            params=params,
        )

    async def _cancel_order(self, order_id, _symbol) -> OrderData:
        order_id = str(order_id)
        self.events.append(("cancel", order_id))
        side = OrderSide.BUY if order_id == "1" else OrderSide.SELL
        return self.order(
            order_id,
            side,
            status=(
                OrderStatus.CANCELED
                if self.terminal_cancel
                else OrderStatus.PENDING
            ),
            params={"cancel_terminal": self.terminal_cancel},
        )

    @staticmethod
    def order(
        order_id: str,
        side: OrderSide,
        *,
        status: OrderStatus = OrderStatus.OPEN,
        price: Decimal = Decimal("99.9"),
        amount: Decimal = Decimal("0.2"),
        params: dict | None = None,
    ) -> OrderData:
        return OrderData(
            id=order_id,
            client_id=f"client-{order_id}",
            symbol="BTC",
            side=side,
            type=OrderType.LIMIT,
            amount=Decimal(amount),
            price=Decimal(price),
            filled=Decimal("0"),
            remaining=Decimal(amount),
            cost=Decimal("0"),
            average=None,
            status=status,
            timestamp=datetime(2026, 1, 1),
            updated=None,
            fee=None,
            trades=[],
            params=params or {},
            raw_data={},
        )

    def coordinator(
        self, *, config: MarketMakerConfig, both_sides: bool = False
    ) -> tuple[MarketMakerCoordinator, MarketMakerOrderManager]:
        manager = MarketMakerOrderManager(
            self.adapter,
            config,
            self.metadata,
            monotonic=self.clock,
        )
        coordinator = MarketMakerCoordinator(
            self.adapter,
            config,
            metadata=self.metadata,
            order_manager=manager,
            strategy=SimpleNamespace(
                calculate_quotes=Mock(
                    return_value=self.desired(both_sides=both_sides)
                )
            ),
            risk_manager=SimpleNamespace(
                evaluate=Mock(return_value=self.risk())
            ),
            quote_controller=self.controller,
            market_feature_store=SimpleNamespace(
                update=Mock(return_value=self.features)
            ),
            monotonic=self.clock,
        )
        coordinator._running = True
        coordinator._authenticated = True
        coordinator._exchange_healthy = True
        coordinator._market = self.market
        coordinator._position = self.position
        coordinator._transition(RuntimeState.SYNCING)
        return coordinator, manager

    async def test_active_side_block_cancels_only_that_entry_side(self) -> None:
        coordinator, manager = self.coordinator(
            config=self.config(min_order_lifetime_ms=30_000),
            both_sides=True,
        )
        await coordinator.run_one_cycle(force=True)
        await coordinator.run_one_cycle(force=True)

        self.controller.bid_blocked = True
        await coordinator.run_one_cycle(force=True)

        self.adapter.cancel_order.assert_awaited_once_with("1", "BTC")
        self.adapter.confirm_terminal_cancellation_outcome.assert_called_once()
        self.assertIsNone(manager.slots[OrderSide.BUY])
        self.assertEqual(manager.slots[OrderSide.SELL].order_id, "2")
        self.assertEqual(self.adapter.create_order.await_count, 2)

    async def test_widening_obeys_reprice_threshold_and_minimum_lifetime(
        self,
    ) -> None:
        coordinator, manager = self.coordinator(
            config=self.config(
                reprice_threshold_ticks=2,
                min_order_lifetime_ms=1000,
            )
        )
        await coordinator.run_one_cycle(force=True)

        self.controller.bid_extra = 1
        self.clock.value += 0.1
        await coordinator.run_one_cycle(force=True)
        self.controller.bid_extra = 2
        await coordinator.run_one_cycle(force=True)

        self.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(manager.slots[OrderSide.BUY].price, Decimal("99.9"))

        self.clock.value += 1
        await coordinator.run_one_cycle(force=True)

        self.adapter.cancel_order.assert_awaited_once_with("1", "BTC")
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertIsNone(manager.slots[OrderSide.BUY])

        await coordinator.run_one_cycle(force=True)
        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertEqual(manager.slots[OrderSide.BUY].price, Decimal("99.7"))
        self.assertEqual(
            self.events,
            [("create", "1"), ("cancel", "1"), ("create", "2")],
        )

    async def test_resume_waits_for_exact_terminal_cancellation_proof(
        self,
    ) -> None:
        coordinator, manager = self.coordinator(config=self.config())
        await coordinator.run_one_cycle(force=True)

        self.terminal_cancel = False
        self.controller.bid_blocked = True
        await coordinator.run_one_cycle(force=True)

        self.assertEqual(
            manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        )
        self.assertEqual(self.adapter.create_order.await_count, 1)

        self.controller.bid_blocked = False
        await coordinator.run_one_cycle(force=True)
        self.assertEqual(self.adapter.create_order.await_count, 1)

        terminal = self.order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.CANCELED,
            params={"cancel_terminal": True},
        )
        await coordinator.on_order_update(terminal)
        await coordinator.run_one_cycle(force=True)

        self.adapter.confirm_terminal_cancellation_outcome.assert_called_once_with(
            terminal
        )
        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertEqual(manager.slots[OrderSide.BUY].order_id, "2")
        self.assertEqual(
            self.events,
            [("create", "1"), ("cancel", "1"), ("create", "2")],
        )


if __name__ == "__main__":
    unittest.main()
