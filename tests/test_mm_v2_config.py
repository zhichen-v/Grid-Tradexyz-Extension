"""Public input and authorization boundaries for the plan's exact 18-field schema."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from core.services.market_maker_v2.config import (
    ConfigError, LIGHTER_VOLUME_RUNTIME_PROFILE, LighterVolumeRuntimeProfile,
    MarketMakerV2Config, load_config, require_authorization,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "config/market_maker_v2/lighter_btc_volume.example.yaml"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.source = EXAMPLE.read_text(encoding="utf-8")
        self.values = yaml.safe_load(self.source)["market_maker_v2"]

    def parse_text(self, source):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "example.yaml"
            path.write_text(source, encoding="utf-8")
            return load_config(path)

    def test_example_exact_eighteen_leaves_and_immutable_nested_decimal_values(self):
        config = load_config(EXAMPLE)
        def leaves(value):
            return sum(leaves(item) for item in value.values()) if isinstance(value, dict) else 1
        self.assertEqual(leaves(self.values), 18)
        self.assertEqual(len(fields(MarketMakerV2Config)), 7)
        self.assertIs(config.dry_run, True)
        self.assertEqual(config.quote.order_size, Decimal("0.00020"))
        with self.assertRaises(FrozenInstanceError):
            config.quote.order_size = Decimal("1")
        with self.assertRaises(FrozenInstanceError):
            config.dry_run = False

    def test_only_dry_run_defaults_other_fields_are_required(self):
        del self.values["dry_run"]
        self.assertTrue(MarketMakerV2Config.from_mapping(self.values).dry_run)
        for key in tuple(self.values):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                broken = deepcopy(self.values)
                del broken[key]
                MarketMakerV2Config.from_mapping(broken)
        del self.values["quote"]["order_size"]
        with self.assertRaises(ConfigError):
            MarketMakerV2Config.from_mapping(self.values)

    def test_unknown_legacy_fee_secret_and_yaml_authorization_fields_rejected(self):
        for section, name in ((None, "authorize_bounded_flatten"), (None, "api_key"),
                              ("quote", "post_only"), ("quote", "maker_fee_rate"),
                              ("flatten", "max_flatten_attempts")):
            with self.subTest(name=name), self.assertRaises(ConfigError) as error:
                broken = deepcopy(self.values)
                (broken if section is None else broken[section])[name] = "private-sentinel"
                MarketMakerV2Config.from_mapping(broken)
            self.assertNotIn("private-sentinel", str(error.exception))

    def test_financial_strings_not_floats_and_nonfinite_or_negative_rejected(self):
        for value in (0.0002, 1, True, Decimal("0.0002"), "NaN", "Infinity", "-1", "garbage"):
            with self.subTest(value=repr(value)), self.assertRaises(ConfigError):
                self.values["quote"]["order_size"] = value
                MarketMakerV2Config.from_mapping(self.values)
        for unquoted in ("0.00020", "1e-4", "1_000"):
            with self.subTest(unquoted=unquoted), self.assertRaises(ConfigError):
                self.parse_text(self.source.replace('"0.00020"', unquoted))

    def test_boolean_and_integer_fields_are_strict_not_coerced(self):
        for value in ("false", 0, 1, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                broken = deepcopy(self.values)
                broken["dry_run"] = value
                MarketMakerV2Config.from_mapping(broken)
        for section, name in (("quote", "max_quote_age_ms"), ("flatten", "max_hold_seconds"),
                              ("session", "duration_seconds")):
            for value in ("10", 1.0, True, 0, -1):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                    broken = deepcopy(self.values)
                    broken[section][name] = value
                    MarketMakerV2Config.from_mapping(broken)
        for value in ("yes", "on", "False"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.parse_text(self.source.replace("dry_run: true", f"dry_run: {value}"))

    def test_relational_inventory_loss_and_passive_grace_bounds(self):
        cases = (("inventory", "soft_limit", "0.00040"),
                 ("quote", "order_size", "0.00041"),
                 ("flatten", "stop_loss_usdg", "0.21"),
                 ("flatten", "stop_loss_usdg", "0"),
                 ("flatten", "passive_grace_seconds", 30),
                 ("flatten", "passive_grace_seconds", -1),
                 ("inventory", "skew_bps_at_hard", "10000"),
                 ("quote", "target_net_edge_bps", "20000"))
        for section, name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                broken = deepcopy(self.values)
                broken[section][name] = value
                MarketMakerV2Config.from_mapping(broken)
        self.values["flatten"]["passive_grace_seconds"] = 0
        self.values["flatten"]["ioc_slippage_ticks"] = 0
        self.values["session"]["cooldown_seconds"] = 0
        self.values["quote"]["target_net_edge_bps"] = "0"
        self.values["quote"]["volatility_multiplier"] = "0"
        self.assertEqual(MarketMakerV2Config.from_mapping(self.values).flatten.passive_grace_seconds, 0)

    def test_unknown_profile_symbol_and_untyped_direct_construction_rejected(self):
        config = load_config(EXAMPLE)
        for value in ("btc", "BTC/USDC", "", "BTC\n"):
            with self.subTest(symbol=value), self.assertRaises(ConfigError):
                replace(config, symbol=value)
        with self.assertRaises(ConfigError):
            replace(config, profile="legacy")
        with self.assertRaises(ConfigError):
            replace(config, quote=self.values["quote"])
        with self.assertRaises(ConfigError):
            replace(config.quote, order_size="0.00020")

    def test_duplicate_keys_and_malformed_or_wrong_root_yaml_rejected_without_contents(self):
        cases = (self.source.replace("dry_run: true", "dry_run: true\n  dry_run: false"),
                 self.source.replace("reprice_threshold_ticks: 5", "reprice_threshold_ticks: 5\n    reprice_threshold_ticks: 1"),
                 self.source + "unexpected: private-sentinel\n", "[private-sentinel", "null", "[]", "",
                 "!!map [private-sentinel]", "? [private-sentinel]\n: x",
                 "a: &a {b: *a}", "a: !!python/object:private-sentinel {}")
        for source in cases:
            with self.subTest(source=source[:20]), self.assertRaises(ConfigError) as error:
                self.parse_text(source)
            self.assertNotIn("private-sentinel", str(error.exception))

    def test_live_authorization_is_per_run_not_persisted_in_config(self):
        dry = load_config(EXAMPLE)
        live = replace(dry, dry_run=False)
        require_authorization(dry)
        with self.assertRaisesRegex(ConfigError, "authorize-bounded-flatten"):
            require_authorization(live)
        require_authorization(live, True)
        with self.assertRaises(ConfigError):
            require_authorization(live)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                require_authorization(live, value)

    def test_runtime_safety_profile_is_fixed_and_not_a_config_extension(self):
        profile = LIGHTER_VOLUME_RUNTIME_PROFILE
        self.assertEqual((profile.stale_book_seconds, profile.stale_position_seconds,
                          profile.max_flatten_attempts, profile.max_flatten_seconds), (3, 10, 3, 30))
        with self.assertRaises(FrozenInstanceError):
            profile.stale_book_seconds = 300
        with self.assertRaises(TypeError):
            LighterVolumeRuntimeProfile(max_flatten_attempts=100)


if __name__ == "__main__":
    unittest.main()
