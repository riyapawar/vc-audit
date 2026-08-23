"""Presentation-layer formatting.

Rounding lives here and only here. The calculation layers carry full precision;
the moment a number is shown to a person it is rounded once, in one place, so
the memo and the console can never disagree about what the same figure is.
"""

from __future__ import annotations

from typing import Any


def money(value: float | None, *, precise: bool = False) -> str:
    """Format USD. Abbreviated by default, since valuations are read at a glance."""
    if value is None:
        return "n/a"
    if precise:
        return f"${value:,.0f}"
    magnitude = abs(value)
    if magnitude >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if magnitude >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    if magnitude >= 1_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,.0f}"


def percent(value: float | None, *, places: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{places}%}"


def multiple(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def by_unit(value: Any, unit: str | None) -> str:
    """Format a value according to a declared unit, falling back to ``repr``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if unit == "usd":
            return money(value, precise=True)
        if unit == "percent":
            return percent(value, places=2)
        if unit == "multiple":
            return multiple(value)
        if unit == "ratio":
            return f"{value:,.2f}"
        return f"{value:,.4g}"
    return str(value)


def compact(value: Any, *, limit: int = 90) -> str:
    """Render a step input/output for a table cell, truncating structures.

    Nested detail stays in the JSON evidence pack; the Markdown memo needs the
    row to remain readable.
    """
    if isinstance(value, dict):
        parts = [f"{k}={compact(v, limit=24)}" for k, v in value.items()]
        text = ", ".join(parts)
    elif isinstance(value, (list, tuple)):
        text = ", ".join(compact(v, limit=24) for v in value)
    elif isinstance(value, float):
        # Never scientific notation: "1.885e+06" is unreadable in a workpaper
        # where every large float is a dollar amount.
        text = f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.4g}"
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def escape_pipes(text: str) -> str:
    """Escape pipes so a value never breaks out of a Markdown table cell."""
    return text.replace("|", "\\|")
