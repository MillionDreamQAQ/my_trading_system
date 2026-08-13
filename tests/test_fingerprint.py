from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from gold_research.cli import _dashboard_loader, _load_oanda
from gold_research.config import load_config
from gold_research.data.fingerprint import fingerprint_bar_series
from gold_research.data.loader import _series_digest, load_oanda_candles
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import BarSeries, InstrumentMetadata, PriceBasis
from gold_research.research import fingerprint_bars


def _metadata(price_basis: PriceBasis = PriceBasis.MID) -> InstrumentMetadata:
    return InstrumentMetadata(
        provider="oanda",
        symbol="XAU_USD",
        price_basis=price_basis,
        source_timezone="UTC",
        source_interval="1min",
    )


def _series() -> BarSeries:
    timestamps = pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [10.0, 11.0, 12.0],
        }
    )
    return normalize_ohlc_frame(frame, _metadata(), "1min")


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.status_code = 200

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses

    def get(self, url: str, **kwargs):
        return self.responses.pop(0)


def _candle(timestamp: pd.Timestamp) -> dict:
    return {
        "time": timestamp.isoformat().replace("+00:00", "Z"),
        "complete": True,
        "volume": 10,
        "mid": {"o": "100.0", "h": "101.0", "l": "99.0", "c": "100.5"},
    }


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_shared_paths_agree(self) -> None:
        series = _series()

        digest = fingerprint_bar_series(series)

        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, fingerprint_bar_series(series.copy()))
        self.assertEqual(digest, fingerprint_bars(series))
        self.assertEqual(digest, _series_digest(series))

    def test_fingerprint_does_not_serialize_the_frame_as_csv(self) -> None:
        series = _series()

        with patch.object(pd.DataFrame, "to_csv", side_effect=AssertionError("CSV serialization is not allowed")):
            fingerprint_bar_series(series)

    def test_fingerprint_detects_data_schema_and_context_changes(self) -> None:
        series = _series()
        original = fingerprint_bar_series(series)

        changed_value = series.copy()
        changed_value.bars.loc[1, "close"] += 0.01

        changed_order = series.copy()
        changed_order.bars = changed_order.bars.iloc[::-1].reset_index(drop=True)

        changed_schema = series.copy()
        changed_schema.bars = changed_schema.bars.drop(columns=["volume"])

        changed_metadata = BarSeries(
            series.bars.copy(),
            series.timeframe,
            replace(series.metadata, price_basis=PriceBasis.BID),
        )
        changed_timeframe = BarSeries(series.bars.copy(), "5min", series.metadata)

        self.assertNotEqual(original, fingerprint_bar_series(changed_value))
        self.assertNotEqual(original, fingerprint_bar_series(changed_order))
        self.assertNotEqual(original, fingerprint_bar_series(changed_schema))
        self.assertNotEqual(original, fingerprint_bar_series(changed_metadata))
        self.assertNotEqual(original, fingerprint_bar_series(changed_timeframe))

        changed_index = series.copy()
        changed_index.bars.index = [10, 20, 30]
        self.assertEqual(original, fingerprint_bar_series(changed_index))

    def test_fingerprint_handles_empty_frames_and_nullable_values(self) -> None:
        series = _series()
        empty = BarSeries(series.bars.iloc[0:0].copy(), series.timeframe, series.metadata)
        nullable = series.copy()
        nullable.bars.loc[1, "volume"] = float("nan")
        nullable_copy = nullable.copy()
        changed = series.copy()
        changed.bars.loc[1, "volume"] = 0.0

        self.assertEqual(fingerprint_bar_series(empty), fingerprint_bar_series(empty.copy()))
        self.assertNotEqual(fingerprint_bar_series(series), fingerprint_bar_series(nullable))
        self.assertEqual(fingerprint_bar_series(nullable), fingerprint_bar_series(nullable_copy))
        self.assertNotEqual(fingerprint_bar_series(nullable), fingerprint_bar_series(changed))

    def test_loader_can_skip_digest_without_changing_loaded_series(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        end = start + pd.Timedelta(minutes=1)
        payload = {"candles": [_candle(start)]}

        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "oanda.sqlite3"
            loaded, expected_digest, _ = load_oanda_candles(
                "XAU_USD",
                "1min",
                _metadata(),
                start=start.to_pydatetime(),
                end=end.to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=_FakeSession([_FakeResponse(payload)]),
            )

            with patch(
                "gold_research.data.loader._series_digest",
                side_effect=AssertionError("digest should be skipped"),
            ):
                cached, skipped_digest, _ = load_oanda_candles(
                    "XAU_USD",
                    "1min",
                    _metadata(),
                    start=start.to_pydatetime(),
                    end=end.to_pydatetime(),
                    base_url="https://example.test",
                    database_path=database_path,
                    session=_FakeSession([]),
                    compute_digest=False,
                )

        self.assertIsInstance(expected_digest, str)
        self.assertIsNone(skipped_digest)
        pd.testing.assert_frame_equal(loaded.bars, cached.bars)
        self.assertEqual(loaded.metadata.to_dict(), cached.metadata.to_dict())

    def test_dashboard_window_loader_requests_no_digest(self) -> None:
        config = load_config("configs/xauusd_baseline.toml")
        args = SimpleNamespace(oanda_database=Path("data/cache/oanda.sqlite3"))
        start = pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime()
        end = pd.Timestamp("2026-01-01T01:00:00Z").to_pydatetime()

        with patch(
            "gold_research.cli.load_oanda_candles",
            return_value=("series", None, Path("data/cache/oanda.sqlite3")),
        ) as loader:
            callback = _dashboard_loader(args, config)
            self.assertEqual(callback(start, end), "series")

        self.assertFalse(loader.call_args.kwargs["compute_digest"])

    def test_dashboard_initial_loader_requests_no_digest(self) -> None:
        config = load_config("configs/xauusd_baseline.toml")
        args = SimpleNamespace(
            oanda_database=Path("data/cache/oanda.sqlite3"),
            warmup_start=None,
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T01:00:00Z",
        )

        with patch(
            "gold_research.cli.load_oanda_candles",
            return_value=("series", None, Path("data/cache/oanda.sqlite3")),
        ) as loader:
            self.assertEqual(_load_oanda(args, config, compute_digest=False), ("series", None))

        self.assertFalse(loader.call_args.kwargs["compute_digest"])


if __name__ == "__main__":
    unittest.main()
