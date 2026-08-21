import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "grid_selective_cancel_logger_test",
    ROOT / "core/services/grid/selective_cancel.py",
)
grid = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = grid
spec.loader.exec_module(grid)


class StrictLogger:
    """Match BaseLogger's one-positional-message calling convention."""

    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message, **kwargs):
        self.warnings.append(message)

    def error(self, message, **kwargs):
        self.errors.append(message)


class Order:
    def __init__(self, order_id):
        self.order_id = str(order_id)
        self.grid_id = int(order_id)
        self.cancelled = False

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
        self.exchange = SimpleNamespace(
            config=SimpleNamespace(exchange_id="lighter"),
            _rest=SimpleNamespace(config={}),
        )
        self.coordinator = SimpleNamespace(
            state=SimpleNamespace(active_orders=dict(self.orders))
        )
        self.logger = StrictLogger()
        self._placements_drained = asyncio.Event()
        self._placements_drained.set()

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

    async def _resolve_unresolved_submissions_read_only(self):
        return [], []

    def _unresolved_submission_descriptions(self):
        return []

    async def cancel_orders(self, ids):
        return await grid.engine_cancel_orders(self, ids)


class LoggerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_route_accepts_base_logger_signature(self):
        engine = Engine([101, 102])
        report = SimpleNamespace(
            requested={"101", "102"},
            acknowledged={"101", "102"},
            cancelled={"101", "102"},
            filled=set(),
            still_open=set(),
            uncertain=set(),
            rejected={},
            terminal_orders={},
        )
        engine.exchange.cancel_orders = AsyncMock(return_value=report)

        count = await grid.engine_cancel_all_owned(engine)

        self.assertEqual(count, 2)
        self.assertEqual(engine.get_pending_orders(), [])
        self.assertTrue(
            any("version=" in message for message in engine.logger.warnings)
        )
        self.assertTrue(
            any("selectively cancel 2" in message for message in engine.logger.warnings)
        )
        self.assertTrue(
            any("cancelled=2" in message for message in engine.logger.warnings)
        )

    async def test_ops_error_accepts_base_logger_signature(self):
        strict_logger = StrictLogger()
        engine = SimpleNamespace(
            config=SimpleNamespace(exchange="lighter"),
            cancel_all_orders=AsyncMock(side_effect=RuntimeError("boom")),
            _last_selective_cancel_report=None,
        )
        ops = SimpleNamespace(engine=engine, logger=strict_logger)

        self.assertFalse(await grid.ops_cancel_all(ops, max_retries=1))
        self.assertEqual(
            strict_logger.errors,
            ["selective Lighter cancellation failed: boom"],
        )

    async def test_second_cancel_warning_accepts_base_logger_signature(self):
        started = asyncio.Event()
        release = asyncio.Event()
        strict_logger = StrictLogger()

        async def legacy_stop():
            started.set()
            await release.wait()

        coordinator = SimpleNamespace(
            engine=SimpleNamespace(
                config=SimpleNamespace(exchange="lighter"),
                exchange=SimpleNamespace(
                    _rest=SimpleNamespace(
                        config={"shutdown_cleanup_timeout": 10}
                    )
                ),
            ),
            _legacy_stop=legacy_stop,
            _selective_stop_task=None,
            _unsafe_shutdown_incident=None,
            logger=strict_logger,
        )

        task = asyncio.create_task(grid.coordinator_stop_guarded(coordinator))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        await task

        self.assertEqual(len(strict_logger.warnings), 1)
        self.assertIn("10.0s timeout", strict_logger.warnings[0])


if __name__ == "__main__":
    unittest.main()
