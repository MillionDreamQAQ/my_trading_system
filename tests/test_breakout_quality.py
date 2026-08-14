from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from gold_research.config import EntryPoint2Config
from gold_research.strategy.breakout_quality import breakout_quality_masks


def _config(**overrides: object) -> EntryPoint2Config:
    values = {
        "enabled": True,
        "breakout_lookback": 20,
        "volume_filter_enabled": False,
        "volume_multiplier": 1.5,
        "kline_quality_enabled": False,
        "min_body_ratio": 0.65,
        "max_close_extreme_ratio": 0.20,
        "squeeze_filter_enabled": False,
        "squeeze_lookback": 50,
        "squeeze_recent_bars": 5,
        "squeeze_percentile": 0.30,
    }
    values.update(overrides)
    return EntryPoint2Config(**values)


class BreakoutQualityTests(unittest.TestCase):
    def test_volume_filter_uses_only_the_previous_twenty_bars(self) -> None:
        frame = pd.DataFrame(
            {
                "volume": [100.0] * 20 + [151.0, 100.0],
            }
        )

        long_mask, short_mask = breakout_quality_masks(
            frame,
            _config(volume_filter_enabled=True),
        )

        self.assertFalse(long_mask[19])
        self.assertTrue(long_mask[20])
        self.assertFalse(long_mask[21])
        np.testing.assert_array_equal(long_mask, short_mask)

    def test_kline_quality_is_directional_and_rejects_long_wicks(self) -> None:
        frame = pd.DataFrame(
            {
                "open": [100.0, 100.0, 100.0],
                "high": [110.0, 101.0, 110.0],
                "low": [99.0, 90.0, 99.0],
                "close": [109.0, 91.0, 101.0],
            }
        )

        long_mask, short_mask = breakout_quality_masks(
            frame,
            _config(kline_quality_enabled=True),
        )

        self.assertTrue(long_mask[0])
        self.assertFalse(short_mask[0])
        self.assertFalse(long_mask[1])
        self.assertTrue(short_mask[1])
        self.assertFalse(long_mask[2])
        self.assertFalse(short_mask[2])

    def test_atr_squeeze_compares_recent_bars_with_a_prior_distribution(self) -> None:
        frame = pd.DataFrame(
            {
                "atr": [10.0] * 10 + [1.0] * 6,
            }
        )

        long_mask, short_mask = breakout_quality_masks(
            frame,
            _config(
                squeeze_filter_enabled=True,
                squeeze_lookback=10,
                squeeze_recent_bars=5,
            ),
        )

        self.assertFalse(long_mask[14])
        self.assertTrue(long_mask[15])
        np.testing.assert_array_equal(long_mask, short_mask)

    def test_bollinger_squeeze_can_confirm_when_atr_is_unavailable(self) -> None:
        frame = pd.DataFrame(
            {
                "close": [90.0, 110.0] * 15 + [100.0] * 6,
                "atr": [np.nan] * 36,
            }
        )

        long_mask, short_mask = breakout_quality_masks(
            frame,
            _config(
                squeeze_filter_enabled=True,
                squeeze_lookback=10,
                squeeze_recent_bars=5,
            ),
        )

        self.assertTrue(long_mask[-1])
        np.testing.assert_array_equal(long_mask, short_mask)


if __name__ == "__main__":
    unittest.main()
