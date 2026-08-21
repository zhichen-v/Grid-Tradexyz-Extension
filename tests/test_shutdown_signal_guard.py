import importlib.util
import signal
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "grid_signal_guard_test",
    ROOT / "core/services/grid/signal_guard.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Loop:
    def is_closed(self):
        return False

    def call_soon_threadsafe(self, callback, *args):
        callback(*args)


class SignalGuardTests(unittest.TestCase):
    def test_first_sigint_cancels_main_and_second_is_ignored(self):
        task = MagicMock()
        task.done.return_value = False
        guard = module.ProcessSigintGuard(
            Loop(),
            task,
            signal.default_int_handler,
        )

        with patch.object(module.logger, "warning") as warning:
            guard.handle(signal.SIGINT, None)
            guard.handle(signal.SIGINT, None)

        task.cancel.assert_called_once_with()
        warning.assert_called_once()

    def test_done_task_delegates_to_previous_handler(self):
        task = MagicMock()
        task.done.return_value = True
        previous = MagicMock()
        guard = module.ProcessSigintGuard(Loop(), task, previous)

        guard.handle(signal.SIGINT, None)

        previous.assert_called_once_with(signal.SIGINT, None)


if __name__ == "__main__":
    unittest.main()
