from __future__ import annotations

import math
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path

from gold_research.config import ConfigError, load_config, validate_oanda_xauusd_config
from gold_research.domain import Direction, PriceBasis


class ConfigContractTests(unittest.TestCase):
    def test_baseline_config_loads_with_explicit_costs(self) -> None:
        config = load_config("configs/xauusd_baseline.toml")

        self.assertEqual(config.instrument.symbol, "XAU_USD")
        self.assertEqual(config.instrument.provider, "oanda")
        self.assertEqual(config.instrument.price_basis, PriceBasis.MID)
        self.assertEqual(config.direction, Direction.BOTH)
        self.assertEqual(config.timeframes.base, "1min")
        self.assertEqual(config.timeframes.medium, "5min")
        self.assertEqual(config.timeframes.large, "30min")
        self.assertEqual(config.risk.max_hold_bars, 80)
        self.assertTrue(config.costs.require_explicit_costs)
        self.assertEqual(len(config.fingerprint()), 64)

    def test_invalid_direction_is_rejected(self) -> None:
        from pathlib import Path
        import tomllib

        with Path("configs/xauusd_baseline.toml").open("rb") as handle:
            raw = tomllib.load(handle)
        raw["strategy"]["direction"] = "sideways"

        with self.assertRaisesRegex(ConfigError, "direction"):
            load_config_from_mapping(raw)

    def test_target_must_exceed_stop(self) -> None:
        with Path("configs/xauusd_baseline.toml").open("rb") as handle:
            raw = tomllib.load(handle)
        raw["risk"]["target_atr"] = raw["risk"]["stop_atr"]

        with self.assertRaisesRegex(ConfigError, "target_atr"):
            load_config_from_mapping(raw)

    def test_numeric_fields_reject_bool_nan_and_infinity(self) -> None:
        variants = (
            ("instrument", "point_value", True),
            ("instrument", "tick_size", math.nan),
            ("risk", "stop_atr", math.inf),
            ("costs", "spread_value", False),
        )
        for section, field, invalid in variants:
            with self.subTest(section=section, field=field, invalid=invalid):
                with Path("configs/xauusd_baseline.toml").open("rb") as handle:
                    raw = tomllib.load(handle)
                raw[section][field] = invalid

                with self.assertRaisesRegex(ConfigError, field):
                    load_config_from_mapping(raw)

    def test_boolean_fields_require_real_booleans(self) -> None:
        variants = (
            ("entry_point_2", "enabled"),
            ("entry_point_3", "enabled"),
            ("costs", "require_explicit_costs"),
        )
        for section, field in variants:
            with self.subTest(section=section, field=field):
                with Path("configs/xauusd_baseline.toml").open("rb") as handle:
                    raw = tomllib.load(handle)
                raw[section][field] = "false"

                with self.assertRaisesRegex(ConfigError, field):
                    load_config_from_mapping(raw)

    def test_only_fixed_cost_models_are_supported(self) -> None:
        for field in ("spread_model", "slippage_model"):
            with self.subTest(field=field):
                with Path("configs/xauusd_baseline.toml").open("rb") as handle:
                    raw = tomllib.load(handle)
                raw["costs"][field] = "unsupported"

                with self.assertRaisesRegex(ConfigError, field):
                    load_config_from_mapping(raw)

    def test_oanda_contract_fields_are_fixed(self) -> None:
        config = load_config("configs/xauusd_baseline.toml")
        invalid_values = (
            ("provider", "another-provider"),
            ("symbol", "XAUUSD"),
            ("timezone", "Asia/Shanghai"),
            ("venue", "another-venue"),
            ("contract_unit", "100 troy ounces"),
            ("quote_currency", "EUR"),
            ("tick_size", 0.1),
            ("point_value", 100.0),
        )
        for field, value in invalid_values:
            with self.subTest(field=field):
                invalid = replace(config, instrument=replace(config.instrument, **{field: value}))
                with self.assertRaisesRegex(ConfigError, field):
                    validate_oanda_xauusd_config(invalid)


def load_config_from_mapping(mapping: dict) -> object:
    from gold_research.config import ResearchConfig

    return ResearchConfig.from_mapping(mapping)


if __name__ == "__main__":
    unittest.main()
