"""Read-only OANDA XAU_USD historical-candle adapter."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

from ..domain import BarSeries, InstrumentMetadata, PriceBasis
from ..market_calendar import NO_MARKET_CALENDAR
from .normalize import _time_delta, normalize_ohlc_frame
from .validate import DataValidationError, fatal_data_issues, validate_bar_series


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


def _oanda_cache_payload(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"cached OANDA response is invalid JSON: {path}") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("pages"), list)
        or not isinstance(payload.get("acquired_at"), str)
    ):
        raise DataSourceError(f"cached OANDA response has an invalid shape: {path}")
    return payload, raw


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


def _oanda_frame(pages: list[dict[str, Any]], price_key: str, start: datetime, end: datetime) -> pd.DataFrame:
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
    if not records:
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
    cache_dir: str | Path = "data/cache/oanda",
    timeout: float = 30.0,
    session: requests.Session | None = None,
    closed_weekdays: tuple[int, ...] = (),
    market_calendar: str = NO_MARKET_CALENDAR,
    missing_bar_policy: str = "block",
    max_gap_bars: int = 0,
) -> tuple[BarSeries, str, Path]:
    """Load complete OANDA XAU_USD candles with deterministic pagination.

    The adapter calls only OANDA's read-only candle endpoint. The API token is
    read from ``OANDA_API_TOKEN`` when omitted and is excluded from metadata,
    cache keys, and cached payloads.
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
        # Older cache entries treated a short OANDA page as the end of the
        # requested range. Keep them isolated because they may be truncated.
        "pagination": "cover-request-end-v2",
        "start": _oanda_time(start_dt),
        "end": _oanda_time(end_dt),
    }
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    identity_hash = hashlib.sha256(
        json.dumps(request_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    cache_file = cache_path / f"{canonical_instrument}_{granularity}_{price_parameter}_{identity_hash}.json"

    if cache_file.exists():
        cached, raw = _oanda_cache_payload(cache_file)
        if cached.get("request") != request_identity:
            raise DataSourceError(f"cached OANDA response does not match its request: {cache_file}")
        pages = cached["pages"]
        acquired_at = cached["acquired_at"]
    else:
        access_token = token or os.environ.get("OANDA_API_TOKEN", "").strip()
        if not access_token:
            raise DataSourceError("OANDA_API_TOKEN is not set")
        client = session or requests.Session()
        pages: list[dict[str, Any]] = []
        current_start = start_dt
        requested_end = pd.Timestamp(end_dt)
        bar_delta = _time_delta(timeframe)
        first_page = True
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
            # A trailing incomplete candle denotes the provider's current
            # partial interval. It cannot be used for historical research and
            # there is no later complete candle to request.
            if any(isinstance(candle, dict) and candle.get("complete") is False for candle in page_candles):
                break
            last_candle = page_candles[-1] if page_candles else None
            if not isinstance(last_candle, dict) or "time" not in last_candle:
                raise DataSourceError("OANDA returned no candles before the requested end")
            last_time = _utc_timestamp(last_candle["time"], "OANDA response contains an invalid candle timestamp")
            # OANDA can return fewer than ``count`` candles even when later
            # historical candles exist. Continue until the final returned bar
            # covers the requested end; page size alone is not a completion
            # signal.
            if last_time + bar_delta >= requested_end:
                break
            # OANDA excludes the `from` candle when includeFirst=false, so
            # resume at the final returned timestamp without skipping a bar.
            next_start = last_time.to_pydatetime()
            if next_start <= current_start:
                # Retain malformed page data so the canonical validator can
                # report duplicate or unsorted timestamps to the caller.
                break
            current_start = next_start
            first_page = False
        else:
            raise DataSourceError("OANDA pagination exceeded the safety limit")

        acquired_at = datetime.now(timezone.utc).isoformat()
        cache_payload = {
            "request": request_identity,
            "pages": pages,
            "acquired_at": acquired_at,
        }
        raw_to_write = json.dumps(cache_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_bytes(raw_to_write)
        temporary.replace(cache_file)
        raw = cache_file.read_bytes()

    frame = _oanda_frame(pages, price_key, start_dt, end_dt)
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
        closed_weekdays=closed_weekdays,
        market_calendar=market_calendar,
        raise_on_error=False,
    )
    series.quality_issues.extend(issues)
    fatal = fatal_data_issues(issues, missing_bar_policy, max_gap_bars)
    if fatal:
        raise DataValidationError(fatal)
    return series, hashlib.sha256(raw).hexdigest(), cache_file
