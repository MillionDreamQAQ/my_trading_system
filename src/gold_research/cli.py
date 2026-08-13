"""Command-line interface for historical gold research."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_config, validate_oanda_xauusd_config
from .data.loader import DataSourceError, OANDA_XAU_USD, load_oanda_candles
from .dashboard import DashboardDataStore, build_dashboard_payload, serve_dashboard
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
    run_parser.add_argument("--oanda-database", default="data/cache/oanda.sqlite3")
    run_parser.add_argument("--strategy", choices=["entry_point_2"], required=True)
    run_parser.add_argument("--output-root", default="runs")

    dashboard_parser = subparsers.add_parser("dashboard", help="serve a local historical chart dashboard")
    dashboard_parser.add_argument("--config", required=True)
    dashboard_parser.add_argument("--start", required=True, help="UTC ISO start of the displayed evaluation window")
    dashboard_parser.add_argument(
        "--end",
        default=_default_dashboard_end(),
        help="UTC ISO end of the displayed evaluation window (default: current UTC time)",
    )
    dashboard_parser.add_argument("--warmup-start", help="optional UTC ISO start used only for indicator warm-up")
    dashboard_parser.add_argument("--oanda-database", default="data/cache/oanda.sqlite3")
    dashboard_parser.add_argument("--host", default="127.0.0.1")
    dashboard_parser.add_argument("--port", type=int, default=8000)
    return parser


def _default_dashboard_end() -> str:
    """Return the current UTC minute for a dashboard's default end."""

    return datetime.now(timezone.utc).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _load_oanda(args, config, *, compute_digest: bool = True):
    token = os.environ.get("OANDA_API_TOKEN", "").strip() or None
    return load_oanda_candles(
        OANDA_XAU_USD,
        config.timeframes.base,
        _oanda_metadata(config),
        start=_parse_utc(getattr(args, "warmup_start", None) or args.start),
        end=_parse_utc(args.end),
        token=token,
        database_path=args.oanda_database,
        compute_digest=compute_digest,
    )[:2]


def _dashboard_loader(args, config):
    """Create the dashboard's on-demand OANDA loader without exposing its token."""

    token = os.environ.get("OANDA_API_TOKEN", "").strip() or None

    def load_window(start: datetime, end: datetime):
        return load_oanda_candles(
            OANDA_XAU_USD,
            config.timeframes.base,
            _oanda_metadata(config),
            start=start,
            end=end,
            token=token,
            database_path=args.oanda_database,
            compute_digest=False,
        )[0]

    return load_window


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
        series, fingerprint = _load_oanda(args, config, compute_digest=args.command != "dashboard")
        if args.command == "dashboard":
            # The dashboard remains read-only and preserves gap warnings so users
            # can inspect data around known market closures.
            payload = build_dashboard_payload(
                series,
                config,
                display_start=_parse_utc(args.start),
                display_end=_parse_utc(args.end),
            )
            _print_config(config)
            print(json.dumps({"data_fingerprint": fingerprint, "quality_issues": payload["quality_issues"]}, ensure_ascii=False))
            serve_dashboard(
                payload,
                args.host,
                args.port,
                base=series,
                config=config,
                data_store=DashboardDataStore(
                    series,
                    config,
                    _dashboard_loader(args, config),
                    initial_start=_parse_utc(args.warmup_start or args.start),
                    initial_end=_parse_utc(args.end),
                ),
                default_display_start=_parse_utc(args.start),
                default_display_end=_parse_utc(args.end),
            )
            return 0
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
