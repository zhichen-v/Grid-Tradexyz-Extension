"""No live network or order writes: bounded, exact-identity evidence inspection."""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import lighter_submission_diagnostics as diagnostics
from core.lighter_submission_journal import SubmissionJournal


URL = "https://api.rh.lighter.xyz"
SETTINGS = {"account_index": 42, "api_key_index": 3, "network": "robinhood"}
RECORD = {"base_url": URL, "account_index": 42, "api_key_index": 3,
          "market_index": 1, "client_order_id": "12345", "nonce": 88, "tx_hash": "abc123"}


def order(**overrides):
    return NS(**dict({"client_order_id": "12345", "client_order_index": 12345,
                      "market_index": 1, "owner_account_index": 42,
                      "order_index": 67890, "status": "open"}, **overrides))


class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tx = NS(tx=AsyncMock(return_value=NS(
            code=200, hash="abc123", account_index=42, api_key_index=3,
            nonce=88, status=2, info="SECRET SIGNED TX INFO")))
        self.orders = NS(
            account_active_orders=AsyncMock(return_value=NS(code=200, orders=[])),
            account_inactive_orders=AsyncMock(return_value=NS(code=200, orders=[], next_cursor=None)),
        )

    async def query(self, record=None, **kwargs):
        return await diagnostics.query_record(
            record or RECORD, SETTINGS, URL, self.tx, self.orders, "SECRET AUTH TOKEN", **kwargs)

    async def test_not_found_never_proves_rejection_and_output_omits_signed_info(self):
        result = await self.query()
        self.assertEqual(result["query_state"], "observations_only")
        self.assertEqual(result["transaction"]["status"], 2)
        self.assertEqual(result["active_orders"]["state"], "not_found")
        self.assertEqual(result["history"]["state"], "not_found")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("rejected", json.dumps(result))
        self.assertEqual(self.tx.tx.call_args.kwargs["by"], "hash")
        self.assertEqual(self.orders.account_active_orders.call_args.kwargs["market_id"], 1)

    async def test_wrong_scope_never_queries(self):
        for change in ({"base_url": "https://mainnet.zklighter.elliot.ai"},
                       {"account_index": 99}, {"api_key_index": 99}):
            result = await self.query({**RECORD, **change})
            self.assertEqual(result["query_state"], "scope_mismatch_no_queries")
        self.tx.tx.assert_not_called()
        self.orders.account_active_orders.assert_not_called()

    async def test_hash_or_nonce_mismatch_is_not_accepted(self):
        self.tx.tx.return_value.hash = "other_hash"
        self.assertEqual((await self.query())["transaction"]["state"], "identity_mismatch")
        self.tx.tx.return_value.hash = RECORD["tx_hash"]
        self.tx.tx.return_value.nonce = 89
        self.assertEqual((await self.query())["transaction"]["state"], "identity_mismatch")

    async def test_order_requires_client_market_and_owner_match(self):
        self.orders.account_active_orders.return_value.orders = [
            order(market_index=2), order(owner_account_index=99),
            order(client_order_id="999"), order(),
        ]
        result = await self.query()
        self.assertEqual(len(result["active_orders"]["matches"]), 1)
        self.assertEqual(result["active_orders"]["matches"][0]["order_index"], 67890)

    async def test_history_pagination_finds_exact_order(self):
        self.orders.account_inactive_orders.side_effect = [
            NS(code=200, orders=[], next_cursor="next"),
            NS(code=200, orders=[order(status="filled")], next_cursor=None),
        ]
        result = await self.query()
        self.assertEqual(result["history"]["state"], "found")
        self.assertEqual(result["history"]["pages"], 2)
        self.assertEqual(self.orders.account_inactive_orders.call_args.kwargs["cursor"], "next")

    async def test_repeated_cursor_and_budget_remain_incomplete(self):
        self.orders.account_inactive_orders.return_value.next_cursor = "same"
        result = await self.query()
        self.assertEqual(result["history"]["state"], "incomplete_repeated_cursor")
        self.assertEqual(self.orders.account_inactive_orders.call_count, 2)
        result = await self.query(pages=1)
        self.assertEqual(result["history"]["state"], "incomplete_page_limit")

    async def test_http_failure_is_sanitized_and_history_can_still_find_order(self):
        error = RuntimeError("SECRET response payload")
        error.status = 502
        error.headers = {"x-amz-cf-id": "trace-id", "Authorization": "SECRET token"}
        self.tx.tx.side_effect = error
        self.orders.account_inactive_orders.return_value.orders = [order(status="filled")]
        result = await self.query()
        self.assertEqual(result["transaction"]["http_status"], 502)
        self.assertEqual(result["history"]["state"], "found")
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_history_failure_does_not_become_not_found(self):
        self.orders.account_inactive_orders.side_effect = RuntimeError("SECRET")
        result = await self.query()
        self.assertEqual(result["history"]["state"], "query_failed")
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_rate_limit_stops_remaining_gets(self):
        error = RuntimeError("SECRET")
        error.status = 429
        self.tx.tx.side_effect = error
        result = await self.query()
        self.assertEqual(result["query_state"], "rate_limited")
        self.orders.account_active_orders.assert_not_called()
        self.orders.account_inactive_orders.assert_not_called()

    async def test_wrong_scope_does_not_construct_signer(self):
        with patch("lighter.SignerClient", side_effect=AssertionError("No signer")):
            result = await diagnostics.query_records(
                [{**RECORD, "account_index": 99}], SETTINGS, pages=3, timeout=5.0)
        self.assertEqual(result[0]["query_state"], "scope_mismatch_no_queries")

    async def test_missing_order_list_is_failed_read_not_not_found(self):
        self.orders.account_active_orders.return_value = NS(code=200)
        self.orders.account_inactive_orders.return_value = NS(code=200, orders=None)
        result = await self.query()
        self.assertEqual(result["active_orders"]["state"], "query_failed")
        self.assertEqual(result["history"]["state"], "query_failed")

    async def test_request_timeout_is_bounded(self):
        async def slow(**kwargs):
            await asyncio.sleep(10)
        self.tx.tx.side_effect = slow
        result = await self.query(timeout=0.001)
        self.assertEqual(result["transaction"]["state"], "query_failed")
        self.assertEqual(result["transaction"]["error_type"], "TimeoutError")


class OfflineTests(unittest.TestCase):
    def test_offline_never_loads_credentials_or_changes_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SubmissionJournal(URL, 42, 3, directory=Path(directory))
            journal.append("pre_send", "abc123", client_order_id="12345", market_index=1, nonce=88)
            before = journal.path.read_bytes()
            output = io.StringIO()
            with patch.object(diagnostics, "query_records", side_effect=AssertionError("No network")):
                with redirect_stdout(output):
                    code = diagnostics.main(["--journal", directory])
            self.assertEqual(code, 0)
            self.assertEqual(journal.path.read_bytes(), before)
            report = json.loads(output.getvalue())
            self.assertFalse(report["queried"])
            self.assertEqual(report["displayed_records"], 1)

    def test_explicit_hash_keeps_resolved_history_and_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SubmissionJournal(URL, 42, 3, directory=Path(directory))
            journal.append("pre_send", "abc123", client_order_id="12345", market_index=1, nonce=88)
            journal.append("http_error", "abc123", http_status=502, trace_headers={"x-amz-cf-id": "trace123"})
            journal.append("tx_lookup_failed", "abc123", http_status=404,
                           trace_headers={"x-amz-cf-id": "later-trace"})
            journal.append("order_observed", "abc123", order_id="67890", order_status="filled")
            self.assertEqual(diagnostics.select_records(Path(directory)), [])
            records = diagnostics.select_records(Path(directory), "abc123")
            self.assertEqual(records[0]["client_order_id"], "12345")
            self.assertEqual(records[0]["last_event"], "order_observed")
            self.assertEqual(diagnostics._display(records[0])["trace_headers"], {"x-amz-cf-id": "trace123"})
            self.assertEqual(records[0]["http_status"], 502)
            self.assertEqual(records[0]["events"][2]["http_status"], 404)

    def test_missing_directory_returns_empty_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-created"
            self.assertEqual(diagnostics.select_records(missing), [])
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
