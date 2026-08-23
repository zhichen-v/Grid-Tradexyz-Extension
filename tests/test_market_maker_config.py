import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.services.market_maker.config import (
    MarketMakerConfig,
    ceil_to_step,
    floor_to_step,
    is_step_aligned,
    load_market_maker_config,
    parse_decimal,
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
"""
        )

        self.assertEqual(config.symbol, "ETH")
        self.assertEqual(config.order_size, Decimal("0.00020000000000000001"))
        self.assertIsInstance(config.order_size, Decimal)

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


if __name__ == "__main__":
    unittest.main()
