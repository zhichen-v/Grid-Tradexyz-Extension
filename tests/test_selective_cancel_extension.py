import asyncio
import importlib.util
import logging
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from lighter.exceptions import BadRequestException, ServiceException
from lighter.models.result_code import ResultCode
from lighter.nonce_manager import OptimisticNonceManager

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PACKAGE = "selective_cancel_extension_testpkg"
package = ModuleType(PACKAGE)
package.__path__ = []
sys.modules[PACKAGE] = package
lighter = load(
    f"{PACKAGE}.lighter_selective_cancel",
    "core/adapters/exchanges/adapters/lighter_selective_cancel.py",
)
paginated = load(
    f"{PACKAGE}.lighter_selective_cancel_v3",
    "core/adapters/exchanges/adapters/lighter_selective_cancel_v3.py",
)
grid = load("grid_cancel_test", "core/services/grid/selective_cancel.py")


class Nonces:
    def __init__(self):
        self.value = 10
        self.rollbacks = 0

    @asynccontextmanager
    async def lock(self, _key):
        yield

    async def async_next_nonce(self, key):
        value = self.value
        self.value += 1
        return key, value

    def acknowledge_failure(self, _key):
        self.rollbacks += 1
        self.value -= 1


class Signer:
    def __init__(self, error=None, hang=False):
        self.nonce_manager = Nonces()
        self.error = error
        self.hang = hang
        self.sent = []
        self.signed = []

    def sign_cancel_order(self, **kwargs):
        self.signed.append(kwargs)
        return 15, str(kwargs), "hash", None

    async def send_tx_batch(self, **kwargs):
        self.sent.append(kwargs)
        if self.hang:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return SimpleNamespace(code=200, predicted_execution_time_ms=0)


class Rest:
    MUTATION_RECONCILIATION_ATTEMPTS = 1
    MUTATION_RECONCILIATION_DELAY = 0

    def __init__(self, ids, error=None, active=False, hang=False, history_empty=False):
        self.config = {
            "cancel_batch_size": 50,
            "cancel_send_timeout": 0.01 if hang else 2,
            "cancel_read_timeout": 2,
            "cancel_reconcile_attempts": 1,
            "cancel_reconcile_group_size": 80,
            "cancel_execution_wait_max": 1,
        }
        self.api_key_index = 2
        self.signer_client = Signer(error, hang=hang)
        self._uncertain_cancellations = set()
        self.ids = {str(i) for i in ids}
        self.active = active
        self.history_empty = history_empty
        self.open_reads = self.history_reads = 0

    async def _call_api(self, _name, factory, retry_on_429=False):
        self.retry_on_429 = retry_on_429
        return await factory()

    @staticmethod
    def _is_rate_limited(value):
        return "429" in str(value)

    @staticmethod
    def get_market_index(_symbol):
        return 7

    async def get_open_orders(self, _symbol):
        self.open_reads += 1
        if not self.active:
            return []
        return [SimpleNamespace(id=i, status="open") for i in self.ids]

    async def get_order_history(self, _symbol, limit=100):
        self.history_reads += 1
        if self.active or self.history_empty:
            return []
        return [SimpleNamespace(id=i, status="canceled") for i in self.ids]


class LighterBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_51_ids_use_two_mutations_and_one_bulk_snapshot(self):
        ids = list(range(1, 52))
        rest = Rest(ids)
        with patch.object(lighter.asyncio, "sleep", new=AsyncMock()):
            report = await lighter.cancel_orders_batch(rest, "BTC", ids)

        self.assertEqual(
            [len(item["tx_types"]) for item in rest.signer_client.sent],
            [50, 1],
        )
        self.assertEqual(rest.open_reads, 1)
        self.assertEqual(rest.history_reads, 1)
        self.assertEqual(report.cancelled, {str(item) for item in ids})
        self.assertEqual(
            [item["nonce"] for item in rest.signer_client.signed],
            list(range(10, 61)),
        )

    async def test_transport_loss_is_uncertain_and_never_resubmitted(self):
        rest = Rest([101], ConnectionError("lost"), history_empty=True)
        first = await lighter.cancel_orders_batch(rest, "BTC", [101])
        self.assertEqual(first.uncertain, {"101"})

        rest.signer_client.error = None
        rest.active = True
        second = await lighter.cancel_orders_batch(rest, "BTC", [101])
        self.assertEqual(len(rest.signer_client.sent), 1)
        self.assertEqual(second.uncertain, {"101"})

    async def test_send_timeout_is_uncertain_and_does_not_rollback_nonce(self):
        rest = Rest([151], hang=True, history_empty=True)
        report = await lighter.cancel_orders_batch(rest, "BTC", [151])
        self.assertEqual(report.uncertain, {"151"})
        self.assertEqual(rest.signer_client.nonce_manager.rollbacks, 0)
        self.assertIn(("BTC", "151"), rest._uncertain_cancellations)

    async def test_429_rolls_back_nonces_and_is_retryable(self):
        rest = Rest([201, 202], RuntimeError("HTTP 429"))
        report = await lighter.cancel_orders_batch(rest, "BTC", [201, 202])
        self.assertEqual(
            report.rejected,
            {"201": "HTTP 429", "202": "HTTP 429"},
        )
        self.assertEqual(rest.signer_client.nonce_manager.rollbacks, 2)
        self.assertEqual(report.uncertain, set())

    async def test_account_wide_cancel_is_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "account-wide"):
            await lighter.adapter_cancel_all_disabled(SimpleNamespace(), "BTC")


class LighterBatchNonceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def rejection(*, raw_body=False, code=21104):
        if raw_body:
            return BadRequestException(
                status=400, body='{"code":21104,"message":"invalid nonce"}',
            )
        return BadRequestException(
            status=400, data=ResultCode(code=code, message="invalid nonce"),
        )

    def rest(self, outcomes, *, active=False):
        rest = Rest([301, 302], active=active)
        manager = OptimisticNonceManager(7, SimpleNamespace(), [2])
        manager.nonce[2] = 9

        async def fetch_nonce(key):
            self.assertEqual(key, 2)
            self.assertTrue(manager.lock(key).locked())
            return 55

        manager._fetch_nonce = AsyncMock(side_effect=fetch_nonce)
        rest.signer_client.nonce_manager = manager
        rest.signer_client.send_tx_batch = AsyncMock(side_effect=outcomes)
        rest.signer_client.create_auth_token_with_expiry = lambda **_kwargs: ("test-token", None)
        return rest, manager

    @staticmethod
    def accepted():
        return SimpleNamespace(code=200, predicted_execution_time_ms=0)

    async def cancel(self, module, rest):
        with patch.object(lighter.asyncio, "sleep", new=AsyncMock()):
            return await module.cancel_orders_batch(rest, "BTC", [301, 302])

    async def test_sdk_nonce_rejection_refreshes_and_resigns_once_for_base_and_v3(self):
        for module in (lighter, paginated):
            for raw_body in (False, True):
                with self.subTest(module=module.__name__, raw_body=raw_body):
                    rest, manager = self.rest([
                        self.rejection(raw_body=raw_body), self.accepted(),
                    ])
                    report = await self.cancel(module, rest)
                    self.assertEqual(report.cancelled, {"301", "302"})
                    self.assertEqual(report.acknowledged, {"301", "302"})
                    self.assertEqual(report.rejected, {})
                    self.assertEqual(rest._uncertain_cancellations, set())
                    self.assertEqual(rest.signer_client.send_tx_batch.await_count, 2)
                    manager._fetch_nonce.assert_awaited_once_with(2)
                    signed = rest.signer_client.signed
                    self.assertEqual([row["nonce"] for row in signed], [10, 11, 55, 56])
                    self.assertEqual([row["order_index"] for row in signed], [301, 302, 301, 302])
                    self.assertTrue(all(row["api_key_index"] == 2 for row in signed))
                    self.assertFalse(rest.retry_on_429)

    async def test_recurrent_nonce_rejection_is_bounded_and_not_read_only_on_outer_retry(self):
        for module in (lighter, paginated):
            with self.subTest(module=module.__name__):
                rest, manager = self.rest([self.rejection(), self.rejection()])
                report = await self.cancel(module, rest)
                self.assertEqual(set(report.rejected), {"301", "302"})
                self.assertEqual(report.uncertain, set())
                self.assertEqual(rest._uncertain_cancellations, set())
                self.assertEqual(rest.signer_client.send_tx_batch.await_count, 2)
                self.assertEqual(manager._fetch_nonce.await_count, 2)
                self.assertEqual(rest.open_reads, 0)

                rest.signer_client.send_tx_batch.side_effect = None
                rest.signer_client.send_tx_batch.return_value = self.accepted()
                retry = await self.cancel(module, rest)
                self.assertEqual(retry.cancelled, {"301", "302"})
                self.assertEqual(rest.signer_client.send_tx_batch.await_count, 3)

    async def test_failed_nonce_refresh_does_not_resend_or_mark_cancellation_uncertain(self):
        for module in (lighter, paginated):
            for error in (ConnectionError("nonce lookup unavailable"), asyncio.TimeoutError()):
                with self.subTest(module=module.__name__, error=type(error).__name__):
                    rest, manager = self.rest([self.rejection(), self.accepted()])
                    manager._fetch_nonce.side_effect = error
                    report = await self.cancel(module, rest)
                    self.assertEqual(set(report.rejected), {"301", "302"})
                    self.assertEqual(report.uncertain, set())
                    self.assertEqual(rest._uncertain_cancellations, set())
                    self.assertEqual(rest.signer_client.send_tx_batch.await_count, 1)
                    self.assertEqual(len(rest.signer_client.signed), 2)
                    self.assertFalse(manager.lock(2).locked())

    async def test_nonce_refresh_timeout_is_definitive_without_resend(self):
        for module in (lighter, paginated):
            with self.subTest(module=module.__name__):
                rest, manager = self.rest([self.rejection(), self.accepted()])

                async def never_returns(_key):
                    await asyncio.Event().wait()

                manager._fetch_nonce.side_effect = never_returns
                with patch.object(lighter, "_send_timeout", return_value=0.01):
                    report = await self.cancel(module, rest)
                self.assertEqual(set(report.rejected), {"301", "302"})
                self.assertEqual(report.uncertain, set())
                self.assertEqual(rest._uncertain_cancellations, set())
                self.assertEqual(rest.signer_client.send_tx_batch.await_count, 1)
                self.assertFalse(manager.lock(2).locked())

    async def test_presend_timeout_returns_reserved_nonce_before_releasing_key_lock(self):
        for module in (lighter, paginated):
            with self.subTest(module=module.__name__):
                rest, manager = self.rest([self.accepted()])
                next_nonce = manager.async_next_nonce
                rollback = manager.acknowledge_failure

                async def reserve_then_wait(key):
                    if manager.nonce[key] == 10:
                        await asyncio.Event().wait()
                    return await next_nonce(key)

                def rollback_locked(key):
                    self.assertTrue(manager.lock(key).locked())
                    rollback(key)

                manager.async_next_nonce = AsyncMock(side_effect=reserve_then_wait)
                manager.acknowledge_failure = MagicMock(side_effect=rollback_locked)
                with patch.object(lighter, "_send_timeout", return_value=0.01):
                    report = await self.cancel(module, rest)
                self.assertEqual(set(report.rejected), {"301", "302"})
                self.assertEqual(report.uncertain, set())
                self.assertEqual(rest._uncertain_cancellations, set())
                rest.signer_client.send_tx_batch.assert_not_awaited()
                manager.acknowledge_failure.assert_called_once_with(2)
                self.assertEqual(manager.nonce[2], 9)
                self.assertFalse(manager.lock(2).locked())

    async def test_502_or_timeout_remains_uncertain_without_refresh_or_resend(self):
        for module in (lighter, paginated):
            for error in (ServiceException(status=502, body="Bad Gateway"), asyncio.TimeoutError()):
                with self.subTest(module=module.__name__, error=type(error).__name__):
                    rest, manager = self.rest([error], active=True)
                    first = await self.cancel(module, rest)
                    second = await self.cancel(module, rest)
                    self.assertEqual(first.uncertain, {"301", "302"})
                    self.assertEqual(second.uncertain, {"301", "302"})
                    self.assertEqual(first.rejected, {})
                    self.assertEqual(rest.signer_client.send_tx_batch.await_count, 1)
                    manager._fetch_nonce.assert_not_awaited()
                    self.assertEqual(manager.nonce[2], 11)

    async def test_502_after_nonce_recovery_retains_second_attempt_nonces_and_markers(self):
        for module in (lighter, paginated):
            with self.subTest(module=module.__name__):
                rest, manager = self.rest([
                    self.rejection(), ServiceException(status=502, body="Bad Gateway"),
                ], active=True)
                report = await self.cancel(module, rest)
                self.assertEqual(report.uncertain, {"301", "302"})
                self.assertEqual(report.rejected, {})
                await self.cancel(module, rest)
                self.assertEqual(rest.signer_client.send_tx_batch.await_count, 2)
                manager._fetch_nonce.assert_awaited_once_with(2)
                self.assertEqual(manager.nonce[2], 56)
                self.assertEqual(rest._uncertain_cancellations, {("BTC", "301"), ("BTC", "302")})

    async def test_unrelated_or_unstructured_error_does_not_trigger_nonce_resend(self):
        for error in (
            self.rejection(code=21105),
            RuntimeError("HTTP 400 code=21104 invalid nonce"),
            ServiceException(status=502, data=ResultCode(code=21104, message="invalid nonce")),
        ):
            with self.subTest(error=str(error)):
                rest, manager = self.rest([error], active=True)
                report = await self.cancel(lighter, rest)
                self.assertEqual(report.uncertain, {"301", "302"})
                self.assertEqual(rest.signer_client.send_tx_batch.await_count, 1)
                manager._fetch_nonce.assert_not_awaited()

    def test_batch_diagnostics_reach_rest_file_handler(self):
        parent = logging.getLogger("core.adapters.exchanges.adapters.lighter_rest")
        with tempfile.TemporaryDirectory(dir=".", prefix="cancel-log-test-") as directory:
            path = Path(directory) / "adapter.log"
            handler = logging.FileHandler(path, encoding="utf-8")
            parent.addHandler(handler)
            try:
                for module in (lighter, paginated):
                    module.logger.warning("cancel regression %s", module.PATCH_VERSION)
                handler.flush()
                messages = path.read_text(encoding="utf-8")
                for module in (lighter, paginated):
                    self.assertIn(f"cancel regression {module.PATCH_VERSION}", messages)
            finally:
                parent.removeHandler(handler)
                handler.close()


class Order:
    def __init__(self, order_id):
        self.order_id = str(order_id)
        self.grid_id = int(order_id)
        self.cancelled = False
        self.exchange_data = {}

    def mark_cancelled(self):
        self.cancelled = True


class Engine:
    def __init__(self, ids):
        self.config = SimpleNamespace(exchange="lighter", symbol="BTC")
        self.orders = {str(item): Order(item) for item in ids}
        self._pending_orders = dict(self.orders)
        self._expected_cancellations = set()
        self._uncertain_cancel_order_ids = set()
        self._last_selective_cancel_report = None
        self.exchange = SimpleNamespace(config=SimpleNamespace(exchange_id="lighter"))
        self.coordinator = SimpleNamespace(
            state=SimpleNamespace(active_orders=dict(self.orders))
        )
        self.logger = MagicMock()
        self._placements_drained = asyncio.Event()
        self._placements_drained.set()
        self._resolve_unresolved_submissions_read_only = AsyncMock(return_value=([], []))
        self._unresolved_submission_descriptions = MagicMock(return_value=[])

    def _find_cached_order(self, *ids):
        for order_id in ids:
            if str(order_id) in self._pending_orders:
                return str(order_id), self._pending_orders[str(order_id)]
        return None, None

    def _pending_keys_for_order(self, order):
        return [
            key for key, value in self._pending_orders.items()
            if value is order
        ]

    def _claim_order_finalization(self, *_args):
        return True

    def _clear_restore_state(self, _order):
        pass

    def _remove_order_from_coordinator_state(self, order):
        self.coordinator.state.active_orders.pop(order.order_id, None)

    def _clear_pending_order_refs(self, *ids):
        _, order = self._find_cached_order(*ids)
        if order:
            for key in self._pending_keys_for_order(order):
                self._pending_orders.pop(key)

    def _consume_expected_cancellation(self, *ids):
        for order_id in ids:
            self._expected_cancellations.discard(str(order_id))

    async def _handle_exchange_order_object(self, _order):
        pass

    def get_pending_orders(self):
        return list(self._pending_orders.values())

    @staticmethod
    def _string_or_none(value):
        return None if value in (None, "") else str(value)

    async def cancel_orders(self, ids):
        return await grid.engine_cancel_orders(self, ids)


class ShutdownCancellationSafetyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def cancellation_report(ids, _symbol):
        return SimpleNamespace(
            requested=set(ids),
            acknowledged=set(ids),
            cancelled=set(ids),
            filled=set(),
            still_open=set(),
            uncertain=set(),
            rejected={},
            terminal_orders={},
        )

    def engine_with_unresolved_order(self, client_id):
        engine = Engine([101, 102, 103])
        phantom = engine._pending_orders.pop("103")
        phantom.order_id = client_id
        phantom.exchange_data["submission_uncertain"] = True
        engine._pending_orders[client_id] = phantom
        engine._unresolved_submission_descriptions.return_value = [
            f"limit:{client_id}", "adapter:1790173573186"
        ]
        engine.exchange.cancel_orders = AsyncMock(side_effect=self.cancellation_report)
        return engine, phantom

    async def test_unresolved_submission_does_not_block_known_owned_cancellation(self):
        for client_id in ("grid_44_769_130000", "1790173573186"):
            with self.subTest(client_id=client_id):
                engine, phantom = self.engine_with_unresolved_order(client_id)
                with self.assertRaisesRegex(RuntimeError, "orders lack exact exchange IDs"):
                    await grid.engine_cancel_all_owned(engine)
                engine.exchange.cancel_orders.assert_awaited_once_with(["101", "102"], "BTC")
                self.assertEqual(engine.get_pending_orders(), [phantom])
                self.assertFalse(phantom.cancelled)

                # Retrying unsafe cleanup must not resend proven cancellations.
                with self.assertRaisesRegex(RuntimeError, "orders lack exact exchange IDs"):
                    await grid.engine_cancel_all_owned(engine)
                self.assertEqual(engine.exchange.cancel_orders.await_count, 1)

    async def test_read_failures_and_reconciled_fill_preserve_known_order_cleanup(self):
        for failure in ("resolve", "inspect", "filled"):
            with self.subTest(failure=failure):
                engine = Engine([101, 102])
                engine.exchange.cancel_orders = AsyncMock(side_effect=self.cancellation_report)
                if failure == "resolve":
                    engine._resolve_unresolved_submissions_read_only.side_effect = RuntimeError("read unavailable")
                    message = "submission reconciliation failed: read unavailable"
                elif failure == "inspect":
                    engine._unresolved_submission_descriptions.side_effect = RuntimeError("registry unavailable")
                    message = "unresolved submission inspection failed: registry unavailable"
                else:
                    engine._resolve_unresolved_submissions_read_only.return_value = ([], ["999"])
                    message = "uncertain submissions resolved as filled: 999"
                with self.assertRaisesRegex(RuntimeError, message):
                    await grid.engine_cancel_all_owned(engine)
                engine.exchange.cancel_orders.assert_awaited_once_with(["101", "102"], "BTC")
                self.assertEqual(engine.get_pending_orders(), [])

    async def test_invalid_placeholder_does_not_block_known_order_cleanup(self):
        engine, phantom = self.engine_with_unresolved_order("pending")
        phantom.exchange_data.clear()
        engine._unresolved_submission_descriptions.return_value = []
        with self.assertRaisesRegex(RuntimeError, "non-final Lighter order IDs: pending"):
            await grid.engine_cancel_all_owned(engine)
        engine.exchange.cancel_orders.assert_awaited_once_with(["101", "102"], "BTC")
        self.assertEqual(engine.get_pending_orders(), [phantom])

    async def test_cancellation_failure_keeps_original_submission_uncertainty(self):
        engine, phantom = self.engine_with_unresolved_order("1790173573186")
        engine.exchange.cancel_orders.side_effect = RuntimeError("cancel response lost")
        with self.assertRaises(RuntimeError) as raised:
            await grid.engine_cancel_all_owned(engine)
        self.assertIn("orders lack exact exchange IDs", str(raised.exception))
        self.assertIn("selective cancellation failed: cancel response lost", str(raised.exception))
        engine.exchange.cancel_orders.assert_awaited_once_with(["101", "102"], "BTC")
        self.assertEqual(len(engine.get_pending_orders()), 3)
        self.assertFalse(phantom.cancelled)

    async def test_task_cancellation_is_not_swallowed(self):
        for phase in ("reconcile", "cancel"):
            with self.subTest(phase=phase):
                engine = Engine([101])
                engine.exchange.cancel_orders = AsyncMock(side_effect=self.cancellation_report)
                if phase == "reconcile":
                    engine._resolve_unresolved_submissions_read_only.side_effect = asyncio.CancelledError
                else:
                    engine.exchange.cancel_orders.side_effect = asyncio.CancelledError
                with self.assertRaises(asyncio.CancelledError):
                    await grid.engine_cancel_all_owned(engine)
                self.assertEqual(len(engine.get_pending_orders()), 1)
                if phase == "reconcile":
                    engine.exchange.cancel_orders.assert_not_awaited()

    async def test_exact_reconciliation_makes_previously_unknown_order_cancellable(self):
        engine, phantom = self.engine_with_unresolved_order("1790173573186")

        async def reconcile():
            engine._pending_orders.pop(phantom.order_id)
            phantom.order_id = "104"
            phantom.exchange_data["submission_uncertain"] = False
            engine._pending_orders[phantom.order_id] = phantom
            engine._unresolved_submission_descriptions.return_value = []
            return ["104"], []

        engine._resolve_unresolved_submissions_read_only.side_effect = reconcile
        self.assertEqual(await grid.engine_cancel_all_owned(engine), 3)
        engine.exchange.cancel_orders.assert_awaited_once_with(["101", "102", "104"], "BTC")
        self.assertEqual(engine.get_pending_orders(), [])

    async def test_fill_incident_stays_unsafe_after_retry_has_no_pending_orders(self):
        for phase in ("reconcile", "cancel"):
            with self.subTest(phase=phase):
                engine = Engine([101, 102])
                if phase == "reconcile":
                    engine._resolve_unresolved_submissions_read_only.side_effect = [
                        ([], ["999"]), ([], [])
                    ]
                    engine.exchange.cancel_orders = AsyncMock(side_effect=self.cancellation_report)
                else:
                    report = self.cancellation_report(["101", "102"], "BTC")
                    report.cancelled.remove("102")
                    report.filled.add("102")
                    report.terminal_orders["102"] = SimpleNamespace(id="102")
                    engine.exchange.cancel_orders = AsyncMock(return_value=report)
                    engine._handle_exchange_order_object = AsyncMock(
                        side_effect=lambda order: engine._clear_pending_order_refs(order.id)
                    )
                with self.assertRaisesRegex(RuntimeError, "filled"):
                    await grid.engine_cancel_all_owned(engine)
                self.assertEqual(engine.get_pending_orders(), [])
                with self.assertRaisesRegex(RuntimeError, "filled"):
                    await grid.engine_cancel_all_owned(engine)
                engine.exchange.cancel_orders.assert_awaited_once_with(["101", "102"], "BTC")


class GridOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owned_ids_reach_exchange(self):
        engine = Engine([301, 302])
        report = SimpleNamespace(
            requested={"301", "302"},
            acknowledged={"301", "302"},
            cancelled={"301", "302"},
            filled=set(),
            still_open=set(),
            uncertain=set(),
            rejected={},
            terminal_orders={},
        )
        engine.exchange.cancel_orders = AsyncMock(return_value=report)
        await grid.engine_cancel_orders(engine, [301, 302])
        engine.exchange.cancel_orders.assert_awaited_once_with(
            ["301", "302"],
            "BTC",
        )
        self.assertEqual(engine._pending_orders, {})

    async def test_unowned_id_blocks_entire_mutation(self):
        engine = Engine([401])
        engine.exchange.cancel_orders = AsyncMock()
        report = await grid.engine_cancel_orders(engine, [401, 999])
        self.assertIn("999", report.rejected)
        engine.exchange.cancel_orders.assert_not_awaited()


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_uses_local_ids_not_exchange_side_scan(self):
        engine = Engine([501, 502])
        engine.orders["501"].side = "sell"
        engine.orders["502"].side = "buy"
        report = SimpleNamespace(
            requested={"501"},
            acknowledged={"501"},
            cancelled={"501"},
            filled=set(),
            still_open=set(),
            uncertain=set(),
            rejected={},
            terminal_orders={},
        )
        engine.cancel_orders = AsyncMock(return_value=report)
        ops = SimpleNamespace(
            engine=engine,
            state=engine.coordinator.state,
            logger=MagicMock(),
        )
        ok = await grid.ops_cancel_filtered(
            ops,
            lambda order: order.side == "sell",
            "sell orders",
            3,
        )
        self.assertTrue(ok)
        engine.cancel_orders.assert_awaited_once_with(["501"])

    async def test_global_zero_order_verifier_is_not_used(self):
        engine = SimpleNamespace(
            config=SimpleNamespace(exchange="lighter"),
            cancel_all_orders=AsyncMock(return_value=2),
        )
        ops = SimpleNamespace(
            engine=engine,
            logger=MagicMock(),
            verifier=SimpleNamespace(get_open_orders_count=AsyncMock()),
        )
        self.assertTrue(await grid.ops_cancel_all(ops))
        ops.verifier.get_open_orders_count.assert_not_awaited()

    async def test_second_cancellation_does_not_abort_critical_stop(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def legacy_stop():
            started.set()
            await release.wait()

        coordinator = SimpleNamespace(
            engine=SimpleNamespace(
                config=SimpleNamespace(exchange="lighter"),
                exchange=SimpleNamespace(_rest=SimpleNamespace(config={
                    "shutdown_cleanup_timeout": 2,
                })),
            ),
            _legacy_stop=legacy_stop,
            _selective_stop_task=None,
            _unsafe_shutdown_incident=None,
            logger=MagicMock(),
        )

        task = asyncio.create_task(grid.coordinator_stop_guarded(coordinator))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        await task
        coordinator.logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
