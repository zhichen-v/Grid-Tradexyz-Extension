"""Lighter SDK boundary and WebSocket lifecycle regressions; no account calls."""

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.adapters.exchanges.exceptions import OrderSubmissionRejectedError
from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket


def _order(
    order_id: str,
    side: OrderSide,
    price: Decimal,
    amount: Decimal,
    status: OrderStatus,
    params: dict | None = None,
) -> OrderData:
    return OrderData(
        id=order_id,
        client_id=f"client-{order_id}",
        symbol="BTC",
        side=side,
        type=OrderType.LIMIT,
        amount=amount,
        price=price,
        filled=Decimal("0"),
        remaining=Decimal("0") if status is OrderStatus.CANCELED else amount,
        cost=Decimal("0"),
        average=None,
        status=status,
        timestamp=datetime.now(timezone.utc),
        updated=None,
        fee=None,
        trades=[],
        params=params or {},
        raw_data=params or {},
    )


class LighterRateLimitBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _submission_rest(result) -> LighterRest:
        rest = object.__new__(LighterRest)
        rest._validate_order_preconditions = Mock(return_value=True)
        rest._get_market_info = AsyncMock(
            return_value={"price_decimals": 1}
        )
        rest._validate_order_minimums = Mock()
        rest._next_client_order_index = Mock(return_value=1)
        rest._convert_limit_order_params = Mock(return_value={})
        rest._call_api = AsyncMock(return_value=result)
        rest._handle_ambiguous_order_submission = AsyncMock(
            return_value=SimpleNamespace(id="uncertain")
        )
        rest.signer_client = SimpleNamespace(create_order=Mock())
        return rest

    async def test_invalid_nonce_is_a_sanitized_definitive_rejection(
        self,
    ) -> None:
        rest = self._submission_rest(
            (
                None,
                None,
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={}",
            )
        )

        with self.assertRaisesRegex(
            OrderSubmissionRejectedError,
            "invalid nonce",
        ) as raised:
            await rest.place_order(
                "BTC",
                "sell",
                "limit",
                Decimal("0.00020"),
                Decimal("77912.4"),
                reduce_only=True,
                time_in_force="POST_ONLY",
                _raise_on_definitive_submission_rejection=True,
            )

        self.assertEqual(
            str(raised.exception),
            "order submission rejected: invalid nonce",
        )
        rest._handle_ambiguous_order_submission.assert_not_awaited()

    async def test_invalid_nonce_rejection_requires_exact_tuple_and_code(
        self,
    ) -> None:
        cases = (
            (
                "default-off",
                None,
                None,
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={}",
                False,
            ),
            (
                "wrong-code",
                None,
                None,
                "HTTP response body: code=21105 message='invalid nonce' "
                "additional_properties={}",
                True,
            ),
            (
                "wrong-message",
                None,
                None,
                "HTTP response body: code=21104 message='nonce unavailable' "
                "additional_properties={}",
                True,
            ),
            (
                "extra-payload",
                None,
                None,
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={} extra",
                True,
            ),
        )
        for label, tx, response, error, opt_in in cases:
            with self.subTest(label=label):
                rest = self._submission_rest((tx, response, error))

                result = await rest.place_order(
                    "BTC",
                    "sell",
                    "limit",
                    Decimal("0.00020"),
                    Decimal("77912.4"),
                    reduce_only=True,
                    time_in_force="POST_ONLY",
                    _raise_on_definitive_submission_rejection=opt_in,
                )

                self.assertEqual(result.id, "uncertain")
                rest._handle_ambiguous_order_submission.assert_awaited_once()

    async def test_invalid_order_expiry_without_provenance_remains_ambiguous(
        self,
    ) -> None:
        rest = self._submission_rest((None, None, "OrderExpiry is invalid"))

        result = await rest.place_order(
            "BTC",
            "buy",
            "limit",
            Decimal("0.00020"),
            Decimal("78127.5"),
            reduce_only=True,
            time_in_force="IOC",
            _raise_on_definitive_pre_send_failure=True,
        )

        self.assertEqual(result.id, "uncertain")
        rest._handle_ambiguous_order_submission.assert_awaited_once()

    async def test_fast_fill_order_index_falls_back_to_exact_history(
        self,
    ) -> None:
        rest = object.__new__(LighterRest)
        rest.get_open_orders = AsyncMock(return_value=[])
        filled = replace(
            _order(
                "987",
                OrderSide.SELL,
                Decimal("72444.6"),
                Decimal("0.00020"),
                OrderStatus.FILLED,
            ),
            client_id="42",
            filled=Decimal("0.00020"),
            remaining=Decimal("0"),
        )
        rest.get_order_history = AsyncMock(return_value=[filled])

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            order_id = await rest._query_order_index(
                "BTC",
                "sell",
                Decimal("72444.6"),
                Decimal("0.00020"),
                client_order_id=42,
                max_retries=3,
                retry_delay=0,
            )

        self.assertEqual(order_id, "987")
        self.assertEqual(rest.get_open_orders.await_count, 3)
        rest.get_order_history.assert_awaited_once_with("BTC", limit=100)

    def test_post_only_maps_to_lighter_sdk_constant(self) -> None:
        import lighter

        rest = object.__new__(LighterRest)
        rest._convert_base_amount = Mock(return_value=2)
        rest._next_client_order_index = Mock(return_value=7)

        params = rest._convert_limit_order_params(
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            Decimal("0.00020"),
            Decimal("65000.0"),
            "buy",
            time_in_force="POST_ONLY",
            reduce_only=False,
        )

        self.assertEqual(
            params["time_in_force"],
            lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
        )
        self.assertEqual(
            params["order_type"], lighter.SignerClient.ORDER_TYPE_LIMIT
        )
        self.assertFalse(params["is_ask"])
        self.assertEqual(params["integrator_account_index"], 0)
        self.assertEqual(params["integrator_taker_fee"], 0)
        self.assertEqual(params["integrator_maker_fee"], 0)

    def test_active_ioc_reduce_only_maps_to_lighter_sdk_constant(self) -> None:
        import lighter

        rest = object.__new__(LighterRest)
        rest._convert_base_amount = Mock(return_value=2)
        rest._next_client_order_index = Mock(return_value=7)

        params = rest._convert_limit_order_params(
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            Decimal("0.00020"),
            Decimal("65000.0"),
            "buy",
            time_in_force="IOC",
            reduce_only=True,
        )

        self.assertEqual(
            params["time_in_force"],
            lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
        )
        self.assertEqual(
            params["order_expiry"], lighter.SignerClient.DEFAULT_IOC_EXPIRY
        )
        self.assertEqual(
            params["order_type"], lighter.SignerClient.ORDER_TYPE_LIMIT
        )
        self.assertTrue(params["reduce_only"])

        post_only_params = rest._convert_limit_order_params(
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            Decimal("0.00020"),
            Decimal("65000.0"),
            "sell",
            time_in_force="POST_ONLY",
        )
        self.assertNotIn("order_expiry", post_only_params)

    async def test_limit_submission_passes_explicit_zero_integrator_fees(self) -> None:
        import lighter

        rest = object.__new__(LighterRest)
        rest._convert_base_amount = Mock(return_value=2)
        rest._next_client_order_index = Mock(return_value=7)
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(return_value=(None, None, None))
        )

        async def call_api(_operation, request, **_kwargs):
            return await request()

        rest._call_api = call_api
        rest._handle_order_result = AsyncMock(return_value=SimpleNamespace(id="1"))

        await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("65000.0"),
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            time_in_force="POST_ONLY",
        )

        submitted = rest.signer_client.create_order.await_args.kwargs
        self.assertEqual(submitted["integrator_account_index"], 0)
        self.assertEqual(submitted["integrator_taker_fee"], 0)
        self.assertEqual(submitted["integrator_maker_fee"], 0)
        self.assertNotIn("order_expiry", submitted)

        await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("65000.0"),
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            time_in_force="IOC",
        )

        submitted = rest.signer_client.create_order.await_args.kwargs
        self.assertEqual(
            submitted["order_expiry"], lighter.SignerClient.DEFAULT_IOC_EXPIRY
        )

    async def test_order_submission_429_is_sanitized_and_propagated(self) -> None:
        rest = object.__new__(LighterRest)
        rest._validate_order_preconditions = Mock(return_value=True)
        rest._get_market_info = AsyncMock(
            return_value={"price_decimals": 1}
        )
        rest._validate_order_minimums = Mock()
        rest._next_client_order_index = Mock(return_value=1)
        rest._convert_limit_order_params = Mock(return_value={})
        rest._call_api = AsyncMock(
            side_effect=RuntimeError("HTTP 429 test-secret-payload")
        )
        rest.signer_client = SimpleNamespace(create_order=Mock())

        with self.assertRaisesRegex(RuntimeError, "HTTP 429") as raised:
            await rest.place_order(
                "BTC",
                "buy",
                "limit",
                Decimal("0.2"),
                Decimal("100"),
            )

        self.assertNotIn("test-secret-payload", str(raised.exception))

    async def test_order_cancellation_429_is_sanitized_and_propagated(self) -> None:
        rest = object.__new__(LighterRest)
        rest.signer_client = SimpleNamespace(cancel_order=Mock())
        rest._uncertain_cancellations = set()
        rest.get_market_index = Mock(return_value=1)
        rest._call_api = AsyncMock(
            side_effect=RuntimeError("HTTP 429 test-secret-payload")
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 429") as raised:
            await rest.cancel_order("BTC", "1")

        self.assertNotIn("test-secret-payload", str(raised.exception))


class LighterWebSocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_ws_stop_cancels_connecting_async_client(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Client:
            ws = None

            @staticmethod
            async def run_async() -> None:
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        websocket = object.__new__(LighterWebSocket)
        websocket.ws_client = Client()
        websocket._ws_task = asyncio.create_task(websocket._run_ws_client())
        websocket._stopping_sdk_ws = False
        websocket._connected = True
        websocket._explicit_stop = True
        await asyncio.wait_for(started.wait(), timeout=0.2)

        await asyncio.wait_for(
            websocket._stop_sdk_ws_client(), timeout=0.2
        )

        self.assertTrue(cancelled.is_set())
        self.assertIsNone(websocket._ws_task)
        self.assertIsNone(websocket.ws_client)

    async def test_sdk_ws_stop_awaits_connected_async_close(self) -> None:
        closed = asyncio.Event()

        class Connection:
            @staticmethod
            async def close() -> None:
                closed.set()

        class Client:
            ws = Connection()

            @staticmethod
            async def run_async() -> None:
                await closed.wait()

        websocket = object.__new__(LighterWebSocket)
        websocket.ws_client = Client()
        websocket._ws_task = asyncio.create_task(websocket._run_ws_client())
        websocket._stopping_sdk_ws = False
        websocket._connected = True
        websocket._explicit_stop = True

        await asyncio.wait_for(
            websocket._stop_sdk_ws_client(), timeout=0.2
        )

        self.assertTrue(closed.is_set())
        self.assertIsNone(websocket._ws_task)
        self.assertIsNone(websocket.ws_client)

    async def test_sdk_ws_stop_cancellation_does_not_orphan_stream(self) -> None:
        close_started = asyncio.Event()
        stream_cancelled = asyncio.Event()

        class Connection:
            @staticmethod
            async def close() -> None:
                close_started.set()
                await asyncio.Future()

        class Client:
            ws = Connection()

            @staticmethod
            async def run_async() -> None:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    stream_cancelled.set()
                    raise

        websocket = object.__new__(LighterWebSocket)
        websocket.ws_client = Client()
        stream_task = asyncio.create_task(websocket._run_ws_client())
        websocket._ws_task = stream_task
        websocket._stopping_sdk_ws = False
        websocket._connected = True
        websocket._explicit_stop = True
        stop_task = asyncio.create_task(websocket._stop_sdk_ws_client())

        try:
            await asyncio.wait_for(close_started.wait(), timeout=0.2)
            stop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task

            self.assertTrue(stream_task.done())
            self.assertTrue(stream_cancelled.is_set())
            self.assertIsNone(websocket._ws_task)
            self.assertIsNone(websocket.ws_client)
        finally:
            if not stream_task.done():
                stream_task.cancel()
                await stream_task



if __name__ == "__main__":
    unittest.main()
