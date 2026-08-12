"""Command-line entry point; data and strategy commands are added incrementally."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gold-research")
    parser.add_argument("--version", action="version", version="gold-research 0.1.0")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("validate-config", help="validate a TOML research configuration")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-config":
        raise SystemExit("validate-config implementation is pending")
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

