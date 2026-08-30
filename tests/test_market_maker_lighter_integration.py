from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import run_market_maker as entrypoint
from core.adapters.exchanges.exceptions import OrderSubmissionRejectedError
from core.adapters.exchanges.models import (
    ExchangeInfo,
    ExchangeType,
    OrderBookData,
    OrderBookLevel,
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.coordinator import MarketMakerCoordinator
from core.services.market_maker.models import RuntimeState


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


class FakeLighterAdapter:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.create_calls: list[tuple] = []
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.cancel_responses: list[OrderData] = []
        self.active: dict[str, OrderData] = {}
        self.history: list[OrderData] = []
        self._next_id = 0
        self.stop_event: asyncio.Event | None = None
        self.stop_after_creates: int | None = None

    def enable_market_maker_cancellation_outcomes(self) -> None:
        self.events.append(("enable_cancel_outcomes",))

    async def connect(self) -> bool:
        self.events.append(("connect",))
        return True

    async def authenticate(self) -> bool:
        self.events.append(("authenticate",))
        return True

    async def disconnect(self) -> None:
        self.events.append(("disconnect",))

    async def health_check(self) -> dict[str, bool]:
        self.events.append(("health",))
        return {"healthy": True}

    async def get_exchange_info(self) -> ExchangeInfo:
        self.events.append(("metadata",))
        return ExchangeInfo(
            name="Lighter",
            id="lighter",
            type=ExchangeType.PERPETUAL,
            supported_features=[],
            rate_limits={},
            precision={},
            fees={},
            markets={
                "BTC": {
                    "symbol": "BTC",
                    "price_decimals": 0,
                    "size_decimals": 3,
                    "min_base_amount": "0.001",
                    "min_quote_amount": "0.01",
                }
            },
            status="ok",
            timestamp=datetime.now(timezone.utc),
        )

    async def get_orderbook(self, symbol: str) -> OrderBookData:
        self.events.append(("book", symbol))
        return OrderBookData(
            symbol=symbol,
            bids=[OrderBookLevel(Decimal("99"), Decimal("1"))],
            asks=[OrderBookLevel(Decimal("101"), Decimal("1"))],
            timestamp=datetime.now(timezone.utc),
            nonce=1,
        )

    async def get_positions(self, symbols=None) -> list:
        self.events.append(("positions", tuple(symbols or ())))
        return []

    async def get_balances(self) -> list:
        self.events.append(("balances",))
        return [SimpleNamespace(currency="USDG", total=Decimal("300"))]

    async def get_account_trades(self, symbol: str, limit: int = 100) -> list:
        self.events.append(("account_trades", symbol, limit))
        return []

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderData]:
        snapshot = list(self.active.values())
        self.events.append(("open_orders", symbol, tuple(self.active)))
        return snapshot

    async def get_order_history(self, symbol: str | None = None) -> list[OrderData]:
        self.events.append(("order_history", symbol))
        return list(self.history)

    def get_unresolved_submissions(self) -> list:
        return []

    async def resolve_unresolved_submissions(self) -> list:
        self.events.append(("resolve_unresolved",))
        return []

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Decimal,
        params: dict,
    ) -> OrderData:
        self._next_id += 1
        order_id = str(self._next_id)
        self.create_calls.append((symbol, side, order_type, amount, price, dict(params)))
        self.events.append(("create", order_id, side))
        order = _order(order_id, side, price, amount, OrderStatus.OPEN, dict(params))
        self.active[order_id] = order
        if (
            self.stop_event is not None
            and self.stop_after_creates is not None
            and len(self.create_calls) >= self.stop_after_creates
        ):
            self.stop_event.set()
        return order

    async def cancel_order(
        self, order_id: str, symbol: str | None = None
    ) -> OrderData:
        self.cancel_calls.append((order_id, symbol))
        self.events.append(("cancel", order_id, symbol))
        live = self.active.pop(order_id)
        result = _order(
            order_id,
            live.side,
            live.price or Decimal("0"),
            live.amount,
            OrderStatus.CANCELED,
            {"cancel_terminal": True},
        )
        self.cancel_responses.append(result)
        self.history.append(result)
        return result

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        self.events.append(("cancel_all", symbol))
        self.active.clear()

    async def subscribe_orderbook(self, symbol: str, callback) -> None:
        self.events.append(("subscribe_orderbook", symbol, callback))
        await callback(await self.get_orderbook(symbol))

    async def subscribe_user_data(self, callback) -> None:
        self.events.append(("subscribe_user", callback))

    async def subscribe_positions(self, callback) -> None:
        self.events.append(("subscribe_positions", callback))

    async def unsubscribe(self) -> None:
        self.events.append(("unsubscribe",))


class MarketMakerLighterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def config(
        self, *, dry_run: bool, quote_mode: str = "both"
    ) -> MarketMakerConfig:
        return MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.001"),
            min_profit_buffer_bps=Decimal("0"),
            max_position=Decimal("0.010"),
            max_raw_spread_bps=Decimal("500"),
            min_order_lifetime_ms=1,
            refresh_interval_ms=1,
            dry_run=dry_run,
            quote_mode=quote_mode,
            account_audit_interval_seconds=60 if not dry_run else 0,
            max_session_drawdown=Decimal("1") if not dry_run else Decimal("0"),
            require_flat_start=not dry_run,
        )

    async def run_fake(self, *, dry_run: bool, quote_mode: str = "both"):
        adapter = FakeLighterAdapter()
        factory_in_loop = False

        def factory(settings):
            nonlocal factory_in_loop
            asyncio.get_running_loop()
            factory_in_loop = True
            self.assertEqual(settings["network"], "robinhood_testnet")
            return adapter

        stop = asyncio.Event()
        if dry_run:
            stop.set()
        else:
            adapter.stop_event = stop
            adapter.stop_after_creates = 2 if quote_mode == "both" else 1
        coordinator = await entrypoint.run_market_maker(
            self.config(dry_run=dry_run, quote_mode=quote_mode),
            {
                "network": "robinhood_testnet",
                "testnet": True,
                "api_key_private_key": "test-only",
                "account_index": 1,
                "api_key_index": 2,
                "expected_l1_address": None,
            },
            adapter_factory=factory,
            stop_event=stop,
        )
        return adapter, coordinator, factory_in_loop

    async def test_live_fake_covers_lifecycle_and_exact_order_contract(self) -> None:
        adapter, coordinator, factory_in_loop = await self.run_fake(dry_run=False)

        self.assertTrue(factory_in_loop)
        labels = [event[0] for event in adapter.events]
        for expected in (
            "enable_cancel_outcomes",
            "connect",
            "authenticate",
            "metadata",
            "health",
            "book",
            "positions",
            "subscribe_orderbook",
            "subscribe_user",
            "subscribe_positions",
            "unsubscribe",
            "disconnect",
        ):
            self.assertIn(expected, labels)
        self.assertEqual(coordinator.position_snapshot.signed_size, Decimal("0"))

        self.assertEqual(len(adapter.create_calls), 2)
        self.assertEqual(
            {call[1] for call in adapter.create_calls},
            {OrderSide.BUY, OrderSide.SELL},
        )
        for symbol, _, order_type, amount, _, params in adapter.create_calls:
            self.assertEqual(symbol, "BTC")
            self.assertIs(order_type, OrderType.LIMIT)
            self.assertEqual(amount, Decimal("0.001"))
            self.assertEqual(
                params,
                {"time_in_force": "POST_ONLY", "reduce_only": False},
            )

        created_ids = {str(index) for index in range(1, 3)}
        self.assertEqual({order_id for order_id, _ in adapter.cancel_calls}, created_ids)
        self.assertTrue(
            all(response.status is OrderStatus.CANCELED for response in adapter.cancel_responses)
        )
        self.assertTrue(
            all(response.params["cancel_terminal"] for response in adapter.cancel_responses)
        )
        self.assertEqual(adapter.active, {})
        self.assertEqual(adapter.events[-2:], [("unsubscribe",), ("disconnect",)])
        last_cancel = max(
            index
            for index, event in enumerate(adapter.events)
            if event[0] == "cancel"
        )
        final_empty_query = max(
            index
            for index, event in enumerate(adapter.events)
            if event[0] == "open_orders" and not event[2]
        )
        unsubscribe = next(
            index
            for index, event in enumerate(adapter.events)
            if event[0] == "unsubscribe"
        )
        self.assertLess(last_cancel, final_empty_query)
        self.assertLess(final_empty_query, unsubscribe)

    async def test_live_fake_single_side_places_and_cancels_one_slot(self) -> None:
        for quote_mode, expected_side in (
            ("bid_only", OrderSide.BUY),
            ("ask_only", OrderSide.SELL),
        ):
            with self.subTest(quote_mode=quote_mode):
                adapter, _, _ = await self.run_fake(
                    dry_run=False,
                    quote_mode=quote_mode,
                )

                self.assertEqual(
                    [call[1] for call in adapter.create_calls],
                    [expected_side],
                )
                self.assertEqual(
                    adapter.create_calls[0][5],
                    {"time_in_force": "POST_ONLY", "reduce_only": False},
                )
                self.assertEqual(len(adapter.cancel_calls), 1)
                self.assertTrue(adapter.cancel_responses[0].params["cancel_terminal"])
                self.assertEqual(adapter.active, {})

    async def test_dry_run_single_side_plans_one_slot_without_mutation(self) -> None:
        for quote_mode in ("bid_only", "ask_only"):
            with self.subTest(quote_mode=quote_mode):
                adapter, coordinator, _ = await self.run_fake(
                    dry_run=True,
                    quote_mode=quote_mode,
                )

                self.assertEqual(coordinator.metrics.counters["would_place"], 1)
                self.assertEqual(adapter.create_calls, [])
                self.assertEqual(adapter.cancel_calls, [])
                self.assertNotIn(
                    "cancel_all", [event[0] for event in adapter.events]
                )

    async def test_dry_run_performs_zero_exchange_mutations(self) -> None:
        adapter, _, _ = await self.run_fake(dry_run=True)

        self.assertEqual(adapter.create_calls, [])
        self.assertEqual(adapter.cancel_calls, [])
        self.assertNotIn("cancel_all", [event[0] for event in adapter.events])
        self.assertEqual(adapter.events[-2:], [("unsubscribe",), ("disconnect",)])

    async def test_wide_market_pauses_once_and_tight_book_recovers_without_rest_churn(
        self,
    ) -> None:
        adapter = FakeLighterAdapter()
        config = self.config(dry_run=False, quote_mode="bid_only")
        config = replace(
            config,
            max_raw_spread_bps=Decimal("100"),
            position_poll_interval_seconds=60,
            order_sync_interval_seconds=60,
            health_check_interval_seconds=60,
        )
        coordinator = MarketMakerCoordinator(adapter, config)

        tight = OrderBookData(
            symbol="BTC",
            bids=[
                OrderBookLevel(Decimal("90"), Decimal("1")),
                OrderBookLevel(Decimal("100"), Decimal("1")),
            ],
            asks=[
                OrderBookLevel(Decimal("110"), Decimal("1")),
                OrderBookLevel(Decimal("101"), Decimal("1")),
            ],
            timestamp=datetime.now(timezone.utc),
            nonce=2,
        )
        wide = await adapter.get_orderbook("BTC")

        try:
            await coordinator.start()
            self.assertEqual(coordinator.state, RuntimeState.PAUSED_MARKET)
            self.assertEqual(adapter.create_calls, [])

            await coordinator.on_orderbook(tight)
            await coordinator.run_one_cycle(force=True)
            self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
            self.assertEqual(len(adapter.create_calls), 1)

            await coordinator.on_orderbook(wide)
            await coordinator.run_one_cycle(force=True)
            self.assertEqual(coordinator.state, RuntimeState.PAUSED_MARKET)
            self.assertEqual(len(adapter.cancel_calls), 1)

            reads_before = [
                event
                for event in adapter.events
                if event[0] in {"health", "book", "positions", "open_orders"}
            ]
            await coordinator.on_orderbook(wide)
            await coordinator.run_one_cycle(force=True)
            reads_after = [
                event
                for event in adapter.events
                if event[0] in {"health", "book", "positions", "open_orders"}
            ]
            self.assertEqual(reads_after, reads_before)
            self.assertEqual(len(adapter.cancel_calls), 1)
            self.assertEqual(len(adapter.create_calls), 1)

            await coordinator.on_orderbook(tight)
            await coordinator.run_one_cycle(force=True)
            self.assertEqual(coordinator.state, RuntimeState.ACTIVE)
            self.assertEqual(len(adapter.create_calls), 2)
        finally:
            await coordinator.stop()


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

    async def test_limit_submission_passes_explicit_zero_integrator_fees(self) -> None:
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


class CliAndWalletProfileTests(unittest.TestCase):
    def test_cli_accepts_wallet_alias_and_single_shortcut(self) -> None:
        self.assertEqual(
            entrypoint.parse_cli(["mm.yaml", "--walletname", "desk"]).wallet_name,
            "desk",
        )
        self.assertEqual(entrypoint.parse_cli(["mm.yaml", "--desk"]).wallet_name, "desk")

    def test_profile_shortcuts_are_not_parsed_as_abbreviated_flags(self) -> None:
        abbreviated_debug = entrypoint.parse_cli(["mm.yaml", "--deb"])
        abbreviated_dry_run = entrypoint.parse_cli(["mm.yaml", "--dry"])
        exact_flags = entrypoint.parse_cli(
            ["mm.yaml", "--debug", "--dry-run"]
        )

        self.assertEqual(abbreviated_debug.wallet_name, "deb")
        self.assertFalse(abbreviated_debug.debug)
        self.assertEqual(abbreviated_dry_run.wallet_name, "dry")
        self.assertFalse(abbreviated_dry_run.dry_run)
        self.assertTrue(exact_flags.debug)
        self.assertTrue(exact_flags.dry_run)

    def test_cli_rejects_shortcut_with_explicit_wallet(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            entrypoint.parse_cli(
                ["mm.yaml", "--wallet-name", "desk", "--another"]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_profile_names_reject_path_traversal(self) -> None:
        for invalid in ("", "../desk", "desk/name", ".desk", "a" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                entrypoint._validate_profile_name(invalid)

    def test_named_profile_is_complete_and_does_not_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "desk.env").write_text(
                "\n".join(
                    (
                        "LIGHTER_API_KEY_PRIVATE_KEY='profile-private-key'",
                        "LIGHTER_ACCOUNT_INDEX=7",
                        "LIGHTER_API_KEY_INDEX=3",
                        "LIGHTER_NETWORK=robinhood_testnet",
                        "LIGHTER_EXPECTED_L1_ADDRESS=0x1111111111111111111111111111111111111111",
                    )
                ),
                encoding="utf-8",
            )
            with patch.object(
                entrypoint, "load_settings", side_effect=AssertionError("fallback used")
            ):
                settings = entrypoint.load_lighter_settings(
                    "desk", profiles_dir=directory
                )

        self.assertEqual(settings["network"], "robinhood_testnet")
        self.assertTrue(settings["testnet"])
        self.assertEqual(settings["account_index"], 7)
        self.assertEqual(settings["api_key_index"], 3)
        self.assertEqual(settings["api_key_private_key"], "profile-private-key")

    def test_profile_errors_never_echo_private_key(self) -> None:
        secret = "never-print-this-private-key"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "broken.env").write_text(
                f"LIGHTER_API_KEY_PRIVATE_KEY={secret}\n",
                encoding="utf-8",
            )
            with self.assertRaises(entrypoint.WalletProfileError) as raised:
                entrypoint.load_wallet_profile("broken", profiles_dir=directory)
        self.assertNotIn(secret, str(raised.exception))

    def test_default_adapter_disables_credential_file_loading(self) -> None:
        settings = {
            "network": "robinhood_testnet",
            "testnet": True,
            "api_key_private_key": "test-only",
            "account_index": 1,
            "api_key_index": 2,
        }
        with patch(
            "core.adapters.exchanges.adapters.lighter.LighterAdapter"
        ) as adapter_class:
            entrypoint.build_adapter(settings)
        config = adapter_class.call_args.args[0]
        self.assertIs(config.extra_params["load_credentials_from_file"], False)

    def test_adapter_does_not_read_default_yaml_when_loading_is_disabled(
        self,
    ) -> None:
        from core.adapters.exchanges.adapters.lighter import LighterAdapter

        settings = {
            "network": "robinhood_testnet",
            "testnet": True,
            "api_key_private_key": "profile-only-test-key",
            "account_index": 1,
            "api_key_index": 2,
        }
        rest = SimpleNamespace(
            signer_client=None,
            base_url="https://example.invalid",
            ws_url="wss://example.invalid",
        )
        with (
            patch.object(
                LighterAdapter,
                "_load_lighter_config",
                side_effect=AssertionError("default YAML must not be read"),
            ) as load_config,
            patch(
                "core.adapters.exchanges.adapters.lighter.LighterRest",
                return_value=rest,
            ),
            patch("core.adapters.exchanges.adapters.lighter.LighterWebSocket"),
            patch.object(
                LighterAdapter, "_get_symbol_cache_service", return_value=None
            ),
            patch(
                "core.adapters.exchanges.adapters.lighter.create_subscription_manager",
                return_value=SimpleNamespace(),
            ) as create_subscriptions,
        ):
            entrypoint.build_adapter(settings)

        load_config.assert_not_called()
        self.assertEqual(
            create_subscriptions.call_args.kwargs["exchange_config"],
            {
                "exchange_id": "lighter",
                "subscription_mode": {
                    "mode": "predefined",
                    "predefined": {"symbols": [], "data_types": {}},
                },
            },
        )

    def test_log_redaction_covers_messages_and_tracebacks(self) -> None:
        secret = "traceback-secret-private-key"
        stream = io.StringIO()
        logger = logging.getLogger("market-maker-redaction-test")
        previous_handlers = list(logger.handlers)
        previous_propagate = logger.propagate
        logger.handlers = [logging.StreamHandler(stream)]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        previous_factory = entrypoint._install_log_redaction(
            {"api_key_private_key": secret}
        )
        try:
            try:
                raise RuntimeError(secret)
            except RuntimeError:
                logger.exception("adapter failed with %s", secret)
        finally:
            logging.setLogRecordFactory(previous_factory)
            logger.handlers = previous_handlers
            logger.propagate = previous_propagate

        output = stream.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("<redacted>", output)

    def test_operator_keyboard_interrupt_is_a_clean_exit(self) -> None:
        logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)

        async def runtime(*_args, **_kwargs):
            return None

        def interrupt(awaitable):
            awaitable.close()
            raise KeyboardInterrupt

        with (
            patch.object(
                entrypoint,
                "load_market_maker_config",
                return_value=MarketMakerConfig(),
            ),
            patch.object(
                entrypoint,
                "load_lighter_settings",
                return_value={"api_key_private_key": "test-only"},
            ),
        ):
            exit_code = entrypoint.main(
                ["config.yaml"],
                run=interrupt,
                runtime=runtime,
                logger_factory=lambda _debug: logger,
            )

        self.assertEqual(exit_code, 0)

    def test_main_forces_dry_run_and_redacts_runtime_errors(self) -> None:
        secret = "runtime-secret-private-key"
        captured = {}

        class Logger:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def info(self, message: str, **kwargs) -> None:
                self.messages.append(message)

            def warning(self, message: str, **kwargs) -> None:
                self.messages.append(message)

            def error(self, message: str, **kwargs) -> None:
                self.messages.append(message)

        logger = Logger()

        async def failing_runtime(config, settings, **kwargs):
            captured["config"] = config
            raise RuntimeError(f"adapter failed with {settings['api_key_private_key']}")

        live_config = MarketMakerConfig(
            dry_run=False,
            account_audit_interval_seconds=60,
            max_session_drawdown=Decimal("1"),
            require_flat_start=True,
        )
        settings = {
            "network": "robinhood",
            "testnet": False,
            "api_key_private_key": secret,
            "account_index": 1,
            "api_key_index": 2,
        }
        stderr = io.StringIO()
        with (
            patch.object(entrypoint, "load_market_maker_config", return_value=live_config),
            patch.object(entrypoint, "load_lighter_settings", return_value=settings),
            redirect_stderr(stderr),
        ):
            code = entrypoint.main(
                ["ignored.yaml", "--dry-run"],
                runtime=failing_runtime,
                logger_factory=lambda debug: logger,
            )

        self.assertEqual(code, 1)
        self.assertTrue(captured["config"].dry_run)
        combined = stderr.getvalue() + "\n".join(logger.messages)
        self.assertIn("<redacted>", combined)
        self.assertNotIn(secret, combined)

    def test_main_preserves_config_mode_and_uses_injected_run_seams(self) -> None:
        captured = {}

        class Logger:
            def info(self, message: str, **kwargs) -> None:
                pass

            def warning(self, message: str, **kwargs) -> None:
                pass

            def error(self, message: str, **kwargs) -> None:
                pass

        async def successful_runtime(config, settings, **kwargs):
            captured["config"] = config
            captured["adapter_factory"] = kwargs["adapter_factory"]

        def factory(settings):
            return None
        runner_calls = 0

        def runner(awaitable):
            nonlocal runner_calls
            runner_calls += 1
            return asyncio.run(awaitable)

        live_config = MarketMakerConfig(
            dry_run=False,
            account_audit_interval_seconds=60,
            max_session_drawdown=Decimal("1"),
            require_flat_start=True,
        )
        with (
            patch.object(entrypoint, "load_market_maker_config", return_value=live_config),
            patch.object(entrypoint, "load_lighter_settings", return_value={}),
        ):
            code = entrypoint.main(
                ["ignored.yaml"],
                adapter_factory=factory,
                run=runner,
                runtime=successful_runtime,
                logger_factory=lambda debug: Logger(),
            )

        self.assertEqual(code, 0)
        self.assertFalse(captured["config"].dry_run)
        self.assertIs(captured["adapter_factory"], factory)
        self.assertEqual(runner_calls, 1)

    def test_main_rejects_live_when_account_audit_is_disabled(self) -> None:
        runtime = Mock()
        settings_loader = Mock(return_value={})
        stderr = io.StringIO()

        with (
            patch.object(
                entrypoint,
                "load_market_maker_config",
                return_value=MarketMakerConfig(dry_run=False),
            ),
            patch.object(
                entrypoint, "load_lighter_settings", settings_loader
            ),
            redirect_stderr(stderr),
        ):
            code = entrypoint.main(
                ["ignored.yaml"],
                runtime=runtime,
                logger_factory=lambda _debug: Mock(),
            )

        self.assertEqual(code, 1)
        self.assertIn("requires account_audit", stderr.getvalue())
        settings_loader.assert_not_called()
        runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
