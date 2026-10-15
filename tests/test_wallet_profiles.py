"""Wallet profiles live under config/exchanges/wallets/<name>.yaml."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_grid_trading
from run_grid_trading import WalletProfileError, _load_wallet_profile


class WalletProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(run_grid_trading, "WALLET_PROFILES_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        (Path(self.tmp.name) / f"{name}.yaml").write_text(text, encoding="utf-8")

    def test_loads_authentication_block(self):
        self.write("main", "authentication:\n  wallet_address: '0xabc'\n  private_key: '0xkey'\n")
        profile = _load_wallet_profile("main")
        self.assertEqual(profile["wallet_address"], "0xabc")
        self.assertEqual(profile["private_key"], "0xkey")

    def test_missing_key_and_missing_file_fail(self):
        self.write("half", "authentication:\n  wallet_address: '0xabc'\n")
        with self.assertRaisesRegex(WalletProfileError, "private_key"):
            _load_wallet_profile("half")
        with self.assertRaisesRegex(WalletProfileError, "Available profiles: half"):
            _load_wallet_profile("nope")

    def test_yaml_error_does_not_echo_contents(self):
        self.write("bad", "authentication: [0xSECRET\n")
        with self.assertRaises(WalletProfileError) as ctx:
            _load_wallet_profile("bad")
        self.assertNotIn("SECRET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
