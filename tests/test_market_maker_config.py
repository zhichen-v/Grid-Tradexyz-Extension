import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

from core.services.market_maker.config import (
    MarketMakerConfig,
    ceil_to_step,
    floor_to_step,
    is_step_aligned,
    load_market_maker_config,
    parse_decimal,
    semantic_config_sha256,
)
from core.services.market_maker.models import MarketMetadata


class MarketMakerConfigTests(unittest.TestCase):
    def load_yaml(self, content: str) -> MarketMakerConfig:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "market_maker.yaml")
            path.write_text(content, encoding="utf-8")
            return load_market_maker_config(path)

    def test_loads_valid_yaml_with_exact_decimals(self) -> None:
        config = self.load_yaml(
            """
market_maker:
  exchange: lighter
  symbol: ETH
  order_size: "0.00020000000000000001"
  max_position: "0.002"
  soft_position_ratio: "0.5"
  hard_position_ratio: "0.8"
  soft_exit_after_seconds: 120
  soft_exit_net_turnover_bps: "-5.0"
  soft_exit_surplus_reserve_bps: "0.03"
  toxicity_apply_bid: true
  toxicity_apply_ask: false
  toxicity_outward_reprice_threshold_ticks: 2
  toxicity_outward_reprice_min_interval_ms: 6000
"""
        )

        self.assertEqual(config.symbol, "ETH")
        self.assertEqual(config.order_size, Decimal("0.00020000000000000001"))
        self.assertIsInstance(config.order_size, Decimal)
        self.assertEqual(config.soft_exit_after_seconds, 120)
        self.assertEqual(
            config.soft_exit_net_turnover_bps, Decimal("-5.0")
        )
        self.assertEqual(
            config.soft_exit_surplus_reserve_bps, Decimal("0.03")
        )
        self.assertTrue(config.toxicity_apply_bid)
        self.assertFalse(config.toxicity_apply_ask)
        self.assertEqual(config.toxicity_outward_reprice_threshold_ticks, 2)
        self.assertEqual(config.toxicity_outward_reprice_min_interval_ms, 6000)

    def test_example_yaml_loads_with_all_rollout_defaults_safe(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "market_maker"
            / "lighter_btc_mvp.example.yaml"
        )

        config = load_market_maker_config(path)

        self.assertTrue(config.dry_run)
        self.assertFalse(config.active_unwind_enabled)
        self.assertEqual(config.quote_controller_mode, "fixed")
        self.assertEqual(config.quote_controller_type, "toxicity_v1")
        self.assertEqual(config.toxicity_profile_id, "disabled")
        self.assertFalse(config.toxicity_apply_bid)
        self.assertFalse(config.toxicity_apply_ask)
        self.assertEqual(config.toxicity_outward_reprice_threshold_ticks, 1)
        self.assertEqual(config.toxicity_outward_reprice_min_interval_ms, 5000)

    def test_quote_controller_defaults_and_valid_active_policy(self) -> None:
        fixed = MarketMakerConfig()
        self.assertEqual(fixed.quote_controller_mode, "fixed")
        self.assertEqual(fixed.toxicity_profile_id, "disabled")
        self.assertEqual(fixed.toxicity_widen_start_ticks, Decimal("0"))
        self.assertFalse(fixed.toxicity_use_markout_feedback)
        self.assertFalse(fixed.toxicity_apply_bid)
        self.assertFalse(fixed.toxicity_apply_ask)

        shadow = MarketMakerConfig(quote_controller_mode="shadow")
        self.assertFalse(shadow.toxicity_apply_bid)
        self.assertFalse(shadow.toxicity_apply_ask)

        active = MarketMakerConfig(
            quote_controller_mode="active",
            toxicity_apply_bid=True,
            toxicity_min_signal_ticks="0.5",
            toxicity_widen_start_ticks="1",
            toxicity_max_extra_spread_ticks=3,
            toxicity_block_threshold_ticks="4",
            toxicity_resume_threshold_ticks="0.5",
        )
        self.assertEqual(active.toxicity_widen_start_ticks, Decimal("1"))
        self.assertEqual(active.toxicity_block_threshold_ticks, Decimal("4"))

        both_sides = MarketMakerConfig(
            quote_controller_mode="active",
            toxicity_apply_bid=True,
            toxicity_apply_ask=True,
            toxicity_max_extra_spread_ticks=1,
        )
        self.assertTrue(both_sides.toxicity_apply_bid)
        self.assertTrue(both_sides.toxicity_apply_ask)

    def test_quote_controller_rejects_unsafe_or_unknown_configuration(self) -> None:
        invalid_cases = (
            ({"quote_controller_mode": "adaptive"}, "quote_controller_mode"),
            ({"quote_controller_type": "unknown"}, "quote_controller_type"),
            ({"toxicity_profile_id": ""}, "toxicity_profile_id"),
            ({"toxicity_profile_id": " candidate"}, "toxicity_profile_id"),
            ({"toxicity_profile_id": "candidate profile"}, "toxicity_profile_id"),
            ({"toxicity_feature_window_seconds": 59}, "60-second"),
            ({"toxicity_book_depth_levels": 0}, "book_depth"),
            ({"toxicity_min_samples": 4097}, "cannot exceed 4096"),
            ({"toxicity_markout_horizon_seconds": 1}, "must be 5 or 15"),
            ({"toxicity_min_signal_ticks": "-0.1"}, "cannot be negative"),
            ({"toxicity_max_extra_spread_ticks": -1}, "max_extra"),
            (
                {"toxicity_block_confirmations": 4},
                "three directional signals",
            ),
            (
                {
                    "quote_controller_mode": "active",
                    "toxicity_apply_bid": True,
                    "toxicity_max_extra_spread_ticks": 1,
                    "toxicity_use_markout_feedback": True,
                },
                "cannot use markout feedback",
            ),
            (
                {
                    "toxicity_block_threshold_ticks": "3",
                    "toxicity_widen_start_ticks": "1",
                    "toxicity_resume_threshold_ticks": "1",
                },
                "resume < widen_start < block",
            ),
            (
                {
                    "quote_controller_mode": "active",
                    "toxicity_apply_bid": True,
                },
                "widening or blocking",
            ),
            (
                {
                    "quote_controller_mode": "active",
                    "toxicity_max_extra_spread_ticks": 1,
                },
                "at least one enabled side",
            ),
            ({"toxicity_apply_bid": "false"}, "boolean"),
            ({"toxicity_apply_ask": 1}, "boolean"),
            ({"toxicity_outward_reprice_threshold_ticks": 0}, "integer >= 1"),
            ({"toxicity_outward_reprice_min_interval_ms": 0}, "integer >= 1"),
            ({"toxicity_use_markout_feedback": "false"}, "boolean"),
        )
        for values, message in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, message):
                    MarketMakerConfig(**values)  # type: ignore[arg-type]

    def test_requires_market_maker_block(self) -> None:
        with self.assertRaisesRegex(ValueError, "market_maker"):
            self.load_yaml("exchange: lighter")

    def test_rejects_invalid_position_ratios(self) -> None:
        with self.assertRaisesRegex(ValueError, "soft < hard"):
            MarketMakerConfig(
                soft_position_ratio=Decimal("0.8"),
                hard_position_ratio=Decimal("0.8"),
            )

    def test_rejects_non_positive_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "refresh_interval_ms"):
            MarketMakerConfig(refresh_interval_ms=0)

    def test_soft_exit_requires_valid_relaxed_fee_target(self) -> None:
        for timeout in (-1, True):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "soft_exit_after_seconds"):
                    MarketMakerConfig(soft_exit_after_seconds=timeout)
        with self.assertRaisesRegex(ValueError, "must be below"):
            MarketMakerConfig(
                soft_exit_after_seconds=1,
                min_completed_net_turnover_bps="0.1",
                soft_exit_net_turnover_bps="0.1",
            )
        with self.assertRaisesRegex(ValueError, "between -1 and 1"):
            MarketMakerConfig(
                soft_exit_after_seconds=1,
                soft_exit_net_turnover_bps="-10000",
            )

    def test_soft_exit_surplus_reserve_is_finite_and_nonnegative(self) -> None:
        self.assertEqual(
            MarketMakerConfig().soft_exit_surplus_reserve_bps,
            Decimal("0.02"),
        )
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            MarketMakerConfig(soft_exit_surplus_reserve_bps="-0.01")
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    MarketMakerConfig(soft_exit_surplus_reserve_bps=value)

    def test_trend_guard_is_disabled_by_default_and_requires_a_pair(self) -> None:
        config = MarketMakerConfig()
        self.assertEqual(config.trend_guard_window_seconds, 0)
        self.assertEqual(config.trend_guard_threshold_ticks, 0)

        enabled = MarketMakerConfig(
            trend_guard_window_seconds=60,
            trend_guard_threshold_ticks=125,
        )
        self.assertEqual(enabled.trend_guard_window_seconds, 60)
        self.assertEqual(enabled.trend_guard_threshold_ticks, 125)

        for values in (
            {"trend_guard_window_seconds": 60},
            {"trend_guard_threshold_ticks": 125},
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "trend guard"):
                    MarketMakerConfig(**values)

    def test_session_loss_maker_exit_requires_bounded_audited_soft_exit(
        self,
    ) -> None:
        values = {
            "account_audit_interval_seconds": 15,
            "max_session_drawdown": "0.50",
            "max_session_loss_for_maker_exit": "0.10",
            "require_flat_start": True,
            "soft_exit_after_seconds": 120,
            "soft_exit_net_turnover_bps": "-0.5",
        }
        config = MarketMakerConfig(**values)
        self.assertEqual(
            config.max_session_loss_for_maker_exit, Decimal("0.10")
        )

        invalid_cases = (
            ({**values, "max_session_loss_for_maker_exit": "-0.01"}, "negative"),
            (
                {
                    **values,
                    "account_audit_interval_seconds": 0,
                    "require_flat_start": False,
                },
                "account audit",
            ),
            ({**values, "soft_exit_after_seconds": 0}, "soft exit"),
            (
                {**values, "max_session_loss_for_maker_exit": "0.50"},
                "below max_session_drawdown",
            ),
        )
        for invalid, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    MarketMakerConfig(**invalid)

    def test_active_unwind_is_default_off_and_requires_bounded_policy(
        self,
    ) -> None:
        disabled = MarketMakerConfig()
        self.assertFalse(disabled.active_unwind_enabled)
        self.assertEqual(disabled.taker_fee_rate, Decimal("0"))

        values = {
            "active_unwind_enabled": True,
            "active_unwind_after_seconds": 300,
            "active_unwind_loss_trigger": "0.20",
            "active_unwind_max_slippage_ticks": 3,
            "active_unwind_max_attempts": 2,
            "active_unwind_confirmation_timeout_seconds": 5,
            "taker_fee_rate": "0.0004",
            "max_episode_loss_for_unwind": "0.30",
            "max_session_loss_for_unwind": "0.40",
            "max_session_loss_for_maker_exit": "0.15",
            "soft_exit_after_seconds": 120,
            "soft_exit_net_turnover_bps": "-0.5",
            "min_completed_net_turnover_bps": "0.1",
            "account_audit_interval_seconds": 15,
            "max_session_drawdown": "0.50",
            "require_flat_start": True,
            "ping_pong_enabled": True,
        }
        enabled = MarketMakerConfig(**values)
        self.assertEqual(enabled.taker_fee_rate, Decimal("0.0004"))
        self.assertEqual(
            enabled.active_unwind_loss_trigger, Decimal("0.20")
        )
        self.assertEqual(
            enabled.max_episode_loss_for_unwind, Decimal("0.30")
        )

        invalid_cases = (
            (
                {**values, "exclusive_symbol_control": False},
                "exclusive_symbol_control",
            ),
            (
                {**values, "cancel_on_shutdown": False},
                "cancel_on_shutdown",
            ),
            ({**values, "taker_fee_rate": "0"}, "positive authenticated"),
            ({**values, "ping_pong_enabled": False}, "ping_pong_enabled"),
            ({**values, "active_unwind_after_seconds": 120}, "after soft exit"),
            ({**values, "active_unwind_loss_trigger": "0.30"}, "below episode"),
            ({**values, "max_episode_loss_for_unwind": "0.40"}, "below session"),
            ({**values, "max_episode_loss_for_unwind": "0.41"}, "episode loss"),
            ({**values, "max_session_loss_for_unwind": "0.50"}, "drawdown"),
            ({**values, "active_unwind_max_slippage_ticks": 0}, "slippage"),
            (
                {**values, "max_session_loss_for_maker_exit": "0"},
                "maker exit loss budget",
            ),
            ({**values, "taker_fee_rate": "NaN"}, "finite"),
        )
        for invalid, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    MarketMakerConfig(**invalid)

    def test_account_audit_requires_positive_drawdown_and_flat_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "account_audit_timeout_seconds"):
            MarketMakerConfig(account_audit_timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "max_session_drawdown"):
            MarketMakerConfig(account_audit_interval_seconds=15)
        with self.assertRaisesRegex(ValueError, "require_flat_start"):
            MarketMakerConfig(
                account_audit_interval_seconds=15,
                max_session_drawdown="5",
            )
        config = MarketMakerConfig(
            account_audit_interval_seconds=15,
            max_session_drawdown="5",
            require_flat_start=True,
            min_completed_net_turnover_bps="0.1",
        )
        self.assertEqual(config.max_session_drawdown, Decimal("5"))
        self.assertEqual(
            config.min_completed_net_turnover_bps, Decimal("0.1")
        )

    def test_rejects_false_or_wrong_type_post_only(self) -> None:
        for value in (False, "true", 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "post_only"):
                    MarketMakerConfig(post_only=value)  # type: ignore[arg-type]

    def test_ping_pong_requires_boolean(self) -> None:
        for value in ("true", 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "ping_pong_enabled"):
                    MarketMakerConfig(  # type: ignore[arg-type]
                        ping_pong_enabled=value
                    )

    def test_ping_pong_requires_both_and_account_audit(self) -> None:
        with self.assertRaisesRegex(ValueError, "account audit"):
            MarketMakerConfig(ping_pong_enabled=True)
        with self.assertRaisesRegex(ValueError, "quote_mode 'both'"):
            MarketMakerConfig(
                ping_pong_enabled=True,
                quote_mode="bid_only",
                account_audit_interval_seconds=15,
                max_session_drawdown=Decimal("0.5"),
                require_flat_start=True,
            )

    def test_decimal_parser_rejects_non_finite_values(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    parse_decimal(value)

    def test_step_helpers_round_outward_and_check_alignment(self) -> None:
        value = Decimal("1.234")
        step = Decimal("0.05")

        self.assertEqual(floor_to_step(value, step), Decimal("1.20"))
        self.assertEqual(ceil_to_step(value, step), Decimal("1.25"))
        self.assertTrue(is_step_aligned(Decimal("1.20"), step))
        self.assertFalse(is_step_aligned(value, step))

    def test_rejects_unknown_and_secret_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret"):
            MarketMakerConfig.from_mapping({"api_key": "do-not-store-here"})
        with self.assertRaisesRegex(ValueError, "unknown"):
            MarketMakerConfig.from_mapping({"extra": 1})

    def test_cancel_all_startup_requires_exclusive_symbol_control(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclusive_symbol_control"):
            MarketMakerConfig(
                startup_open_order_policy="cancel_all",
                exclusive_symbol_control=False,
            )

    def test_rejects_wrong_policy_yaml_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "startup_open_order_policy"):
            self.load_yaml(
                "market_maker:\n  startup_open_order_policy: []\n"
            )

    def test_quote_mode_accepts_slots_and_rejects_other_values(self) -> None:
        for quote_mode in ("both", "bid_only", "ask_only"):
            with self.subTest(quote_mode=quote_mode):
                self.assertEqual(
                    MarketMakerConfig(quote_mode=quote_mode).quote_mode,
                    quote_mode,
                )
        for quote_mode in ("bid", "ask", "buy", "sell", "", None, 1, []):
            with self.subTest(quote_mode=quote_mode):
                with self.assertRaisesRegex(ValueError, "quote_mode"):
                    MarketMakerConfig(quote_mode=quote_mode)  # type: ignore[arg-type]

    def test_max_raw_spread_bps_is_positive_decimal(self) -> None:
        self.assertEqual(
            MarketMakerConfig(max_raw_spread_bps="12.5").max_raw_spread_bps,
            Decimal("12.5"),
        )
        for value in ("0", "-1", "NaN"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_raw_spread_bps"):
                    MarketMakerConfig(max_raw_spread_bps=value)

    def test_validates_order_size_against_market_metadata(self) -> None:
        config = MarketMakerConfig(symbol="BTC", order_size=Decimal("0.00020"))
        metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=2,
            size_decimals=5,
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("0.00001"),
            min_base_amount=Decimal("0.00020"),
            min_quote_amount=Decimal("10"),
        )

        config.validate_order_size(metadata, Decimal("50000"))
        with self.assertRaisesRegex(ValueError, "min_quote_amount"):
            config.validate_order_size(metadata, Decimal("100"))

    def test_semantic_config_hash_is_decimal_context_independent(self) -> None:
        first = MarketMakerConfig(
            max_raw_spread_bps=Decimal("500.000"),
            toxicity_widen_start_ticks=Decimal("-0"),
        )
        equivalent = MarketMakerConfig(
            max_raw_spread_bps=Decimal("5E+2"),
            toxicity_widen_start_ticks=Decimal("0.000"),
        )
        with localcontext() as context:
            context.prec = 2
            low_precision = semantic_config_sha256(first)
        with localcontext() as context:
            context.prec = 50
            high_precision = semantic_config_sha256(equivalent)

        self.assertRegex(low_precision, r"^[0-9a-f]{64}$")
        self.assertEqual(low_precision, high_precision)

    def test_semantic_config_hash_changes_with_effective_fields(self) -> None:
        base = MarketMakerConfig()
        base_hash = semantic_config_sha256(base)
        variants = (
            replace(base, dry_run=False),
            replace(base, toxicity_profile_id="candidate-v1"),
            replace(base, maker_fee_rate=Decimal("0.00021")),
            replace(base, symbol="ETH"),
            replace(base, refresh_interval_ms=1001),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    semantic_config_sha256(variant),
                    base_hash,
                )


if __name__ == "__main__":
    unittest.main()
