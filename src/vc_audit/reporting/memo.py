"""Renders a :class:`ValuationReport` as a Markdown workpaper.

The memo is the actual deliverable: the thing that goes in the file and gets
reviewed. It is ordered the way a reviewer reads -- conclusion first, then the
things most likely to be challenged (agreement between methods, unreviewed
assumptions, exceptions), and only then the arithmetic.

The memo contains no generation timestamp. That is deliberate: the same inputs
must render byte-identical output, so a reviewer can diff last quarter's memo
against this quarter's and see only the changes that are real. The valuation
date and the fingerprint carry the temporal information instead.
"""

from __future__ import annotations

from vc_audit.domain.models import MethodResult, ValuationReport
from vc_audit.methods.registry import get_method
from vc_audit.reporting.formatting import by_unit, compact, escape_pipes, money, percent

_AGREEMENT_MARK = {
    "tight": "Converging",
    "moderate": "Moderate divergence",
    "wide": "Material divergence",
    "single-method": "Uncorroborated",
}


def render(report: ValuationReport) -> str:
    """Produce the full Markdown memo."""
    sections = [
        _header(report),
        _conclusion(report),
        _methods_summary(report),
        _concordance(report),
        _review_queue(report),
        _exceptions(report),
        _method_details(report),
        _not_applied(report),
        _sources(report),
        _reproduction(report),
    ]
    return "\n\n".join(section for section in sections if section).rstrip() + "\n"


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _header(report: ValuationReport) -> str:
    return "\n".join(
        [
            f"# Fair Value Memorandum — {report.company_name}",
            "",
            "| | |",
            "|---|---|",
            f"| **Valuation date** | {report.as_of.isoformat()} |",
            f"| **Sector** | {report.sector} |",
            f"| **Run ID** | `{report.run_id}` |",
            f"| **Trail fingerprint** | `{report.fingerprint[:32]}…` |",
            f"| **Methods applied** | {len(report.method_results)} of "
            f"{len(report.method_results) + len(report.skipped_methods)} |",
        ]
    )


def _conclusion(report: ValuationReport) -> str:
    rng = report.concluded_range
    return "\n".join(
        [
            "## 1. Conclusion",
            "",
            f"**Estimated fair value: {money(report.concluded_value_usd, precise=True)}**",
            "",
            f"Range: {money(rng.low_usd, precise=True)} – {money(rng.high_usd, precise=True)} "
            f"(spanning {percent(rng.width_pct)} of the point estimate)",
            "",
            report.narrative,
        ]
    )


def _methods_summary(report: ValuationReport) -> str:
    rows = [
        "## 2. Methods applied",
        "",
        "| Method | Value | Range | Weight | Basis for weight |",
        "|---|---:|---|---:|---|",
    ]
    total_weight = sum(get_method(r.method_id).default_weight for r in report.method_results)
    for result in report.method_results:
        method = get_method(result.method_id)
        weight = method.default_weight / total_weight if total_weight else 0.0
        rows.append(
            f"| {result.method_name} "
            f"| {money(result.equity_value_usd, precise=True)} "
            f"| {money(result.value_range.low_usd)} – {money(result.value_range.high_usd)} "
            f"| {percent(weight, places=0)} "
            f"| {escape_pipes(method.weight_rationale)} |"
        )
    rows += [
        "",
        "Weights express relative confidence in each methodology and are renormalised "
        "over the methods that actually ran.",
    ]
    return "\n".join(rows)


def _concordance(report: ValuationReport) -> str:
    concordance = report.concordance
    mark = _AGREEMENT_MARK.get(concordance.agreement, concordance.agreement)
    lines = [
        "## 3. Cross-method agreement",
        "",
        f"**{mark}** — coefficient of variation "
        f"{percent(concordance.coefficient_of_variation)}, spread "
        f"{money(concordance.spread_usd, precise=True)} across "
        f"{len(concordance.values_by_method)} method(s).",
        "",
        concordance.commentary,
    ]
    return "\n".join(lines)


def _review_queue(report: ValuationReport) -> str:
    """Assumptions the engine defaulted, grouped by method, awaiting sign-off."""
    rows = [
        "## 4. Assumption review queue",
        "",
        "Assumptions applied from engine defaults rather than auditor input. Each is a "
        "judgement call the tool made on the reviewer's behalf and should be confirmed "
        "or overridden.",
        "",
        "| Method | Assumption | Applied value | Rationale |",
        "|---|---|---:|---|",
    ]
    found = False
    for result in report.method_results:
        for assumption in result.trail.unreviewed_defaults():
            found = True
            rows.append(
                f"| {result.method_id} | `{assumption.key}` | {assumption.display_value()} "
                f"| {escape_pipes(assumption.rationale)} |"
            )
    if not found:
        return "\n".join(
            [
                "## 4. Assumption review queue",
                "",
                "Every assumption in this valuation was supplied explicitly by the auditor. "
                "No engine defaults were applied.",
            ]
        )
    return "\n".join(rows)


def _exceptions(report: ValuationReport) -> str:
    warnings = report.all_warnings
    if not warnings:
        return "\n".join(["## 5. Exceptions", "", "None raised."])
    rows = ["## 5. Exceptions", "", f"{len(warnings)} item(s) flagged during the run.", ""]
    rows += [f"{i}. {warning}" for i, warning in enumerate(warnings, start=1)]
    return "\n".join(rows)


def _method_details(report: ValuationReport) -> str:
    blocks = ["## 6. Method detail"]
    for index, result in enumerate(report.method_results, start=1):
        blocks.append(_one_method(result, f"6.{index}"))
    return "\n\n".join(blocks)


def _one_method(result: MethodResult, number: str) -> str:
    method = get_method(result.method_id)
    lines = [
        f"### {number} {result.method_name}",
        "",
        f"*{method.summary}*",
        "",
        result.narrative,
        "",
        f"**Conclusion: {money(result.equity_value_usd, precise=True)}** "
        f"(range {money(result.value_range.low_usd, precise=True)} – "
        f"{money(result.value_range.high_usd, precise=True)})",
        "",
        _assumptions_table(result),
        "",
        _steps_table(result),
    ]
    sensitivity = _sensitivity_table(result)
    if sensitivity:
        lines += ["", sensitivity]
    return "\n".join(lines)


def _assumptions_table(result: MethodResult) -> str:
    if not result.trail.assumptions:
        return "_No assumptions recorded._"
    rows = [
        "**Inputs and assumptions**",
        "",
        "| Key | Value | Origin | Rationale |",
        "|---|---:|---|---|",
    ]
    for assumption in result.trail.assumptions:
        rows.append(
            f"| `{assumption.key}` | {assumption.display_value()} | {assumption.origin} "
            f"| {escape_pipes(assumption.rationale)} |"
        )
    return "\n".join(rows)


def _steps_table(result: MethodResult) -> str:
    rows = [
        "**Calculation trail**",
        "",
        "| # | Step | Calculation | Result |",
        "|---:|---|---|---:|",
    ]
    for step in result.trail.steps:
        output = (
            compact(step.output)
            if isinstance(step.output, (dict, list))
            else by_unit(step.output, step.unit)
        )
        rows.append(
            f"| {step.seq} | {escape_pipes(step.description)} "
            f"| `{escape_pipes(compact(step.formula, limit=120))}` "
            f"| {escape_pipes(output)} |"
        )
    return "\n".join(rows)


def _sensitivity_table(result: MethodResult) -> str:
    if result.sensitivity is None:
        return ""
    rows = [
        "**Sensitivity** — each assumption moved on its own, ordered by influence.",
        "",
        "| Assumption | Low | High | Value at low | Value at high | Swing |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in result.sensitivity.ranked_rows:
        rows.append(
            f"| {row.label} "
            f"| {by_unit(row.low_input, row.unit)} "
            f"| {by_unit(row.high_input, row.unit)} "
            f"| {money(row.low_value_usd)} "
            f"| {money(row.high_value_usd)} "
            f"| {money(row.swing_usd)} ({percent(row.swing_pct, places=0)}) |"
        )
    return "\n".join(rows)


def _not_applied(report: ValuationReport) -> str:
    if not report.skipped_methods:
        return ""
    rows = [
        "## 7. Methods not applied",
        "",
        "Recorded rather than omitted: which methods could *not* be run is itself "
        "audit evidence.",
        "",
        "| Method | Reason | Error type |",
        "|---|---|---|",
    ]
    for skip in report.skipped_methods:
        rows.append(
            f"| {skip.method_name} | {escape_pipes(skip.reason)} | `{skip.error_type}` |"
        )
    return "\n".join(rows)


def _sources(report: ValuationReport) -> str:
    sources = report.all_sources
    if not sources:
        return ""
    rows = ["## 8. Data sources", "", "| Provider | Dataset | As of | Note |", "|---|---|---|---|"]
    for source in sources:
        rows.append(
            f"| {source.provider} | `{source.dataset}` | {source.as_of.isoformat()} "
            f"| {escape_pipes(source.note or '')} |"
        )
    rows += [
        "",
        "> Market data is served from checked-in fixtures standing in for a vendor feed. "
        "Company names and figures in that universe are synthetic.",
    ]
    return "\n".join(rows)


def _reproduction(report: ValuationReport) -> str:
    return "\n".join(
        [
            "## 9. Reproducing this valuation",
            "",
            "This memo is a pure function of its inputs. Re-running the same company "
            "record at the same valuation date with the same overrides reproduces it "
            "byte for byte, including the run ID and the fingerprint below. A "
            "fingerprint that no longer matches means an input or the code changed.",
            "",
            "```",
            f"run id      {report.run_id}",
            f"as of       {report.as_of.isoformat()}",
            f"fingerprint {report.fingerprint}",
            "```",
        ]
    )
