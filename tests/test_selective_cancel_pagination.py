import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "selective_cancel_pagination_test"


spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.lighter_selective_cancel",
    ROOT / "core/adapters/exchanges/adapters/lighter_selective_cancel.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def send_chunk(rest, market, indexes):
    rest.sent.append((market, list(indexes)))
    return SimpleNamespace(code=200, predicted_execution_time_ms=0)


# Pagination tests exercise reconciliation only; signing is covered elsewhere.
module._send_chunk = send_chunk


class Signer:
    def create_auth_token_with_expiry(self, **_kwargs):
        return "token", None


class RawOrder:
    def __init__(self, order_id, status="canceled"):
        self.order_index = int(order_id)
        self.market_index = 1
        self.status = status


class HistoryPage:
    def __init__(self, order_ids, next_cursor):
        self.code = 200
        self.orders = [RawOrder(order_id) for order_id in order_ids]
        self.next_cursor = next_cursor


class OrderApi:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def account_inactive_orders(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[kwargs.get("cursor")]


class Rest:
    MUTATION_RECONCILIATION_ATTEMPTS = 2
    MUTATION_RECONCILIATION_DELAY = 0.001

    def __init__(self, pages, active_snapshots=None):
        self.config = {
            "cancel_reconcile_attempts": 3,
            "cancel_reconcile_delay": 0.001,
            "cancel_execution_wait_max": 0.001,
            "cancel_history_max_pages": 5,
            "cancel_read_timeout": 2,
        }
        self.api_key_index = 0
        self.account_index = 7
        self.signer_client = Signer()
        self.order_api = OrderApi(pages)
        self._uncertain_cancellations = set()
        self.sent = []
        self.active_snapshots = list(active_snapshots or [[]])

    def get_market_index(self, symbol):
        assert symbol == "BTC"
        return 1

    async def _call_api(self, _name, factory, **_kwargs):
        return await factory()

    @staticmethod
    def _require_success_response(response, _name):
        if response.code != 200:
            raise RuntimeError("request failed")

    @staticmethod
    def _get_symbol_from_market_index(market_index):
        assert market_index == 1
        return "BTC"

    @staticmethod
    def _parse_order(order_info, symbol):
        return SimpleNamespace(
            id=str(order_info.order_index),
            client_id=None,
            status=order_info.status,
            symbol=symbol,
        )

    async def get_open_orders(self, symbol):
        assert symbol == "BTC"
        if len(self.active_snapshots) > 1:
            snapshot = self.active_snapshots.pop(0)
        else:
            snapshot = self.active_snapshots[0]
        return [
            SimpleNamespace(id=str(order_id), status="open")
            for order_id in snapshot
        ]


class SelectiveCancelPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_120_cancellations_follow_history_cursor(self):
        ids = list(range(1, 121))
        rest = Rest(
            {
                None: HistoryPage(range(21, 121), "page-2"),
                "page-2": HistoryPage(range(1, 21), None),
            }
        )

        with patch.object(module.asyncio, "sleep", new=AsyncMock()):
            report = await module.cancel_orders_batch(rest, "BTC", ids)

        self.assertEqual(report.cancelled, {str(value) for value in ids})
        self.assertEqual(report.uncertain, set())
        self.assertEqual([len(chunk) for _, chunk in rest.sent], [50, 50, 20])
        self.assertEqual(
            [call["cursor"] for call in rest.order_api.calls],
            [None, "page-2"],
        )
        self.assertTrue(
            all(call["market_id"] == 1 for call in rest.order_api.calls)
        )
        self.assertTrue(all(market == 1 for market, _ in rest.sent))

    async def test_two_empty_active_snapshots_finish_history_lag(self):
        rest = Rest(
            {None: HistoryPage([], None)},
            active_snapshots=[[], [], []],
        )

        with patch.object(module.asyncio, "sleep", new=AsyncMock()):
            report = await module.cancel_orders_batch(rest, "BTC", [301, 302])

        self.assertEqual(report.cancelled, {"301", "302"})
        self.assertEqual(report.cancelled_by_absence, {"301", "302"})
        self.assertEqual(report.uncertain, set())

    async def test_active_target_remains_incomplete(self):
        rest = Rest(
            {None: HistoryPage([], None)},
            active_snapshots=[[401], [401], [401]],
        )

        with patch.object(module.asyncio, "sleep", new=AsyncMock()):
            report = await module.cancel_orders_batch(rest, "BTC", [401])

        self.assertEqual(report.cancelled, set())
        self.assertEqual(report.still_open, {"401"})
        self.assertEqual(report.uncertain, {"401"})


class SelectiveCancelInstallTests(unittest.TestCase):
    def test_single_module_installs_paginated_cancel(self):
        import importlib

        from core.adapters.exchanges.adapters import lighter_selective_cancel as merged
        from core.adapters.exchanges.adapters.lighter import LighterAdapter
        from core.adapters.exchanges.adapters.lighter_rest import LighterRest

        self.assertIs(LighterRest.cancel_orders_batch, merged.cancel_orders_batch)
        self.assertIs(LighterAdapter.cancel_orders, merged.adapter_cancel_orders)
        self.assertIs(
            LighterAdapter.cancel_all_orders, merged.adapter_cancel_all_disabled
        )
        self.assertEqual(LighterRest._selective_cancel_version, merged.PATCH_VERSION)
        self.assertEqual(merged.PATCH_VERSION, "2026-09-24.1")
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module(
                "core.adapters.exchanges.adapters.lighter_selective_cancel_v3"
            )


if __name__ == "__main__":
    unittest.main()
