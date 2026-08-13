from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
import requests

from gold_research.data.loader import OANDA_MAX_CANDLES, DataSourceError, load_oanda_candles
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.data.validate import DataValidationError, validate_bar_series
from gold_research.domain import InstrumentMetadata, PriceBasis


def metadata(timezone: str = "UTC") -> InstrumentMetadata:
    return InstrumentMetadata(
        provider="oanda",
        symbol="XAU_USD",
        price_basis=PriceBasis.MID,
        source_timezone=timezone,
        source_interval="15min",
    )


def _oanda_candle(timestamp: pd.Timestamp, *, complete: bool = True, base: float = 100.0) -> dict:
    values = {
        "o": f"{base:.5f}",
        "h": f"{base + 1:.5f}",
        "l": f"{base - 1:.5f}",
        "c": f"{base + 0.5:.5f}",
    }
    return {
        "time": timestamp.isoformat().replace("+00:00", "Z"),
        "complete": complete,
        "volume": 10,
        "mid": values,
        "bid": {key: f"{float(value) - 0.2:.5f}" for key, value in values.items()},
        "ask": {key: f"{float(value) + 0.2:.5f}" for key, value in values.items()},
    }


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self.payload = payload or {}
        self.status_code = status_code
        self.content = json.dumps(self.payload, sort_keys=True).encode("utf-8")

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class OandaDataLoadingTests(unittest.TestCase):
    def test_oanda_supports_one_minute_candles(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        with TemporaryDirectory() as temporary:
            session = _FakeSession([_FakeResponse({"candles": [_oanda_candle(start)]})])
            series, _, _ = load_oanda_candles(
                "XAU_USD",
                "1min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=1)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=Path(temporary) / "oanda.sqlite3",
                session=session,
            )

        self.assertEqual(session.calls[0]["params"]["granularity"], "M1")
        self.assertEqual(series.timeframe, "1min")

    def test_naive_timestamps_use_source_timezone_and_become_utc(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2026-01-01 08:00", "2026-01-01 08:15"],
                "open": [100, 101],
                "high": [102, 103],
                "low": [99, 100],
                "close": [101, 102],
            }
        )

        result = normalize_ohlc_frame(frame, metadata("Asia/Shanghai"), "15min")

        self.assertEqual(result.bars["open_time"].iloc[0], pd.Timestamp("2026-01-01 00:00", tz="UTC"))
        self.assertEqual(result.bars["close_time"].iloc[1], pd.Timestamp("2026-01-01 00:30", tz="UTC"))

    def test_invalid_ohlc_is_rejected_with_stable_code(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2026-01-01T00:00:00Z"],
                "open": [100],
                "high": [99],
                "low": [98],
                "close": [100],
            }
        )
        series = normalize_ohlc_frame(frame, metadata(), "15min")

        with self.assertRaises(DataValidationError) as context:
            validate_bar_series(series, expected_interval="15min")

        self.assertIn("INVALID_OHLC", {issue.code for issue in context.exception.issues})

    def test_oanda_returned_gaps_are_accepted_without_forward_fill(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:15:00Z",
                    "2026-01-01T00:45:00Z",
                ],
                "open": [100, 101, 103],
                "high": [102, 103, 105],
                "low": [99, 100, 102],
                "close": [101, 102, 104],
            }
        )
        series = normalize_ohlc_frame(frame, metadata(), "15min")

        issues = validate_bar_series(series, expected_interval="15min")
        self.assertEqual(len(series.bars), 3)
        self.assertEqual(issues, [])

    def test_oanda_returned_maintenance_gap_is_accepted(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2026-08-10T20:59:00Z", "2026-08-10T22:04:00Z"],
                "open": [100, 101],
                "high": [101, 102],
                "low": [99, 100],
                "close": [100.5, 101.5],
            }
        )
        series = normalize_ohlc_frame(frame, metadata(), "1min")

        issues = validate_bar_series(
            series,
            expected_interval="1min",
            raise_on_error=False,
        )

        self.assertEqual(issues, [])

    def test_oanda_selects_price_basis_and_discards_incomplete_candles(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        payload = {
            "candles": [
                _oanda_candle(start, base=100.0),
                _oanda_candle(start + pd.Timedelta(minutes=15), base=101.0),
                _oanda_candle(start + pd.Timedelta(minutes=30), complete=False, base=102.0),
            ]
        }

        for basis, expected_price, expected_close in (
            (PriceBasis.MID, "M", 100.5),
            (PriceBasis.BID, "B", 100.3),
            (PriceBasis.ASK, "A", 100.7),
        ):
            with self.subTest(basis=basis):
                with TemporaryDirectory() as temporary:
                    session = _FakeSession([_FakeResponse(payload)])
                    series, _, _ = load_oanda_candles(
                        "XAU_USD",
                        "15min",
                        InstrumentMetadata(
                            provider="oanda", symbol="XAU_USD", price_basis=basis, source_timezone="UTC"
                        ),
                        start=start.to_pydatetime(),
                        end=(start + pd.Timedelta(hours=1)).to_pydatetime(),
                        token="unit-test-token",
                        base_url="https://example.test",
                        database_path=Path(temporary) / "oanda.sqlite3",
                        session=session,
                    )

                self.assertEqual(session.calls[0]["params"]["price"], expected_price)
                self.assertEqual(len(series.bars), 2)
                self.assertEqual(series.bars.loc[0, "close"], expected_close)
                self.assertEqual(series.metadata.provider, "oanda")
                self.assertEqual(series.metadata.symbol, "XAU_USD")
                self.assertEqual(series.metadata.point_value, 1.0)
                self.assertEqual(series.metadata.tick_size, 0.01)

    def test_oanda_rejects_non_xauusd_instruments(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(DataSourceError, "XAU_USD"):
                load_oanda_candles(
                    "EUR_USD",
                    "15min",
                    metadata(),
                    start=start.to_pydatetime(),
                    end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                    token="unit-test-token",
                    base_url="https://example.test",
                    database_path=Path(temporary) / "oanda.sqlite3",
                    session=_FakeSession([]),
                )

    def test_oanda_sqlite_cache_is_reused_without_token_and_digest_is_stable(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        payload = {"candles": [_oanda_candle(start)]}
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "oanda.sqlite3"
            first_session = _FakeSession([_FakeResponse(payload)])
            first, first_digest, cache_file = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                token="sensitive-unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=first_session,
            )

            second_session = _FakeSession([])
            second, second_digest, second_cache_file = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                base_url="https://example.test",
                database_path=database_path,
                session=second_session,
            )

            self.assertTrue(database_path.is_file())
            self.assertFalse(list(Path(temporary).glob("*.json")))
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM oanda_bars").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM oanda_coverage").fetchone()[0], 1)
            finally:
                connection.close()

        self.assertEqual(len(first_session.calls), 1)
        self.assertEqual(second_session.calls, [])
        self.assertEqual(cache_file, database_path)
        self.assertEqual(second_cache_file, database_path)
        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first.bars.to_csv(index=False), second.bars.to_csv(index=False))
        self.assertEqual(first.metadata.to_dict(), second.metadata.to_dict())
        self.assertNotIn("token", first.metadata.source_url.lower())

    def test_oanda_sqlite_cache_merges_overlapping_ranges(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        candles = [_oanda_candle(start + index * pd.Timedelta(minutes=15), base=100 + index) for index in range(3)]
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "oanda.sqlite3"
            first_session = _FakeSession([_FakeResponse({"candles": candles[:2]})])
            load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=30)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=first_session,
            )

            second_start = start + pd.Timedelta(minutes=15)
            second_session = _FakeSession([_FakeResponse({"candles": [candles[2]]})])
            series, _, _ = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=second_start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=45)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=second_session,
            )

        self.assertEqual(len(first_session.calls), 1)
        self.assertEqual(len(second_session.calls), 1)
        self.assertEqual(len(series.bars), 2)

    def test_oanda_sqlite_cache_does_not_mark_incomplete_tail_as_covered(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "oanda.sqlite3"
            first_session = _FakeSession(
                [
                    _FakeResponse(
                        {
                            "candles": [
                                _oanda_candle(start),
                                _oanda_candle(start + pd.Timedelta(minutes=15), complete=False),
                            ]
                        }
                    )
                ]
            )
            first, _, _ = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=30)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=first_session,
            )

            second_session = _FakeSession(
                [_FakeResponse({"candles": [_oanda_candle(start + pd.Timedelta(minutes=15), base=101.0)]})]
            )
            second, _, _ = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=30)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=second_session,
            )

        self.assertEqual(len(first.bars), 1)
        self.assertEqual(len(second_session.calls), 1)
        self.assertEqual(len(second.bars), 2)

    def test_oanda_sqlite_cache_does_not_mark_an_empty_response_as_covered(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "oanda.sqlite3"
            with self.assertRaisesRegex(DataSourceError, "no complete candles"):
                load_oanda_candles(
                    "XAU_USD",
                    "15min",
                    metadata(),
                    start=start.to_pydatetime(),
                    end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                    token="unit-test-token",
                    base_url="https://example.test",
                    database_path=database_path,
                    session=_FakeSession([_FakeResponse({"candles": []})]),
                )

            session = _FakeSession([_FakeResponse({"candles": [_oanda_candle(start)]})])
            series, _, _ = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=database_path,
                session=session,
            )

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(series.bars), 1)

    def test_oanda_paginates_after_5000_candles(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        first_page = [
            _oanda_candle(start + index * pd.Timedelta(minutes=15), base=100.0 + index)
            for index in range(OANDA_MAX_CANDLES)
        ]
        second_timestamp = start + OANDA_MAX_CANDLES * pd.Timedelta(minutes=15)
        session = _FakeSession(
            [
                _FakeResponse({"candles": first_page}),
                _FakeResponse({"candles": [_oanda_candle(second_timestamp, base=5100.0)]}),
            ]
        )

        with TemporaryDirectory() as temporary:
            series, _, _ = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(second_timestamp + pd.Timedelta(minutes=15)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=Path(temporary) / "oanda.sqlite3",
                session=session,
            )

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["params"]["includeFirst"], "true")
        self.assertEqual(session.calls[1]["params"]["includeFirst"], "false")
        self.assertNotIn("to", session.calls[0]["params"])
        self.assertNotIn("to", session.calls[1]["params"])
        last_first_page_timestamp = second_timestamp - pd.Timedelta(minutes=15)
        self.assertEqual(
            session.calls[1]["params"]["from"],
            last_first_page_timestamp.isoformat().replace("+00:00", ".000000Z"),
        )
        self.assertEqual(len(series.bars), OANDA_MAX_CANDLES + 1)

    def test_oanda_continues_after_a_short_page_until_the_requested_end(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        first_page = [_oanda_candle(start + index * pd.Timedelta(minutes=15)) for index in range(3)]
        second_page = [_oanda_candle(start + index * pd.Timedelta(minutes=15)) for index in range(3, 6)]
        session = _FakeSession(
            [
                _FakeResponse({"candles": first_page}),
                _FakeResponse({"candles": second_page}),
            ]
        )

        with TemporaryDirectory() as temporary:
            series, _, _ = load_oanda_candles(
                "XAU_USD",
                "15min",
                metadata(),
                start=start.to_pydatetime(),
                end=(start + 6 * pd.Timedelta(minutes=15)).to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=Path(temporary) / "oanda.sqlite3",
                session=session,
            )

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["params"]["includeFirst"], "true")
        self.assertEqual(session.calls[1]["params"]["includeFirst"], "false")
        self.assertEqual(len(series.bars), 6)

    def test_oanda_accepts_an_empty_follow_up_page_at_a_market_closure(self) -> None:
        start = pd.Timestamp("2026-06-19T16:45:00Z")
        first_page = [_oanda_candle(start + index * pd.Timedelta(minutes=1)) for index in range(15)]
        session = _FakeSession(
            [
                _FakeResponse({"candles": first_page}),
                _FakeResponse({"candles": []}),
            ]
        )
        with TemporaryDirectory() as temporary:
            series, _, _ = load_oanda_candles(
                "XAU_USD",
                "1min",
                metadata(),
                start=start.to_pydatetime(),
                end=pd.Timestamp("2026-06-19T21:00:00Z").to_pydatetime(),
                token="unit-test-token",
                base_url="https://example.test",
                database_path=Path(temporary) / "oanda.sqlite3",
                session=session,
            )

        self.assertEqual(len(series.bars), 15)
        self.assertEqual(len(session.calls), 2)

    def test_oanda_auth_and_rate_limit_errors_are_explicit(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(DataSourceError, "OANDA_AUTH_FAILED"):
                load_oanda_candles(
                    "XAU_USD",
                    "15min",
                    metadata(),
                    start=start.to_pydatetime(),
                    end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                    token="invalid-unit-test-token",
                    base_url="https://example.test",
                    database_path=Path(temporary) / "oanda.sqlite3",
                    session=_FakeSession([_FakeResponse(status_code=401)]),
                )

            rate_limited = _FakeSession([_FakeResponse(status_code=429) for _ in range(3)])
            with patch("gold_research.data.loader.time.sleep"):
                with self.assertRaisesRegex(DataSourceError, "OANDA_RATE_LIMITED"):
                    load_oanda_candles(
                        "XAU_USD",
                        "15min",
                        metadata(),
                        start=start.to_pydatetime(),
                        end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                        token="unit-test-token",
                        base_url="https://example.test",
                        database_path=Path(temporary) / "oanda.sqlite3",
                        session=rate_limited,
                    )
            self.assertEqual(len(rate_limited.calls), 3)

    def test_oanda_applies_ohlc_validation_and_gap_policy(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        invalid = _oanda_candle(start)
        invalid["mid"]["h"] = "99.00000"

        with TemporaryDirectory() as temporary:
            with self.assertRaises(DataValidationError) as context:
                load_oanda_candles(
                    "XAU_USD",
                    "15min",
                    metadata(),
                    start=start.to_pydatetime(),
                    end=(start + pd.Timedelta(minutes=15)).to_pydatetime(),
                    token="unit-test-token",
                    base_url="https://example.test",
                    database_path=Path(temporary) / "oanda.sqlite3",
                    session=_FakeSession([_FakeResponse({"candles": [invalid]})]),
                )

        self.assertIn("INVALID_OHLC", {issue.code for issue in context.exception.issues})

    def test_oanda_does_not_silently_reorder_or_deduplicate_candles(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        cases = {
            "duplicate": [
                _oanda_candle(start),
                _oanda_candle(start),
            ],
            "out_of_order": [
                _oanda_candle(start + pd.Timedelta(minutes=15)),
                _oanda_candle(start),
            ],
        }

        for name, candles in cases.items():
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                with self.assertRaises(DataValidationError) as context:
                    load_oanda_candles(
                        "XAU_USD",
                        "15min",
                        metadata(),
                        start=start.to_pydatetime(),
                        end=(start + pd.Timedelta(minutes=30)).to_pydatetime(),
                        token="unit-test-token",
                        base_url="https://example.test",
                        database_path=Path(temporary) / "oanda.sqlite3",
                        session=_FakeSession([_FakeResponse({"candles": candles})]),
                    )

            codes = {issue.code for issue in context.exception.issues}
            expected = "DUPLICATE_TIMESTAMP" if name == "duplicate" else "UNSORTED_TIMESTAMP"
            self.assertIn(expected, codes)


if __name__ == "__main__":
    unittest.main()
