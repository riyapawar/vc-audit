"""A provider that prefers live data and survives losing it.

Live sources are the better evidence: a peer multiple built from a filed 10-Q
beats one read off a fixture. But live sources are also the thing most likely to
be unavailable during a review -- a network the auditor's machine cannot reach,
a rate limit, an endpoint that changed.

So this wraps two providers. The live one is tried first; on a typed data
failure it falls back to fixtures and **records the substitution**. A silent
fallback would be the worst outcome of the three, because the memo would then
present fixture figures with the authority of filed ones.

The recorded degradations are drained by the engine into the audit trail as
warnings, so a reviewer opening the memo sees "these peers came from fixtures
because EDGAR was unreachable" rather than having to notice it themselves.
"""

from __future__ import annotations

from datetime import date

from vc_audit.data.base import MarketDataProvider, PeerScreenResult
from vc_audit.domain.audit import SourceRef
from vc_audit.domain.errors import DataUnavailableError
from vc_audit.domain.models import IndexObservation


class ResilientMarketDataProvider:
    """Live data with an automatic, disclosed fallback to fixtures."""

    name = "resilient"

    def __init__(self, primary: MarketDataProvider, fallback: MarketDataProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        #: Substitutions made during this run. Drained by the engine; see
        #: ``engine._record_data_provenance``.
        self.degradations: list[str] = []
        self.used_primary_for: set[str] = set()
        self.used_fallback_for: set[str] = set()

    def describe(self) -> str:
        return (
            f"Primary: {self._primary.describe()} "
            f"Fallback on failure: {self._fallback.describe()}"
        )

    def known_sectors(self) -> list[str]:
        """Union of both providers, so discovery never depends on the network."""
        return sorted({*self._primary.known_sectors(), *self._fallback.known_sectors()})

    def _degrade(self, what: str, exc: DataUnavailableError) -> None:
        self.used_fallback_for.add(what)
        self.degradations.append(
            f"{what}: live source unavailable ({exc.provider} -- {exc.reason}); "
            f"fell back to checked-in fixtures, whose figures are synthetic and must "
            f"not be relied on as filed data."
        )

    def get_peers(
        self,
        *,
        sector: str,
        as_of: date,
        subject_revenue_usd: float | None = None,
        size_band: float = 500.0,
        extra_tickers: tuple[str, ...] = (),
    ) -> PeerScreenResult:
        try:
            result = self._primary.get_peers(
                sector=sector,
                as_of=as_of,
                subject_revenue_usd=subject_revenue_usd,
                size_band=size_band,
                extra_tickers=extra_tickers,
            )
        except DataUnavailableError as exc:
            self._degrade("comparable company data", exc)
            return self._fallback.get_peers(
                sector=sector,
                as_of=as_of,
                subject_revenue_usd=subject_revenue_usd,
                size_band=size_band,
                extra_tickers=extra_tickers,
            )
        self.used_primary_for.add("comparable company data")
        return result

    def get_index_level(self, *, index_id: str, on: date) -> tuple[IndexObservation, SourceRef]:
        try:
            observation = self._primary.get_index_level(index_id=index_id, on=on)
        except DataUnavailableError as exc:
            self._degrade(f"index history for {index_id}", exc)
            return self._fallback.get_index_level(index_id=index_id, on=on)
        self.used_primary_for.add(f"index history for {index_id}")
        return observation
