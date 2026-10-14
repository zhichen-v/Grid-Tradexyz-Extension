import asyncio
import io
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import core.logging as managed_logging


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LazyLoggingInitializationTests(unittest.TestCase):
    def test_first_logger_access_does_not_request_log_cleanup(self):
        accessors = (
            (managed_logging.get_logger, ("probe",), "_get_logger"),
            (managed_logging.get_system_logger, (), "_get_system_logger"),
        )

        for accessor, args, backend_name in accessors:
            with self.subTest(accessor=accessor.__name__):
                with (
                    patch.object(managed_logging, "_auto_initialized", False),
                    patch.object(
                        managed_logging,
                        "_initialize_logging",
                        return_value=True,
                    ) as initialize,
                    patch.object(
                        managed_logging,
                        backend_name,
                        return_value=MagicMock(),
                    ),
                ):
                    accessor(*args)

                initialize.assert_called_once_with(
                    log_dir="logs",
                    level="INFO",
                    enable_console=True,
                    clear_existing=False,
                )


class RotatingFileLogTests(unittest.TestCase):
    def test_file_logs_rotate_by_size_instead_of_truncating(self):
        from logging.handlers import RotatingFileHandler

        from core.logging.logger import BaseLogger, LogConfig

        with tempfile.TemporaryDirectory() as log_dir:
            log_file = Path(log_dir) / "rotation.probe.log"
            log_file.write_text("previous run line\n", encoding="utf-8")
            config = LogConfig(
                log_dir=log_dir,
                max_file_size=2000,
                backup_count=2,
                enable_console=False,
            )
            wrapper = BaseLogger("rotation.probe", config)
            try:
                handlers = [
                    handler
                    for handler in wrapper.logger.handlers
                    if isinstance(handler, RotatingFileHandler)
                ]
                self.assertEqual(len(handlers), 1)
                self.assertEqual(handlers[0].maxBytes, 2000)
                self.assertEqual(handlers[0].backupCount, 2)

                for index in range(200):
                    wrapper.info(f"line {index}")
                for handler in handlers:
                    handler.flush()

                backup = Path(f"{log_file}.1")
                self.assertTrue(backup.exists())
                self.assertFalse(Path(f"{log_file}.3").exists())
                self.assertIn("line 199", log_file.read_text(encoding="utf-8"))
            finally:
                for handler in list(wrapper.logger.handlers):
                    handler.close()
                    wrapper.logger.removeHandler(handler)


class CliFailureMessageTests(unittest.TestCase):
    def test_runtime_failure_is_not_reported_as_startup_failure(self):
        output = io.StringIO()

        def fail_runtime(coroutine):
            coroutine.close()
            raise RuntimeError("shutdown cleanup failed")

        with (
            patch.object(
                sys,
                "argv",
                ["run_grid_trading.py", str(Path(__file__).resolve())],
            ),
            patch.object(asyncio, "run", side_effect=fail_runtime),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as exit_context,
        ):
            runpy.run_path(
                str(PROJECT_ROOT / "run_grid_trading.py"),
                run_name="__main__",
            )

        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("Program failed: shutdown cleanup failed", output.getvalue())
        self.assertNotIn("Startup failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
