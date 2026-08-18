import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from core.services.grid.terminal_ui import GridTerminalUI


class GridTerminalUITimeTests(unittest.TestCase):
    def setUp(self):
        self.ui = object.__new__(GridTerminalUI)

    def test_naive_local_fill_time_is_not_shifted(self):
        local_time = datetime(2026, 8, 16, 21, 23, 24)

        self.assertEqual(self.ui._format_display_time(local_time), "21:23:24")

    def test_aware_utc_fill_time_is_converted_to_utc_plus_eight(self):
        utc_time = datetime(2026, 8, 16, 13, 23, 24, tzinfo=timezone.utc)

        self.assertEqual(self.ui._format_display_time(utc_time), "21:23:24")

    def test_trade_panel_is_labeled_as_confirmed_fills(self):
        self.ui.coordinator = SimpleNamespace(
            tracker=SimpleNamespace(get_trade_history=lambda _limit: [])
        )
        self.ui.history_limit = 10
        self.ui.base_currency = "BTC"

        panel = self.ui.create_recent_trades_table(None)

        self.assertEqual(str(panel.title), "Confirmed Fills (Last 5)")

    def test_terminal_loop_can_detect_automatic_strategy_stop(self):
        self.ui.coordinator = SimpleNamespace(is_stopped=lambda: True)

        self.assertTrue(self.ui._coordinator_is_stopped())


if __name__ == "__main__":
    unittest.main()
