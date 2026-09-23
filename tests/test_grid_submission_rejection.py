import asyncio
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lighter.exceptions import BadRequestException
from lighter.models.result_code import ResultCode
from lighter.nonce_manager import OptimisticNonceManager
from lighter.signer_client import SignerClient

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.exceptions import OrderSubmissionRejectedError
from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from core.services.grid.implementations.grid_engine_impl import GridEngineImpl
from core.services.grid.models import GridOrder, GridOrderSide, GridOrderStatus, GridType


class GridSubmissionRejectionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _rejection(code=21104):
        # Use the installed SDK's HTTP error model and nonce decorator. Only
        # native signing, HTTP, and account reads are replaced in these tests.
        return BadRequestException(
            status=400,
            data=ResultCode(code=code, message="invalid nonce"),
        )

    def _engine(self):
        signer = object.__new__(SignerClient)
        signer.nonce_manager = OptimisticNonceManager(7, SimpleNamespace(), [0])
        signer.nonce_manager.nonce[0] = 40
        signer.nonce_manager._fetch_nonce = AsyncMock(return_value=55)
        signer.sign_create_order = Mock(return_value=(14, "{}", "fixture-tx", None))
        signer.tx_api = SimpleNamespace(send_tx=AsyncMock())

        rest = object.__new__(LighterRest)
        rest.signer_client = signer
        rest._request_lock = asyncio.Lock()
        rest._next_request_at = 0.0
        rest._rate_limit_failures = 0
        rest._request_interval = 0.0
        rest._max_rate_limit_delay = 30.0
        rest._next_client_order_index = Mock(side_effect=[1800000000000, 1800000000001])
        rest._get_market_info = AsyncMock(return_value={
            "market_index": 1,
            "price_decimals": 1,
            "size_decimals": 5,
            "price_multiplier": Decimal("10"),
            "size_multiplier": Decimal("100000"),
            "min_base_amount": Decimal("0.00020"),
            "min_quote_amount": Decimal("10"),
        })
        rest._query_order_index = AsyncMock(return_value=987)
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.get_order_history = AsyncMock(return_value=[])

        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter.config = SimpleNamespace(exchange_id="lighter")
        adapter._normalize_symbol = Mock(side_effect=lambda symbol: symbol)
        engine = GridEngineImpl(adapter)
        engine.config = SimpleNamespace(
            exchange="lighter", symbol="SPY", grid_type=GridType.LONG,
        )
        engine._running = True
        engine.coordinator = SimpleNamespace(
            can_place_order_within_max_position=Mock(return_value=True),
            _grid_level_locks={},
            _request_fatal_stop=Mock(),
        )
        return engine, rest, signer

    @staticmethod
    def _order(side=GridOrderSide.SELL):
        return GridOrder(
            order_id="", grid_id=44, side=side,
            price=Decimal("769.4"), amount=Decimal("0.13000"),
            status=GridOrderStatus.PENDING, created_at=datetime.now(),
        )

    async def test_exact_nonce_rejection_refreshes_then_retries_once(self):
        engine, rest, signer = self._engine()
        signer.tx_api.send_tx.side_effect = [
            self._rejection(), SimpleNamespace(code=200, tx_hash="fixture-tx"),
        ]
        order = self._order(GridOrderSide.BUY)

        self.assertIs(await engine.place_order(order), order)

        self.assertEqual(signer.tx_api.send_tx.await_count, 2)
        signer.nonce_manager._fetch_nonce.assert_awaited_once_with(0)
        signed = signer.sign_create_order.call_args_list
        self.assertEqual([call.args[1] for call in signed], [1800000000000, 1800000000001])
        self.assertEqual([call.kwargs["nonce"] for call in signed], [41, 55])
        self.assertEqual(signed[0].args[2:], signed[1].args[2:])
        self.assertEqual(order.order_id, "987")
        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertEqual(rest.get_unresolved_submissions(), [])
        self.assertFalse(engine._placements_paused)
        self.assertEqual(engine._inflight_placements, 0)
        self.assertEqual(engine.coordinator.can_place_order_within_max_position.call_count, 2)
        engine.coordinator._request_fatal_stop.assert_not_called()

    async def test_repeated_rejection_leaves_no_phantom_order_for_cleanup(self):
        engine, rest, signer = self._engine()
        signer.tx_api.send_tx.side_effect = self._rejection()
        order = self._order()

        with self.assertRaisesRegex(OrderSubmissionRejectedError, "invalid nonce"):
            await engine.place_order(order)

        self.assertEqual(signer.tx_api.send_tx.await_count, 2)
        self.assertEqual(order.status, GridOrderStatus.FAILED)
        self.assertEqual(engine.get_pending_orders(), [])
        self.assertEqual(rest.get_unresolved_submissions(), [])
        self.assertEqual(engine._inflight_placements, 0)
        rest.get_open_orders.assert_not_awaited()
        rest.get_order_history.assert_not_awaited()

    async def test_closing_retry_ack_tracks_second_client_until_exact_order_proof(self):
        engine, rest, signer = self._engine()
        signer.tx_api.send_tx.side_effect = [
            self._rejection(), SimpleNamespace(code=200, tx_hash="fixture-tx"),
        ]
        order = self._order()

        self.assertIs(await engine.place_order(order), order)

        self.assertEqual(signer.tx_api.send_tx.await_count, 2)
        for call in signer.sign_create_order.call_args_list:
            self.assertTrue(call.args[4])  # is_ask
            self.assertEqual(call.args[6], SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME)
            self.assertTrue(call.args[7])  # reduce_only
        self.assertTrue(order.exchange_data["submission_acknowledged"])
        self.assertTrue(order.exchange_data["submission_uncertain"])
        self.assertEqual(
            [row["client_order_id"] for row in rest.get_unresolved_submissions()],
            ["1800000000001"],
        )
        self.assertNotIn("1800000000000", engine._pending_orders)
        self.assertFalse(engine._placements_paused)
        engine.coordinator._request_fatal_stop.assert_not_called()
        rest._query_order_index.assert_not_awaited()

        rest.get_open_orders.return_value = [OrderData(
            id="987", client_id="1800000000001", symbol="SPY",
            side=OrderSide.SELL, type=OrderType.LIMIT,
            amount=order.amount, price=order.price,
            filled=Decimal("0"), remaining=order.amount, cost=Decimal("0"),
            average=None, status=OrderStatus.OPEN, timestamp=datetime.now(),
            updated=None, fee=None, trades=[], params={}, raw_data={},
        )]
        self.assertEqual(await engine._resolve_unresolved_submissions_read_only(), (["987"], []))

        self.assertEqual(order.order_id, "987")
        self.assertFalse(order.exchange_data["submission_uncertain"])
        self.assertEqual(rest.get_unresolved_submissions(), [])
        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertFalse(engine._placements_paused)
        engine.coordinator._request_fatal_stop.assert_not_called()
        self.assertEqual(signer.tx_api.send_tx.await_count, 2)

    async def test_retry_rechecks_shutdown_pause_running_and_exposure(self):
        for gate in ("shutdown", "pause", "running", "exposure"):
            with self.subTest(gate=gate):
                engine, rest, signer = self._engine()
                order = self._order(GridOrderSide.BUY)

                async def reject_and_close_gate(**kwargs):
                    if gate == "shutdown":
                        engine._shutting_down = True
                    elif gate == "pause":
                        engine.pause_placements()
                    elif gate == "running":
                        engine._running = False
                    else:
                        engine.coordinator.can_place_order_within_max_position.return_value = False
                    raise self._rejection()

                signer.tx_api.send_tx.side_effect = reject_and_close_gate

                self.assertIsNone(await engine.place_order(order))

                signer.tx_api.send_tx.assert_awaited_once()
                self.assertEqual(engine.get_pending_orders(), [])
                self.assertEqual(rest.get_unresolved_submissions(), [])
                self.assertEqual(engine._inflight_placements, 0)

    async def test_timeout_and_unrecognized_rejection_remain_uncertain_without_retry(self):
        for error in (TimeoutError("fixture timeout"), self._rejection(code=21105)):
            with self.subTest(error=type(error).__name__):
                engine, rest, signer = self._engine()
                signer.tx_api.send_tx.side_effect = error
                order = self._order()

                with patch("asyncio.sleep", new=AsyncMock()):
                    self.assertIs(await engine.place_order(order), order)

                signer.tx_api.send_tx.assert_awaited_once()
                self.assertTrue(order.exchange_data["submission_uncertain"])
                self.assertEqual(engine.get_pending_orders(), [order])
                self.assertEqual(
                    [row["client_order_id"] for row in rest.get_unresolved_submissions()],
                    ["1800000000000"],
                )
                self.assertTrue(engine._placements_paused)
                engine.coordinator._request_fatal_stop.assert_called_once()

    async def test_timeout_on_retry_retains_only_the_second_submission(self):
        engine, rest, signer = self._engine()
        signer.tx_api.send_tx.side_effect = [
            self._rejection(), TimeoutError("fixture retry timeout"),
        ]
        order = self._order()

        with patch("asyncio.sleep", new=AsyncMock()):
            self.assertIs(await engine.place_order(order), order)

        self.assertEqual(signer.tx_api.send_tx.await_count, 2)
        self.assertTrue(order.exchange_data["submission_uncertain"])
        self.assertEqual(
            [row["client_order_id"] for row in rest.get_unresolved_submissions()],
            ["1800000000001"],
        )
        self.assertEqual(engine.get_pending_orders(), [order])
        self.assertEqual(engine._inflight_placements, 0)
        engine.coordinator._request_fatal_stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
