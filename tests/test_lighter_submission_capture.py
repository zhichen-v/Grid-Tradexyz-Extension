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
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.exceptions import OrderSubmissionNotSentError
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
