from __future__ import annotations

import unittest

from gold_research.config import ConfigError, load_config
from gold_research.domain import Direction, PriceBasis


class ConfigContractTests(unittest.TestCase):
    def test_baseline_config_loads_with_explicit_costs(self) -> None:
        config = load_config("configs/xauusd_baseline.toml")

        self.assertEqual(config.instrument.symbol, "XAUUSD")
        self.assertEqual(config.instrument.price_basis, PriceBasis.MID)
        self.assertEqual(config.direction, Direction.BOTH)
        self.assertEqual(config.timeframes.base, "15min")
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
        from pathlib import Path
        import tomllib

        with Path("configs/xauusd_baseline.toml").open("rb") as handle:
            raw = tomllib.load(handle)
        raw["risk"]["target_atr"] = raw["risk"]["stop_atr"]

        with self.assertRaisesRegex(ConfigError, "target_atr"):
            load_config_from_mapping(raw)


def load_config_from_mapping(mapping: dict) -> object:
    from gold_research.config import ResearchConfig

    return ResearchConfig.from_mapping(mapping)


if __name__ == "__main__":
    unittest.main()

