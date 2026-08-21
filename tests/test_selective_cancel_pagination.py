import asyncio
import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "selective_cancel_v3_testpkg.adapters"


for package_name in (
    "selective_cancel_v3_testpkg",
    PACKAGE,
):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package


base = types.ModuleType(f"{PACKAGE}.lighter_selective_cancel")
base.CANCELLED = {"canceled", "cancelled", "rejected", "expired"}
base.FILLED = {"filled", "closed"}


@dataclass
class CancelReport:
    requested: set = field(default_factory=set)
    acknowledged: set = field(default_factory=set)
    cancelled: set = field(default_factory=set)
    filled: set = field(default_factory=set)
    still_open: set = field(default_factory=set)
    uncertain: set = field(default_factory=set)
    rejected: dict = field(default_factory=dict)
    terminal_orders: dict = field(default_factory=dict)

    def merge_resolution(self, other):
        terminal = other.cancelled | other.filled
        self.cancelled.update(other.cancelled)
        self.filled.update(other.filled)
        self.terminal_orders.update(other.terminal_orders)
        self.still_open.difference_update(terminal)
        self.uncertain.difference_update(terminal)
        self.still_open.update(other.still_open)
        self.uncertain.update(other.uncertain)


class BatchSubmissionError(RuntimeError):
    def __init__(self, message, *, ambiguous=False, rate_limited=False):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.rate_limited = rate_limited


def order_keys(order):
    return {
        str(value)
        for value in (
            getattr(order, "id", None),
            getattr(order, "client_id", None),
            getattr(order, "order_id", None),
        )
        if value not in (None, "")
    }


def validate_ids(values):
    report = CancelReport()
    indexes = []
    for value in values:
        index = int(value)
        report.requested.add(str(index))
        indexes.append(index)
    return indexes, report


async def send_chunk(rest, market, indexes):
    rest.sent.append((market, list(indexes)))
    return SimpleNamespace(code=200, predicted_execution_time_ms=0)


async def disabled_cancel_all(*_args, **_kwargs):
    raise RuntimeError("account-wide cancel disabled")


base.CancelReport = CancelReport
base.BatchSubmissionError = BatchSubmissionError
base._keys = order_keys
base._status = lambda order: str(
    getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))
    or ""
).lower()
base._validated_ids = validate_ids
base._batch_size = lambda _rest: 50
base._send_chunk = send_chunk
base.adapter_cancel_all_disabled = disabled_cancel_all
sys.modules[base.__name__] = base


spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.lighter_selective_cancel_v3",
    ROOT / "core/adapters/exchanges/adapters/lighter_selective_cancel_v3.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


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


if __name__ == "__main__":
    unittest.main()
