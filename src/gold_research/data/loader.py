"""Read-only OANDA XAU_USD historical-candle adapter."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

from ..domain import BarSeries, InstrumentMetadata, PriceBasis
from .normalize import _time_delta, normalize_ohlc_frame
from .validate import DataValidationError, validate_bar_series


class DataSourceError(RuntimeError):
    """Raised when OANDA cannot supply usable historical bars."""


OANDA_XAU_USD = "XAU_USD"
OANDA_MAX_CANDLES = 5000


def _oanda_granularity(timeframe: str) -> str:
    value = timeframe.lower().replace(" ", "")
    mapping = {
        "1min": "M1",
        "5min": "M5",
        "15min": "M15",
        "30min": "M30",
        "1h": "H1",
        "60min": "H1",
        "2h": "H2",
        "4h": "H4",
        "240min": "H4",
        "1d": "D",
        "1day": "D",
    }
    if value not in mapping:
        raise DataSourceError(f"OANDA does not support configured interval: {timeframe}")
    return mapping[value]


def _oanda_price_basis(price_basis: PriceBasis) -> tuple[str, str]:
    mapping = {
        PriceBasis.MID: ("M", "mid"),
        PriceBasis.BID: ("B", "bid"),
        PriceBasis.ASK: ("A", "ask"),
    }
    return mapping[price_basis]


def _utc_datetime(value: datetime, name: str) -> datetime:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise DataSourceError(f"OANDA {name} must be timezone-aware")
    return parsed.to_pydatetime().astimezone(timezone.utc)


def _utc_timestamp(value: object, message: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DataSourceError(message) from exc
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _oanda_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _timestamp_ns(value: object, message: str) -> int:
    return int(_utc_timestamp(value, message).value)


def _open_oanda_database(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS oanda_bars (
            base_url TEXT NOT NULL,
            instrument TEXT NOT NULL,
            granularity TEXT NOT NULL,
            price_basis TEXT NOT NULL,
            timestamp_ns INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER,
            PRIMARY KEY (base_url, instrument, granularity, price_basis, timestamp_ns)
        );

        CREATE TABLE IF NOT EXISTS oanda_coverage (
            base_url TEXT NOT NULL,
            instrument TEXT NOT NULL,
            granularity TEXT NOT NULL,
            price_basis TEXT NOT NULL,
            start_ns INTEGER NOT NULL,
            end_ns INTEGER NOT NULL,
            acquired_at TEXT NOT NULL,
            PRIMARY KEY (base_url, instrument, granularity, price_basis, start_ns, end_ns),
            CHECK (end_ns > start_ns)
        );

        CREATE INDEX IF NOT EXISTS oanda_coverage_lookup
            ON oanda_coverage (base_url, instrument, granularity, price_basis, start_ns, end_ns);
        """
    )
    connection.commit()
    return connection


def _dataset_key(
    *,
    base_url: str,
    instrument: str,
    granularity: str,
    price_parameter: str,
) -> tuple[str, str, str, str]:
    return base_url, instrument, granularity, price_parameter


def _covered_windows(
    connection: sqlite3.Connection,
    dataset: tuple[str, str, str, str],
    start_ns: int,
    end_ns: int,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    rows = connection.execute(
        """
        SELECT start_ns, end_ns
        FROM oanda_coverage
        WHERE base_url = ?
          AND instrument = ?
          AND granularity = ?
          AND price_basis = ?
          AND start_ns < ?
          AND end_ns > ?
        ORDER BY start_ns
        """,
        (*dataset, end_ns, start_ns),
    ).fetchall()
    return [
        (
            pd.Timestamp(int(row[0]), unit="ns", tz="UTC"),
            pd.Timestamp(int(row[1]), unit="ns", tz="UTC"),
        )
        for row in rows
    ]


def _missing_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
    covered: list[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    missing: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    covered_until = start
    for window_start, window_end in sorted(covered):
        if window_end <= covered_until:
            continue
        if window_start > covered_until:
            missing.append((covered_until, min(window_start, end)))
        covered_until = max(covered_until, window_end)
        if covered_until >= end:
            break
    if covered_until < end:
        missing.append((covered_until, end))
    return [(left, right) for left, right in missing if right > left]


def _latest_acquired_at(
    connection: sqlite3.Connection,
    dataset: tuple[str, str, str, str],
    start_ns: int,
    end_ns: int,
) -> str:
    row = connection.execute(
        """
        SELECT MAX(acquired_at)
        FROM oanda_coverage
        WHERE base_url = ?
          AND instrument = ?
          AND granularity = ?
          AND price_basis = ?
          AND start_ns < ?
          AND end_ns > ?
        """,
        (*dataset, end_ns, start_ns),
    ).fetchone()
    if not row or not row[0]:
        raise DataSourceError("OANDA database has no acquisition metadata for the requested range")
    return str(row[0])


def _store_oanda_frame(
    connection: sqlite3.Connection,
    frame: pd.DataFrame,
    *,
    dataset: tuple[str, str, str, str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    acquired_at: str,
) -> None:
    rows: list[tuple[Any, ...]] = []
    for record in frame.to_dict(orient="records"):
        volume = record.get("volume")
        rows.append(
            (
                *dataset,
                _timestamp_ns(record["timestamp"], "OANDA response contains an invalid candle timestamp"),
                float(record["open"]),
                float(record["high"]),
                float(record["low"]),
                float(record["close"]),
                None if volume is None or pd.isna(volume) else int(volume),
            )
        )
    connection.executemany(
        """
        INSERT INTO oanda_bars (
            base_url, instrument, granularity, price_basis, timestamp_ns,
            open, high, low, close, volume
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (base_url, instrument, granularity, price_basis, timestamp_ns)
        DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume
        """,
        rows,
    )
    connection.execute(
        """
        INSERT INTO oanda_coverage (
            base_url, instrument, granularity, price_basis,
            start_ns, end_ns, acquired_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (base_url, instrument, granularity, price_basis, start_ns, end_ns)
        DO UPDATE SET acquired_at = excluded.acquired_at
        """,
        (*dataset, _timestamp_ns(start, "OANDA start timestamp is invalid"), _timestamp_ns(end, "OANDA end timestamp is invalid"), acquired_at),
    )
    connection.commit()


def _read_oanda_frame(
    connection: sqlite3.Connection,
    *,
    dataset: tuple[str, str, str, str],
    start_ns: int,
    end_ns: int,
) -> pd.DataFrame:
    rows = connection.execute(
        """
        SELECT timestamp_ns, open, high, low, close, volume
        FROM oanda_bars
        WHERE base_url = ?
          AND instrument = ?
          AND granularity = ?
          AND price_basis = ?
          AND timestamp_ns >= ?
          AND timestamp_ns < ?
        ORDER BY timestamp_ns
        """,
        (*dataset, start_ns, end_ns),
    ).fetchall()
    if not rows:
        raise DataSourceError("OANDA returned no complete candles in the requested range")
    frame = pd.DataFrame(
        rows,
        columns=["timestamp_ns", "open", "high", "low", "close", "volume"],
    )
    frame["timestamp"] = pd.to_datetime(frame.pop("timestamp_ns"), unit="ns", utc=True)
    if frame["volume"].isna().all():
        frame = frame.drop(columns=["volume"])
    columns = ["timestamp", "open", "high", "low", "close"]
    if "volume" in frame:
        columns.append("volume")
    return frame[columns]


def _series_digest(series: BarSeries) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(series.metadata.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(series.timeframe.encode("utf-8"))
    digest.update(series.bars.to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%S%z").encode("utf-8"))
    return digest.hexdigest()


def _oanda_page(
    client: requests.Session,
    url: str,
    params: dict[str, Any],
    token: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Accept-Datetime-Format": "RFC3339",
        "Authorization": f"Bearer {token}",
        "User-Agent": "gold-research/0.1 historical-research",
    }
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, 0.75, 1.5), start=1):
        if delay:
            time.sleep(delay)
        try:
            response = client.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code == 429:
                last_error = DataSourceError(
                    f"OANDA_RATE_LIMITED: OANDA rate-limited the request after attempt {attempt}"
                )
                continue
            if response.status_code in {401, 403}:
                raise DataSourceError("OANDA_AUTH_FAILED: OANDA rejected the API token")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise DataSourceError("OANDA returned a non-object JSON response")
            return payload
        except DataSourceError:
            raise
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    if isinstance(last_error, DataSourceError):
        raise last_error
    raise DataSourceError(f"OANDA request failed: {last_error}") from last_error


def _oanda_frame(
    pages: list[dict[str, Any]],
    price_key: str,
    start: datetime,
    end: datetime,
    *,
    allow_empty: bool = False,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    start_timestamp = pd.Timestamp(start)
    end_timestamp = pd.Timestamp(end)
    for page in pages:
        candles = page.get("candles")
        if not isinstance(candles, list):
            raise DataSourceError("OANDA response is missing candles")
        for candle in candles:
            if not isinstance(candle, dict) or "time" not in candle or "complete" not in candle:
                raise DataSourceError("OANDA response contains a malformed candle")
            if candle["complete"] is not True:
                continue
            timestamp = _utc_timestamp(candle["time"], "OANDA response contains an invalid candle timestamp")
            if timestamp < start_timestamp or timestamp >= end_timestamp:
                continue
            quote_values = candle.get(price_key)
            if not isinstance(quote_values, dict):
                raise DataSourceError(f"OANDA candle at {candle['time']} is missing {price_key} prices")
            record: dict[str, Any] = {"timestamp": timestamp}
            for field, provider_field in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
                if provider_field not in quote_values:
                    raise DataSourceError(f"OANDA candle at {candle['time']} is missing {provider_field}")
                record[field] = quote_values[provider_field]
            if "volume" in candle:
                record["volume"] = candle["volume"]
            records.append(record)
    if not records and not allow_empty:
        raise DataSourceError("OANDA returned no complete candles in the requested range")
    # Keep the provider's order and duplicates intact so the canonical data
    # validator can reject an anomalous response instead of silently repairing it.
    return pd.DataFrame(records).reset_index(drop=True)


def _oanda_metadata(
    metadata: InstrumentMetadata,
    *,
    instrument: str,
    timeframe: str,
    endpoint: str,
    request: dict[str, str],
    frame: pd.DataFrame,
    acquired_at: str,
) -> InstrumentMetadata:
    values = metadata.to_dict()
    values.update(
        {
            "provider": "oanda",
            "symbol": instrument,
            "source_timezone": "UTC",
            "venue": "OANDA spot/CFD",
            "contract_unit": "1 troy ounce",
            "quote_currency": "USD",
            "tick_size": 0.01,
            "point_value": 1.0,
            "source_interval": timeframe,
            "source_url": f"{endpoint}?" + "&".join(
                f"{key}={value}" for key, value in request.items() if key not in {"base_url", "instrument"}
            ),
            "acquired_at": acquired_at,
            "raw_start": pd.Timestamp(frame["timestamp"].min()).isoformat(),
            "raw_end": (pd.Timestamp(frame["timestamp"].max()) + _time_delta(timeframe)).isoformat(),
        }
    )
    return InstrumentMetadata(**values)


def load_oanda_candles(
    instrument: str,
    timeframe: str,
    metadata: InstrumentMetadata,
    *,
    start: datetime,
    end: datetime,
    token: str | None = None,
    base_url: str = "https://api-fxpractice.oanda.com",
    database_path: str | Path = "data/cache/oanda.sqlite3",
    timeout: float = 30.0,
    session: requests.Session | None = None,
) -> tuple[BarSeries, str, Path]:
    """Load complete OANDA XAU_USD candles with deterministic pagination.

    The adapter calls only OANDA's read-only candle endpoint. The API token is
    read from ``OANDA_API_TOKEN`` when omitted. Complete candles are stored in
    a local SQLite database and are reused by timestamp range; raw JSON
    responses are never written to disk.
    """

    canonical_instrument = instrument.strip().upper()
    if canonical_instrument != OANDA_XAU_USD:
        raise DataSourceError("OANDA historical research supports XAU_USD only")
    granularity = _oanda_granularity(timeframe)
    price_parameter, price_key = _oanda_price_basis(metadata.price_basis)
    start_dt = _utc_datetime(start, "start")
    end_dt = _utc_datetime(end, "end")
    if end_dt <= start_dt:
        raise DataSourceError("OANDA end must be after start")

    endpoint = f"{base_url.rstrip('/')}/v3/instruments/{quote(canonical_instrument, safe='_')}/candles"
    request_identity = {
        "base_url": base_url.rstrip("/"),
        "instrument": canonical_instrument,
        "granularity": granularity,
        "price": price_parameter,
        "pagination": "cover-request-end-v2",
        "start": _oanda_time(start_dt),
        "end": _oanda_time(end_dt),
    }
    database_file = Path(database_path)
    dataset = _dataset_key(
        base_url=base_url.rstrip("/"),
        instrument=canonical_instrument,
        granularity=granularity,
        price_parameter=price_parameter,
    )
    start_timestamp = pd.Timestamp(start_dt)
    end_timestamp = pd.Timestamp(end_dt)
    start_ns = _timestamp_ns(start_timestamp, "OANDA start timestamp is invalid")
    end_ns = _timestamp_ns(end_timestamp, "OANDA end timestamp is invalid")
    connection = _open_oanda_database(database_file)
    try:
        covered = _covered_windows(connection, dataset, start_ns, end_ns)
        missing = _missing_windows(start_timestamp, end_timestamp, covered)
        access_token = token or os.environ.get("OANDA_API_TOKEN", "").strip() or None
        client = session or requests.Session()
        bar_delta = _time_delta(timeframe)

        for missing_start, missing_end in missing:
            if not access_token:
                raise DataSourceError("OANDA_API_TOKEN is not set")
            pages: list[dict[str, Any]] = []
            current_start = missing_start.to_pydatetime()
            requested_end = pd.Timestamp(missing_end)
            first_page = True
            coverage_end = requested_end
            saw_incomplete = False
            for _ in range(10000):
                params = {
                    "from": _oanda_time(current_start),
                    "granularity": granularity,
                    "price": price_parameter,
                    "count": OANDA_MAX_CANDLES,
                    "includeFirst": "true" if first_page else "false",
                    "smooth": "false",
                }
                page = _oanda_page(client, endpoint, params, access_token, timeout=timeout)
                page_candles = page.get("candles")
                if not isinstance(page_candles, list):
                    raise DataSourceError("OANDA response is missing candles")
                pages.append(page)
                if not page_candles:
                    break
                if any(isinstance(candle, dict) and candle.get("complete") is False for candle in page_candles):
                    saw_incomplete = True
                    break
                last_candle = page_candles[-1]
                if not isinstance(last_candle, dict) or "time" not in last_candle:
                    raise DataSourceError("OANDA returned no candles before the requested end")
                last_time = _utc_timestamp(last_candle["time"], "OANDA response contains an invalid candle timestamp")
                if last_time + bar_delta >= requested_end:
                    break
                next_start = last_time.to_pydatetime()
                if next_start <= current_start:
                    break
                current_start = next_start
                first_page = False
            else:
                raise DataSourceError("OANDA pagination exceeded the safety limit")

            acquired_at = datetime.now(timezone.utc).isoformat()
            frame = _oanda_frame(
                pages,
                price_key,
                missing_start.to_pydatetime(),
                missing_end.to_pydatetime(),
                allow_empty=True,
            )
            if saw_incomplete:
                coverage_end = (
                    min(requested_end, pd.Timestamp(frame["timestamp"].max()) + bar_delta)
                    if not frame.empty
                    else missing_start
                )
            if not frame.empty:
                fetched_series = normalize_ohlc_frame(
                    frame,
                    _oanda_metadata(
                        metadata,
                        instrument=canonical_instrument,
                        timeframe=timeframe,
                        endpoint=endpoint,
                        request=request_identity,
                        frame=frame,
                        acquired_at=acquired_at,
                    ),
                    timeframe,
                )
                fetched_issues = validate_bar_series(
                    fetched_series,
                    expected_interval=timeframe,
                    raise_on_error=False,
                )
                if fetched_issues:
                    raise DataValidationError(fetched_issues)
            if not frame.empty and coverage_end > missing_start:
                _store_oanda_frame(
                    connection,
                    frame,
                    dataset=dataset,
                    start=missing_start,
                    end=coverage_end,
                    acquired_at=acquired_at,
                )

        frame = _read_oanda_frame(
            connection,
            dataset=dataset,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        acquired_at = _latest_acquired_at(connection, dataset, start_ns, end_ns)
        series = normalize_ohlc_frame(
            frame,
            _oanda_metadata(
                metadata,
                instrument=canonical_instrument,
                timeframe=timeframe,
                endpoint=endpoint,
                request=request_identity,
                frame=frame,
                acquired_at=acquired_at,
            ),
            timeframe,
        )
        issues = validate_bar_series(
            series,
            expected_interval=timeframe,
            raise_on_error=False,
        )
        series.quality_issues.extend(issues)
        if issues:
            raise DataValidationError(issues)
        return series, _series_digest(series), database_file
    finally:
        connection.close()
