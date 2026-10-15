import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.adapters.exchanges.models import OrderSide, PositionSide
from core.services.grid.implementations.order_health_checker import OrderHealthChecker
from core.services.grid.models import GridConfig, GridOrderSide, GridType


def _config(grid_type, mode=None):
    kwargs = {}
    if mode is not None:
        kwargs["position_repair_mode"] = mode
    return GridConfig(
        exchange="lighter",
        symbol="BTC",
        grid_type=grid_type,
        grid_interval=Decimal("50"),
        order_amount=Decimal("0.001"),
        lower_price=Decimal("83000"),
        upper_price=Decimal("84000"),
        quantity_precision=5,
        **kwargs,
    )


def _order(side, price, amount="0.001"):
    return SimpleNamespace(
        id=f"{side}-{price}",
        client_id=None,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        price=Decimal(price),
        amount=Decimal(amount),
        filled=Decimal("0"),
        remaining=Decimal(amount),
    )


def _position(signed_size):
    size = Decimal(signed_size)
    return SimpleNamespace(
        symbol="BTC",
        side=PositionSide.LONG if size > 0 else PositionSide.SHORT,
        size=abs(size),
    )


class TakeProfitRestoreRepairTests(unittest.IsolatedAsyncioTestCase):
    def _checker(self, config, current_price, locks=None):
        engine = SimpleNamespace(
            placement_epoch=0,
            exchange=SimpleNamespace(),
            coordinator=SimpleNamespace(
                _grid_level_locks={} if locks is None else locks,
                _active_fill_callbacks=0,
                _last_fill_time=0,
                state=None,
            ),
            get_pending_orders=lambda: [],
            get_current_price=AsyncMock(return_value=Decimal(current_price)),
            place_market_order=AsyncMock(),
            place_batch_orders=AsyncMock(side_effect=lambda orders, **_: list(orders)),
            place_order=AsyncMock(side_effect=lambda order, **_: order),
        )
        checker = OrderHealthChecker(config, engine)
        checker.logger = MagicMock()
        checker._health_cycle_placement_epoch = 0
        return checker, engine

    async def _confirm(self, checker, exchange_orders, positions):
        first = await checker._repair_position_if_confirmed(exchange_orders, positions)
        self.assertFalse(first)
        return await checker._repair_position_if_confirmed(exchange_orders, positions)

    def _long_snapshot(self):
        # Market 83180; buy fills at 83200-83350 lost their TPs while TPs for
        # 83400/83450 survive, leaving 0.004 uncovered across four orphaned slots.
        exchange_orders = [
            _order("buy", "83150"),
            _order("buy", "83100"),
            _order("sell", "83450"),
            _order("sell", "83500"),
        ]
        return exchange_orders, [_position("0.006")]

    def _short_snapshot(self):
        exchange_orders = [
            _order("sell", "83850"),
            _order("sell", "83900"),
            _order("buy", "83550"),
            _order("buy", "83500"),
        ]
        return exchange_orders, [_position("-0.006")]

    async def test_default_mode_keeps_market_close(self):
        config = _config(GridType.LONG)
        self.assertEqual(config.position_repair_mode, "market_close")
        checker, engine = self._checker(config, "83180")

        self.assertTrue(await self._confirm(checker, *self._long_snapshot()))

        engine.place_batch_orders.assert_not_awaited()
        engine.place_market_order.assert_awaited_once()
        kwargs = engine.place_market_order.await_args.kwargs
        self.assertEqual(kwargs["side"], GridOrderSide.SELL)
        self.assertEqual(kwargs["amount"], Decimal("0.004"))
        self.assertTrue(kwargs["reduce_only"])

    async def test_long_restores_orphaned_take_profits(self):
        checker, engine = self._checker(
            _config(GridType.LONG, "restore_take_profit"),
            "83180",
        )

        self.assertTrue(await self._confirm(checker, *self._long_snapshot()))

        engine.place_market_order.assert_not_awaited()
        placed = engine.place_batch_orders.await_args.args[0]
        self.assertEqual(
            [(o.side, o.price, o.amount) for o in placed],
            [
                (GridOrderSide.SELL, Decimal(price), Decimal("0.001"))
                for price in ("83250", "83300", "83350", "83400")
            ],
        )
        self.assertTrue(all(o.parent_order_id for o in placed))
        locks = engine.coordinator._grid_level_locks
        self.assertEqual(
            {grid_id: lock["tp_price"] for grid_id, lock in locks.items()},
            {o.grid_id: o.price for o in placed},
        )
        self.assertEqual(checker._position_drift_cycles, 0)

    async def test_short_restores_orphaned_take_profits(self):
        checker, engine = self._checker(
            _config(GridType.SHORT, "restore_take_profit"),
            "83820",
        )

        self.assertTrue(await self._confirm(checker, *self._short_snapshot()))

        engine.place_market_order.assert_not_awaited()
        placed = engine.place_batch_orders.await_args.args[0]
        self.assertEqual(
            sorted((o.side, o.price) for o in placed),
            [
                (GridOrderSide.BUY, Decimal(price))
                for price in ("83600", "83650", "83700", "83750")
            ],
        )

    async def test_unexplained_excess_falls_back_to_market_close(self):
        checker, engine = self._checker(
            _config(GridType.LONG, "restore_take_profit"),
            "83180",
        )
        exchange_orders, _ = self._long_snapshot()

        self.assertTrue(await self._confirm(checker, exchange_orders, [_position("0.005")]))

        engine.place_batch_orders.assert_not_awaited()
        engine.place_market_order.assert_awaited_once()
        self.assertEqual(
            engine.place_market_order.await_args.kwargs["amount"],
            Decimal("0.003"),
        )

    async def test_no_live_take_profit_anchor_falls_back_to_market_close(self):
        checker, engine = self._checker(
            _config(GridType.LONG, "restore_take_profit"),
            "83180",
        )
        exchange_orders = [_order("buy", "83150")]

        self.assertTrue(await self._confirm(checker, exchange_orders, [_position("0.004")]))

        engine.place_batch_orders.assert_not_awaited()
        engine.place_market_order.assert_awaited_once()

    async def test_conflicting_lock_falls_back_to_market_close(self):
        config = _config(GridType.LONG, "restore_take_profit")
        grid_id = config.get_grid_index_by_price(Decimal("83250"))
        checker, engine = self._checker(
            config,
            "83180",
            locks={grid_id: {"tp_side": "sell", "tp_price": Decimal("83350")}},
        )

        self.assertTrue(await self._confirm(checker, *self._long_snapshot()))

        engine.place_batch_orders.assert_not_awaited()
        engine.place_market_order.assert_awaited_once()

    async def test_open_restore_circuit_defers_without_market_order(self):
        checker, engine = self._checker(
            _config(GridType.LONG, "restore_take_profit"),
            "83180",
        )
        engine._restore_circuit_retry_after = lambda order: (
            30.0 if order.price == Decimal("83300") else 0.0
        )

        self.assertFalse(await self._confirm(checker, *self._long_snapshot()))

        engine.place_batch_orders.assert_not_awaited()
        engine.place_market_order.assert_not_awaited()
        self.assertEqual(engine.coordinator._grid_level_locks, {})

    async def test_stale_health_cycle_defers_without_orders(self):
        checker, engine = self._checker(
            _config(GridType.LONG, "restore_take_profit"),
            "83180",
        )
        exchange_orders, positions = self._long_snapshot()
        self.assertFalse(await checker._repair_position_if_confirmed(exchange_orders, positions))
        engine.placement_epoch = 1

        self.assertFalse(await checker._repair_position_if_confirmed(exchange_orders, positions))

        engine.place_batch_orders.assert_not_awaited()
        engine.place_market_order.assert_not_awaited()

    async def test_repeated_placement_failure_falls_back_to_market_close(self):
        checker, engine = self._checker(
            _config(GridType.LONG, "restore_take_profit"),
            "83180",
        )
        engine.place_batch_orders = AsyncMock(return_value=[])
        exchange_orders, positions = self._long_snapshot()

        self.assertFalse(await self._confirm(checker, exchange_orders, positions))
        self.assertFalse(await checker._repair_position_if_confirmed(exchange_orders, positions))
        engine.place_market_order.assert_not_awaited()
        self.assertEqual(engine.coordinator._grid_level_locks, {})

        self.assertTrue(await checker._repair_position_if_confirmed(exchange_orders, positions))

        self.assertEqual(engine.place_batch_orders.await_count, 3)
        engine.place_market_order.assert_awaited_once()

    def test_invalid_repair_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "position_repair_mode"):
            _config(GridType.LONG, "flatten")


if __name__ == "__main__":
    unittest.main()
