import asyncio
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lighter.exceptions import ApiException, BadRequestException
from lighter.models.result_code import ResultCode
from lighter.nonce_manager import OptimisticNonceManager
from lighter.signer_client import SignerClient

from core.adapters.exchanges.adapters.lighter_submission_capture import SubmissionCapture
from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket
from core.adapters.exchanges.exceptions import OrderSubmissionNotSentError
from core.adapters.exchanges.models import OrderStatus
from core.lighter_submission_journal import SubmissionJournal, read_pending


class LighterSubmissionCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.journal = SubmissionJournal(
            "https://api.rh.lighter.xyz", 7, 0, directory=Path(directory.name),
        )
        self.signer = object.__new__(SignerClient)
        self.signer.nonce_manager = OptimisticNonceManager(7, SimpleNamespace(), [0])
        self.signer.nonce_manager.nonce[0] = 40
        self.signer.nonce_manager._fetch_nonce = AsyncMock(return_value=55)

        def native_sign(*args, **kwargs):
            return (
                14, json.dumps({"ClientOrderIndex": args[1], "Sig": "SECRET_SIGNATURE"}),
                f"fixture-{args[1]}-{kwargs['nonce']}", None,
            )

        self.native_sign = Mock(side_effect=native_sign)
        self.signer.sign_create_order = self.native_sign
        self.response = SimpleNamespace(code=200, tx_hash="response-hash")
        self.signer.tx_api = SimpleNamespace(send_tx=AsyncMock(return_value=self.response))
        self.capture = SubmissionCapture(self.signer, self.journal)

    async def _create(self, client_id=1001, **intent):
        return await self.capture.run(
            lambda: self.signer.create_order(
                market_index=1, client_order_index=client_id, base_amount=100,
                price=841500, is_ask=False, order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            ), symbol="BTC", **intent,
        )

    def _events(self):
        if not self.journal.path.exists():
            return []
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    async def _adapter_fixture(self, subscribe=True):
        rest, _ = self._rest_fixture()
        rest.base_url = "https://api.rh.lighter.xyz"
        rest.ws_url = "wss://api.rh.lighter.xyz/stream"
        rest.account_index = 7
        websocket = object.__new__(LighterWebSocket)
        websocket._order_callbacks = []
        websocket._order_fill_callbacks = []
        websocket._markets_cache = {1: {"symbol": "BTC"}, 2: {"symbol": "ETH"}}
        websocket._subscribed_markets = []
        websocket._subscribed_accounts = []
        websocket._subscribed_market_stats = []
        websocket._subscribed_trades = []
        websocket._subscribe_account_all_orders = AsyncMock()
        websocket._ensure_direct_ws_running = AsyncMock()
        websocket.subscribe_positions = AsyncMock()
        adapter = object.__new__(LighterAdapter)
        adapter.logger = Mock()
        config = SimpleNamespace(extra_params={"load_credentials_from_file": False})
        with (
            patch("core.adapters.exchanges.adapters.lighter.ExchangeAdapter.__init__", return_value=None),
            patch.object(LighterAdapter, "_convert_config_to_dict", return_value={}),
            patch.object(LighterAdapter, "_get_symbol_cache_service", return_value=None),
            patch("core.adapters.exchanges.adapters.lighter.LighterRest", return_value=rest),
            patch("core.adapters.exchanges.adapters.lighter.LighterWebSocket", return_value=websocket),
            patch("core.adapters.exchanges.adapters.lighter.create_subscription_manager"),
        ):
            adapter.__init__(config)
        if subscribe:
            await adapter.subscribe_orders()
        return adapter, websocket

    def _ws_order(self, **overrides):
        return {
            "order_index": 844424837519875, "client_order_index": 1001,
            "market_index": 1, "owner_account_index": 7,
            "initial_base_amount": "0.00100", "filled_base_amount": "0.00100",
            "remaining_base_amount": "0", "filled_quote_amount": "84.15",
            "price": "84150", "is_ask": False, "status": "filled", "type": "limit",
            **overrides,
        }

    async def _ws_update(self, websocket, **overrides):
        await websocket._handle_direct_ws_message({
            "type": "update/account_all_orders", "channel": "account_all_orders:7",
            "orders": {"1": [self._ws_order(**overrides)]},
        })

    async def test_fast_ws_fill_records_exact_observation_before_strategy_callback(self):
        await self._create()
        adapter, websocket = await self._adapter_fixture()

        def strategy_callback(order):
            self.assertEqual(read_pending(self.journal.path), [])
            self.assertEqual(order.status, OrderStatus.FILLED)

        callback = Mock(side_effect=strategy_callback)
        websocket._order_callbacks.append(callback)
        await self._ws_update(websocket)
        await self._ws_update(websocket)
        adapter._rest._clear_resolved_submissions_from_orders([
            websocket._parse_order_from_direct_ws(self._ws_order()),
        ])

        observations = [row for row in self._events() if row["event"] == "order_observed"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["order_id"], "844424837519875")
        self.assertEqual(observations[0]["order_status"], "filled")
        self.assertEqual(observations[0]["source"], "websocket")
        self.assertEqual(callback.call_count, 2)
        self.signer.tx_api.send_tx.assert_awaited_once()

    async def test_partial_ws_observation_does_not_change_order_or_emit_full_fill(self):
        await self._create()
        _, websocket = await self._adapter_fixture()
        callback = Mock()
        filled_callback = Mock()
        websocket._order_callbacks.append(callback)
        websocket._order_fill_callbacks.append(filled_callback)

        await self._ws_update(websocket, status="open", filled_base_amount="0.00089",
                              remaining_base_amount="0.00011")

        order = callback.call_args.args[0]
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(order.remaining, Decimal("0.00011"))
        filled_callback.assert_not_called()
        self.assertEqual(read_pending(self.journal.path), [])
        self.signer.tx_api.send_tx.assert_awaited_once()

    async def test_ws_observation_requires_exact_client_market_account_and_order_ids(self):
        await self._create()
        _, websocket = await self._adapter_fixture()
        for overrides in (
            {"client_order_index": None}, {"client_order_index": 2002},
            {"market_index": 2}, {"owner_account_index": 8},
            {"order_index": None}, {"order_index": "tx-hash-not-an-index"},
        ):
            with self.subTest(overrides=overrides):
                await self._ws_update(websocket, **overrides)
                self.assertEqual(len(read_pending(self.journal.path)), 1)
        self.assertEqual([row["event"] for row in self._events()], ["pre_send", "acknowledged"])

    async def test_ws_journal_failure_does_not_block_fill_and_can_retry_observation(self):
        await self._create()
        _, websocket = await self._adapter_fixture()
        callback = Mock()
        websocket._order_fill_callbacks.append(callback)
        with patch.object(self.journal, "append", side_effect=OSError("disk fixture")):
            await self._ws_update(websocket)
        callback.assert_called_once()
        self.assertIn("1001", self.capture.pending)
        self.assertEqual(len(read_pending(self.journal.path)), 1)

        await self._ws_update(websocket)
        self.assertEqual(read_pending(self.journal.path), [])
        self.assertEqual(len([row for row in self._events() if row["event"] == "order_observed"]), 1)
        self.signer.tx_api.send_tx.assert_awaited_once()

    async def test_ws_observation_before_http_ack_does_not_reopen_pending(self):
        _, websocket = await self._adapter_fixture()

        async def send(**kwargs):
            await self._ws_update(websocket)
            return self.response

        self.signer.tx_api.send_tx.side_effect = send
        await self._create()

        self.assertEqual([row["event"] for row in self._events()],
                         ["pre_send", "order_observed", "acknowledged"])
        self.assertEqual(read_pending(self.journal.path), [])
        self.assertEqual(self.capture.pending, {})

    async def test_history_resolves_fast_fill_when_ws_update_was_missed(self):
        await self._create()
        adapter, _ = await self._adapter_fixture()
        rest = adapter._rest
        rest.api_key_index = 0
        rest._markets_cache = {1: {"symbol": "BTC"}}
        rest.get_market_index = Mock(return_value=1)
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.signer_client.create_auth_token_with_expiry = Mock(return_value=("fixture-token", None))
        rest.order_api = SimpleNamespace(account_inactive_orders=AsyncMock(return_value=SimpleNamespace(
            code=200, orders=[SimpleNamespace(**self._ws_order())],
        )))

        result = await rest.get_order("1001", "BTC")

        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(read_pending(self.journal.path), [])
        self.assertEqual(self._events()[-1]["order_id"], "844424837519875")
        self.signer.tx_api.send_tx.assert_awaited_once()

    async def test_observer_is_lazy_and_single_across_subscribe_and_reconnect(self):
        adapter, websocket = await self._adapter_fixture(subscribe=False)
        self.assertEqual(websocket._order_callbacks, [])
        self.assertFalse(websocket._needs_direct_ws())

        callback = Mock()
        await adapter.subscribe_user_data(callback)
        await adapter.subscribe_orders(callback)
        await adapter.subscribe_orders()
        await websocket._resubscribe_all()

        self.assertEqual(websocket._order_callbacks[0], adapter._capture_submission_order_update)
        self.assertEqual(websocket._order_callbacks.count(adapter._capture_submission_order_update), 1)
        websocket._ensure_direct_ws_running.assert_awaited_once()

    async def test_pre_send_fsync_failure_prevents_http_and_rolls_back_under_sdk_lock(self):
        lock_held = []

        def failed_sync(descriptor):
            lock_held.append(self.signer.nonce_manager.lock(0).locked())
            raise OSError("disk fixture")

        with patch("core.lighter_submission_journal.os.fsync", side_effect=failed_sync):
            with self.assertRaises(OrderSubmissionNotSentError):
                await self._create()

        self.assertTrue(lock_held)
        self.assertTrue(all(lock_held))
        self.signer.tx_api.send_tx.assert_not_awaited()
        self.assertEqual(self.signer.nonce_manager.nonce[0], 40)
        self.assertFalse(self.signer.nonce_manager.lock(0).locked())
        self.assertEqual(self.capture.pending, {})
        self.assertIsNone(self.capture._context.get())

    async def test_pre_send_contains_actual_signed_values_before_http_and_no_signature(self):
        async def send(**kwargs):
            records = self._events()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["event"], "pre_send")
            self.assertTrue(self.signer.nonce_manager.lock(0).locked())
            return self.response

        self.signer.tx_api.send_tx.side_effect = send
        result = await self._create(grid_id=34, logical_client_id="grid_34_84150_1000")

        record = self._events()[0]
        self.assertEqual((record["nonce"], record["api_key_index"]), (41, 0))
        self.assertEqual((record["base_amount"], record["price"]), (100, 841500))
        self.assertEqual(record["time_in_force"], SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY)
        self.assertEqual(record["client_order_id"], "1001")
        self.assertEqual(record["grid_id"], 34)
        self.assertIs(result[1], self.response)
        self.assertNotIn("SECRET_SIGNATURE", self.journal.path.read_text())
        self.assertEqual(len(read_pending(self.journal.path)), 1)

    async def test_post_send_append_failure_preserves_result_and_restart_evidence(self):
        append = self.journal.append

        def fail_after_send(event, tx_hash, **fields):
            if event != "pre_send":
                raise OSError("disk fixture after send")
            return append(event, tx_hash, **fields)

        with patch.object(self.journal, "append", side_effect=fail_after_send):
            result = await self._create()

        self.assertIs(result[1], self.response)
        self.assertIsNone(result[2])
        self.signer.tx_api.send_tx.assert_awaited_once()
        self.assertEqual(self.signer.nonce_manager.nonce[0], 41)
        self.assertEqual(read_pending(self.journal.path)[0]["last_event"], "pre_send")

    async def test_http_502_retains_trace_without_body_and_never_retries(self):
        error = ApiException(
            status=502, reason="Bad Gateway SECRET_REASON", body="SECRET_BODY",
        )
        error.headers = {
            "X-Amz-Cf-Id": "edge-fixture", "X-Amz-Cf-Pop": "NRT-fixture",
            "Authorization": "SECRET_AUTH", "Set-Cookie": "SECRET_COOKIE",
        }
        self.signer.tx_api.send_tx.side_effect = error

        with self.assertRaises(ApiException) as caught:
            await self._create()

        self.assertIs(caught.exception, error)
        event = self._events()[-1]
        self.assertEqual((event["event"], event["http_status"]), ("uncertain", 502))
        self.assertEqual(event["trace_headers"], {
            "x-amz-cf-id": "edge-fixture", "x-amz-cf-pop": "NRT-fixture",
        })
        self.assertNotIn("SECRET", self.journal.path.read_text())
        self.signer.tx_api.send_tx.assert_awaited_once()
        self.assertEqual(self.signer.nonce_manager.nonce[0], 41)
        self.assertIn("1001", self.capture.pending)

    async def test_http_metadata_is_captured_before_sdk_bad_request_string_conversion(self):
        error = BadRequestException(status=400, data=ResultCode(code=21104, message="invalid nonce"))
        error.headers = {"X-Request-Id": "request-fixture"}
        self.signer.tx_api.send_tx.side_effect = error

        result = await self._create()

        self.assertIsInstance(result[2], str)
        event = self._events()[-1]
        self.assertEqual(event["http_status"], 400)
        self.assertEqual(event["event"], "rejected")
        self.assertEqual(event["trace_headers"], {"x-request-id": "request-fixture"})
        self.signer.nonce_manager._fetch_nonce.assert_awaited_once_with(0)
        self.signer.tx_api.send_tx.assert_awaited_once()
        self.assertEqual(self.capture.pending, {})
        self.assertEqual(read_pending(self.journal.path), [])

    async def test_cancelled_send_is_uncertain_without_retry_or_nonce_rollback(self):
        self.signer.tx_api.send_tx.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self._create()

        event = self._events()[-1]
        self.assertEqual((event["event"], event["error_type"]), ("uncertain", "CancelledError"))
        self.signer.tx_api.send_tx.assert_awaited_once()
        self.assertEqual(self.signer.nonce_manager.nonce[0], 41)
        self.assertIn("1001", self.capture.pending)
        self.assertIsNone(self.capture._context.get())

    async def test_concurrent_keys_preserve_task_local_hash_and_intent(self):
        self.signer.nonce_manager = OptimisticNonceManager(7, SimpleNamespace(), [0, 1])
        self.signer.nonce_manager.nonce.update({0: 40, 1: 90})
        both_sending = asyncio.Event()
        entered = []

        async def overlapping_send(**kwargs):
            entered.append(json.loads(kwargs["tx_info"])["ClientOrderIndex"])
            if len(entered) == 2:
                both_sending.set()
            await asyncio.wait_for(both_sending.wait(), timeout=1)
            return self.response

        self.signer.tx_api.send_tx.side_effect = overlapping_send
        await asyncio.gather(self._create(1001, grid_id=1), self._create(1002, grid_id=2))

        pre = {row["client_order_id"]: row for row in self._events() if row["event"] == "pre_send"}
        self.assertEqual(pre["1001"]["grid_id"], 1)
        self.assertEqual(pre["1002"]["grid_id"], 2)
        self.assertEqual({row["api_key_index"] for row in pre.values()}, {0, 1})
        acknowledgements = [row["tx_hash"] for row in self._events() if row["event"] == "acknowledged"]
        self.assertCountEqual(acknowledgements, [row["tx_hash"] for row in pre.values()])
        self.assertEqual(len(read_pending(self.journal.path)), 2)
        self.capture.observe_order("1001", "exchange-1001", "open")
        self.assertEqual([row["client_order_id"] for row in read_pending(self.journal.path)], ["1002"])
        self.assertIsNone(self.capture._context.get())

    async def test_unrelated_cancel_stays_outside_capture_context(self):
        self.signer.sign_cancel_order = Mock(return_value=(15, "{}", "cancel-fixture", None))

        result = await self.signer.cancel_order(market_index=1, order_index=123456)

        self.assertIs(result[1], self.response)
        self.signer.tx_api.send_tx.assert_awaited_once_with(tx_type=15, tx_info="{}")
        self.assertEqual(self._events(), [])
        self.assertEqual(self.capture.pending, {})

    async def test_market_order_funnels_through_capture_and_uses_signed_ioc_price(self):
        result = await self.capture.run(
            lambda: self.signer.create_market_order(
                market_index=1, client_order_index=1003, base_amount=100,
                avg_execution_price=841234, is_ask=True, reduce_only=True,
            ), symbol="BTC",
        )

        record = self._events()[0]
        self.assertEqual(record["price"], 841234)
        self.assertEqual(record["order_type"], SignerClient.ORDER_TYPE_MARKET)
        self.assertEqual(record["time_in_force"], SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL)
        self.assertTrue(record["reduce_only"])
        self.assertTrue(record["is_ask"])
        self.assertIs(result[1], self.response)

    def _rest_fixture(self):
        rest = object.__new__(LighterRest)
        rest.signer_client = self.signer
        rest._submission_capture = self.capture
        rest._calculate_slippage_protection_price = AsyncMock(return_value=Decimal("84151"))
        rest._handle_order_result = AsyncMock(return_value="handled")
        rest._handle_ambiguous_order_submission = AsyncMock()
        market = {
            "market_index": 1, "price_decimals": 1, "size_decimals": 5,
            "price_multiplier": Decimal("10"), "size_multiplier": Decimal("100000"),
            "min_base_amount": Decimal("0.00020"), "min_quote_amount": Decimal("10"),
        }
        return rest, market

    async def test_actual_rest_limit_and_market_paths_capture_grid_metadata(self):
        rest, market = self._rest_fixture()
        for client_id, execute in ((1001, rest._execute_limit_order), (1002, rest._execute_market_order)):
            result = await execute(
                "BTC", "buy", Decimal("0.00100"), Decimal("84150"), market,
                client_order_id=client_id, time_in_force="POST_ONLY",
                _evidence_grid_id=34, _evidence_logical_client_id="grid_34_84150_1000",
            )
            self.assertEqual(result, "handled")

        records = [row for row in self._events() if row["event"] == "pre_send"]
        self.assertEqual([row["client_order_id"] for row in records], ["1001", "1002"])
        self.assertEqual([row["price"] for row in records], [841500, 841510])
        for row in records:
            self.assertEqual(row["grid_id"], 34)
            self.assertEqual(row["logical_client_id"], "grid_34_84150_1000")
        self.assertEqual(self.signer.tx_api.send_tx.await_count, 2)
        rest._handle_ambiguous_order_submission.assert_not_awaited()

    async def test_actual_rest_paths_propagate_pre_send_failure_without_ambiguous_placeholder(self):
        rest, market = self._rest_fixture()
        with patch.object(self.journal, "append", side_effect=OSError("disk fixture")):
            for execute in (rest._execute_limit_order, rest._execute_market_order):
                with self.subTest(execute=execute.__name__):
                    with self.assertRaises(OrderSubmissionNotSentError):
                        await execute(
                            "BTC", "buy", Decimal("0.00100"), Decimal("84150"), market,
                            client_order_id=1001,
                        )

        self.signer.tx_api.send_tx.assert_not_awaited()
        self.assertEqual(self.signer.nonce_manager.nonce[0], 40)
        rest._handle_order_result.assert_not_awaited()
        rest._handle_ambiguous_order_submission.assert_not_awaited()

    async def test_actual_rest_502_path_probes_saved_hash_before_existing_order_reconciliation(self):
        rest, market = self._rest_fixture()
        del rest._handle_ambiguous_order_submission
        rest.transaction_api = SimpleNamespace(tx=AsyncMock(return_value=SimpleNamespace(
            code=200, hash="fixture-1001-41", account_index=7, api_key_index=0,
            nonce=41, status=1,
        )))
        rest._reconcile_order_submission = AsyncMock(return_value="exact-order-fixture")
        self.signer.tx_api.send_tx.side_effect = ApiException(status=502, reason="Bad Gateway")

        result = await rest._execute_limit_order(
            "BTC", "buy", Decimal("0.00100"), Decimal("84150"), market,
            client_order_id=1001,
        )

        self.assertEqual(result, "exact-order-fixture")
        rest.transaction_api.tx.assert_awaited_once_with(
            by="hash", value="fixture-1001-41", _request_timeout=3.0,
        )
        rest._reconcile_order_submission.assert_awaited_once_with("BTC", 1001)
        self.signer.tx_api.send_tx.assert_awaited_once()
        self.assertEqual(self._events()[-1]["event"], "tx_observed")

    async def test_tx_probe_requires_exact_identity_and_never_clears_pending(self):
        await self._create()
        record = self.capture.pending["1001"]
        response = SimpleNamespace(
            code=200, hash=record["tx_hash"], account_index=7,
            api_key_index=0, nonce=41, status=2,
        )

        async def call_api(operation, request, **kwargs):
            self.assertFalse(kwargs["retry_on_429"])
            return await request()

        rest = SimpleNamespace(
            transaction_api=SimpleNamespace(tx=AsyncMock(return_value=response)),
            _call_api=AsyncMock(side_effect=call_api),
        )
        await self.capture.probe_transaction("1001", rest)
        self.assertTrue(self._events()[-1]["tx_found"])
        self.assertIn("1001", self.capture.pending)
        self.assertEqual(len(read_pending(self.journal.path)), 1)
        rest.transaction_api.tx.assert_awaited_once_with(
            by="hash", value=record["tx_hash"], _request_timeout=3.0,
        )

        response.nonce = 999
        await self.capture.probe_transaction("1001", rest)
        self.assertFalse(self._events()[-1]["tx_found"])
        self.assertIn("1001", self.capture.pending)
        self.signer.tx_api.send_tx.assert_awaited_once()

    async def test_tx_probe_deadline_includes_rate_limit_wait_and_never_retries(self):
        await self._create()
        cancelled = asyncio.Event()

        async def wait_for_cooldown(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        rest = SimpleNamespace(
            transaction_api=SimpleNamespace(tx=AsyncMock()),
            _call_api=AsyncMock(side_effect=wait_for_cooldown),
        )
        original_wait_for = asyncio.wait_for

        async def shortened_budget(request, timeout):
            self.assertEqual(timeout, 3.0)
            return await original_wait_for(request, timeout=0.01)

        with patch("core.adapters.exchanges.adapters.lighter_submission_capture.asyncio.wait_for",
                   side_effect=shortened_budget):
            await self.capture.probe_transaction("1001", rest)

        self.assertTrue(cancelled.is_set())
        rest._call_api.assert_awaited_once()
        rest.transaction_api.tx.assert_not_awaited()
        self.assertEqual(self._events()[-1]["event"], "tx_lookup_failed")
        self.assertIn("1001", self.capture.pending)
        self.signer.tx_api.send_tx.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
