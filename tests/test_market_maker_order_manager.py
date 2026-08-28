import asyncio
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientConnectorDNSError

from core.adapters.exchanges.exceptions import OrderSubmissionNotSentError
from core.adapters.exchanges.models import (
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.models import (
    DesiredOrder,
    DesiredQuotes,
    ManagedOrder,
    MarketMetadata,
    MarketSnapshot,
    OrderBookLevel,
    OrderSlotState,
    PositionSnapshot,
    RuntimeState,
)
from core.services.market_maker.order_manager import MarketMakerOrderManager
from core.services.market_maker.risk_manager import RiskDecision, RiskManager
from core.services.market_maker.strategy import MarketMakerStrategy


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def exchange_order(
    order_id: str,
    side: OrderSide,
    *,
    status: OrderStatus = OrderStatus.OPEN,
    price: str = "100",
    amount: str = "0.2",
    remaining: str | None = None,
    client_id: str | None = None,
    params: dict | None = None,
    raw_data: dict | None = None,
) -> OrderData:
    amount_value = Decimal(amount)
    remaining_value = (
        amount_value if remaining is None else Decimal(remaining)
    )
    return OrderData(
        id=order_id,
        client_id=client_id or f"client-{order_id}",
        symbol="BTC",
        side=side,
        type=OrderType.LIMIT,
        amount=amount_value,
        price=Decimal(price),
        filled=amount_value - remaining_value,
        remaining=remaining_value,
        cost=Decimal("0"),
        average=None,
        status=status,
        timestamp=datetime.now(),
        updated=None,
        fee=None,
        trades=[],
        params=params or {},
        raw_data=raw_data or {},
    )


class MarketMakerOrderManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("1"),
            min_profit_buffer_bps=Decimal("0"),
            min_order_lifetime_ms=1000,
            dry_run=False,
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
        self.adapter = SimpleNamespace(
            create_order=AsyncMock(side_effect=self._create_order),
            cancel_order=AsyncMock(side_effect=self._cancel_order),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[]),
            get_unresolved_submissions=Mock(return_value=[]),
            get_unresolved_cancellations=Mock(return_value=[]),
            get_terminal_cancellation_outcome=Mock(return_value=None),
            confirm_terminal_cancellation_outcome=Mock(return_value=False),
            resolve_unresolved_submissions=AsyncMock(return_value=[]),
        )
        self.manager = self.make_manager()
        self.created = 0

    def make_manager(
        self, config: MarketMakerConfig | None = None
    ) -> MarketMakerOrderManager:
        return MarketMakerOrderManager(
            self.adapter,
            config or self.config,
            self.metadata,
            monotonic=self.clock,
        )

    async def _create_order(
        self,
        symbol,
        side,
        order_type,
        amount,
        price,
        params,
    ):
        self.created += 1
        return exchange_order(
            str(self.created),
            side,
            price=str(price),
            amount=str(amount),
            params=params,
        )

    async def _cancel_order(self, order_id, symbol):
        side = OrderSide.BUY if str(order_id) == "1" else OrderSide.SELL
        return exchange_order(
            str(order_id),
            side,
            status=OrderStatus.CANCELED,
            params={"cancel_terminal": True},
        )

    @staticmethod
    def desired(
        *,
        bid_price: str | None = "99.9",
        ask_price: str | None = "100.1",
        bid_amount: str = "0.2",
        ask_amount: str = "0.2",
        bid_reduce_only: bool = False,
        ask_reduce_only: bool = False,
        state: RuntimeState = RuntimeState.ACTIVE,
    ) -> DesiredQuotes:
        bid = (
            DesiredOrder(
                OrderSide.BUY,
                Decimal(bid_price),
                Decimal(bid_amount),
                bid_reduce_only,
                "test",
            )
            if bid_price is not None
            else None
        )
        ask = (
            DesiredOrder(
                OrderSide.SELL,
                Decimal(ask_price),
                Decimal(ask_amount),
                ask_reduce_only,
                "test",
            )
            if ask_price is not None
            else None
        )
        return DesiredQuotes(
            bid=bid,
            ask=ask,
            reference_price=Decimal("100"),
            reservation_price=Decimal("100"),
            half_spread=Decimal("0.1"),
            inventory_ratio=Decimal("0"),
            runtime_state=state,
            reason="test",
        )

    @staticmethod
    def risk(
        *,
        buy_amount: str | None = "0.2",
        sell_amount: str | None = "0.2",
        buy_reduce_only: bool = False,
        sell_reduce_only: bool = False,
        safe: bool = True,
        state: RuntimeState = RuntimeState.ACTIVE,
    ) -> RiskDecision:
        return RiskDecision(
            buy_amount=(Decimal(buy_amount) if buy_amount else None),
            sell_amount=(Decimal(sell_amount) if sell_amount else None),
            buy_reduce_only=buy_reduce_only,
            sell_reduce_only=sell_reduce_only,
            buy_capacity=Decimal("1"),
            sell_capacity=Decimal("1"),
            worst_long=Decimal("0.2"),
            worst_short=Decimal("-0.2"),
            inventory_ratio=Decimal("0"),
            runtime_state=state,
            reason="test",
            safe=safe,
        )

    async def test_initial_bid_and_ask_are_post_only(self) -> None:
        results = (
            await self.manager.reconcile(self.desired(), self.risk()),
            await self.manager.reconcile(self.desired(), self.risk()),
        )

        self.assertEqual(self.adapter.create_order.await_count, 2)
        for result in results:
            self.assertEqual(
                {action.operation for action in result.actions}, {"place"}
            )
            self.assertTrue(
                all(action.success is True for action in result.actions)
            )
            self.assertFalse(result.position_refresh_required)
        for call in self.adapter.create_order.await_args_list:
            self.assertEqual(call.args[0], "BTC")
            self.assertEqual(call.args[2], OrderType.LIMIT)
            self.assertEqual(
                call.kwargs["params"],
                {"time_in_force": "POST_ONLY", "reduce_only": False},
            )
        self.assertIsNotNone(self.manager.slots[OrderSide.BUY])
        self.assertIsNotNone(self.manager.slots[OrderSide.SELL])

    async def test_first_create_prefers_inventory_reducing_side(self) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("1"),
            min_profit_buffer_bps=Decimal("0"),
            max_mutations_per_minute=1,
            dry_run=False,
        )
        cases = (
            (Decimal("0.5"), OrderSide.SELL),
            (Decimal("-0.5"), OrderSide.BUY),
            (Decimal("0"), OrderSide.BUY),
        )

        for inventory_ratio, expected_side in cases:
            with self.subTest(inventory_ratio=inventory_ratio):
                self.adapter.create_order.reset_mock()
                manager = self.make_manager(config)
                risk = replace(
                    self.risk(), inventory_ratio=inventory_ratio
                )

                result = await manager.reconcile(self.desired(), risk)

                self.assertEqual(result.errors, ())
                self.adapter.create_order.assert_awaited_once()
                call = self.adapter.create_order.await_args
                self.assertEqual(call.args[1], expected_side)
                self.assertEqual(
                    call.kwargs["params"],
                    {"time_in_force": "POST_ONLY", "reduce_only": False},
                )
                self.assertIsNotNone(manager.slots[expected_side])
                opposite_side = (
                    OrderSide.SELL
                    if expected_side is OrderSide.BUY
                    else OrderSide.BUY
                )
                self.assertIsNone(manager.slots[opposite_side])

    async def test_hard_risk_order_is_reduce_only(self) -> None:
        desired = self.desired(
            bid_price=None, ask_reduce_only=True
        )
        risk = self.risk(
            buy_amount=None,
            sell_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )

        await self.manager.reconcile(desired, risk)

        self.adapter.create_order.assert_awaited_once()
        self.assertEqual(
            self.adapter.create_order.await_args.kwargs["params"],
            {"time_in_force": "POST_ONLY", "reduce_only": True},
        )

    async def test_lighter_residual_risk_to_post_only_submission(self) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.00020"),
            max_position=Decimal("0.00040"),
            maker_fee_rate=Decimal("0"),
            min_profit_buffer_bps=Decimal("0"),
            min_completed_net_turnover_bps=Decimal("0"),
            dry_run=False,
        )
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=5,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("10"),
        )
        market = MarketSnapshot(
            symbol="BTC",
            bids=(OrderBookLevel(Decimal("77999.9"), Decimal("1")),),
            asks=(OrderBookLevel(Decimal("78000.1"), Decimal("1")),),
            best_bid=Decimal("77999.9"),
            best_ask=Decimal("78000.1"),
            exchange_timestamp=None,
            received_monotonic=self.clock(),
        )

        for size, reducing_side in (
            (Decimal("0.00009"), OrderSide.SELL),
            (Decimal("-0.00009"), OrderSide.BUY),
        ):
            with self.subTest(size=size):
                self.adapter.create_order.reset_mock()
                position = PositionSnapshot(
                    symbol="BTC",
                    signed_size=size,
                    entry_price=Decimal("78000"),
                    unrealized_pnl=Decimal("0"),
                    received_monotonic=self.clock(),
                )
                risk = RiskManager(config).evaluate(
                    position,
                    (),
                    self.metadata,
                    now_monotonic=self.clock(),
                )
                quotes = MarketMakerStrategy(config).calculate_quotes(
                    market,
                    position,
                    self.metadata,
                    risk,
                    now_monotonic=self.clock(),
                )
                manager = self.make_manager(config)

                result = await manager.reconcile(quotes, risk)

                desired = (
                    quotes.ask
                    if reducing_side is OrderSide.SELL
                    else quotes.bid
                )
                opposite = (
                    quotes.bid
                    if reducing_side is OrderSide.SELL
                    else quotes.ask
                )
                self.assertIsNone(opposite)
                self.assertIsNotNone(desired)
                self.assertEqual(desired.amount, Decimal("0.00020"))
                self.assertTrue(desired.reduce_only)
                self.assertGreater(desired.amount, abs(size))
                self.assertEqual(risk.worst_long, size)
                self.assertEqual(risk.worst_short, size)
                self.assertEqual(result.errors, ())
                self.adapter.create_order.assert_awaited_once()
                call = self.adapter.create_order.await_args
                self.assertEqual(call.args[1], reducing_side)
                self.assertEqual(call.args[2], OrderType.LIMIT)
                self.assertEqual(call.args[3], Decimal("0.00020"))
                self.assertEqual(
                    call.kwargs["params"],
                    {"time_in_force": "POST_ONLY", "reduce_only": True},
                )

    async def test_reduce_only_below_base_minimum_fails_closed(
        self,
    ) -> None:
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=2,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            min_base_amount=Decimal("0.10"),
            min_quote_amount=Decimal("0"),
        )
        manager = self.make_manager()
        desired = self.desired(
            bid_price=None,
            ask_amount="0.05",
            ask_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )
        risk = self.risk(
            buy_amount=None,
            sell_amount="0.05",
            sell_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )

        result = await manager.reconcile(desired, risk)

        self.adapter.create_order.assert_not_awaited()
        self.assertEqual(
            result.errors,
            ("sell desired price/amount is invalid",),
        )

    async def test_reduce_only_at_base_minimum_is_submitted_post_only(
        self,
    ) -> None:
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=5,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("0"),
        )
        manager = self.make_manager()
        desired = self.desired(
            bid_price=None,
            ask_amount="0.00020",
            ask_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )
        risk = self.risk(
            buy_amount=None,
            sell_amount="0.00020",
            sell_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )

        result = await manager.reconcile(desired, risk)

        self.assertEqual(result.errors, ())
        self.adapter.create_order.assert_awaited_once()
        call = self.adapter.create_order.await_args
        self.assertEqual(call.args[3], Decimal("0.00020"))
        self.assertEqual(
            call.kwargs["params"],
            {"time_in_force": "POST_ONLY", "reduce_only": True},
        )

    async def test_non_reduce_only_dust_still_obeys_base_minimum(self) -> None:
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=2,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            min_base_amount=Decimal("0.10"),
            min_quote_amount=Decimal("0"),
        )
        manager = self.make_manager()

        result = await manager.reconcile(
            self.desired(bid_price=None, ask_amount="0.05"),
            self.risk(buy_amount=None, sell_amount="0.05"),
        )

        self.adapter.create_order.assert_not_awaited()
        self.assertEqual(result.errors, ("sell desired price/amount is invalid",))

    async def test_reduce_only_still_obeys_quote_minimum(self) -> None:
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("25"),
        )
        normal_manager = self.make_manager()
        normal_result = await normal_manager.reconcile(
            self.desired(bid_price=None),
            self.risk(buy_amount=None),
        )

        self.adapter.create_order.assert_not_awaited()
        self.assertEqual(
            normal_result.errors,
            ("sell desired price/amount is invalid",),
        )

        reduce_manager = self.make_manager()
        reduce_result = await reduce_manager.reconcile(
            self.desired(
                bid_price=None,
                ask_reduce_only=True,
                state=RuntimeState.RISK_REDUCTION,
            ),
            self.risk(
                buy_amount=None,
                sell_reduce_only=True,
                state=RuntimeState.RISK_REDUCTION,
            ),
        )

        self.adapter.create_order.assert_not_awaited()
        self.assertEqual(
            reduce_result.errors,
            ("sell desired price/amount is invalid",),
        )

    def test_reduce_only_keeps_all_non_minimum_validation(self) -> None:
        desired = DesiredOrder(
            OrderSide.SELL,
            Decimal("100.1"),
            Decimal("0.1"),
            True,
            "dust reduction",
        )
        validation_risk = self.risk(
            buy_amount=None,
            sell_amount="0.2",
            sell_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )
        cases = (
            replace(desired, amount=Decimal("0")),
            replace(desired, amount=Decimal("-0.1")),
            replace(desired, amount=Decimal("NaN")),
            replace(desired, amount=Decimal("Infinity")),
            replace(desired, amount=Decimal("0.15")),
            replace(desired, price=Decimal("0")),
            replace(desired, price=Decimal("NaN")),
            replace(desired, price=Decimal("Infinity")),
            replace(desired, price=Decimal("100.05")),
        )

        for invalid in cases:
            with self.subTest(order=invalid):
                self.assertIsNotNone(
                    self.manager._validate_desired(invalid, validation_risk)
                )

        for invalid, risk in (
            (
                replace(desired, amount=Decimal("0.3")),
                validation_risk,
            ),
            (
                desired,
                self.risk(
                    buy_amount=None,
                    sell_amount="0.1",
                    sell_reduce_only=False,
                    state=RuntimeState.RISK_REDUCTION,
                ),
            ),
            (replace(desired, reduce_only=1), validation_risk),
            (
                desired,
                self.risk(
                    buy_amount=None,
                    sell_amount=None,
                    sell_reduce_only=True,
                    state=RuntimeState.RISK_REDUCTION,
                ),
            ),
        ):
            with self.subTest(order=invalid, risk=risk):
                self.assertIsNotNone(
                    self.manager._validate_desired(invalid, risk)
                )

    async def test_unchanged_reduce_only_bid_is_not_safety_canceled(
        self,
    ) -> None:
        desired = self.desired(
            ask_price=None,
            bid_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )
        risk = self.risk(
            sell_amount=None,
            buy_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )

        await self.manager.reconcile(desired, risk)
        await self.manager.reconcile(desired, risk)

        self.adapter.create_order.assert_awaited_once()
        self.adapter.cancel_order.assert_not_awaited()
        self.assertTrue(
            self.manager.slots[OrderSide.BUY].reduce_only
        )

    async def test_below_reprice_threshold_keeps_live_order(self) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("1"),
            min_profit_buffer_bps=Decimal("0"),
            reprice_threshold_ticks=2,
            dry_run=False,
        )
        manager = self.make_manager(config)
        await manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2

        await manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(self.adapter.create_order.await_count, 1)

    async def test_more_aggressive_reduce_only_reprice_bypasses_threshold_after_lifetime(
        self,
    ) -> None:
        config = MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.2"),
            max_position=Decimal("1"),
            min_profit_buffer_bps=Decimal("0"),
            reprice_threshold_ticks=125,
            min_order_lifetime_ms=30_000,
            dry_run=False,
        )
        cases = (
            (OrderSide.BUY, "99.9", "100.0", "99.8"),
            (OrderSide.SELL, "100.1", "100.0", "100.2"),
        )
        for side, initial, more_aggressive, less_aggressive in cases:
            with self.subTest(side=side):
                self.clock.value = 100.0
                self.created = 0
                self.adapter.create_order.reset_mock()
                self.adapter.cancel_order.reset_mock()

                async def cancel(order_id, _symbol, *, cancel_side=side):
                    return exchange_order(
                        str(order_id),
                        cancel_side,
                        status=OrderStatus.CANCELED,
                        params={"cancel_terminal": True},
                    )

                self.adapter.cancel_order.side_effect = cancel
                manager = self.make_manager(config)
                risk = self.risk(
                    buy_amount="0.2" if side is OrderSide.BUY else None,
                    sell_amount="0.2" if side is OrderSide.SELL else None,
                    buy_reduce_only=side is OrderSide.BUY,
                    sell_reduce_only=side is OrderSide.SELL,
                    state=RuntimeState.RISK_REDUCTION,
                )

                def quotes(price: str) -> DesiredQuotes:
                    return self.desired(
                        bid_price=price if side is OrderSide.BUY else None,
                        ask_price=price if side is OrderSide.SELL else None,
                        bid_reduce_only=side is OrderSide.BUY,
                        ask_reduce_only=side is OrderSide.SELL,
                        state=RuntimeState.RISK_REDUCTION,
                    )

                await manager.reconcile(quotes(initial), risk)
                await manager.reconcile(quotes(more_aggressive), risk)
                self.adapter.cancel_order.assert_not_awaited()

                self.clock.value += 30
                await manager.reconcile(quotes(less_aggressive), risk)
                self.adapter.cancel_order.assert_not_awaited()

                result = await manager.reconcile(
                    quotes(more_aggressive), risk
                )

                self.adapter.cancel_order.assert_awaited_once()
                self.assertEqual(self.adapter.create_order.await_count, 1)
                self.assertTrue(result.position_refresh_required)

    async def test_reprice_defers_replacement_until_after_position_refresh(self) -> None:
        sequence = []

        async def create(*args, **kwargs):
            sequence.append("create")
            return await self._create_order(*args, **kwargs)

        async def cancel(*args, **kwargs):
            sequence.append("cancel")
            return await self._cancel_order(*args, **kwargs)

        self.adapter.create_order.side_effect = create
        self.adapter.cancel_order.side_effect = cancel
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        sequence.clear()

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertEqual(sequence, ["cancel"])
        self.assertTrue(result.position_refresh_required)
        self.assertFalse(result.fill_observed)

        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )
        self.assertEqual(sequence, ["cancel", "create"])

    async def test_cancel_with_visible_fill_never_replaces_in_same_cycle(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.CANCELED,
            remaining="0.1",
            params={"cancel_terminal": True},
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

    async def test_non_terminal_cancel_never_places_replacement(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        self.adapter.cancel_order.side_effect = None

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertEqual(self.adapter.create_order.await_count, 1)
        slot = self.manager.slots[OrderSide.BUY]
        self.assertEqual(slot.state, OrderSlotState.UNCERTAIN_CANCELLATION)
        self.assertTrue(slot.cancellation_uncertain)
        self.assertEqual(self.manager.unresolved_cancellation_count, 1)
        self.assertEqual(self.manager.resolved_ambiguous_cancellations, 0)
        self.adapter.cancel_order.assert_awaited_once()
        self.assertIsNone(
            next(
                action for action in result.actions if action.operation == "cancel"
            ).success
        )

    async def test_exact_fill_resolves_cancellation_ambiguity_once(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        self.adapter.cancel_order.side_effect = None
        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        terminal = exchange_order(
            "1", OrderSide.BUY, status=OrderStatus.FILLED, remaining="0"
        )
        self.assertTrue(await self.manager.handle_order_update(terminal))
        self.assertEqual(self.manager.unresolved_cancellation_count, 0)
        self.assertEqual(self.manager.resolved_ambiguous_cancellations, 1)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

        self.assertFalse(await self.manager.handle_order_update(terminal))
        self.assertEqual(self.manager.resolved_ambiguous_cancellations, 1)
        self.adapter.cancel_order.assert_awaited_once()

    async def test_cancel_race_uses_exact_fill_without_counting_cancel_success(
        self,
    ) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        exact_fill = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.FILLED,
            remaining="0",
        )
        self.adapter.get_terminal_cancellation_outcome.return_value = exact_fill
        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "1")]

        def confirm(order):
            self.adapter.get_unresolved_cancellations.return_value = []
            return order is exact_fill

        self.adapter.confirm_terminal_cancellation_outcome.side_effect = confirm

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        cancel_action = next(
            action for action in result.actions if action.operation == "cancel"
        )
        self.assertFalse(cancel_action.success)
        self.assertEqual(result.errors, ())
        self.assertTrue(result.fill_observed)
        self.assertTrue(result.position_refresh_required)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertEqual(self.manager.unresolved_cancellation_count, 0)
        self.assertEqual(self.manager.resolved_ambiguous_cancellations, 0)
        self.adapter.cancel_order.assert_awaited_once()
        self.adapter.confirm_terminal_cancellation_outcome.assert_called_once_with(
            exact_fill
        )
        self.assertFalse(await self.manager.handle_order_update(exact_fill))
        self.assertEqual(self.manager.resolved_ambiguous_cancellations, 0)

    async def test_opposite_side_cached_cancel_fill_remains_uncertain(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        invalid_fill = exchange_order(
            "1",
            OrderSide.SELL,
            status=OrderStatus.FILLED,
            remaining="0",
        )
        self.adapter.get_terminal_cancellation_outcome.return_value = invalid_fill
        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "1")]

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertTrue(result.errors)
        self.assertFalse(result.fill_observed)
        self.assertFalse(result.position_refresh_required)
        self.assertEqual(self.manager.unresolved_cancellation_count, 1)
        self.assertIsNotNone(self.manager.slots[OrderSide.BUY])
        self.adapter.confirm_terminal_cancellation_outcome.assert_not_called()
        self.adapter.cancel_order.assert_awaited_once()

    async def test_unowned_cached_cancel_fill_remains_uncertain(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        exact_fill = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.FILLED,
            remaining="0",
        )
        self.adapter.get_terminal_cancellation_outcome.return_value = exact_fill
        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "1")]
        self.adapter.confirm_terminal_cancellation_outcome.return_value = False

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertTrue(result.errors)
        self.assertTrue(result.fill_observed)
        self.assertTrue(result.position_refresh_required)
        self.assertEqual(self.manager.unresolved_cancellation_count, 1)
        self.assertIsNotNone(self.manager.slots[OrderSide.BUY])
        self.adapter.cancel_order.assert_awaited_once()

    async def test_shutdown_preserves_exact_cancel_fill_effect(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        exact_fill = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.FILLED,
            remaining="0",
        )
        self.adapter.get_terminal_cancellation_outcome.return_value = exact_fill
        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "1")]

        def confirm(order):
            self.adapter.get_unresolved_cancellations.return_value = []
            return order is exact_fill

        self.adapter.confirm_terminal_cancellation_outcome.side_effect = confirm

        await self.manager.shutdown()

        self.assertIs(self.manager.runtime_state, RuntimeState.STOPPED)
        self.assertTrue(self.manager.last_result.fill_observed)
        self.assertTrue(self.manager.last_result.position_refresh_required)
        self.assertFalse(self.manager.last_result.actions[0].success)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertEqual(self.manager.unresolved_cancellation_count, 0)
        self.assertEqual(self.manager.resolved_ambiguous_cancellations, 0)

    async def test_nonterminal_update_preserves_cancellation_uncertainty(
        self,
    ) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        partial = exchange_order(
            "1", OrderSide.BUY, status=OrderStatus.OPEN, remaining="0.1"
        )
        self.assertTrue(await self.manager.handle_order_update(partial))

        slot = self.manager.slots[OrderSide.BUY]
        self.assertEqual(slot.state, OrderSlotState.UNCERTAIN_CANCELLATION)
        self.assertTrue(slot.cancellation_uncertain)
        self.assertEqual(self.manager.unresolved_cancellation_count, 1)
        self.assertTrue(self.manager.has_uncertain_state)
        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )
        self.adapter.cancel_order.assert_awaited_once()
        self.assertEqual(self.adapter.create_order.await_count, 1)

    async def test_adapter_and_slot_cancellation_keys_are_deduplicated(
        self,
    ) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "1")]
        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertEqual(self.manager.unresolved_cancellation_count, 1)
        self.assertTrue(self.manager.has_uncertain_state)

        def confirm(order):
            self.adapter.get_unresolved_cancellations.return_value = []
            return order.status in {OrderStatus.CANCELED, OrderStatus.FILLED}

        self.adapter.confirm_terminal_cancellation_outcome.side_effect = confirm
        terminal = exchange_order(
            "1", OrderSide.BUY, status=OrderStatus.CANCELED
        )
        self.assertFalse(await self.manager.handle_order_update(terminal))
        self.assertEqual(self.manager.unresolved_cancellation_count, 0)
        self.assertFalse(self.manager.has_uncertain_state)
        self.adapter.confirm_terminal_cancellation_outcome.assert_called_once_with(
            terminal
        )

    async def test_nonterminal_cancel_placeholder_is_not_fill_proof(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = OrderData(
            id="1",
            client_id=None,
            symbol="BTC",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            amount=Decimal("0"),
            price=None,
            filled=Decimal("0"),
            remaining=Decimal("0"),
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.PENDING,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params={"cancel_terminal": False},
            raw_data={"cancel_terminal": False},
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertFalse(result.position_refresh_required)
        self.assertFalse(result.fill_observed)
        self.assertEqual(self.manager.unresolved_cancellation_count, 1)

    async def test_non_terminal_cancel_reports_visible_partial_fill(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            remaining="0.1",
            params={"cancel_terminal": False},
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.assertEqual(self.adapter.create_order.await_count, 1)
        slot = self.manager.slots[OrderSide.BUY]
        self.assertEqual(slot.state, OrderSlotState.UNCERTAIN_CANCELLATION)
        self.assertTrue(slot.cancellation_uncertain)

    async def test_wrong_cancel_ack_never_places_replacement(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "other",
            OrderSide.BUY,
            status=OrderStatus.CANCELED,
            params={"cancel_terminal": True},
        )

        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        )

    async def test_minimum_lifetime_blocks_normal_reprice(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.adapter.cancel_order.assert_not_awaited()

    async def test_disabled_exit_side_bypasses_minimum_lifetime(self) -> None:
        await self.manager.reconcile(self.desired(), self.risk())
        await self.manager.reconcile(self.desired(), self.risk())

        result = await self.manager.reconcile(
            self.desired(ask_price=None), self.risk()
        )

        self.adapter.cancel_order.assert_awaited_once_with("2", "BTC")
        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertIsNone(self.manager.slots[OrderSide.SELL])
        self.assertTrue(result.position_refresh_required)

    async def test_partial_and_terminal_updates_change_slot(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.handle_order_update(
            exchange_order(
                "1", OrderSide.BUY, price="99.9", remaining="0.1"
            )
        )
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.PARTIALLY_FILLED,
        )
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].remaining, Decimal("0.1")
        )

        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.FILLED,
                price="99.9",
                remaining="0",
            )
        )
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

    async def test_post_only_cancellation_does_not_mutate_in_callback(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                price="99.9",
            )
        )
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )
        self.assertEqual(self.adapter.create_order.await_count, 2)

    async def test_nonterminal_ws_replay_after_terminal_is_ignored(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.cancel_managed_orders("test terminal proof")
        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.assertFalse(
            await self.manager.handle_order_update(
                exchange_order("1", OrderSide.BUY, price="99.9")
            )
        )
        self.assertTrue(
            await self.manager.handle_order_update(
                exchange_order(
                    "1",
                    OrderSide.BUY,
                    price="99.9",
                    remaining="0.1",
                    client_id="client-1",
                )
            )
        )
        self.assertFalse(
            await self.manager.handle_order_update(
                exchange_order(
                    "1",
                    OrderSide.BUY,
                    price="99.9",
                    remaining="0.1",
                    client_id="client-1",
                )
            )
        )

        self.assertEqual(self.manager.slots[OrderSide.BUY].order_id, "2")
        self.assertIsNone(self.manager.pause_reason)

    async def test_terminal_ws_replay_with_wrong_side_still_pauses(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                price="99.9",
            )
        )

        await self.manager.handle_order_update(
            exchange_order("1", OrderSide.SELL, price="100.1")
        )

        self.assertIn("unknown open order update", self.manager.pause_reason)

    async def test_terminal_proof_keeps_partial_fill_low_watermark(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        partial = exchange_order(
            "1",
            OrderSide.BUY,
            price="99.9",
            remaining="0.1",
            client_id="client-1",
        )
        self.assertTrue(await self.manager.handle_order_update(partial))

        await self.manager.cancel_managed_orders("test stale terminal remaining")

        self.assertFalse(await self.manager.handle_order_update(partial))
        self.assertIsNone(self.manager.pause_reason)

    async def test_terminal_replay_cross_namespace_collision_pauses(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.cancel_managed_orders("test terminal proof")

        await self.manager.handle_order_update(
            exchange_order(
                "client-1",
                OrderSide.BUY,
                price="99.9",
                client_id="foreign-client",
            )
        )

        self.assertIn("unknown open order update", self.manager.pause_reason)

    async def test_unknown_ws_order_update_still_pauses(self) -> None:
        await self.manager.handle_order_update(
            exchange_order("foreign", OrderSide.SELL)
        )

        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.assertIn("unknown open order update", self.manager.pause_reason)

    async def test_direct_post_only_terminal_stops_cycle_and_arms_cooldown(
        self,
    ) -> None:
        attempts = 0

        async def create(
            _symbol, side, _order_type, amount, price, params
        ):
            del params
            nonlocal attempts
            attempts += 1
            return exchange_order(
                str(attempts),
                side,
                status=(
                    OrderStatus.CANCELED
                    if attempts == 1
                    else OrderStatus.OPEN
                ),
                price=str(price),
                amount=str(amount),
                raw_data=(
                    {"post_only_canceled": True}
                    if attempts == 1
                    else {}
                ),
            )

        self.adapter.create_order.side_effect = create

        result = await self.manager.reconcile(self.desired(), self.risk())

        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.actions[0].reason, "post-only canceled")
        self.assertFalse(any(self.manager.slots.values()))
        count, generation = self.manager.consume_post_only_cancellations()
        self.assertEqual(count, 1)
        self.assertTrue(self.manager.post_only_book_refresh_required)

        self.manager.acknowledge_post_only_book_refresh(generation)
        await self.manager.reconcile(self.desired(), self.risk())
        self.assertEqual(self.adapter.create_order.await_count, 1)

        self.clock.value += self.config.refresh_interval_ms / 1000
        await self.manager.reconcile(self.desired(), self.risk())
        self.assertEqual(self.adapter.create_order.await_count, 2)
        await self.manager.reconcile(self.desired(), self.risk())
        self.assertEqual(self.adapter.create_order.await_count, 3)

    async def test_immediate_full_fill_keeps_confirmed_order_id(self) -> None:
        self.adapter.create_order.side_effect = lambda *args, **kwargs: exchange_order(
            "fast-fill",
            args[1],
            status=OrderStatus.FILLED,
            price=str(args[4]),
            amount=str(args[3]),
            remaining="0",
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.assertEqual(result.errors, ())
        self.assertTrue(result.actions[0].success)
        self.assertIn("fast-fill", self.manager.known_order_ids)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

    async def test_duplicate_partial_order_update_reports_fill_once(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        partial = exchange_order(
            "1", OrderSide.BUY, price="99.9", remaining="0.1"
        )

        self.assertTrue(await self.manager.handle_order_update(partial))
        self.assertFalse(await self.manager.handle_order_update(partial))

    async def test_immediate_create_fill_ws_replay_is_not_new_fill(self) -> None:
        filled = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.FILLED,
            price="99.9",
            remaining="0",
        )
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = filled

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertTrue(result.fill_observed)
        self.assertFalse(await self.manager.handle_order_update(filled))

    async def test_partial_reduce_only_remaining_keeps_existing_order(self) -> None:
        await self.manager.reconcile(
            self.desired(
                bid_price=None,
                ask_price="100.1",
                ask_amount="0.4",
                ask_reduce_only=True,
                state=RuntimeState.RISK_REDUCTION,
            ),
            self.risk(
                buy_amount=None,
                sell_amount="0.4",
                sell_reduce_only=True,
                state=RuntimeState.RISK_REDUCTION,
            ),
        )
        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.SELL,
                price="100.1",
                amount="0.4",
                remaining="0.2",
            )
        )

        await self.manager.reconcile(
            self.desired(
                bid_price=None,
                ask_price="100.1",
                ask_amount="0.2",
                ask_reduce_only=True,
                state=RuntimeState.RISK_REDUCTION,
            ),
            self.risk(
                buy_amount=None,
                sell_amount="0.2",
                sell_reduce_only=True,
                state=RuntimeState.RISK_REDUCTION,
            ),
        )

        self.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.assertEqual(
            self.manager.slots[OrderSide.SELL].remaining,
            Decimal("0.2"),
        )

    async def test_matched_post_only_terminal_blocks_create_but_allows_cancel(
        self,
    ) -> None:
        await self.manager.reconcile(self.desired(), self.risk())
        await self.manager.reconcile(self.desired(), self.risk())
        await self.manager.handle_order_update(
            exchange_order(
                "foreign",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                raw_data={"post_only_canceled": True},
            )
        )
        self.assertEqual(
            self.manager.consume_post_only_cancellations()[0], 0
        )

        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                price="99.9",
                raw_data={"post_only_canceled": True},
            )
        )
        result = await self.manager.reconcile(
            self.desired(ask_price=None), self.risk()
        )

        self.assertEqual(
            self.manager.consume_post_only_cancellations()[0], 1
        )
        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.adapter.cancel_order.assert_awaited_once_with("2", "BTC")
        self.assertEqual(result.errors, ())

    async def test_uncertain_submission_is_not_duplicated(self) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )

        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.adapter.create_order.assert_awaited_once()
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )
        self.assertNotIn("1", self.manager.known_order_ids)
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_create_confirmation_requires_an_exact_identifier(self) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = replace(
            exchange_order("ignored", OrderSide.BUY, price="99.9"),
            id=None,
            client_id=None,
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertIsNone(result.actions[0].success)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_create_rate_limit_is_definitive_and_stops_the_cycle(self) -> None:
        self.adapter.create_order.side_effect = RuntimeError(
            "HTTP 429 response containing test-secret"
        )

        result = await self.manager.reconcile(self.desired(), self.risk())

        self.adapter.create_order.assert_awaited_once()
        self.assertEqual(result.actions[0].success, False)
        self.assertEqual(result.errors, ("create rejected: http_429",))
        self.assertNotIn("test-secret", str(result))
        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertIsNone(self.manager.slots[OrderSide.SELL])
        self.assertFalse(self.manager.has_uncertain_state)

    async def test_invalid_create_values_are_not_reported_as_success(self) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = replace(
            exchange_order("1", OrderSide.BUY, price="99.9"),
            remaining=Decimal("0.3"),
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertIsNone(result.actions[0].success)
        self.assertTrue(
            any("invalid confirmation" in error for error in result.errors)
        )
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )

    async def test_partial_create_confirmation_requires_position_refresh(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            price="99.9",
            amount="0.2",
            remaining="0.1",
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.PARTIALLY_FILLED,
        )

    async def test_filled_create_confirmation_requires_position_refresh(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.FILLED,
            price="99.9",
            remaining="0",
        )

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

    async def test_cancel_rate_limit_restores_live_state(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2
        self.adapter.cancel_order.side_effect = RuntimeError("HTTP 429 test-secret")

        result = await self.manager.reconcile(
            self.desired(bid_price="99.0", ask_price=None), self.risk()
        )

        self.assertEqual(result.actions[0].operation, "cancel")
        self.assertEqual(result.actions[0].success, False)
        self.assertEqual(result.errors, ("cancel rejected: http_429",))
        self.assertNotIn("test-secret", str(result))
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state, OrderSlotState.LIVE
        )
        self.assertFalse(self.manager.has_uncertain_state)

    async def test_pause_state_survives_rate_limited_safety_cancel(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.cancel_order.side_effect = RuntimeError("HTTP 429")

        result = await self.manager.reconcile(
            self.desired(
                bid_price=None,
                ask_price=None,
                state=RuntimeState.PAUSED_DATA,
            ),
            self.risk(),
        )

        self.assertEqual(result.runtime_state, RuntimeState.PAUSED_DATA)
        self.assertEqual(result.actions[0].success, False)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state, OrderSlotState.LIVE
        )

    async def test_market_quality_pause_cancels_live_quotes(self) -> None:
        await self.manager.reconcile(self.desired(), self.risk())
        await self.manager.reconcile(self.desired(), self.risk())

        result = await self.manager.reconcile(
            self.desired(
                bid_price=None,
                ask_price=None,
                state=RuntimeState.PAUSED_MARKET,
            ),
            self.risk(),
        )

        self.assertEqual(result.runtime_state, RuntimeState.PAUSED_MARKET)
        self.assertEqual(self.adapter.cancel_order.await_count, 2)
        self.assertFalse(any(self.manager.slots.values()))

    async def test_cancel_task_cancellation_marks_outcome_uncertain(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        entered = asyncio.Event()

        async def cancel_order(_order_id, _symbol):
            entered.set()
            await asyncio.Event().wait()

        self.adapter.cancel_order.side_effect = cancel_order
        task = asyncio.create_task(self.manager.cancel_managed_orders("test"))
        await entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        )
        self.assertTrue(self.manager.has_uncertain_state)

    async def test_uncertain_submission_requires_exact_identifier_match(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = None
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.resolve_unresolved_submissions.return_value = [
            exchange_order(
                "other",
                OrderSide.BUY,
                price="99.9",
                amount="0.2",
                client_id="other-client",
            )
        ]

        await self.manager.resolve_unresolved_submissions()

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )

    async def test_uncertain_submission_adopts_exact_open_and_unpauses(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.resolve_unresolved_submissions.return_value = [
            exchange_order(
                "late-order",
                OrderSide.BUY,
                price="99.9",
                client_id="client-1",
            )
        ]

        await self.manager.resolve_unresolved_submissions()

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.LIVE,
        )
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].order_id, "late-order"
        )
        self.assertEqual(self.manager.runtime_state, RuntimeState.SYNCING)
        self.assertIsNone(self.manager.pause_reason)
        self.assertFalse(self.manager.has_uncertain_state)
        self.adapter.create_order.assert_awaited_once()

    async def test_create_not_sent_is_definitive_without_reconcile_failure(
        self,
    ) -> None:
        self.adapter.supports_definitive_pre_send_failure = True
        self.adapter.create_order.side_effect = OrderSubmissionNotSentError(
            "limit order was not submitted: DNS resolution failed"
        )

        result = await self.manager.reconcile(self.desired(), self.risk())

        self.adapter.create_order.assert_awaited_once()
        self.assertEqual(result.actions[0].success, False)
        self.assertEqual(result.errors, ())
        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertIsNone(self.manager.slots[OrderSide.SELL])
        self.assertFalse(self.manager.has_uncertain_state)
        self.assertTrue(
            self.adapter.create_order.await_args.kwargs["params"][
                "_raise_on_definitive_pre_send_failure"
            ]
        )

    async def test_dns_failure_during_cancel_stays_uncertain(self) -> None:
        slot = ManagedOrder(
            side=OrderSide.BUY,
            state=OrderSlotState.LIVE,
            order_id="1",
            client_id="client-1",
            price=Decimal("99.9"),
            amount=Decimal("0.2"),
            remaining=Decimal("0.2"),
            reduce_only=False,
            created_monotonic=self.clock(),
            updated_monotonic=self.clock(),
        )
        self.manager._slots[OrderSide.BUY] = slot
        connection = SimpleNamespace(
            host="api.rh.lighter.xyz",
            port=443,
            ssl=True,
        )
        self.adapter.cancel_order.side_effect = ClientConnectorDNSError(
            connection,
            OSError("Timeout while contacting DNS servers"),
        )

        result = await self.manager.cancel_managed_orders("test")

        self.assertTrue(result.errors)
        self.assertTrue(slot.cancellation_uncertain)
        self.assertEqual(slot.state, OrderSlotState.UNCERTAIN_CANCELLATION)
        self.assertTrue(self.manager.has_uncertain_state)

    async def test_adapter_unresolved_submission_blocks_create_without_slot(
        self,
    ) -> None:
        self.adapter.get_unresolved_submissions.return_value = [
            {"client_order_id": "client-1"}
        ]

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.adapter.resolve_unresolved_submissions.assert_awaited_once()
        self.adapter.create_order.assert_not_awaited()
        self.assertTrue(self.manager.has_uncertain_state)
        self.assertEqual(result.runtime_state, RuntimeState.PAUSED_ORDER_STATE)

    async def test_resolved_filled_submission_refreshes_before_next_create(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.resolve_unresolved_submissions.return_value = [
            exchange_order(
                "late-order",
                OrderSide.BUY,
                status=OrderStatus.FILLED,
                price="99.9",
                remaining="0",
                client_id="client-1",
            )
        ]

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.adapter.create_order.assert_awaited_once()
        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.assertFalse(self.manager.has_uncertain_state)
        self.assertEqual(self.manager.runtime_state, RuntimeState.SYNCING)

        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.assertEqual(self.adapter.create_order.await_count, 2)

    async def test_resolved_submission_does_not_mask_uncertain_cancel(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.resolve_unresolved_submissions.return_value = [
            exchange_order(
                "late-order",
                OrderSide.BUY,
                price="99.9",
                remaining="0.1",
                client_id="client-1",
            )
        ]
        await self.manager.resolve_unresolved_submissions()
        self.assertFalse(self.manager.has_uncertain_state)

        self.clock.value += 2
        self.adapter.resolve_unresolved_submissions.return_value = []
        self.adapter.cancel_order.side_effect = TimeoutError("response lost")
        await self.manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )
        self.assertTrue(self.manager.has_uncertain_state)
        self.assertIn(
            "cancellation outcome is uncertain", self.manager.pause_reason
        )

        self.adapter.get_open_orders.return_value = []
        self.adapter.get_order_history.return_value = [
            exchange_order(
                "late-order",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                price="99.9",
                remaining="0.1",
                client_id="client-1",
            )
        ]
        refresh_required = await self.manager.sync_open_orders()

        self.adapter.create_order.assert_awaited_once()
        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertFalse(refresh_required)
        self.assertFalse(self.manager.has_uncertain_state)
        self.assertEqual(self.manager.runtime_state, RuntimeState.SYNCING)

    async def test_uncertain_submission_rejects_wrong_symbol(self) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.resolve_unresolved_submissions.return_value = [
            replace(
                exchange_order(
                    "late-order",
                    OrderSide.BUY,
                    price="99.9",
                    client_id="client-1",
                ),
                symbol="ETH",
            )
        ]

        await self.manager.resolve_unresolved_submissions()

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )

    async def test_resolved_partial_submission_is_adopted_before_refresh(
        self,
    ) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.resolve_unresolved_submissions.return_value = [
            exchange_order(
                "late-order",
                OrderSide.BUY,
                price="99.9",
                amount="0.2",
                remaining="0.1",
                client_id="client-1",
            )
        ]

        result = await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        self.assertTrue(result.position_refresh_required)
        self.assertTrue(result.fill_observed)
        self.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.PARTIALLY_FILLED,
        )
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].order_id, "late-order"
        )
        self.assertFalse(self.manager.has_uncertain_state)
        self.assertEqual(self.manager.runtime_state, RuntimeState.SYNCING)

    async def test_uncertain_first_create_stops_second_side(self) -> None:
        self.adapter.create_order.side_effect = None
        self.adapter.create_order.return_value = exchange_order(
            "client-1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            price="99.9",
            client_id="client-1",
            params={"submission_uncertain": True},
        )

        await self.manager.reconcile(self.desired(), self.risk())

        self.adapter.create_order.assert_awaited_once()
        self.assertIsNone(self.manager.slots[OrderSide.SELL])
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_cancelled_create_task_marks_submission_uncertain(self) -> None:
        entered = asyncio.Event()
        blocker = asyncio.Event()

        async def create(*_args, **_kwargs):
            entered.set()
            await blocker.wait()

        self.adapter.create_order.side_effect = create
        task = asyncio.create_task(
            self.manager.reconcile(
                self.desired(bid_price="99.9", ask_price=None), self.risk()
            )
        )
        await entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_uncertain_cancel_stops_create_and_cancels_other_side(
        self,
    ) -> None:
        await self.manager.reconcile(self.desired(), self.risk())
        await self.manager.reconcile(self.desired(), self.risk())
        self.clock.value += 2

        async def cancel(order_id, symbol):
            if str(order_id) == "1":
                return exchange_order(
                    "1",
                    OrderSide.BUY,
                    status=OrderStatus.PENDING,
                    params={"cancel_terminal": False},
                )
            return await self._cancel_order(order_id, symbol)

        self.adapter.cancel_order.side_effect = cancel
        await self.manager.reconcile(
            self.desired(bid_price="99.8"), self.risk()
        )

        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertEqual(self.adapter.cancel_order.await_count, 2)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_CANCELLATION,
        )
        self.assertIsNone(self.manager.slots[OrderSide.SELL])

    async def test_unsafe_risk_cancels_even_if_desired_still_has_quotes(
        self,
    ) -> None:
        await self.manager.reconcile(self.desired(), self.risk())
        await self.manager.reconcile(self.desired(), self.risk())

        await self.manager.reconcile(
            self.desired(),
            self.risk(safe=False, state=RuntimeState.PAUSED_DATA),
        )

        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertEqual(self.adapter.cancel_order.await_count, 2)
        self.assertEqual(self.manager.runtime_state, RuntimeState.PAUSED_DATA)
        self.assertFalse(any(self.manager.slots.values()))

    async def test_unknown_status_on_managed_order_pauses(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.UNKNOWN,
                price="99.9",
            )
        )

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_non_finite_order_update_pauses_without_raising(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )

        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                price="99.9",
                remaining="NaN",
            )
        )

        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.UNCERTAIN_SUBMISSION,
        )
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_invalid_desired_side_fails_closed(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        invalid = self.desired(bid_price=None, ask_price=None)
        invalid = DesiredQuotes(
            bid=DesiredOrder(
                OrderSide.SELL,
                Decimal("100.1"),
                Decimal("0.2"),
                False,
                "wrong slot",
            ),
            ask=None,
            reference_price=invalid.reference_price,
            reservation_price=invalid.reservation_price,
            half_spread=invalid.half_spread,
            inventory_ratio=invalid.inventory_ratio,
            runtime_state=invalid.runtime_state,
            reason=invalid.reason,
        )

        await self.manager.reconcile(invalid, self.risk())

        self.assertEqual(self.adapter.create_order.await_count, 1)
        self.adapter.cancel_order.assert_awaited_once()
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_dry_run_returns_plan_with_zero_mutations(self) -> None:
        manager = self.make_manager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                min_profit_buffer_bps=Decimal("0"),
                dry_run=True,
            )
        )

        result = await manager.reconcile(self.desired(), self.risk())

        self.adapter.create_order.assert_not_awaited()
        self.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(
            [action.operation for action in result.actions],
            ["would_place", "would_place"],
        )

    async def test_budget_block_does_not_report_cancel_as_executed(self) -> None:
        manager = self.make_manager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                min_profit_buffer_bps=Decimal("0"),
                reprice_threshold_ticks=1,
                max_mutations_per_minute=1,
                dry_run=False,
            )
        )
        await manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.clock.value += 2

        result = await manager.reconcile(
            self.desired(bid_price="99.8", ask_price=None), self.risk()
        )

        self.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(
            [action.operation for action in result.actions], ["blocked"]
        )

    async def test_mutation_budget_blocks_create_but_not_safety_cancel(self) -> None:
        manager = self.make_manager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                min_profit_buffer_bps=Decimal("0"),
                max_mutations_per_minute=1,
                dry_run=False,
            )
        )
        result = await manager.reconcile(self.desired(), self.risk())
        self.assertEqual(self.adapter.create_order.await_count, 1)
        result = await manager.reconcile(self.desired(), self.risk())
        self.assertIn("blocked", {action.operation for action in result.actions})

        unsafe = self.risk(
            buy_amount=None,
            sell_amount=None,
            safe=False,
            state=RuntimeState.PAUSED_DATA,
        )
        await manager.reconcile(
            self.desired(
                bid_price=None,
                ask_price=None,
                state=RuntimeState.PAUSED_DATA,
            ),
            unsafe,
        )
        self.adapter.cancel_order.assert_awaited_once()

    async def test_budget_allows_one_emergency_risk_reducing_create(self) -> None:
        manager = self.make_manager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                min_profit_buffer_bps=Decimal("0"),
                max_mutations_per_minute=1,
                dry_run=False,
            )
        )
        await manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        desired = self.desired(
            bid_price=None,
            ask_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )
        risk = self.risk(
            buy_amount=None,
            sell_reduce_only=True,
            state=RuntimeState.RISK_REDUCTION,
        )

        await manager.reconcile(desired, risk)
        result = await manager.reconcile(desired, risk)

        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertTrue(
            self.adapter.create_order.await_args.kwargs["params"]["reduce_only"]
        )
        self.assertNotIn("blocked", {action.operation for action in result.actions})

        await manager.handle_order_update(
            exchange_order("2", OrderSide.SELL, status=OrderStatus.CANCELED)
        )
        result = await manager.reconcile(desired, risk)

        self.assertEqual(self.adapter.create_order.await_count, 2)
        self.assertIn("blocked", {action.operation for action in result.actions})

    async def test_shutdown_success_and_failure_are_explicit(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.shutdown()
        self.assertEqual(self.manager.runtime_state, RuntimeState.STOPPED)

        failing = self.make_manager()
        self.adapter.create_order.reset_mock()
        self.adapter.cancel_order.reset_mock()
        self.adapter.create_order.side_effect = self._create_order
        await failing.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "2",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        with self.assertRaises(RuntimeError):
            await failing.shutdown()

    async def test_shutdown_rejects_adapter_cancellation_marker_without_slot(
        self,
    ) -> None:
        self.adapter.get_unresolved_cancellations.return_value = [("BTC", "1")]

        with self.assertRaisesRegex(
            RuntimeError, "adapter cancellations remain unresolved"
        ):
            await self.manager.shutdown()

        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.assertFalse(any(self.manager.slots.values()))

    async def test_initialize_rejects_target_symbol_cancellation_marker(
        self,
    ) -> None:
        self.adapter.get_unresolved_cancellations.return_value = [
            ("ETH", "other"),
            ("BTC", "1"),
        ]

        with self.assertRaisesRegex(
            RuntimeError, "startup unresolved cancellations"
        ):
            await self.manager.initialize()

        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.adapter.get_open_orders.assert_not_awaited()

    async def test_shutdown_rejects_unknown_target_symbol_order(self) -> None:
        unknown = exchange_order("foreign-1", OrderSide.BUY)
        self.adapter.get_open_orders.side_effect = ([], [unknown])

        with self.assertRaisesRegex(RuntimeError, "open orders remain active"):
            await self.manager.shutdown()

        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.assertEqual(self.adapter.get_open_orders.await_count, 2)
        self.adapter.cancel_order.assert_not_awaited()
        self.adapter.cancel_all_orders.assert_not_awaited()

    async def test_shutdown_accepts_later_exact_terminal_proof(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        self.adapter.get_order_history.return_value = [
            exchange_order(
                "1", OrderSide.BUY, status=OrderStatus.CANCELED
            )
        ]

        await self.manager.shutdown()

        self.assertEqual(self.manager.runtime_state, RuntimeState.STOPPED)

    async def test_shutdown_rejects_wrong_symbol_terminal_history(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.cancel_order.side_effect = None
        self.adapter.cancel_order.return_value = exchange_order(
            "1",
            OrderSide.BUY,
            status=OrderStatus.PENDING,
            params={"cancel_terminal": False},
        )
        self.adapter.get_order_history.return_value = [
            replace(
                exchange_order(
                    "1", OrderSide.BUY, status=OrderStatus.CANCELED
                ),
                symbol="ETH",
            )
        ]

        with self.assertRaises(RuntimeError):
            await self.manager.shutdown()

        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_unknown_open_order_update_pauses(self) -> None:
        await self.manager.handle_order_update(
            exchange_order("other", OrderSide.BUY)
        )
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.assertIn("unknown", self.manager.pause_reason)

    async def test_cross_namespace_identifier_collision_is_unknown(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        foreign = replace(
            exchange_order("client-1", OrderSide.BUY, price="88.8"),
            client_id="other-client",
        )

        await self.manager.handle_order_update(foreign)

        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.assertIn("unknown", self.manager.pause_reason)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].order_id, "1"
        )
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].price, Decimal("99.9")
        )

    async def test_startup_open_order_policies(self) -> None:
        existing = exchange_order("existing", OrderSide.BUY)
        self.adapter.get_open_orders.return_value = [existing]
        with self.assertRaises(RuntimeError):
            await self.manager.initialize()
        self.adapter.cancel_all_orders.assert_not_awaited()

        cancel_manager = self.make_manager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                min_profit_buffer_bps=Decimal("0"),
                startup_open_order_policy="cancel_all",
                dry_run=False,
            )
        )
        self.adapter.get_open_orders.side_effect = [[existing], []]
        await cancel_manager.initialize()
        self.adapter.cancel_all_orders.assert_awaited_once_with("BTC")

        dry_manager = self.make_manager(
            MarketMakerConfig(
                symbol="BTC",
                order_size=Decimal("0.2"),
                max_position=Decimal("1"),
                min_profit_buffer_bps=Decimal("0"),
                dry_run=True,
            )
        )
        self.adapter.get_open_orders.side_effect = None
        self.adapter.get_open_orders.return_value = [existing]
        self.adapter.cancel_all_orders.reset_mock()
        await dry_manager.initialize()
        self.adapter.cancel_all_orders.assert_not_awaited()
        self.assertEqual(
            dry_manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_startup_unresolved_submission_aborts(self) -> None:
        self.adapter.get_unresolved_submissions.return_value = [
            {"client_order_id": "uncertain-client"}
        ]

        with self.assertRaises(RuntimeError):
            await self.manager.initialize()

        self.adapter.get_open_orders.assert_not_awaited()
        self.adapter.cancel_all_orders.assert_not_awaited()
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )

    async def test_history_terminal_proof_clears_missing_slot(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.get_open_orders.return_value = []
        self.adapter.get_order_history.return_value = [
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                price="99.9",
            )
        ]

        await self.manager.sync_open_orders()

        self.assertIsNone(self.manager.slots[OrderSide.BUY])
        self.assertFalse(self.manager.has_uncertain_state)

    async def test_history_fill_requires_position_refresh(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.get_open_orders.return_value = []
        self.adapter.get_order_history.return_value = [
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.FILLED,
                price="99.9",
                remaining="0",
            )
        ]

        position_refresh_required = await self.manager.sync_open_orders()

        self.assertTrue(position_refresh_required)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

    async def test_history_terminal_records_exchange_id_from_client_match(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        slot = self.manager._slots[OrderSide.BUY]
        self.manager._known_order_ids.clear()
        slot.order_id = None
        self.adapter.get_open_orders.return_value = []
        self.adapter.get_order_history.return_value = [
            exchange_order(
                "exchange-1",
                OrderSide.BUY,
                status=OrderStatus.FILLED,
                price="99.9",
                remaining="0",
                client_id=slot.client_id,
            )
        ]

        self.assertTrue(await self.manager.sync_open_orders())

        self.assertIn("exchange-1", self.manager.known_order_ids)
        self.assertIsNone(self.manager.slots[OrderSide.BUY])

    async def test_shutdown_wraps_resolution_and_cancel_in_safety_scope(self) -> None:
        self.adapter.begin_safety_requests = Mock()
        self.adapter.end_safety_requests = Mock()

        await self.manager.shutdown()

        self.adapter.begin_safety_requests.assert_called_once_with()
        self.adapter.end_safety_requests.assert_called_once_with()

    async def test_rest_partial_fill_requires_position_refresh(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        self.adapter.get_open_orders.return_value = [
            exchange_order(
                "1",
                OrderSide.BUY,
                price="99.9",
                amount="0.2",
                remaining="0.1",
            )
        ]

        position_refresh_required = await self.manager.sync_open_orders()

        self.assertTrue(position_refresh_required)
        self.assertEqual(
            self.manager.slots[OrderSide.BUY].state,
            OrderSlotState.PARTIALLY_FILLED,
        )
        self.assertFalse(await self.manager.sync_open_orders())

    async def test_unknown_rest_order_pauses(self) -> None:
        self.adapter.get_open_orders.return_value = [
            exchange_order("other", OrderSide.SELL)
        ]
        await self.manager.sync_open_orders()
        self.assertEqual(
            self.manager.runtime_state, RuntimeState.PAUSED_ORDER_STATE
        )
        self.assertIn("unknown open orders", self.manager.pause_reason)

    async def test_active_rest_order_after_terminal_fails_closed(self) -> None:
        await self.manager.reconcile(
            self.desired(bid_price="99.9", ask_price=None), self.risk()
        )
        await self.manager.handle_order_update(
            exchange_order(
                "1",
                OrderSide.BUY,
                status=OrderStatus.CANCELED,
                price="99.9",
            )
        )
        self.adapter.get_open_orders.return_value = [
            exchange_order("1", OrderSide.BUY, price="99.9")
        ]

        await self.manager.sync_open_orders()

        self.assertIn("unknown open orders", self.manager.pause_reason)


if __name__ == "__main__":
    unittest.main()
