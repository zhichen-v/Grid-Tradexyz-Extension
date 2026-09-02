from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import run_market_maker as entrypoint
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.coordinator import MarketMakerCoordinator
from core.services.market_maker.models import PositionSnapshot


class CoordinatorEvidenceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_boundary_uses_one_audit_and_preserves_newer_ws_position(
        self,
    ) -> None:
        adapter = SimpleNamespace(
            get_positions=AsyncMock(),
            get_open_orders=AsyncMock(),
        )
        monitor = SimpleNamespace(
            last_audit_authenticated=True,
            audited_position=Decimal("0"),
            audited_open_order_count=0,
        )
        coordinator = MarketMakerCoordinator(
            adapter,
            MarketMakerConfig(),
            account_monitor=monitor,
            authenticated_evidence=True,
        )
        coordinator._authenticated = True
        newer_position = PositionSnapshot(
            symbol="BTC",
            signed_size=Decimal("0.001"),
            entry_price=Decimal("100000"),
            unrealized_pnl=Decimal("0"),
            received_monotonic=2.0,
        )
        coordinator._position = newer_position

        with self.assertRaisesRegex(RuntimeError, "websocket position"):
            await coordinator._capture_authenticated_flat_boundary()

        self.assertIs(coordinator.position_snapshot, newer_position)
        adapter.get_positions.assert_not_awaited()
        adapter.get_open_orders.assert_not_awaited()

    async def test_boundary_rejects_audited_open_order(self) -> None:
        monitor = SimpleNamespace(
            last_audit_authenticated=True,
            audited_position=Decimal("0"),
            audited_open_order_count=1,
        )
        coordinator = MarketMakerCoordinator(
            SimpleNamespace(),
            MarketMakerConfig(),
            account_monitor=monitor,
            authenticated_evidence=True,
        )
        coordinator._authenticated = True
        coordinator._position = PositionSnapshot(
            symbol="BTC",
            signed_size=Decimal("0"),
            entry_price=None,
            unrealized_pnl=Decimal("0"),
            received_monotonic=1.0,
        )

        with self.assertRaisesRegex(RuntimeError, "zero-order account audit"):
            await coordinator._capture_authenticated_flat_boundary()

    async def test_live_guard_rejects_unsafe_config_before_connect(self) -> None:
        base = MarketMakerConfig(
            dry_run=False,
            account_audit_interval_seconds=60,
            max_session_drawdown=Decimal("1"),
            require_flat_start=True,
        )
        for field, expected in (
            ("startup_open_order_policy", "startup_open_order_policy=abort"),
            ("exclusive_symbol_control", "exclusive_symbol_control=true"),
            ("cancel_on_shutdown", "cancel_on_shutdown=true"),
        ):
            value = "cancel_all" if field == "startup_open_order_policy" else False
            adapter = SimpleNamespace(connect=AsyncMock())
            coordinator = MarketMakerCoordinator(
                adapter,
                replace(base, **{field: value}),
                authenticated_evidence=True,
            )

            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, expected):
                    await coordinator.start()
                adapter.connect.assert_not_awaited()

    async def test_runner_rejects_unsafe_live_evidence_before_factory(self) -> None:
        base = MarketMakerConfig(
            dry_run=False,
            account_audit_interval_seconds=60,
            max_session_drawdown=Decimal("1"),
            require_flat_start=True,
        )
        for field, expected in (
            ("require_flat_start", "require_flat_start"),
            ("startup_open_order_policy", "startup_open_order_policy=abort"),
            ("exclusive_symbol_control", "exclusive_symbol_control=true"),
            ("cancel_on_shutdown", "cancel_on_shutdown=true"),
        ):
            factory = Mock()
            value = "cancel_all" if field == "startup_open_order_policy" else False
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, expected):
                    await entrypoint.run_market_maker(
                        replace(base, **{field: value}),
                        {},
                        adapter_factory=factory,
                        status_callback=lambda _status: None,
                        authenticated_evidence=True,
                    )
                factory.assert_not_called()

    async def test_shutdown_failure_cannot_publish_postflight(self) -> None:
        adapter = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            get_open_orders=AsyncMock(return_value=[]),
        )
        manager = SimpleNamespace(
            shutdown=AsyncMock(side_effect=RuntimeError("shutdown failed")),
        )
        coordinator = MarketMakerCoordinator(
            adapter,
            MarketMakerConfig(),
            order_manager=manager,
            authenticated_evidence=True,
        )
        coordinator._authenticated = True

        error = await coordinator._cleanup_orders_and_final_audit()

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("shutdown failed", str(error))
        self.assertIsNone(coordinator._authenticated_postflight)
        adapter.get_positions.assert_not_awaited()
        adapter.get_open_orders.assert_not_awaited()


class MarketMakerRunnerEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "evidence.json"
        self.config = MarketMakerConfig(
            toxicity_profile_id="profile-v1",
            maker_fee_rate=Decimal("0.000120"),
            taker_fee_rate=Decimal("0.000350"),
        )
        self.commit_sha = "a" * 40
        self.config_sha = entrypoint.semantic_config_sha256(self.config)
        self.network = "robinhood"
        self.account_identity_sha = "c" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_account_identity_binds_network_and_account_not_credentials(self) -> None:
        settings = {
            "network": "robinhood",
            "account_index": 7,
            "api_key_index": 1,
            "api_key_private_key": "first-secret",
        }
        network, fingerprint = entrypoint._evidence_account_identity(
            settings,
            "lighter",
        )
        _, rotated = entrypoint._evidence_account_identity(
            {
                **settings,
                "api_key_index": 2,
                "api_key_private_key": "second-secret",
            },
            "LIGHTER",
        )
        _, owner_assertion_changed = entrypoint._evidence_account_identity(
            {
                **settings,
                "expected_l1_address": "0x" + "a" * 40,
            },
            "lighter",
        )
        _, other_account = entrypoint._evidence_account_identity(
            {**settings, "account_index": 8},
            "lighter",
        )
        other_network, testnet_account = entrypoint._evidence_account_identity(
            {**settings, "network": "robinhood_testnet"},
            "lighter",
        )

        self.assertEqual(network, "robinhood")
        self.assertEqual(other_network, "robinhood_testnet")
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(fingerprint, rotated)
        self.assertEqual(fingerprint, owner_assertion_changed)
        self.assertNotEqual(fingerprint, other_account)
        self.assertNotEqual(fingerprint, testnet_account)

    def test_account_identity_rejects_untrusted_settings(self) -> None:
        invalid = (
            ({"network": "unknown", "account_index": 1}, "lighter"),
            ({"network": "robinhood", "account_index": True}, "lighter"),
            ({"network": "robinhood", "account_index": -1}, "lighter"),
            ({"network": "robinhood", "account_index": 1}, "other"),
        )
        for settings, exchange in invalid:
            with self.subTest(settings=settings, exchange=exchange):
                with self.assertRaises(entrypoint.EvidenceError):
                    entrypoint._evidence_account_identity(settings, exchange)

    def writer(self) -> entrypoint.CalibrationEvidenceWriter:
        times = iter(
            (
                datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 3, 1, 30, tzinfo=timezone.utc),
            )
        )
        return entrypoint.CalibrationEvidenceWriter(
            output_path=self.output,
            campaign_id="campaign-v1",
            candidate_id="candidate-v1",
            config=self.config,
            commit_sha=self.commit_sha,
            config_sha256=self.config_sha,
            network=self.network,
            account_identity_sha256=self.account_identity_sha,
            repo_root=self.root,
            utcnow=lambda: next(times),
        )

    @staticmethod
    def status(
        *,
        event: str,
        uptime: str,
        cycles: int,
        postflight: bool,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "event_sequence_run_id": "run-v1",
            "event": event,
            "uptime_seconds": Decimal(uptime),
            "cycles": cycles,
            "signed_position": Decimal("0"),
            "account_audit": {"last_audit_authenticated": True},
            "preflight": {
                "authenticated": True,
                "position": Decimal("0"),
                "open_orders": 0,
            },
            "controller_decision_history": [],
            "quote_contexts": [],
            "fill_markouts": [],
        }
        if postflight:
            result.update(
                {
                    "postflight": {
                        "authenticated": True,
                        "position": Decimal("0"),
                        "open_orders": 0,
                    },
                    "authenticated_open_orders": 0,
                }
            )
        return result

    def record_complete_run(
        self,
        writer: entrypoint.CalibrationEvidenceWriter,
    ) -> None:
        writer.record(
            self.status(
                event="market_maker_authenticated_preflight",
                uptime="1",
                cycles=0,
                postflight=False,
            )
        )
        writer.record(
            self.status(
                event="market_maker_final_dry_run",
                uptime="2",
                cycles=1,
                postflight=True,
            )
        )

    def test_writer_publishes_strict_common_identity_and_period(self) -> None:
        writer = self.writer()
        secret = "never-write-this-private-key"
        writer.protect_sensitive_value(secret)
        self.record_complete_run(writer)

        with patch.object(
            entrypoint,
            "_verified_clean_commit",
            return_value=self.commit_sha,
        ):
            self.assertEqual(writer.finalize(), self.output)

        raw = self.output.read_text(encoding="utf-8")
        records = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertEqual(len(records), 2)
        self.assertEqual({item["commit_sha"] for item in records}, {self.commit_sha})
        self.assertEqual(
            {item["semantic_config_sha256"] for item in records},
            {self.config_sha},
        )
        self.assertEqual({item["network"] for item in records}, {self.network})
        self.assertEqual(
            {item["account_identity_sha256"] for item in records},
            {self.account_identity_sha},
        )
        self.assertEqual(
            {item["started_at_utc"] for item in records},
            {"2026-09-03T01:00:00Z"},
        )
        self.assertEqual(
            {item["ended_at_utc"] for item in records},
            {"2026-09-03T01:30:00Z"},
        )
        self.assertEqual(records[-1]["signed_position"], "0")
        self.assertTrue(records[-1]["postflight"]["authenticated"])

    def test_writer_omits_zero_duration_preflight_snapshot(self) -> None:
        writer = self.writer()
        writer.record(
            self.status(
                event="market_maker_authenticated_preflight",
                uptime="0",
                cycles=0,
                postflight=False,
            )
        )
        writer.record(
            self.status(
                event="market_maker_final_dry_run",
                uptime="1",
                cycles=1,
                postflight=True,
            )
        )

        with patch.object(
            entrypoint,
            "_verified_clean_commit",
            return_value=self.commit_sha,
        ):
            writer.finalize()

        records = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "market_maker_final_dry_run")

    def test_writer_rejects_secret_value_under_benign_key(self) -> None:
        writer = self.writer()
        secret = "nested-private-key-sentinel"
        writer.protect_sensitive_value(secret)
        status = self.status(
            event="market_maker_final_dry_run",
            uptime="1",
            cycles=1,
            postflight=True,
        )
        status["state_reason"] = f"adapter failed with {secret}"

        with self.assertRaisesRegex(
            entrypoint.EvidenceError,
            "protected credential",
        ):
            writer.record(status)
        self.assertFalse(self.output.exists())

    def test_dry_writer_requires_authenticated_final_audit(self) -> None:
        writer = self.writer()
        status = self.status(
            event="market_maker_final_dry_run",
            uptime="1",
            cycles=1,
            postflight=True,
        )
        status.pop("account_audit")
        writer.record(status)

        with (
            patch.object(
                entrypoint,
                "_verified_clean_commit",
                return_value=self.commit_sha,
            ),
            self.assertRaisesRegex(entrypoint.EvidenceError, "diagnostic-only"),
        ):
            writer.finalize()

        final = json.loads(self.output.read_text(encoding="utf-8"))[-1]
        self.assertIsNone(final["commit_sha"])
        self.assertIn(
            "missing_authenticated_final_account_audit",
            final["evidence_integrity_errors"],
        )

    def test_failed_fsync_never_creates_final_artifact(self) -> None:
        writer = self.writer()
        self.record_complete_run(writer)

        with (
            patch.object(
                entrypoint,
                "_verified_clean_commit",
                return_value=self.commit_sha,
            ),
            patch.object(entrypoint.os, "fsync", side_effect=OSError("disk")),
            self.assertRaisesRegex(entrypoint.EvidenceError, "could not be published"),
        ):
            writer.finalize()

        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_existing_artifact_is_never_overwritten(self) -> None:
        self.output.write_text("original", encoding="utf-8")
        writer = self.writer()
        self.record_complete_run(writer)

        with (
            patch.object(
                entrypoint,
                "_verified_clean_commit",
                return_value=self.commit_sha,
            ),
            self.assertRaisesRegex(entrypoint.EvidenceError, "already exists"),
        ):
            writer.finalize()

        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_nonfinal_campaign_rank_is_diagnostic_only(self) -> None:
        writer = self.writer()
        writer.record(
            self.status(
                event="market_maker_authenticated_preflight",
                uptime="100",
                cycles=100,
                postflight=False,
            )
        )
        writer.record(
            self.status(
                event="market_maker_final_dry_run",
                uptime="2",
                cycles=1,
                postflight=True,
            )
        )

        with (
            patch.object(
                entrypoint,
                "_verified_clean_commit",
                return_value=self.commit_sha,
            ),
            self.assertRaisesRegex(entrypoint.EvidenceError, "diagnostic-only"),
        ):
            writer.finalize()

        final = json.loads(self.output.read_text(encoding="utf-8"))[-1]
        self.assertIsNone(final["commit_sha"])
        self.assertIn(
            "terminal_status_is_not_final_by_campaign_order",
            final["evidence_integrity_errors"],
        )


if __name__ == "__main__":
    unittest.main()
