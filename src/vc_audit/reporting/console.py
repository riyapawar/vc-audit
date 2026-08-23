"""Terminal rendering of a valuation report.

The CLI is the surface an auditor actually lives in, so the default view is
tuned for the question they ask first -- "what is the number, and can I trust
it?" That means the conclusion, the cross-method agreement, and the exceptions
are always visible, while the full step-by-step arithmetic is behind
``--detail``. Burying the exceptions under two hundred rows of arithmetic would
be a worse tool than one that never computed them.
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from vc_audit.domain.models import MethodResult, ValuationReport
from vc_audit.methods.registry import get_method
from vc_audit.reporting.formatting import by_unit, compact, money, percent

_AGREEMENT_STYLE = {
    "tight": "green",
    "moderate": "yellow",
    "wide": "red",
    "single-method": "yellow",
}


def render(
    report: ValuationReport, *, detail: bool = False, console: Console | None = None
) -> None:
    """Print the report. ``detail`` adds the full calculation trail."""
    console = console or Console()

    _conclusion(report, console)
    _methods(report, console)
    _agreement(report, console)
    _skipped(report, console)
    _review_queue(report, console)
    _exceptions(report, console)

    if detail:
        for result in report.method_results:
            _trail(result, console)

    console.print(
        Text(
            f"run {report.run_id}  ·  fingerprint {report.fingerprint[:16]}…",
            style="dim",
        )
    )


def _conclusion(report: ValuationReport, console: Console) -> None:
    rng = report.concluded_range
    body = Text()
    body.append(f"{money(report.concluded_value_usd, precise=True)}\n", style="bold cyan")
    body.append(
        f"range {money(rng.low_usd, precise=True)} – {money(rng.high_usd, precise=True)}\n\n",
        style="dim",
    )
    body.append(report.narrative)
    console.print(
        Panel(
            body,
            title=f"[bold]{report.company_name}[/] · fair value at {report.as_of.isoformat()}",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def _methods(report: ValuationReport, console: Console) -> None:
    table = Table(title="Methods applied", box=box.SIMPLE_HEAD, title_justify="left")
    table.add_column("Method")
    table.add_column("Value", justify="right")
    table.add_column("Range", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("Top sensitivity")

    total = sum(get_method(r.method_id).default_weight for r in report.method_results)
    for result in report.method_results:
        weight = get_method(result.method_id).default_weight / total if total else 0.0
        dominant = result.sensitivity.dominant_driver if result.sensitivity else None
        driver_text = (
            f"{dominant.label} ±{percent(dominant.swing_pct / 2, places=0)}" if dominant else "—"
        )
        table.add_row(
            result.method_name,
            money(result.equity_value_usd, precise=True),
            f"{money(result.value_range.low_usd)} – {money(result.value_range.high_usd)}",
            percent(weight, places=0),
            driver_text,
        )
    console.print(table)


def _agreement(report: ValuationReport, console: Console) -> None:
    concordance = report.concordance
    style = _AGREEMENT_STYLE.get(concordance.agreement, "white")
    console.print(
        Panel(
            Text(concordance.commentary),
            title=f"[{style}]Cross-method agreement: {concordance.agreement}[/] "
            f"(CV {percent(concordance.coefficient_of_variation)})",
            border_style=style,
            box=box.ROUNDED,
        )
    )


def _skipped(report: ValuationReport, console: Console) -> None:
    if not report.skipped_methods:
        return
    table = Table(title="Methods not applied", box=box.SIMPLE_HEAD, title_justify="left")
    table.add_column("Method")
    table.add_column("Reason")
    for skip in report.skipped_methods:
        table.add_row(skip.method_name, skip.reason)
    console.print(table)


def _review_queue(report: ValuationReport, console: Console) -> None:
    rows = [
        (result.method_id, assumption)
        for result in report.method_results
        for assumption in result.trail.unreviewed_defaults()
    ]
    if not rows:
        return
    table = Table(
        title="Assumption review queue (engine defaults, not auditor-supplied)",
        box=box.SIMPLE_HEAD,
        title_justify="left",
    )
    table.add_column("Method")
    table.add_column("Assumption")
    table.add_column("Value", justify="right")
    for method_id, assumption in rows:
        table.add_row(method_id, assumption.key, assumption.display_value())
    console.print(table)


def _exceptions(report: ValuationReport, console: Console) -> None:
    warnings = report.all_warnings
    if not warnings:
        return
    body = Text()
    for index, warning in enumerate(warnings, start=1):
        body.append(f"{index}. {warning}\n")
    console.print(
        Panel(body, title="[yellow]Exceptions[/]", border_style="yellow", box=box.ROUNDED)
    )


def _trail(result: MethodResult, console: Console) -> None:
    table = Table(
        title=f"{result.method_name} — calculation trail",
        box=box.SIMPLE_HEAD,
        title_justify="left",
        show_lines=False,
    )
    table.add_column("#", justify="right", width=3)
    table.add_column("Step", max_width=44)
    table.add_column("Calculation", max_width=54)
    table.add_column("Result", justify="right", max_width=26)

    for step in result.trail.steps:
        output = (
            compact(step.output, limit=26)
            if isinstance(step.output, (dict, list))
            else by_unit(step.output, step.unit)
        )
        table.add_row(str(step.seq), step.description, compact(step.formula, limit=110), output)
    console.print(table)

    if result.sensitivity is not None:
        sens = Table(
            title=f"{result.method_name} — sensitivity", box=box.SIMPLE_HEAD, title_justify="left"
        )
        sens.add_column("Assumption")
        sens.add_column("Low", justify="right")
        sens.add_column("High", justify="right")
        sens.add_column("Value at low", justify="right")
        sens.add_column("Value at high", justify="right")
        sens.add_column("Swing", justify="right")
        for row in result.sensitivity.ranked_rows:
            sens.add_row(
                row.label,
                by_unit(row.low_input, row.unit),
                by_unit(row.high_input, row.unit),
                money(row.low_value_usd),
                money(row.high_value_usd),
                f"{money(row.swing_usd)} ({percent(row.swing_pct, places=0)})",
            )
        console.print(sens)
