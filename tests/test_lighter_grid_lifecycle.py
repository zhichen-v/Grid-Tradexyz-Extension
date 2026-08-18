import asyncio
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket
from core.adapters.exchanges.models import (
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.coordinator.grid_coordinator import GridCoordinator
from core.services.grid.coordinator.position_monitor import PositionMonitor
from core.services.grid.models import (
    GridOrder,
    GridOrderSide,
    GridOrderStatus,
    GridState,
    GridType,
)


def exchange_order(
    status: OrderStatus,
    filled: str,
    remaining: str,
) -> OrderData:
    return OrderData(
        id="101",
        client_id="client-101",
        symbol="BTC",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        amount=Decimal("0.00020"),
        price=Decimal("64000"),
        filled=Decimal(filled),
        remaining=Decimal(remaining),
        cost=Decimal("0"),
        average=Decimal("64000") if Decimal(filled) else None,
        status=status,
        timestamp=datetime.now(),
        updated=None,
        fee=None,
        trades=[],
        params={},
        raw_data={},
    )


def nonterminal_cancel_ack() -> OrderData:
    order = exchange_order(OrderStatus.PENDING, "0", "0.00020")
    order.params = {"cancel_terminal": False}
    order.raw_data = {"cancel_terminal": False}
    return order


def grid_order() -> GridOrder:
    return GridOrder(
        order_id="101",
        grid_id=61,
        side=GridOrderSide.BUY,
        price=Decimal("64000"),
        amount=Decimal("0.00020"),
        status=GridOrderStatus.PENDING,
        created_at=datetime.now() - timedelta(seconds=10),
    )


def grid_engine(exchange=None) -> GridEngineImpl:
    if exchange is None:
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order=AsyncMock(),
            get_order_history=AsyncMock(return_value=[]),
        )
    engine = GridEngineImpl(exchange)
    engine.config = SimpleNamespace(
        exchange="lighter",
        symbol="BTC",
        grid_type=GridType.LONG,
        quantity_precision=5,
    )
    engine._running = True
    return engine


class LighterPartialFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_updates_finalize_once_with_full_logical_amount(self):
        engine = grid_engine()
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        reverse_amounts = []

        async def on_filled(filled_order):
            reverse_amounts.append(filled_order.filled_amount)

        engine.subscribe_order_updates(on_filled)
        partial = exchange_order(OrderStatus.OPEN, "0.00019", "0.00001")

        await engine._handle_exchange_order_object(partial)
        await engine._handle_exchange_order_object(partial)

        self.assertEqual(reverse_amounts, [])
        self.assertTrue(order.is_pending())
        tracking = order.exchange_data["tradexyz_fill_tracking"]
        self.assertEqual(Decimal(tracking["cumulative_filled"]), Decimal("0.00019"))
        self.assertEqual(Decimal(tracking["remaining_amount"]), Decimal("0.00001"))

        final = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        await engine._handle_exchange_order_object(final)
        await engine._handle_exchange_order_object(final)

        self.assertEqual(reverse_amounts, [Decimal("0.00020")])
        self.assertEqual(order.filled_amount, Decimal("0.00020"))
        self.assertEqual(engine.get_pending_orders(), [])

    async def test_rest_sync_keeps_partial_open_until_final_status(self):
        final = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order=AsyncMock(side_effect=AssertionError("N+1 query")),
            get_order_history=AsyncMock(return_value=[final]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        callbacks = AsyncMock()
        engine.subscribe_order_updates(callbacks)

        partial = exchange_order(OrderStatus.OPEN, "0.00019", "0.00001")
        await engine._sync_orders_from_exchange([partial])
        await engine._sync_orders_from_exchange([partial])

        callbacks.assert_not_awaited()
        exchange.get_order.assert_not_awaited()
        self.assertTrue(order.is_pending())

        await engine._sync_orders_from_exchange([])
        await engine._sync_orders_from_exchange([])

        callbacks.assert_awaited_once()
        self.assertEqual(callbacks.await_args.args[0].filled_amount, Decimal("0.00020"))
        exchange.get_order_history.assert_awaited_once_with("BTC", limit=100)
        exchange.get_order.assert_not_awaited()

    async def test_rest_and_websocket_race_finalize_once(self):
        entered_get_order = asyncio.Event()
        release_get_order = asyncio.Event()
        final = exchange_order(OrderStatus.FILLED, "0.00020", "0")

        async def get_order_history(*_args, **_kwargs):
            entered_get_order.set()
            await release_get_order.wait()
            return [final]

        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order=AsyncMock(side_effect=AssertionError("N+1 query")),
            get_order_history=get_order_history,
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        callbacks = AsyncMock()
        engine.subscribe_order_updates(callbacks)

        rest_sync = asyncio.create_task(engine._sync_orders_from_exchange([]))
        await entered_get_order.wait()
        await engine._handle_exchange_order_object(final)
        release_get_order.set()
        await rest_sync

        callbacks.assert_awaited_once()

    async def test_rest_sync_uses_one_bounded_history_snapshot_for_many_missing(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order=AsyncMock(side_effect=AssertionError("N+1 query")),
            get_order_history=AsyncMock(
                return_value=[
                    SimpleNamespace(id=f"history-{index}", client_id=None)
                    for index in range(100)
                ]
            ),
        )
        engine = grid_engine(exchange)
        first = grid_order()
        second = grid_order()
        second.order_id = "202"
        second.grid_id = 62
        second.price = Decimal("64100")
        engine._register_pending_order(first, first.order_id, "client-101")
        engine._register_pending_order(second, second.order_id, "client-202")

        await engine._sync_orders_from_exchange([])

        exchange.get_order_history.assert_awaited_once_with("BTC", limit=100)
        exchange.get_order.assert_not_awaited()
        self.assertEqual(len(engine.get_pending_orders()), 2)

    async def test_expected_cancel_is_not_reimported_from_lagging_snapshot(self):
        stale_open = exchange_order(OrderStatus.OPEN, "0", "0.00020")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine._expected_cancellations.update({"101", "client-101"})

        await engine._sync_orders_from_exchange([])
        await engine._sync_orders_from_exchange([stale_open])

        self.assertEqual(engine.get_pending_orders(), [])

    async def test_direct_cancel_is_not_reimported_and_failure_clears_marker(self):
        stale_open = exchange_order(OrderStatus.OPEN, "0", "0.00020")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_order=AsyncMock(return_value=True),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")

        self.assertTrue(await engine.cancel_order("101"))
        await engine._sync_orders_from_exchange([stale_open])

        self.assertEqual(engine.get_pending_orders(), [])
        self.assertEqual(engine._expected_cancellations, set())

        failed_exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_order=AsyncMock(side_effect=RuntimeError("429")),
        )
        failed_engine = grid_engine(failed_exchange)
        failed_order = grid_order()
        failed_engine._register_pending_order(
            failed_order,
            failed_order.order_id,
            "client-101",
        )

        self.assertFalse(await failed_engine.cancel_order("101"))
        self.assertEqual(failed_engine._expected_cancellations, set())
        self.assertEqual(failed_engine.get_pending_orders(), [failed_order])

        rejected_exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_order=AsyncMock(return_value=False),
        )
        rejected_engine = grid_engine(rejected_exchange)
        rejected_order = grid_order()
        rejected_engine._register_pending_order(
            rejected_order,
            rejected_order.order_id,
            "client-101",
        )

        self.assertFalse(await rejected_engine.cancel_order("101"))
        self.assertEqual(rejected_engine._expected_cancellations, set())
        self.assertEqual(rejected_engine.get_pending_orders(), [rejected_order])

    async def test_uncertain_cancel_waits_for_exact_terminal_update_without_restore(self):
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=rest,
            cancel_order=AsyncMock(side_effect=RuntimeError("response lost")),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine._restore_cancelled_grid_order = AsyncMock(return_value=False)

        self.assertFalse(await engine.cancel_order("101"))
        self.assertIn("101", engine._expected_cancellations)
        self.assertEqual(engine.get_pending_orders(), [order])

        await engine._sync_orders_from_exchange([])

        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertIn("101", engine._expected_cancellations)

        await engine._handle_exchange_order_object(
            exchange_order(OrderStatus.CANCELED, "0", "0.00020")
        )

        self.assertEqual(order.status, GridOrderStatus.CANCELLED)
        self.assertEqual(engine.get_pending_orders(), [])
        self.assertEqual(engine._expected_cancellations, set())
        engine._restore_cancelled_grid_order.assert_not_awaited()

    async def test_cancel_all_rejects_absent_uncertain_cancel_without_terminal_proof(self):
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=rest,
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine._uncertain_cancel_order_ids.update({"101", "client-101"})

        with self.assertRaisesRegex(RuntimeError, "cancellations remain uncertain"):
            await engine.cancel_all_orders()

        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertEqual(order.status, GridOrderStatus.PENDING)
        self.assertEqual(rest._uncertain_cancellations, {("BTC", "101")})

    async def test_cancel_all_accepts_exact_terminal_cancel_history(self):
        terminal = exchange_order(OrderStatus.CANCELED, "0", "0.00020")
        terminal.id = "101"
        terminal.client_id = "client-101"
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=rest,
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[terminal]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine._uncertain_cancel_order_ids.update({"101", "client-101"})

        cancelled = await engine.cancel_all_orders()

        self.assertEqual(cancelled, 1)
        self.assertEqual(engine.get_pending_orders(), [])
        self.assertEqual(order.status, GridOrderStatus.CANCELLED)
        self.assertEqual(engine._uncertain_cancel_order_ids, set())
        self.assertEqual(rest._uncertain_cancellations, set())

    async def test_shutdown_requires_exact_cancel_history_for_local_pending_order(self):
        terminal = exchange_order(OrderStatus.CANCELED, "0", "0.00020")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(return_value=[nonterminal_cancel_ack()]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[terminal]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine.begin_shutdown()

        self.assertEqual(await engine.cancel_all_orders(), 1)

        self.assertEqual(order.status, GridOrderStatus.CANCELLED)
        self.assertEqual(engine.get_pending_orders(), [])
        exchange.get_order_history.assert_awaited_once_with("BTC", limit=100)

    async def test_shutdown_fill_is_finalized_once_and_remains_unsafe(self):
        filled = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(return_value=[nonterminal_cancel_ack()]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[filled]),
            create_order=AsyncMock(),
        )
        engine = grid_engine(exchange)
        coordinator = SimpleNamespace(
            _grid_level_locks={},
            _request_fatal_stop=MagicMock(),
        )
        engine.coordinator = coordinator
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        reverse_attempts = []

        async def try_reverse(_filled_order):
            reverse = grid_order()
            reverse.order_id = ""
            reverse.side = GridOrderSide.SELL
            reverse_attempts.append(await engine.place_order(reverse))

        engine.subscribe_order_updates(try_reverse)
        engine.begin_shutdown()

        with self.assertRaisesRegex(RuntimeError, "filled during shutdown"):
            await engine.cancel_all_orders()
        with self.assertRaisesRegex(RuntimeError, "filled during shutdown"):
            await engine.cancel_all_orders()

        self.assertEqual(order.status, GridOrderStatus.FILLED)
        self.assertEqual(engine.get_pending_orders(), [])
        self.assertEqual(reverse_attempts, [None])
        exchange.create_order.assert_not_awaited()
        coordinator._request_fatal_stop.assert_called_once()

    async def test_shutdown_absence_without_terminal_proof_stays_unsafe(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine.begin_shutdown()

        with self.assertRaisesRegex(RuntimeError, "lacks exact terminal proof"):
            await engine.cancel_all_orders()

        self.assertEqual(order.status, GridOrderStatus.PENDING)
        self.assertEqual(engine.get_pending_orders(), [order])

    async def test_uncertain_cancel_bulk_history_finalizes_fill_once(self):
        filled = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=rest,
            cancel_order=AsyncMock(side_effect=RuntimeError("response lost")),
            get_order_history=AsyncMock(return_value=[filled]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        callback = AsyncMock()
        engine.subscribe_order_updates(callback)

        self.assertFalse(await engine.cancel_order("101"))
        await engine._sync_orders_from_exchange([])
        await engine._sync_orders_from_exchange([])

        self.assertEqual(order.status, GridOrderStatus.FILLED)
        self.assertEqual(engine.get_pending_orders(), [])
        callback.assert_awaited_once_with(order)
        exchange.get_order_history.assert_awaited_once_with("BTC", limit=100)

    async def test_cancel_ack_waits_for_later_exact_fill(self):
        ack = nonterminal_cancel_ack()
        filled = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=rest,
            cancel_order=AsyncMock(return_value=ack),
            get_order_history=AsyncMock(return_value=[filled]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        callback = AsyncMock()
        engine.subscribe_order_updates(callback)

        self.assertFalse(await engine.cancel_order("101"))
        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertIn("101", engine._expected_cancellations)

        await engine._sync_orders_from_exchange([])
        await engine._sync_orders_from_exchange([])

        self.assertEqual(order.status, GridOrderStatus.FILLED)
        self.assertEqual(order.filled_amount, Decimal("0.00020"))
        self.assertEqual(engine.get_pending_orders(), [])
        callback.assert_awaited_once_with(order)
        self.assertEqual(rest._uncertain_cancellations, set())

    async def test_uncertain_cancel_bulk_history_clears_exact_cancellation(self):
        terminal = exchange_order(OrderStatus.CANCELED, "0", "0.00020")
        rest = SimpleNamespace(_uncertain_cancellations={("BTC", "101")})
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=rest,
            cancel_order=AsyncMock(side_effect=RuntimeError("response lost")),
            get_order_history=AsyncMock(return_value=[terminal]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        callback = AsyncMock()
        engine.subscribe_order_updates(callback)

        self.assertFalse(await engine.cancel_order("101"))
        await engine._sync_orders_from_exchange([])

        self.assertEqual(order.status, GridOrderStatus.CANCELLED)
        self.assertEqual(engine.get_pending_orders(), [])
        self.assertEqual(engine._expected_cancellations, set())
        callback.assert_not_awaited()

    async def test_uncertain_limit_submission_is_quarantined_until_exact_match(self):
        uncertain = exchange_order(OrderStatus.PENDING, "0", "0.00020")
        uncertain.id = "client-uncertain"
        uncertain.client_id = "client-uncertain"
        uncertain.raw_data = {
            "submission_uncertain": True,
            "client_order_id": "client-uncertain",
        }
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(return_value=uncertain),
            get_order_history=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        coordinator = SimpleNamespace(
            _grid_level_locks={},
            _request_fatal_stop=MagicMock(),
        )
        engine.coordinator = coordinator
        engine._restore_cancelled_grid_order = AsyncMock(return_value=False)

        placed = await engine.place_order(order)
        await engine._sync_orders_from_exchange([])
        await engine._sync_orders_from_exchange([])

        self.assertIs(placed, order)
        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertTrue(engine._placements_paused)
        self.assertIn(order.grid_id, coordinator._grid_level_locks)
        coordinator._request_fatal_stop.assert_called_once()
        exchange.create_order.assert_awaited_once()
        engine._restore_cancelled_grid_order.assert_not_awaited()

        late_original = exchange_order(OrderStatus.OPEN, "0", "0.00020")
        late_original.id = "late-original"
        late_original.client_id = "client-uncertain"
        await engine._sync_orders_from_exchange([late_original])

        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertEqual(order.order_id, "late-original")
        self.assertFalse(order.exchange_data["submission_uncertain"])
        exchange.create_order.assert_awaited_once()
        engine._restore_cancelled_grid_order.assert_not_awaited()

    async def test_uncertain_submission_prevents_safe_cancel_all(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            get_unresolved_submissions=MagicMock(
                return_value=[{"client_order_id": "client-uncertain"}]
            ),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        order.order_id = "client-uncertain"
        order.exchange_data = {"submission_uncertain": True}
        engine._register_pending_order(order, order.order_id)

        with self.assertRaisesRegex(RuntimeError, "submissions remain uncertain"):
            await engine.cancel_all_orders()

        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertEqual(engine._expected_cancellations, set())

    async def test_cancel_all_retries_when_resolver_finds_late_active_order(self):
        late_active = exchange_order(OrderStatus.OPEN, "0", "0.00020")
        late_active.id = "late-original"
        late_active.client_id = "client-uncertain"
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(
                side_effect=[
                    [],
                    [
                        SimpleNamespace(
                            id="late-original",
                            client_id="client-uncertain",
                            status=OrderStatus.CANCELED,
                        )
                    ],
                ]
            ),
            get_open_orders=AsyncMock(return_value=[]),
            resolve_unresolved_submissions=AsyncMock(return_value=[late_active]),
            get_unresolved_submissions=MagicMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        order.order_id = "client-uncertain"
        order.exchange_data = {"submission_uncertain": True}
        engine._register_pending_order(order, order.order_id)

        with self.assertRaisesRegex(RuntimeError, "resolved as active"):
            await engine.cancel_all_orders()

        self.assertEqual(order.order_id, "late-original")
        self.assertFalse(order.exchange_data["submission_uncertain"])
        self.assertEqual(engine.get_pending_orders(), [order])

        exchange.resolve_unresolved_submissions.return_value = []
        self.assertEqual(await engine.cancel_all_orders(), 1)
        self.assertEqual(exchange.cancel_all_orders.await_count, 2)
        self.assertEqual(engine.get_pending_orders(), [])

    async def test_cancel_all_accepts_late_terminal_submission_proof(self):
        terminal = exchange_order(OrderStatus.CANCELED, "0", "0.00020")
        terminal.id = "late-original"
        terminal.client_id = "client-uncertain"
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            resolve_unresolved_submissions=AsyncMock(return_value=[terminal]),
            get_unresolved_submissions=MagicMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        order.order_id = "client-uncertain"
        order.exchange_data = {"submission_uncertain": True}
        engine._register_pending_order(order, order.order_id)

        self.assertEqual(await engine.cancel_all_orders(), 1)
        self.assertEqual(order.status, GridOrderStatus.CANCELLED)
        self.assertEqual(engine.get_pending_orders(), [])

    async def test_cancel_all_keeps_filled_uncertain_submission_fatal(self):
        filled = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        filled.id = "late-original"
        filled.client_id = "client-uncertain"
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
            resolve_unresolved_submissions=AsyncMock(return_value=[filled]),
            get_unresolved_submissions=MagicMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        coordinator = SimpleNamespace(
            _grid_level_locks={},
            _request_fatal_stop=MagicMock(),
        )
        engine.coordinator = coordinator
        order = grid_order()
        order.order_id = "client-uncertain"
        order.exchange_data = {"submission_uncertain": True}
        engine._register_pending_order(order, order.order_id)

        with self.assertRaisesRegex(RuntimeError, "resolved as filled"):
            await engine.cancel_all_orders()

        self.assertEqual(order.status, GridOrderStatus.FILLED)
        self.assertEqual(engine.get_pending_orders(), [])
        coordinator._request_fatal_stop.assert_called_once()

    async def test_uncertain_opening_market_submission_is_reserved_and_not_resent(self):
        uncertain = exchange_order(OrderStatus.PENDING, "0", "0.00020")
        uncertain.id = "market-client"
        uncertain.client_id = "market-client"
        uncertain.type = OrderType.MARKET
        uncertain.params = {"submission_uncertain": True}
        uncertain.raw_data = {"submission_uncertain": True}
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(return_value=uncertain),
            cancel_all_orders=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
        )
        engine = grid_engine(exchange)
        engine.coordinator = SimpleNamespace(
            tracker=SimpleNamespace(get_current_position=MagicMock(return_value=Decimal("0"))),
            _request_fatal_stop=MagicMock(),
        )

        with self.assertRaisesRegex(RuntimeError, "outcome is uncertain"):
            await engine.place_market_order(GridOrderSide.BUY, Decimal("0.00020"))

        self.assertEqual(engine._reserved_market_open_amount, Decimal("0.00020"))
        self.assertIn("market-client", engine._uncertain_market_submissions)
        self.assertTrue(engine._placements_paused)

        with self.assertRaises(RuntimeError):
            await engine.place_market_order(GridOrderSide.BUY, Decimal("0.00020"))
        exchange.create_order.assert_awaited_once()

        with self.assertRaisesRegex(RuntimeError, "submissions remain uncertain"):
            await engine.cancel_all_orders()

        engine.reconcile_market_open_reservations(Decimal("0.00020"))
        self.assertEqual(engine._reserved_market_open_amount, Decimal("0"))
        self.assertEqual(engine._uncertain_market_submissions, {})

    async def test_immediate_fill_callback_runs_after_caller_records_order_state(self):
        filled = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(return_value=filled),
        )
        engine = grid_engine(exchange)
        state = GridState()
        callback_observations = []

        async def on_filled(filled_order):
            callback_observations.append(filled_order.order_id in state.active_orders)

        engine.subscribe_order_updates(on_filled)
        order = grid_order()
        placed = await engine.place_order(order)
        state.add_order(placed)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(callback_observations, [True])
        self.assertEqual(order.status, GridOrderStatus.FILLED)
        self.assertEqual(engine.get_pending_orders(), [])
        exchange.create_order.assert_awaited_once()

    async def test_immediate_continuation_fill_finalizes_full_logical_amount_once(self):
        filled = exchange_order(OrderStatus.FILLED, "0.00001", "0")
        filled.amount = Decimal("0.00001")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(return_value=filled),
        )
        engine = grid_engine(exchange)
        callback_amounts = []

        async def on_filled(filled_order):
            callback_amounts.append(filled_order.filled_amount)

        engine.subscribe_order_updates(on_filled)
        order = grid_order()
        order.amount = Decimal("0.00001")
        order.exchange_data = {
            "tradexyz_fill_tracking": {
                "target_amount": "0.00020",
                "carry_filled_amount": "0.00019",
                "cumulative_filled": "0.00019",
                "remaining_amount": "0.00001",
            }
        }

        await engine.place_order(order)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(callback_amounts, [Decimal("0.00020")])
        self.assertEqual(order.filled_amount, Decimal("0.00020"))
        self.assertEqual(engine.get_pending_orders(), [])
        exchange.create_order.assert_awaited_once()

    async def test_subminimum_partial_cancel_is_quarantined_without_reverse(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _market_info={
                "BTC": {
                    "symbol": "BTC",
                    "min_base_amount": "0.00020",
                    "min_quote_amount": "0",
                }
            },
            create_order=AsyncMock(),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")

        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.state = GridState()
        coordinator.state.add_order(order)
        coordinator._grid_level_locks = {}
        coordinator._emergency_stop_requested = False
        coordinator._emergency_stop_task = None
        coordinator._fatal_stop_reason = None
        coordinator.stop = AsyncMock()
        engine.coordinator = coordinator

        from core.services.grid.implementations.order_health_checker import OrderHealthChecker

        checker = OrderHealthChecker(engine.config, engine)
        engine._health_checker = checker
        callbacks = AsyncMock()
        engine.subscribe_order_updates(callbacks)

        await engine._handle_exchange_order_object(
            exchange_order(OrderStatus.OPEN, "0.00019", "0.00001")
        )
        await engine._handle_exchange_order_object(
            exchange_order(OrderStatus.CANCELED, "0.00019", "0.00001")
        )
        await coordinator._emergency_stop_task

        exchange.create_order.assert_not_awaited()
        callbacks.assert_not_awaited()
        self.assertEqual(coordinator.state.active_orders, {})
        self.assertEqual(coordinator.state.pending_buy_orders, 0)
        self.assertIn(order.grid_id, coordinator._grid_level_locks)
        self.assertIn("below exchange minimum", coordinator.get_fatal_stop_reason())
        self.assertEqual(
            checker._calculate_expected_position([]),
            Decimal("0.00019"),
        )

        repeated_continuation = GridOrder(
            order_id="health-repeat",
            grid_id=order.grid_id,
            side=order.side,
            price=order.price,
            amount=Decimal("0.00001"),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
            exchange_data=order.exchange_data,
        )
        self.assertEqual(
            await checker._place_missing_orders([repeated_continuation]),
            0,
        )
        exchange.create_order.assert_not_awaited()

    async def test_health_only_partial_cancel_restores_remaining_amount(self):
        cancelled = exchange_order(OrderStatus.CANCELED, "0.00019", "0.00001")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order_history=AsyncMock(return_value=[cancelled]),
            create_order=AsyncMock(
                return_value=SimpleNamespace(
                    id="replacement",
                    client_id=None,
                    raw_data={},
                )
            ),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        callbacks = AsyncMock()
        engine.subscribe_order_updates(callbacks)

        from core.services.grid.implementations.order_health_checker import OrderHealthChecker

        checker = OrderHealthChecker(engine.config, engine)
        partial = exchange_order(OrderStatus.OPEN, "0.00019", "0.00001")
        await checker._sync_orders_into_engine([partial])
        callbacks.assert_not_awaited()

        await checker._sync_orders_into_engine([])
        restore_task = next(iter(engine._restore_tasks.values()))
        await restore_task

        self.assertEqual(
            exchange.create_order.await_args.kwargs["amount"],
            Decimal("0.00001"),
        )
        callbacks.assert_not_awaited()

    async def test_health_terminal_fill_clears_continuation_restore_state(self):
        final = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            get_order_history=AsyncMock(return_value=[final]),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        order.exchange_data = {
            "tradexyz_fill_tracking": {
                "logical_target_amount": "0.00020",
                "carry_filled_amount": "0.00019",
                "cumulative_filled": "0.00019",
                "remaining_amount": "0.00001",
            }
        }
        engine._register_pending_order(order, order.order_id, "client-101")
        restore_key = engine._restore_key(order)
        engine._restore_state[restore_key] = {
            "attempts": 2.0,
            "last_attempt": 1.0,
            "circuit_until": 0.0,
        }

        from core.services.grid.implementations.order_health_checker import OrderHealthChecker

        checker = OrderHealthChecker(engine.config, engine)
        await checker._sync_orders_into_engine([])

        self.assertNotIn(restore_key, engine._restore_state)

    async def test_rest_pause_replays_partial_then_final_fill_once_at_full_amount(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(
                return_value=SimpleNamespace(
                    id="reverse-101",
                    client_id="reverse-client-101",
                    raw_data={},
                )
            ),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")

        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            exchange="lighter",
            grid_interval=Decimal("100"),
            reverse_order_grid_distance=1,
            max_position=None,
            is_long=lambda: True,
            is_short=lambda: False,
            get_grid_index_by_price=lambda _price: 61,
        )
        coordinator.strategy = SimpleNamespace(
            calculate_reverse_order=MagicMock(
                return_value=(GridOrderSide.SELL, Decimal("64100"), 61)
            )
        )
        coordinator.state = SimpleNamespace(
            active_orders={},
            mark_order_filled=MagicMock(return_value=True),
            add_order=MagicMock(),
            update_current_price=MagicMock(),
        )
        coordinator.tracker = SimpleNamespace(
            record_filled_order=MagicMock(),
            get_current_position=MagicMock(return_value=Decimal("0.00020")),
        )
        coordinator.engine = engine
        coordinator.reserve_manager = None
        coordinator.scalping_manager = None
        coordinator.scalping_ops = None
        coordinator.capital_protection_manager = None
        coordinator._running = True
        coordinator._paused = True
        coordinator._resetting = False
        coordinator._active_fill_callbacks = 0
        coordinator._deferred_fills = {}
        coordinator._deferred_fill_drain_task = None
        coordinator._recent_fills = {}
        coordinator._fill_dedup_window = 10.0
        coordinator._last_fill_time = 0.0
        coordinator._grid_level_locks = {}
        coordinator._check_scalping_mode = AsyncMock()

        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor.logger = MagicMock()
        monitor.engine = engine
        monitor.tracker = coordinator.tracker
        monitor.coordinator = coordinator
        monitor._rest_pause_owned = True
        monitor._rest_failure_count = 3
        monitor._rest_is_available = False
        monitor._rest_query_interval = 10
        monitor._rest_next_query_time = 0
        monitor.trigger_event_query = AsyncMock()
        coordinator.position_monitor = monitor
        engine.coordinator = coordinator
        engine.get_current_price = AsyncMock(return_value=Decimal("64000"))
        engine.subscribe_order_updates(coordinator._on_order_filled)

        await engine._handle_exchange_order_object(
            exchange_order(OrderStatus.OPEN, "0.00019", "0.00001")
        )
        self.assertEqual(coordinator._deferred_fills, {})

        final = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        await engine._handle_exchange_order_object(final)
        await coordinator._on_order_filled(order)

        exchange.create_order.assert_not_awaited()
        self.assertEqual(len(coordinator._deferred_fills), 1)

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            monitor._record_rest_success(100.0)
            drain_task = coordinator._deferred_fill_drain_task
            self.assertIsNotNone(drain_task)
            await drain_task

        exchange.create_order.assert_awaited_once()
        self.assertEqual(
            exchange.create_order.await_args.kwargs["amount"],
            Decimal("0.00020"),
        )
        coordinator.state.add_order.assert_called_once()
        coordinator.tracker.record_filled_order.assert_called_once_with(order)

    async def test_startup_fill_before_batch_returns_still_places_reverse(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(
                return_value=SimpleNamespace(
                    id="startup-reverse",
                    client_id="startup-reverse-client",
                    raw_data={},
                )
            ),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine._register_pending_order(order, order.order_id, "client-101")
        engine.suspend_health_repairs("startup initial grid placement")

        coordinator = GridCoordinator.__new__(GridCoordinator)
        coordinator.logger = MagicMock()
        coordinator.config = SimpleNamespace(
            exchange="lighter",
            grid_interval=Decimal("100"),
            reverse_order_grid_distance=1,
            max_position=None,
            is_long=lambda: True,
            is_short=lambda: False,
            get_grid_index_by_price=lambda _price: 61,
        )
        coordinator.strategy = SimpleNamespace(
            calculate_reverse_order=MagicMock(
                return_value=(GridOrderSide.SELL, Decimal("64100"), 61)
            )
        )
        coordinator.state = GridState()
        coordinator.tracker = SimpleNamespace(
            record_filled_order=MagicMock(),
            get_current_position=MagicMock(return_value=Decimal("0.00020")),
        )
        coordinator.engine = engine
        coordinator.position_monitor = SimpleNamespace(
            _rest_pause_owned=False,
            trigger_event_query=AsyncMock(),
        )
        coordinator.reserve_manager = None
        coordinator.scalping_manager = None
        coordinator.scalping_ops = None
        coordinator.capital_protection_manager = None
        coordinator._running = True
        coordinator._paused = False
        coordinator._manual_pause_owned = False
        coordinator._resetting = False
        coordinator.is_emergency_stopped = False
        coordinator._active_fill_callbacks = 0
        coordinator._deferred_fills = {}
        coordinator._deferred_fill_drain_task = None
        coordinator._recent_fills = {}
        coordinator._fill_dedup_window = 10.0
        coordinator._last_fill_time = 0.0
        coordinator._grid_level_locks = {}
        coordinator._check_scalping_mode = AsyncMock()
        engine.coordinator = coordinator
        engine.get_current_price = AsyncMock(return_value=Decimal("64000"))
        engine.subscribe_order_updates(coordinator._on_order_filled)

        with patch(
            "core.services.grid.coordinator.grid_coordinator.asyncio.sleep",
            new=AsyncMock(),
        ):
            await engine._handle_exchange_order_object(
                exchange_order(OrderStatus.FILLED, "0.00020", "0")
            )

        exchange.create_order.assert_awaited_once()
        self.assertEqual(
            exchange.create_order.await_args.kwargs["amount"],
            Decimal("0.00020"),
        )
        self.assertEqual(coordinator.state.filled_buy_count, 1)
        self.assertEqual(coordinator.state.pending_buy_orders, 0)
        self.assertIn("startup-reverse", coordinator.state.active_orders)


class LighterCancellationRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_cancel_restore_is_bounded_and_opens_circuit(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(side_effect=RuntimeError("post-only rejected")),
        )
        engine = grid_engine(exchange)
        order = grid_order()
        engine.coordinator = SimpleNamespace(
            _paused=False,
            _grid_level_locks={
                order.grid_id: {
                    "tp_side": order.side.value,
                    "tp_price": order.price,
                }
            },
        )

        with patch(
            "core.services.grid.implementations.grid_engine_impl.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            await engine._restore_cancelled_grid_order(order, order.order_id)
            await next(iter(engine._restore_tasks.values()))

        self.assertEqual(exchange.create_order.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.await_args_list],
            [2.0, 4.0],
        )
        state = engine._restore_state[engine._restore_key(order)]
        self.assertGreater(state["circuit_until"], 0)
        self.assertNotIn(order.grid_id, engine.coordinator._grid_level_locks)

        await engine._restore_cancelled_grid_order(order, order.order_id)
        self.assertEqual(exchange.create_order.await_count, 3)

    async def test_paused_coordinator_blocks_already_scheduled_restore(self):
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            create_order=AsyncMock(
                return_value=SimpleNamespace(id="replacement", client_id=None, raw_data={})
            ),
        )
        engine = grid_engine(exchange)
        engine.coordinator = SimpleNamespace(_paused=False, _grid_level_locks={})
        order = grid_order()

        scheduled = await engine._restore_cancelled_grid_order(order, order.order_id)
        engine.coordinator._paused = True

        with patch(
            "core.services.grid.implementations.grid_engine_impl.asyncio.sleep",
            new=AsyncMock(),
        ):
            await next(iter(engine._restore_tasks.values()))

        self.assertTrue(scheduled)
        exchange.create_order.assert_not_awaited()


class LighterWebSocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_engine_uses_direct_stream_health(self):
        websocket = SimpleNamespace(
            get_connection_status=lambda: {"healthy": False},
        )
        exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _websocket=websocket,
            is_connected=lambda: True,
        )
        engine = grid_engine(exchange)

        self.assertFalse(engine._is_websocket_connected())
        websocket.get_connection_status = lambda: {"healthy": True}
        self.assertTrue(engine._is_websocket_connected())

    async def test_resubscribe_restarts_required_direct_stream(self):
        websocket = object.__new__(LighterWebSocket)
        websocket._subscribed_markets = []
        websocket._subscribed_accounts = [7]
        websocket._subscribed_market_stats = []
        websocket._subscribed_trades = []
        websocket._order_callbacks = [lambda order: None]
        websocket._order_fill_callbacks = []
        websocket._recreate_ws_client = AsyncMock()
        websocket._ensure_direct_ws_running = AsyncMock()

        await websocket._resubscribe_all()

        websocket._recreate_ws_client.assert_awaited_once()
        websocket._ensure_direct_ws_running.assert_awaited_once()

    def test_connection_health_requires_authenticated_order_stream(self):
        websocket = object.__new__(LighterWebSocket)
        websocket._connected = True
        websocket._direct_ws_task = SimpleNamespace(done=lambda: False)
        websocket._direct_ws_connected = True
        websocket._account_orders_subscribed = False
        websocket._direct_last_message_time = 0.0
        websocket._subscribed_market_stats = []
        websocket._subscribed_trades = []
        websocket._order_callbacks = [lambda order: None]
        websocket._order_fill_callbacks = []

        self.assertFalse(websocket.get_connection_status()["healthy"])
        websocket._account_orders_subscribed = True
        self.assertTrue(websocket.get_connection_status()["healthy"])

    def test_direct_parser_preserves_partial_amounts(self):
        websocket = object.__new__(LighterWebSocket)
        websocket._markets_cache = {1: {"symbol": "BTC"}}
        parsed = websocket._parse_order_from_direct_ws({
            "order_index": 101,
            "client_order_index": 202,
            "market_index": 1,
            "initial_base_amount": "0.00020",
            "remaining_base_amount": "0.00001",
            "filled_base_amount": "0.00019",
            "filled_quote_amount": "12.16",
            "price": "64000",
            "is_ask": False,
            "status": "partially_filled",
            "type": "limit",
        })

        self.assertEqual(parsed.status, OrderStatus.OPEN)
        self.assertEqual(parsed.filled, Decimal("0.00019"))
        self.assertEqual(parsed.remaining, Decimal("0.00001"))


class LighterRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def _rest(self):
        rest = object.__new__(LighterRest)
        rest._request_lock = asyncio.Lock()
        rest._next_request_at = 0.0
        rest._rate_limit_failures = 0
        rest._request_interval = 0.0
        rest._max_rate_limit_delay = 30.0
        return rest

    async def test_read_retries_once_behind_shared_cooldown(self):
        rest = self._rest()
        request = AsyncMock(
            side_effect=[
                RuntimeError("429 Too Many Requests"),
                SimpleNamespace(code=200),
            ]
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            response = await rest._call_api("positions query", request)

        self.assertEqual(response.code, 200)
        self.assertEqual(request.await_count, 2)
        sleep_mock.assert_awaited_once()

    async def test_mutation_is_never_retried_after_429(self):
        rest = self._rest()
        request = AsyncMock(side_effect=RuntimeError("HTTP 429 Too Many Requests"))

        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            await rest._call_api(
                "limit order submission",
                request,
                retry_on_429=False,
            )

        self.assertEqual(request.await_count, 1)

    async def test_concurrent_client_order_indexes_are_unique_and_monotonic(self):
        rest = object.__new__(LighterRest)
        start = asyncio.Event()

        async def allocate():
            await start.wait()
            return rest._next_client_order_index()

        tasks = [asyncio.create_task(allocate()) for _ in range(100)]
        start.set()
        indexes = await asyncio.gather(*tasks)

        self.assertEqual(len(set(indexes)), len(indexes))
        self.assertEqual(indexes, sorted(indexes))

    def test_client_order_index_uses_epoch_and_never_moves_backwards(self):
        rest = object.__new__(LighterRest)

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.time.time_ns",
            return_value=1_800_000_000_000_000_000,
        ):
            first = rest._next_client_order_index()
            second = rest._next_client_order_index()
            rest._last_client_order_index = second + 50
            third = rest._next_client_order_index()

        self.assertEqual(first, 1_800_000_000_000)
        self.assertEqual(second, first + 1)
        self.assertEqual(third, second + 51)


class LighterMutationAmbiguityTests(unittest.IsolatedAsyncioTestCase):
    CLIENT_ID = 1_800_000_000_000

    def _rest(self) -> LighterRest:
        rest = object.__new__(LighterRest)
        rest._request_lock = asyncio.Lock()
        rest._next_request_at = 0.0
        rest._rate_limit_failures = 0
        rest._request_interval = 0.0
        rest._max_rate_limit_delay = 30.0
        rest._uncertain_cancellations = set()
        rest._next_client_order_index = MagicMock(return_value=self.CLIENT_ID)
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.get_order_history = AsyncMock(return_value=[])
        return rest

    @staticmethod
    def _market_info():
        return {
            "market_index": 1,
            "price_decimals": 1,
            "size_decimals": 5,
            "price_multiplier": Decimal("10"),
            "size_multiplier": Decimal("100000"),
            "min_base_amount": Decimal("0.00020"),
            "min_quote_amount": Decimal("10"),
        }

    async def test_transport_loss_reconciles_limit_order_by_original_client_id(self):
        rest = self._rest()
        found = exchange_order(OrderStatus.OPEN, "0", "0.00020")
        found.id = "101"
        found.client_id = str(self.CLIENT_ID)
        rest.get_open_orders = AsyncMock(return_value=[found])
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(
                side_effect=ConnectionError("connection reset after send")
            )
        )

        result = await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("64000"),
            self._market_info(),
        )

        self.assertIs(result, found)
        rest.signer_client.create_order.assert_awaited_once()
        self.assertEqual(
            rest.signer_client.create_order.await_args.kwargs["client_order_index"],
            self.CLIENT_ID,
        )
        rest._next_client_order_index.assert_called_once()
        rest.get_order_history.assert_not_awaited()

    async def test_unknown_limit_submission_returns_one_tracked_client_id(self):
        rest = self._rest()
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(side_effect=TimeoutError("response lost"))
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await rest._execute_limit_order(
                "BTC",
                "buy",
                Decimal("0.00020"),
                Decimal("64000"),
                self._market_info(),
            )

        self.assertEqual(result.id, str(self.CLIENT_ID))
        self.assertEqual(result.client_id, str(self.CLIENT_ID))
        self.assertTrue(result.raw_data["submission_uncertain"])
        unresolved = rest.get_unresolved_submissions()
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["client_order_id"], str(self.CLIENT_ID))
        self.assertEqual(unresolved[0]["symbol"], "BTC")
        self.assertEqual(unresolved[0]["type"], "limit")
        self.assertIn("time", unresolved[0])
        rest.signer_client.create_order.assert_awaited_once()
        rest._next_client_order_index.assert_called_once()
        self.assertEqual(rest.get_open_orders.await_count, 2)
        self.assertEqual(rest.get_order_history.await_count, 2)

    async def test_response_decode_value_error_is_ambiguous_not_retried(self):
        rest = self._rest()
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(
                side_effect=ValueError("Expecting value: line 1 column 1")
            )
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await rest._execute_limit_order(
                "BTC",
                "buy",
                Decimal("0.00020"),
                Decimal("64000"),
                self._market_info(),
            )

        self.assertTrue(result.raw_data["submission_uncertain"])
        self.assertEqual(result.client_id, str(self.CLIENT_ID))
        rest.signer_client.create_order.assert_awaited_once()
        rest._next_client_order_index.assert_called_once()
        self.assertEqual(
            rest.get_unresolved_submissions()[0]["client_order_id"],
            str(self.CLIENT_ID),
        )

    async def test_transport_loss_can_reconcile_from_history(self):
        rest = self._rest()
        found = exchange_order(OrderStatus.FILLED, "0.00020", "0")
        found.id = "101"
        found.client_id = str(self.CLIENT_ID)
        rest.get_order_history = AsyncMock(return_value=[found])
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(side_effect=TimeoutError("response lost"))
        )

        result = await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("64000"),
            self._market_info(),
        )

        self.assertIs(result, found)
        self.assertEqual(result.status, OrderStatus.FILLED)
        rest.signer_client.create_order.assert_awaited_once()
        rest.get_open_orders.assert_awaited_once_with("BTC")
        rest.get_order_history.assert_awaited_once_with("BTC")

    async def test_unresolved_submission_can_be_resolved_later_by_exact_client_id(self):
        rest = self._rest()
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(side_effect=TimeoutError("response lost"))
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            await rest._execute_limit_order(
                "BTC",
                "buy",
                Decimal("0.00020"),
                Decimal("64000"),
                self._market_info(),
            )

        snapshot = rest.get_unresolved_submissions()
        snapshot[0]["symbol"] = "tampered"
        self.assertEqual(rest.get_unresolved_submissions()[0]["symbol"], "BTC")

        found = exchange_order(OrderStatus.OPEN, "0", "0.00020")
        found.id = "101"
        found.client_id = str(self.CLIENT_ID)
        rest.get_open_orders = AsyncMock(return_value=[found])

        resolved = await rest.resolve_unresolved_submissions()

        self.assertEqual(resolved, [found])
        self.assertEqual(rest.get_unresolved_submissions(), [])

    async def test_unknown_market_submission_is_not_resent(self):
        rest = self._rest()
        rest._calculate_slippage_protection_price = AsyncMock(
            return_value=Decimal("64000")
        )
        rest.signer_client = SimpleNamespace(
            create_market_order=AsyncMock(side_effect=TimeoutError("response lost"))
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await rest._execute_market_order(
                "BTC",
                "buy",
                Decimal("0.00020"),
                None,
                self._market_info(),
            )

        self.assertEqual(result.client_id, str(self.CLIENT_ID))
        self.assertEqual(result.type, OrderType.MARKET)
        self.assertTrue(result.params["submission_uncertain"])
        rest.signer_client.create_market_order.assert_awaited_once()
        rest._next_client_order_index.assert_called_once()

    async def test_definitive_order_rejection_remains_failure(self):
        rest = self._rest()
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(
                return_value=(
                    None,
                    SimpleNamespace(code=400, tx_hash=None),
                    None,
                )
            )
        )

        result = await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("64000"),
            self._market_info(),
        )

        self.assertIsNone(result)
        rest.signer_client.create_order.assert_awaited_once()
        rest.get_open_orders.assert_not_awaited()
        rest.get_order_history.assert_not_awaited()

    async def test_cancel_ack_resolves_epoch_client_id_but_stays_nonterminal(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        rest.get_open_orders = AsyncMock(
            return_value=[
                SimpleNamespace(id="101", client_id=str(self.CLIENT_ID))
            ]
        )
        rest.signer_client = SimpleNamespace(
            cancel_order=AsyncMock(
                return_value=(
                    object(),
                    SimpleNamespace(code=200, tx_hash="cancel-tx"),
                    None,
                )
            )
        )

        cancelled = await rest.cancel_order("BTC", str(self.CLIENT_ID))

        self.assertFalse(cancelled)
        rest.signer_client.cancel_order.assert_awaited_once_with(
            market_index=1,
            order_index=101,
        )
        self.assertEqual(
            rest._uncertain_cancellations,
            {("BTC", str(self.CLIENT_ID))},
        )

    async def test_cancel_ack_succeeds_only_with_exact_cancel_history(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        terminal = SimpleNamespace(
            id="101",
            client_id="client-101",
            status=OrderStatus.CANCELED,
        )
        rest.get_order_history = AsyncMock(return_value=[terminal])
        rest.signer_client = SimpleNamespace(
            cancel_order=AsyncMock(
                return_value=(
                    object(),
                    SimpleNamespace(code=200, tx_hash="cancel-tx"),
                    None,
                )
            )
        )

        cancelled = await rest.cancel_order("BTC", "101")

        self.assertTrue(cancelled)
        self.assertEqual(rest._uncertain_cancellations, set())

    async def test_cancel_client_id_without_order_index_fails_closed(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        rest.signer_client = SimpleNamespace(cancel_order=AsyncMock())

        cancelled = await rest.cancel_order("BTC", str(self.CLIENT_ID))

        self.assertFalse(cancelled)
        rest.signer_client.cancel_order.assert_not_awaited()

    async def test_ambiguous_cancel_never_repeats_signer_mutation(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        rest.get_open_orders = AsyncMock(
            return_value=[SimpleNamespace(id="101", client_id="client-101")]
        )
        rest.signer_client = SimpleNamespace(
            cancel_order=AsyncMock(
                side_effect=ConnectionError("connection reset after send")
            )
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            first = await rest.cancel_order("BTC", "101")
            second = await rest.cancel_order("BTC", "101")

        self.assertFalse(first)
        self.assertFalse(second)
        rest.signer_client.cancel_order.assert_awaited_once()

    async def test_cancel_absence_is_not_terminal_and_later_active_never_resends(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        active_order = SimpleNamespace(id="101", client_id="client-101")
        rest.get_open_orders = AsyncMock(
            side_effect=[[], [active_order], [active_order]]
        )
        rest.signer_client = SimpleNamespace(
            cancel_order=AsyncMock(
                side_effect=ConnectionError("connection reset after send")
            )
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            first = await rest.cancel_order("BTC", "101")
            second = await rest.cancel_order("BTC", "101")

        self.assertFalse(first)
        self.assertFalse(second)
        rest.signer_client.cancel_order.assert_awaited_once()
        self.assertEqual(rest.get_order_history.await_count, 1)

    async def test_ambiguous_cancel_succeeds_only_on_exact_terminal_history(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        terminal = SimpleNamespace(
            id="101",
            client_id="client-101",
            status=OrderStatus.CANCELED,
        )
        rest.get_order_history = AsyncMock(return_value=[terminal])
        rest.signer_client = SimpleNamespace(
            cancel_order=AsyncMock(
                side_effect=ConnectionError("connection reset after send")
            )
        )

        cancelled = await rest.cancel_order("BTC", "101")

        self.assertTrue(cancelled)
        rest.signer_client.cancel_order.assert_awaited_once()
        self.assertEqual(rest._uncertain_cancellations, set())

    async def test_ambiguous_cancel_fill_is_not_reported_as_cancelled(self):
        rest = self._rest()
        rest.get_market_index = MagicMock(return_value=1)
        terminal = SimpleNamespace(
            id="101",
            client_id="client-101",
            status=OrderStatus.FILLED,
        )
        rest.get_order_history = AsyncMock(return_value=[terminal])
        rest.signer_client = SimpleNamespace(
            cancel_order=AsyncMock(
                side_effect=ConnectionError("connection reset after send")
            )
        )

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            cancelled = await rest.cancel_order("BTC", "101")

        self.assertFalse(cancelled)
        rest.signer_client.cancel_order.assert_awaited_once()
        self.assertEqual(rest._uncertain_cancellations, {("BTC", "101")})


class LighterRestOrderParsingTests(unittest.TestCase):
    def test_parser_prefers_numeric_exchange_and_client_indexes(self):
        rest = object.__new__(LighterRest)
        parsed = rest._parse_order(
            SimpleNamespace(
                order_index=101,
                order_id="non-cancellable-string-id",
                client_order_index=202,
                client_order_id="different-client-id",
                initial_base_amount="0.00020",
                filled_base_amount="0",
                remaining_base_amount="0.00020",
                price="64000",
                filled_quote_amount="0",
                is_ask=False,
                type="limit",
                status="open",
                timestamp=None,
            ),
            "BTC",
        )

        self.assertEqual(parsed.id, "101")
        self.assertEqual(parsed.client_id, "202")


class LighterAdapterHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_returns_nonterminal_order_while_proof_is_uncertain(self):
        adapter = object.__new__(LighterAdapter)
        adapter._normalize_symbol = lambda symbol: symbol
        adapter._rest = SimpleNamespace(
            _uncertain_cancellations={("BTC", "101")},
            cancel_order=AsyncMock(return_value=False),
        )

        result = await adapter.cancel_order("101", "BTC")

        self.assertEqual(result.status, OrderStatus.PENDING)
        self.assertFalse(result.params["cancel_terminal"])

    async def test_order_history_delegates_to_rest(self):
        adapter = object.__new__(LighterAdapter)
        adapter._normalize_symbol = lambda symbol: symbol
        adapter._rest = SimpleNamespace(
            get_order_history=AsyncMock(return_value=[]),
        )

        result = await adapter.get_order_history("BTC", limit=25)

        self.assertEqual(result, [])
        adapter._rest.get_order_history.assert_awaited_once_with("BTC", limit=25)

    async def test_unresolved_submission_accessors_delegate_to_rest(self):
        adapter = object.__new__(LighterAdapter)
        pending = [{"client_order_id": "123"}]
        resolved = [SimpleNamespace(id="101", status=OrderStatus.OPEN)]
        adapter._rest = SimpleNamespace(
            get_unresolved_submissions=MagicMock(return_value=pending),
            resolve_unresolved_submissions=AsyncMock(return_value=resolved),
        )

        self.assertEqual(adapter.get_unresolved_submissions(), pending)
        self.assertEqual(await adapter.resolve_unresolved_submissions(), resolved)
        adapter._rest.get_unresolved_submissions.assert_called_once_with()
        adapter._rest.resolve_unresolved_submissions.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
