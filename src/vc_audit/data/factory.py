"""Choosing a data provider.

Three modes, one entry point. ``auto`` is the default because it gives the best
evidence available without requiring the reviewer to know or care whether the
machine has a network: live filings when reachable, fixtures when not, and a
recorded disclosure either way.
"""

from __future__ import annotations

from typing import Literal

from vc_audit.data.base import MarketDataProvider
from vc_audit.data.live_provider import LiveMarketDataProvider
from vc_audit.data.mock_provider import MockMarketDataProvider
from vc_audit.data.resilient import ResilientMarketDataProvider

DataMode = Literal["auto", "live", "fixtures"]

MODE_HELP = {
    "auto": "Live SEC/quote data, falling back to fixtures if unreachable (default).",
    "live": "Live data only; fail rather than substitute fixtures.",
    "fixtures": "Checked-in fixtures only; fully offline and deterministic.",
}


def build_provider(
    mode: DataMode = "auto",
    *,
    user_agent: str | None = None,
    simulate_outage_for: set[str] | None = None,
) -> MarketDataProvider:
    """Construct the provider for ``mode``.

    Args:
        mode: ``auto``, ``live`` or ``fixtures``.
        user_agent: Overrides the SEC EDGAR User-Agent.
        simulate_outage_for: Fixture datasets that should fail, used to
            demonstrate graceful degradation. Applies to the fixture provider.

    Raises:
        ValueError: Unknown mode.
    """
    if mode == "fixtures":
        return MockMarketDataProvider(simulate_outage_for=simulate_outage_for)
    if mode == "live":
        return LiveMarketDataProvider(user_agent=user_agent)
    if mode == "auto":
        return ResilientMarketDataProvider(
            primary=LiveMarketDataProvider(user_agent=user_agent),
            fallback=MockMarketDataProvider(simulate_outage_for=simulate_outage_for),
        )
    raise ValueError(f"unknown data mode '{mode}'; expected one of {', '.join(MODE_HELP)}")
