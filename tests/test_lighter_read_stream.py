import asyncio
import copy
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_read_stream import LighterReadStream
from core.adapters.exchanges.adapters.lighter_rest import LighterRest


def book_message(*, initial=True, nonce=10, offset=20):
    return {"type": "subscribed/order_book" if initial else "update/order_book",
            "channel": "order_book:0", "timestamp": 1000, "offset": offset,
            "order_book": {"code": 0, "nonce": nonce, "begin_nonce": nonce - 1,
                           "offset": offset, "last_updated_at": 999,
                           "bids": [{"price": "99", "size": "2"}],
                           "asks": [{"price": "101", "size": "3"}]}}


def account_message(channel="account_all"):
    result = {"type": f"subscribed/{channel}", "channel": f"{channel}:7"}
    if channel == "account_all_orders":
        result["orders"] = {}
    else:
        result.update(account=7, assets={}, positions={}, shares=[], trades={},
                      funding_histories={}, total_trades_count=10)
    return result


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.reply = account_message
        self.orders_subscribed = False
        self.unsubscribe_reply = lambda: {"type": "unsubscribed", "channel": "account_all_orders:7"}

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "unsubscribe":
            self.orders_subscribed = False
            response = self.unsubscribe_reply()
            if response is not None:
                await self.push(response)
            return
        if message["type"] != "subscribe":
            return
        channel = message["channel"].split("/")[0]
        if channel == "account_all_orders":
            if self.orders_subscribed:
                await self.push({"code": 30003})
                return
            self.orders_subscribed = True
        response = book_message() if channel == "order_book" else self.reply(channel)
        if response is not None:
            await self.push(response)

    async def push(self, value):
        await self.queue.put(json.dumps(value))

    async def close(self):
        self.closed = True


class LighterReadStreamTests(unittest.IsolatedAsyncioTestCase):
    async def make_stream(self, **kwargs):
        socket = Socket()
        connection = AsyncMock(return_value=socket)
        kwargs.setdefault("wall_clock", lambda: 1)
        kwargs.setdefault("clock", lambda: 42.5)
        stream = LighterReadStream("wss://api.rh.lighter.xyz/stream", 7, 0,
                                   lambda: "test-auth", connect_factory=connection,
                                   timeout=0.1, **kwargs)
        await stream.start()
        self.addAsyncCleanup(stream.close)
        return stream, socket, connection

    async def flush(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_fresh_snapshots_preserve_account_fields_and_empty_orders(self):
        stream, socket, connection = await self.make_stream()
        first = await stream.request_snapshot("account_all")
        orders = await stream.request_snapshot("account_all_orders")
        self.assertEqual(first["total_trades_count"], 10)
        self.assertEqual(first["_received_monotonic"], 42.5)
        self.assertEqual(orders["orders"], {})
        self.assertNotIn("auth", first)
        self.assertNotIn("auth", orders)
        self.assertEqual(socket.sent[-1]["auth"], "test-auth")
        self.assertNotIn("auth", socket.sent[-2])
        connection.assert_awaited_once()
        self.assertEqual(connection.call_args.kwargs["ping_interval"], 30)

    async def test_updates_are_not_a_snapshot_or_account_merge(self):
        stream, socket, _ = await self.make_stream()
        socket.reply = lambda _: None
        pending = asyncio.create_task(stream.request_snapshot("account_all"))
        await self.flush()
        update = account_message()
        update["type"] = "update/account_all"
        update["total_trades_count"] = 999
        await socket.push(update)
        await self.flush()
        self.assertFalse(pending.done())
        await socket.push(account_message())
        self.assertEqual((await pending)["total_trades_count"], 10)

    async def test_snapshot_requests_are_serialized(self):
        stream, socket, _ = await self.make_stream()
        socket.reply = lambda _: None
        first = asyncio.create_task(stream.request_snapshot("account_all"))
        second = asyncio.create_task(stream.request_snapshot("account_all_orders"))
        await self.flush()
        self.assertEqual(len(socket.sent), 2)
        await socket.push(account_message())
        await first
        await self.flush()
        self.assertEqual(len(socket.sent), 3)
        await socket.push(account_message("account_all_orders"))
        self.assertEqual((await second)["orders"], {})

    async def test_repeated_order_read_unsubscribes_then_requires_a_new_full_snapshot(self):
        stream, socket, _ = await self.make_stream()
        self.assertEqual((await stream.request_snapshot("account_all_orders"))["orders"], {})
        second_snapshot = account_message("account_all_orders")
        second_snapshot["orders"] = {"0": [{"order_index": 55}]}
        socket.reply = lambda _: second_snapshot
        self.assertEqual((await stream.request_snapshot("account_all_orders"))["orders"],
                         {"0": [{"order_index": 55}]})
        self.assertEqual([message["type"] for message in socket.sent],
                         ["subscribe", "subscribe", "unsubscribe", "subscribe"])
        self.assertNotIn("auth", socket.sent[-2])
        self.assertEqual(socket.sent[-1]["auth"], "test-auth")
        self.assertTrue(stream.transport_healthy)

    async def test_orders_wait_for_matching_unsubscribe_ack_before_resubscribing(self):
        stream, socket, _ = await self.make_stream()
        await stream.request_snapshot("account_all_orders")
        socket.unsubscribe_reply = lambda: None
        pending = asyncio.create_task(stream.request_snapshot("account_all_orders"))
        await self.flush()
        self.assertEqual(socket.sent[-1]["type"], "unsubscribe")
        self.assertFalse(pending.done())
        await socket.push({"type": "unsubscribed", "channel": "account_all_orders:7"})
        self.assertEqual((await pending)["orders"], {})
        self.assertEqual(socket.sent[-1]["type"], "subscribe")

    async def test_unsubscribe_timeout_or_wrong_ack_invalidates_without_retry(self):
        for response in (None, {"type": "unsubscribed", "channel": "account_all_orders:8"},
                         account_message("account_all_orders")):
            stream, socket, _ = await self.make_stream()
            await stream.request_snapshot("account_all_orders")
            socket.unsubscribe_reply = lambda response=response: response
            with self.assertRaises(RuntimeError):
                await stream.request_snapshot("account_all_orders")
            self.assertFalse(stream.transport_healthy)
            self.assertEqual([message["type"] for message in socket.sent],
                             ["subscribe", "subscribe", "unsubscribe"])
        stream, socket, _ = await self.make_stream()
        await socket.push({"type": "unsubscribed", "channel": "account_all_orders:7"})
        await self.flush()
        self.assertFalse(stream.transport_healthy)

    async def test_account_all_repeated_snapshot_does_not_unsubscribe(self):
        stream, socket, _ = await self.make_stream()
        await stream.request_snapshot("account_all")
        await stream.request_snapshot("account_all")
        self.assertEqual([message["type"] for message in socket.sent], ["subscribe"] * 3)

    async def test_timeout_latches_closed_without_retry(self):
        stream, socket, connection = await self.make_stream()
        socket.reply = lambda _: None
        with self.assertRaisesRegex(RuntimeError, "snapshot unavailable"):
            await stream.request_snapshot("account_all")
        self.assertTrue(socket.closed)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            stream.book_snapshot()
        with self.assertRaises(RuntimeError):
            await stream.request_snapshot("account_all")
        self.assertEqual(len(socket.sent), 2)
        connection.assert_awaited_once()

    async def test_wrong_identity_missing_orders_and_error_are_closed_and_redacted(self):
        invalid = [account_message(), account_message("account_all_orders"),
                   {"type": "error", "message": "sensitive-backend-detail"}]
        invalid[0]["account"] = 8
        invalid[1].pop("orders")
        for response in invalid:
            with self.subTest(kind=response["type"]):
                stream, socket, _ = await self.make_stream()
                socket.reply = lambda _, response=response: response
                channel = "account_all_orders" if "orders" in response["type"] else "account_all"
                with self.assertRaises(RuntimeError) as caught:
                    await stream.request_snapshot(channel)
                self.assertNotIn("sensitive", str(caught.exception))
                self.assertTrue(socket.closed)

    async def test_disconnect_and_invalid_json_invalidate_book(self):
        for raw in (None, "not-json"):
            stream, socket, _ = await self.make_stream()
            await socket.queue.put(raw)
            await self.flush()
            with self.assertRaises(RuntimeError):
                stream.book_snapshot()
            self.assertTrue(socket.closed)

    async def test_book_delta_preserves_exact_levels_source_time_and_receipt(self):
        stream, socket, _ = await self.make_stream()
        update = book_message(initial=False, nonce=11, offset=99)
        update["order_book"]["bids"] = [{"price": "99", "size": "0"},
                                           {"price": "98.01", "size": "0.123456789"}]
        await socket.push(update)
        await self.flush()
        book = stream.book_snapshot()
        self.assertEqual(book["bids"], ((Decimal("98.01"), Decimal("0.123456789")),))
        self.assertEqual((book["nonce"], book["offset"], book["timestamp"],
                          book["received_monotonic"]), (11, 99, 1000, 42.5))

    async def test_book_gap_duplicate_nonfinite_negative_and_depth_fail_closed(self):
        base = book_message(initial=False, nonce=11, offset=21)
        variants = []
        for field, value in (("begin_nonce", 9), ("offset", 20), ("nonce", 9)):
            value_book = copy.deepcopy(base)
            value_book["order_book"][field] = value
            variants.append(value_book)
        for rows in ([{"price": "99", "size": "1"}] * 2,
                     [{"price": "NaN", "size": "1"}],
                     [{"price": "99", "size": "-1"}],
                     [{"price": "99", "size": "Infinity"}],
                     [{"price": str(n + 1), "size": "1"} for n in range(20001)]):
            value_book = copy.deepcopy(base)
            value_book["order_book"]["bids"] = rows
            variants.append(value_book)
        for update in variants:
            stream, socket, _ = await self.make_stream()
            await socket.push(update)
            await self.flush()
            with self.assertRaises(RuntimeError):
                stream.book_snapshot()
            self.assertFalse(socket.closed)
            self.assertTrue(stream.transport_healthy)
            self.assertEqual((await stream.request_snapshot("account_all_orders"))["orders"], {})
            await socket.push(book_message(initial=False, nonce=11, offset=21))
            await self.flush()
            with self.assertRaises(RuntimeError):
                stream.book_snapshot()

    async def test_source_timestamp_rejects_backlog_future_and_backward_updates(self):
        for source_timestamp, receipt_wall in ((1000, 4.001), (1001, 1), (999, 1)):
            with self.subTest(source_timestamp=source_timestamp, receipt_wall=receipt_wall):
                wall = [1]
                stream, socket, _ = await self.make_stream(wall_clock=lambda: wall[0], sleep=AsyncMock())
                wall[0] = receipt_wall
                update = book_message(initial=False, nonce=11, offset=21)
                update["timestamp"] = source_timestamp
                await socket.push(update)
                await self.flush()
                with self.assertRaises(RuntimeError):
                    stream.book_snapshot()
                self.assertFalse(socket.closed)
                self.assertTrue(stream.transport_healthy)
                self.assertEqual((await stream.request_snapshot("account_all_orders"))["orders"], {})

    async def test_source_time_boundary_and_cached_expiry_do_not_refresh_receipt(self):
        wall = [4]
        stream, socket, _ = await self.make_stream(wall_clock=lambda: wall[0])
        self.assertEqual(stream.book_snapshot()["received_monotonic"], 42.5)
        self.assertEqual(stream.book_snapshot()["timestamp"], 1000)
        wall[0] = 4.001
        with self.assertRaisesRegex(RuntimeError, "source timestamp unavailable"):
            stream.book_snapshot()
        self.assertEqual(stream.last_book_failure, ("book_snapshot", "source_time_out_of_bounds"))
        await self.flush()
        self.assertFalse(socket.closed)
        self.assertTrue(stream.transport_healthy)
        self.assertEqual((await stream.request_snapshot("account_all"))["total_trades_count"], 10)
        wall[0] = 1
        with self.assertRaises(RuntimeError):
            stream.book_snapshot()

    async def test_one_wall_tick_wait_rechecks_strict_bounds_without_refreshing_receipt(self):
        wall, monotonic = [1], [42.5]

        async def advance_tick(delay):
            wall[0] += delay
            monotonic[0] += delay

        sleep = AsyncMock(side_effect=advance_tick)
        with patch("core.adapters.exchanges.adapters.lighter_read_stream.time.get_clock_info",
                   return_value=SimpleNamespace(resolution=0.015625)):
            stream, socket, _ = await self.make_stream(
                wall_clock=lambda: wall[0], clock=lambda: monotonic[0], sleep=sleep)
        update = book_message(initial=False, nonce=11, offset=21)
        update["timestamp"] = 1002
        await socket.push(update)
        await self.flush()
        result = stream.book_snapshot()
        self.assertEqual(result["timestamp"], 1002)
        self.assertEqual(result["received_monotonic"], 42.5)
        self.assertEqual(result["nonce"], 11)
        self.assertIsNone(stream.last_book_failure)
        sleep.assert_awaited_once_with(0.015625)

    async def test_one_tick_wait_never_accepts_future_or_stale_data(self):
        cases = ((1002, 1, 1, True),       # Host did not advance: still future.
                 (1016, 1, 1, False),    # Future beyond the host quantum: no wait.
                 (1000, 4.001, 4.001, False),  # Already stale: no wait.
                 (1002, 1, 4.003, True))  # Clock jump during wait: now stale.
        for timestamp, wall_before, wall_after, should_wait in cases:
            with self.subTest(timestamp=timestamp, wall_before=wall_before, wall_after=wall_after):
                wall = [1]

                async def tick(_):
                    wall[0] = wall_after

                sleep = AsyncMock(side_effect=tick)
                with patch("core.adapters.exchanges.adapters.lighter_read_stream.time.get_clock_info",
                           return_value=SimpleNamespace(resolution=0.015625)):
                    stream, socket, _ = await self.make_stream(wall_clock=lambda: wall[0], sleep=sleep)
                wall[0] = wall_before
                update = book_message(initial=False, nonce=11, offset=21)
                update["timestamp"] = timestamp
                await socket.push(update)
                await self.flush()
                with self.assertRaises(RuntimeError):
                    stream.book_snapshot()
                self.assertTrue(stream.transport_healthy)
                self.assertEqual(sleep.await_count, int(should_wait))
                self.assertEqual(stream.last_book_failure,
                                 ("receive_book", "source_time_out_of_bounds"))

    async def test_wall_quantum_wait_is_capped_and_never_negative(self):
        for resolution, expected_wait in ((0.5, 0.02), (-1, None)):
            wall = [1]

            async def tick(delay):
                wall[0] += delay

            sleep = AsyncMock(side_effect=tick)
            with patch("core.adapters.exchanges.adapters.lighter_read_stream.time.get_clock_info",
                       return_value=SimpleNamespace(resolution=resolution)):
                stream, socket, _ = await self.make_stream(wall_clock=lambda: wall[0], sleep=sleep)
            update = book_message(initial=False, nonce=11, offset=21)
            update["timestamp"] = 1010
            await socket.push(update)
            await self.flush()
            if expected_wait is None:
                sleep.assert_not_awaited()
                with self.assertRaises(RuntimeError):
                    stream.book_snapshot()
            else:
                sleep.assert_awaited_once_with(expected_wait)
                self.assertEqual(stream.book_snapshot()["timestamp"], 1010)

    async def test_failure_diagnostic_uses_only_owned_codes(self):
        stream, socket, _ = await self.make_stream()
        stream._update_book = MagicMock(side_effect=ValueError("sensitive-backend-detail"))
        await socket.push(book_message(initial=False, nonce=11, offset=21))
        await self.flush()
        self.assertEqual(stream.last_book_failure, ("receive_book", "invalid_payload"))

    async def test_stale_initial_book_is_never_ready(self):
        socket = Socket()
        stream = LighterReadStream("wss://api.rh.lighter.xyz/stream", 7, 0,
                                   lambda: "test-auth", connect_factory=AsyncMock(return_value=socket),
                                   clock=lambda: 42.5, wall_clock=lambda: 4.001)
        await stream.start()
        self.addAsyncCleanup(stream.close)
        with self.assertRaisesRegex(RuntimeError, "read book unavailable"):
            stream.book_snapshot()
        self.assertTrue(stream.transport_healthy)
        self.assertEqual((await stream.request_snapshot("account_all_orders"))["orders"], {})

    async def test_wrong_book_channel_and_protocol_still_invalidate_transport(self):
        for update in (book_message(), {**book_message(initial=False, nonce=11, offset=21),
                                       "channel": "order_book:99"}):
            stream, socket, _ = await self.make_stream()
            await socket.push(update)
            await self.flush()
            self.assertFalse(stream.transport_healthy)
            with self.assertRaises(RuntimeError):
                await stream.request_snapshot("account_all_orders")
            self.assertTrue(socket.closed)

    async def test_auth_error_and_cancel_close_without_leaking_material(self):
        stream, socket, _ = await self.make_stream()
        stream._auth = MagicMock(side_effect=ValueError("sensitive-signing-error"))
        with self.assertRaises(RuntimeError) as caught:
            await stream.request_snapshot("account_all_orders")
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertTrue(socket.closed)
        stream, socket, _ = await self.make_stream()
        socket.reply = lambda _: None
        pending = asyncio.create_task(stream.request_snapshot("account_all"))
        await self.flush()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(socket.closed)

    async def test_startup_failure_and_unsolicited_snapshot_fail_closed(self):
        connection = AsyncMock(side_effect=ValueError("sensitive-connection-details"))
        stream = LighterReadStream("wss://api.rh.lighter.xyz/stream", 7, 0,
                                   lambda: "test-auth", connect_factory=connection)
        with self.assertRaisesRegex(RuntimeError, "startup unavailable") as caught:
            await stream.start()
        self.assertNotIn("sensitive", str(caught.exception))
        with self.assertRaises(RuntimeError):
            await stream.start()
        connection.assert_awaited_once()
        stream, socket, _ = await self.make_stream()
        await socket.push(account_message())
        await self.flush()
        with self.assertRaises(RuntimeError):
            stream.book_snapshot()
        self.assertTrue(socket.closed)


class LighterReadStreamOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self):
        adapter = LighterAdapter.__new__(LighterAdapter)
        adapter._connected = adapter._authenticated = True
        adapter.ws_url = "wss://api.rh.lighter.xyz/stream"
        adapter._normalize_symbol = lambda symbol: symbol
        adapter.logger = MagicMock()
        adapter._websocket = SimpleNamespace(disconnect=AsyncMock())
        adapter._rest = LighterRest.__new__(LighterRest)
        adapter._rest.network, adapter._rest.account_index, adapter._rest.api_key_index = "robinhood", 7, 2
        adapter._rest.get_market_index = lambda _: 0
        adapter._rest.close, adapter._rest.signer_client = AsyncMock(), MagicMock()
        adapter._rest._markets_cache = {0: {"symbol": "ETH"}, 1: {"symbol": "BTC"}}
        adapter._rest.signer_client.create_auth_token_with_expiry.return_value = ("test-auth", None)
        return adapter

    async def test_owned_signer_reused_and_disconnect_closes_opt_in_stream(self):
        adapter = self.make_adapter()
        clock = lambda: 123.456789
        with patch("core.adapters.exchanges.adapters.lighter_read_stream.LighterReadStream") as factory:
            stream = factory.return_value
            stream.start = AsyncMock()
            stream.close = AsyncMock()
            self.assertIs(await adapter.open_read_stream("ETH", clock=clock), stream)
            self.assertIs(factory.call_args.kwargs["clock"], clock)
            auth_factory = factory.call_args.args[3]
            self.assertEqual(auth_factory(), "test-auth")
            adapter._rest.signer_client.create_auth_token_with_expiry.assert_called_once_with(
                deadline=600, api_key_index=2)
            await adapter.disconnect()
            stream.close.assert_awaited_once()
            adapter._websocket.disconnect.assert_awaited_once()
            adapter._rest.close.assert_awaited_once()

    async def test_wrong_network_and_url_rejected_before_stream_creation(self):
        for field, value in (("network", "mainnet"), ("ws_url", "wss://unexpected.example/stream")):
            adapter = self.make_adapter()
            setattr(adapter._rest if field == "network" else adapter, field, value)
            with patch("core.adapters.exchanges.adapters.lighter_read_stream.LighterReadStream") as factory:
                with self.assertRaisesRegex(RuntimeError, "read stream unavailable"):
                    await adapter.open_read_stream("ETH")
                factory.assert_not_called()

    async def test_opt_in_order_snapshot_uses_existing_parser_without_rest_query(self):
        adapter = self.make_adapter()
        row = {"order_index": 123, "order_id": "123", "client_order_index": 456,
               "market_index": 0, "owner_account_index": 7, "is_ask": False,
               "reduce_only": False, "status": "open", "type": "limit",
               "initial_base_amount": "0.3", "filled_base_amount": "0.1",
               "remaining_base_amount": "0.2", "price": "99", "filled_quote_amount": "9.9"}
        other = {**row, "order_index": 124, "order_id": "124", "market_index": 1}
        adapter._read_stream = SimpleNamespace(
            request_snapshot=AsyncMock(return_value={"orders": {"0": [row], "1": [other]}}),
            close=AsyncMock())
        adapter._rest.get_open_orders = AsyncMock(side_effect=AssertionError("REST must not run"))
        orders = await adapter.get_open_orders()
        self.assertEqual([order.id for order in orders], ["123", "124"])
        self.assertEqual(orders[0].remaining, Decimal("0.2"))
        self.assertFalse(orders[0].raw_data["order_info"].reduce_only)
        self.assertEqual([order.symbol for order in await adapter.get_open_orders("BTC")], ["BTC"])
        adapter._rest.get_open_orders.assert_not_awaited()
        adapter._read_stream.request_snapshot.return_value = {"orders": {}}
        self.assertEqual(await adapter.get_open_orders(), [])
        row["owner_account_index"] = 8
        adapter._read_stream.request_snapshot.return_value = {"orders": {"0": [row]}}
        with self.assertRaisesRegex(RuntimeError, "active orders unavailable"):
            await adapter.get_open_orders()
        adapter._read_stream.close.assert_awaited_once()

    async def test_without_opt_in_original_rest_path_is_unchanged(self):
        adapter = self.make_adapter()
        adapter._rest.get_open_orders = AsyncMock(return_value=["original"])
        self.assertEqual(await adapter.get_open_orders("ETH"), ["original"])
        adapter._rest.get_open_orders.assert_awaited_once_with("ETH")

    async def test_book_gap_keeps_public_account_proof_and_transport_failure_needs_explicit_close(self):
        adapter, socket = self.make_adapter(), Socket()
        stream = LighterReadStream("wss://api.rh.lighter.xyz/stream", 7, 0,
                                   lambda: "test-auth", connect_factory=AsyncMock(return_value=socket),
                                   wall_clock=lambda: 1)
        await stream.start()
        self.addAsyncCleanup(stream.close)
        adapter._read_stream = stream
        adapter._rest.get_open_orders = AsyncMock(return_value=["cleanup-only"])
        await socket.push(book_message(initial=False, nonce=12, offset=21))
        for _ in range(5):
            await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            stream.book_snapshot()
        self.assertTrue(stream.transport_healthy)
        self.assertEqual(await adapter.get_open_orders(), [])
        adapter._rest.get_open_orders.assert_not_awaited()
        await socket.queue.put(None)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(stream.transport_healthy)
        with self.assertRaises(RuntimeError):
            await adapter.get_open_orders()
        adapter._rest.get_open_orders.assert_not_awaited()
        await adapter.close_read_stream()
        self.assertIsNone(adapter._read_stream)
        self.assertEqual(await adapter.get_open_orders(), ["cleanup-only"])


if __name__ == "__main__":
    unittest.main()
