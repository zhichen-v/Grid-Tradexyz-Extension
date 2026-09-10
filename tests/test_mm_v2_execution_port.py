"""Current V2 runtime isolation from the removed legacy package."""

import subprocess
import sys
import unittest
from pathlib import Path


class RuntimeIsolationTests(unittest.TestCase):
    def test_removed_runtime_and_launch_paths_are_absent(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("core/services/market_maker", "run_market_maker.py",
                         "config/market_maker"):
            with self.subTest(path=relative):
                self.assertFalse((root / relative).exists())

    def test_importing_current_entrypoint_and_ports_does_not_load_removed_runtime(self):
        check = subprocess.run(
            [sys.executable, "-c", "import sys; import run_volume_market_maker; "
             "from core.services.market_maker_v2.orchestrator import VolumeSession; "
             "from core.services.market_maker_v2.order_manager import MarketMakerOrderManager; "
             "from core.services.market_maker_v2.execution_port import "
             "MarketDataPort, AccountPort, Clock, TelemetrySink, ExecutionPort; "
             "assert not any(name == 'core.services.market_maker' or "
             "name.startswith('core.services.market_maker.') for name in sys.modules)"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
