"""Command-line interface for historical gold research."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_config, validate_oanda_xauusd_config
from .data.loader import DataSourceError, OANDA_XAU_USD, load_oanda_candles
from .domain import InstrumentMetadata
from .reporting.artifacts import write_run_artifacts
from .research import run_research


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gold-research")
    parser.add_argument("--version", action="version", version="gold-research 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("validate-config", help="validate a TOML research configuration")
    config_parser.add_argument("--config", required=True)

    run_parser = subparsers.add_parser("run", help="run one historical strategy")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--start", required=True, help="UTC ISO start for OANDA XAU_USD")
    run_parser.add_argument("--end", required=True, help="UTC ISO end for OANDA XAU_USD")
    run_parser.add_argument("--oanda-cache-dir", default="data/cache/oanda")
    run_parser.add_argument("--strategy", choices=["entry_point_2", "entry_point_3"], required=True)
    run_parser.add_argument("--output-root", default="runs")
    return parser


def _oanda_metadata(config) -> InstrumentMetadata:
    instrument = config.instrument
    return InstrumentMetadata(
        provider=instrument.provider,
        symbol=instrument.symbol,
        price_basis=instrument.price_basis,
        source_timezone=instrument.timezone,
        venue=instrument.venue,
        contract_unit=instrument.contract_unit,
        quote_currency=instrument.quote_currency,
        tick_size=instrument.tick_size,
        point_value=instrument.point_value,
    )


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("date must include a timezone, e.g. 2020-01-01T00:00:00Z")
    return parsed.astimezone(timezone.utc)


def _load_oanda(args, config):
    token = os.environ.get("OANDA_API_TOKEN", "").strip() or None
    return load_oanda_candles(
        OANDA_XAU_USD,
        config.timeframes.base,
        _oanda_metadata(config),
        start=_parse_utc(args.start),
        end=_parse_utc(args.end),
        token=token,
        cache_dir=args.oanda_cache_dir,
        closed_weekdays=config.data_quality.closed_weekdays,
        missing_bar_policy=config.data_quality.missing_bar_policy,
        max_gap_bars=config.data_quality.max_gap_bars,
    )[:2]


def _print_config(config) -> None:
    print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        validate_oanda_xauusd_config(config)
        if args.command == "validate-config":
            _print_config(config)
            return 0
        series, fingerprint = _load_oanda(args, config)
        run = run_research(
            series,
            config,
            args.strategy,
            data_fingerprint=fingerprint,
            code_root=Path(__file__).resolve().parent,
        )
        output = write_run_artifacts(run, args.output_root)
        _print_config(config)
        print(json.dumps({"run_id": run.manifest.run_id, "output": str(output), "metrics": run.metrics, "warnings": [issue.to_dict() for issue in run.warnings]}, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ConfigError, DataSourceError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
