"""Stable Markdown rendering for one research run."""

from __future__ import annotations

from ..research import ResearchRun


def render_markdown_report(run: ResearchRun) -> str:
    manifest = run.manifest
    source = manifest.source_metadata
    lines = [
        f"# Historical Research: {manifest.strategy_id}",
        "",
        f"- Run ID: `{manifest.run_id}`",
        f"- Research usable: `{manifest.research_usable}`",
        f"- Symbol: `{manifest.symbol}`",
        f"- Provider: `{source.get('provider', '')}`",
        f"- Venue / contract: `{source.get('venue', '')}` / `{source.get('contract_unit', '')}`",
        f"- Price basis: `{manifest.price_basis}`",
        f"- Input: `{manifest.input_start}` to `{manifest.input_end}` ({manifest.timezone})",
        f"- Data fingerprint: `{manifest.data_fingerprint}`",
        f"- Config fingerprint: `{manifest.config_fingerprint}`",
        f"- Code fingerprint: `{manifest.code_fingerprint}`",
        "",
        "## Results",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in (
        "signal_count",
        "unfilled_signal_count",
        "trade_count",
        "gross_pnl",
        "spread_cost",
        "slippage_cost",
        "commission",
        "net_pnl",
        "max_drawdown",
        "average_mfe",
        "average_mae",
    ):
        lines.append(f"| {key} | {run.metrics.get(key, 0)} |")
    lines.extend(
        [
            "",
            "## Cost Model",
            "",
            "```json",
            _json_block(manifest.cost_model),
            "```",
            "",
            "## Warnings",
            "",
        ]
    )
    if run.warnings:
        lines.extend(f"- `{issue.code}`: {issue.message}" for issue in run.warnings)
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Signals are confirmed at a base-bar close and, when possible, filled at the next base-bar open.",
            "Signal-quality forward windows are diagnostic measurements and are separate from executable trade results.",
            "Passing code tests does not establish strategy profitability or suitability for live trading.",
            "",
        ]
    )
    return "\n".join(lines)


def _json_block(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
