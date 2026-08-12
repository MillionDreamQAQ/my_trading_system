"""Local and Yahoo Chart API data adapters with explicit metadata."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..domain import InstrumentMetadata, PriceBasis
from .normalize import normalize_ohlc_frame
from .validate import validate_bar_series


class DataSourceError(RuntimeError):
    """Raised when a data provider cannot supply usable historical bars."""


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_local(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        try:
            return pd.read_parquet(path)
        except ImportError as exc:
            raise DataSourceError("Parquet input requires the optional pyarrow dependency") from exc
    raise DataSourceError(f"unsupported input format: {path.suffix}; use CSV or Parquet")


def load_local_file(
    path: str | Path,
    metadata: InstrumentMetadata,
    timeframe: str,
    *,
    closed_weekdays: tuple[int, ...] = (),
    validate: bool = True,
):
    """Read and normalize a local CSV/Parquet file."""

    source_path = Path(path)
    if not source_path.is_file():
        raise DataSourceError(f"data file not found: {source_path}")
    enriched = InstrumentMetadata(
        **{**metadata.to_dict(), "source_interval": timeframe}
    )
    series = normalize_ohlc_frame(_read_local(source_path), enriched, timeframe)
    if validate:
        validate_bar_series(series, closed_weekdays=closed_weekdays)
    return series, _file_fingerprint(source_path)


def _yahoo_interval(interval: str) -> str:
    value = interval.lower().replace(" ", "")
    mapping = {"1d": "1d", "1day": "1d", "1h": "1h", "60min": "60m", "15min": "15m", "30min": "30m", "5min": "5m"}
    if value not in mapping:
        raise DataSourceError(f"Yahoo Chart API does not support configured interval: {interval}")
    return mapping[value]


def load_yahoo_chart(
    symbol: str,
    timeframe: str,
    metadata: InstrumentMetadata | None = None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    cache_dir: str | Path = "data/cache/yahoo",
    timeout: float = 30.0,
    session: requests.Session | None = None,
):
    """Download Yahoo Chart API data and cache the exact response locally.

    Yahoo's ``GC=F`` symbol represents the COMEX gold futures continuous
    contract. It is deliberately kept distinct from a broker's XAUUSD spot
    or CFD feed.
    """

    interval = _yahoo_interval(timeframe)
    start_dt = start or datetime(2000, 1, 1, tzinfo=timezone.utc)
    end_dt = end or datetime.now(timezone.utc)
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise DataSourceError("Yahoo start and end must be timezone-aware")
    if end_dt <= start_dt:
        raise DataSourceError("Yahoo end must be after start")
    query = {
        "period1": int(start_dt.timestamp()),
        "period2": int(end_dt.timestamp()),
        "interval": interval,
        "events": "history",
        "includeAdjustedClose": "true",
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    client = session or requests.Session()
    try:
        response = client.get(url, params=query, timeout=timeout)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise DataSourceError(f"Yahoo request failed for {symbol}: {exc}") from exc
    result = (payload.get("chart") or {}).get("result")
    error = (payload.get("chart") or {}).get("error")
    if not result:
        description = (error or {}).get("description", "empty response")
        raise DataSourceError(f"Yahoo returned no data for {symbol}: {description}")
    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
            "volume": quote.get("volume", []),
        }
    )
    if frame.empty:
        raise DataSourceError(f"Yahoo returned no bars for {symbol}")
    base_metadata = metadata or InstrumentMetadata(
        provider="yahoo",
        symbol=symbol,
        price_basis=PriceBasis.MID,
        source_timezone="UTC",
        venue="COMEX futures continuous contract" if symbol == "GC=F" else "Yahoo Finance",
        contract_unit="100 troy ounces" if symbol == "GC=F" else "",
        quote_currency="USD",
        source_interval=timeframe,
        source_url=url,
        acquired_at=datetime.now(timezone.utc).isoformat(),
    )
    metadata_dict = base_metadata.to_dict()
    metadata_dict.update(
        {
            "provider": "yahoo",
            "symbol": symbol,
            "source_timezone": "UTC",
            "source_interval": timeframe,
            "source_url": response.url,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "raw_start": frame["timestamp"].min().isoformat(),
            "raw_end": frame["timestamp"].max().isoformat(),
        }
    )
    series = normalize_ohlc_frame(frame, InstrumentMetadata(**metadata_dict), timeframe)
    validate_bar_series(series)
    raw = response.content
    digest = hashlib.sha256(raw).hexdigest()
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = cache_path / f"{symbol.replace('/', '_')}_{interval}_{query['period1']}_{query['period2']}_{digest[:12]}.json"
    if not cache_file.exists():
        cache_file.write_bytes(raw)
    return series, digest, cache_file
