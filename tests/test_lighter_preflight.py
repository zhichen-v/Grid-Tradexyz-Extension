"""Tests for the authenticated read-only Lighter preflight."""

import tempfile
import unittest
import asyncio
import io
import sys
from contextlib import redirect_stderr
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lighter.endpoint_profiles import get_endpoint_profile

from lighter_preflight import main, preflight_from_config


class FakeAdapter:
    def __init__(self, settings):
        profile = get_endpoint_profile(settings["network"])
        self.calls = []
        self.order_writes = 0
        l1_address = settings.get("expected_l1_address") or (
            "0x1111111111111111111111111111111111111111"
        )

        class AccountApi:
            async def account(inner_self, **kwargs):
                self.calls.append("account")
                return SimpleNamespace(
                    code=200,
                    accounts=[
                        SimpleNamespace(
                            account_index=settings["account_index"],
                            l1_address=l1_address,
                        )
                    ]
                )

        self._rest = SimpleNamespace(
            network=profile.name,
            chain_id=profile.chain_id,
            base_url=profile.api_url,
            account_index=settings["account_index"],
            api_key_index=settings["api_key_index"],
            signer_client=object(),
            account_api=AccountApi(),
            normalize_symbol=lambda symbol: symbol.split("-")[0],
            markets={
                "ETH": {
                    "market_id": 0,
                    "status": "active",
                    "supported_price_decimals": 2,
                    "supported_size_decimals": 4,
                    "min_base_amount": "0.001",
                    "min_quote_amount": "10",
                }
            },
            _require_success_response=lambda response, _operation: (
                None
                if getattr(response, "code", None) == 200
                else (_ for _ in ()).throw(RuntimeError("response failed"))
            ),
        )

    async def connect(self):
        self.calls.append("connect")
        return True

    async def authenticate(self):
        self.calls.append("authenticate")
        return True

    async def get_balances(self):
        self.calls.append("get_balances")
        return [
            SimpleNamespace(
                currency="USDG",
                total=Decimal("100"),
                free=Decimal("90"),
                used=Decimal("10"),
            )
        ]

    async def get_positions(self):
        self.calls.append("get_positions")
        return [
            SimpleNamespace(
                symbol="ETH",
                side=SimpleNamespace(value="long"),
                size=Decimal("0.01"),
                entry_price=Decimal("1000"),
                leverage=1,
                margin_mode=SimpleNamespace(value="isolated"),
            )
        ]

    async def get_open_orders(self):
        self.calls.append("get_open_orders")
        return []

    async def create_order(self, *args, **kwargs):
        self.order_writes += 1

    async def cancel_order(self, *args, **kwargs):
        self.order_writes += 1

    async def disconnect(self):
        self.calls.append("disconnect")


class LighterPreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_robinhood_preflight_is_read_only_and_redacts_secret(self):
        for network in ("robinhood", "robinhood_testnet"):
            with self.subTest(network=network), tempfile.TemporaryDirectory() as directory:
                secret = "0xTHIS_MUST_NEVER_BE_PRINTED"
                wallet = "0x1234567890abcdef1234567890abcdef12345678"
                config_path = Path(directory) / "lighter_config.yaml"
                config_path.write_text(
                    "api_config:\n"
                    f"  network: {network}\n"
                    f"  testnet: {'true' if network.endswith('_testnet') else 'false'}\n"
                    "  auth:\n"
                    f"    api_key_private_key: {secret}\n"
                    "    account_index: 123\n"
                    "    api_key_index: 4\n"
                    f'    expected_l1_address: "{wallet}"\n',
                    encoding="utf-8",
                )
                output = []
                adapter = None

                def factory(settings):
                    nonlocal adapter
                    self.assertEqual(settings["api_key_private_key"], secret)
                    adapter = FakeAdapter(settings)
                    return adapter

                await preflight_from_config(
                    config_path, "ETH-USD", emit=output.append, adapter_factory=factory
                )

                rendered = "\n".join(output)
                self.assertNotIn(secret, rendered)
                self.assertIn(f"profile={network}", rendered)
                self.assertIn("wallet_check=PASS", rendered)
                self.assertIn("USDG total=100", rendered)
                self.assertIn("market=ETH market_id=0", rendered)
                self.assertIn("positions=1", rendered)
                self.assertIn("open_orders=0", rendered)
                self.assertEqual(adapter.order_writes, 0)
                self.assertEqual(
                    adapter.calls,
                    [
                        "connect",
                        "authenticate",
                        "account",
                        "get_balances",
                        "get_positions",
                        "get_open_orders",
                        "disconnect",
                    ],
                )

    async def test_expected_wallet_mismatch_fails_before_account_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "lighter_config.yaml"
            config_path.write_text(
                "api_config:\n"
                "  network: robinhood\n"
                "  testnet: false\n"
                "  auth:\n"
                "    api_key_private_key: 0xsecret\n"
                "    account_index: 1\n"
                "    api_key_index: 4\n"
                '    expected_l1_address: "0x2222222222222222222222222222222222222222"\n',
                encoding="utf-8",
            )
            adapter = None

            def factory(settings):
                nonlocal adapter
                settings = dict(settings)
                settings["expected_l1_address"] = (
                    "0x1111111111111111111111111111111111111111"
                )
                adapter = FakeAdapter(settings)
                return adapter

            with self.assertRaisesRegex(RuntimeError, "different L1 wallet"):
                await preflight_from_config(
                    config_path, "ETH", adapter_factory=factory
                )

            self.assertEqual(adapter.order_writes, 0)
            self.assertEqual(adapter.calls, ["connect", "authenticate", "account", "disconnect"])

    async def test_mainnet_profile_is_rejected_before_adapter_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "lighter_config.yaml"
            config_path.write_text(
                "api_config:\n"
                "  network: mainnet\n"
                "  testnet: false\n"
                "  auth:\n"
                "    api_key_private_key: 0xsecret\n"
                "    account_index: 1\n"
                "    api_key_index: 4\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "robinhood or robinhood_testnet"):
                await preflight_from_config(config_path, "ETH", adapter_factory=self.fail)


class LighterPreflightCliTest(unittest.TestCase):
    def test_cli_builds_signer_adapter_inside_running_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "lighter_config.yaml"
            config_path.write_text(
                "api_config:\n"
                "  network: robinhood\n"
                "  testnet: false\n"
                "  auth:\n"
                "    api_key_private_key: 0xsecret\n"
                "    account_index: 1\n"
                "    api_key_index: 4\n",
                encoding="utf-8",
            )
            adapter = None

            def factory(settings):
                nonlocal adapter
                asyncio.get_running_loop()
                adapter = FakeAdapter(settings)
                return adapter

            argv = [
                "lighter_preflight.py",
                "--config",
                str(config_path),
                "--symbol",
                "ETH",
            ]
            with patch("lighter_preflight.build_adapter", side_effect=factory), patch.object(
                sys, "argv", argv
            ):
                self.assertEqual(main(), 0)

            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.order_writes, 0)

    def test_malformed_yaml_never_prints_api_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = "0xTHIS_MUST_NOT_PRINT"
            config_path = Path(directory) / "lighter_config.yaml"
            config_path.write_text(
                "api_config:\n"
                "  network: robinhood\n"
                "  auth:\n"
                f"    api_key_private_key: [{secret}\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            argv = ["lighter_preflight.py", "--config", str(config_path)]

            with patch.object(sys, "argv", argv), redirect_stderr(stderr):
                self.assertEqual(main(), 1)

            self.assertNotIn(secret, stderr.getvalue())
            self.assertIn("Invalid YAML", stderr.getvalue())

    def test_api_key_index_255_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "lighter_config.yaml"
            config_path.write_text(
                "api_config:\n"
                "  network: robinhood\n"
                "  testnet: false\n"
                "  auth:\n"
                "    api_key_private_key: 0xsecret\n"
                "    account_index: 1\n"
                "    api_key_index: 255\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            argv = ["lighter_preflight.py", "--config", str(config_path)]

            with patch.object(sys, "argv", argv), redirect_stderr(stderr):
                self.assertEqual(main(), 1)

            self.assertIn("between 0 and 254", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
