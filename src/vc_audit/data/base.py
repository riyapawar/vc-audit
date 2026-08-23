"""The boundary between this system and the outside world.

Valuation methods never touch a file, an HTTP client, or a vendor SDK. They talk
to :class:`MarketDataProvider`, which is a ``Protocol`` -- so the bundled
fixture-backed provider, the live SEC/quote-backed provider, and any future
licensed vendor feed are interchangeable at the constructor. That boundary is
also what makes the methods testable without a network.

Three rules the interface enforces on any implementation:

1. **Every returned record carries its own :class:`SourceRef`.** Provenance is
   produced where the data enters the system, not reconstructed downstream.
2. **Failures are typed.** An implementation raises
   :class:`~vc_audit.domain.errors.DataUnavailableError` rather than returning an
   empty list, so the engine can distinguish "the source is down" from "no peers
   matched the screen" -- which are different audit findings.
3. **Rejections are returned, not swallowed.** A screen reports the candidates
   it dropped and why, because "which companies did you consider and reject" is
   the first challenge a comps conclusion attracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from vc_audit.domain.audit import SourceRef
from vc_audit.domain.models import ExcludedPeer, IndexObservation, PeerCompany


@dataclass(frozen=True)
class PeerScreenResult:
    """What a comparability screen produced, including what it threw away."""

    peers: list[PeerCompany]
    source: SourceRef
    universe_size: int = 0
    excluded: list[ExcludedPeer] = field(default_factory=list)
    #: Human-readable description of the screen applied, for the audit trail.
    screen_description: str = ""

    @property
    def tickers(self) -> list[str]:
        return [peer.ticker for peer in self.peers]


@runtime_checkable
class MarketDataProvider(Protocol):
    """Read-only access to public market data."""

    name: str

    def get_peers(
        self,
        *,
        sector: str,
        as_of: date,
        subject_revenue_usd: float | None = None,
        size_band: float = 500.0,
        extra_tickers: tuple[str, ...] = (),
    ) -> PeerScreenResult:
        """Return public comparables for ``sector``, with provenance and rejects.

        Args:
            sector: Comparability screen key.
            as_of: Valuation date. Implementations must not return data dated
                after it -- a peer's post-valuation-date financials are
                information that did not exist when the mark was struck.
            subject_revenue_usd: Subject's LTM revenue, used to apply a size
                band. ``None`` disables the size screen.
            size_band: Peers are kept when their revenue falls within
                ``[subject / size_band, subject * size_band]``. Very wide by
                default because private companies are usually orders of
                magnitude smaller than their listed comparables; narrowing it is
                an auditor's judgement call.
            extra_tickers: Additional candidates to consider alongside the
                standing universe, e.g. names proposed by the research layer.

        Raises:
            DataUnavailableError: The peer data could not be retrieved at all.
        """
        ...

    def get_index_level(self, *, index_id: str, on: date) -> tuple[IndexObservation, SourceRef]:
        """Return the index level on ``on``, or the nearest prior observation.

        Substitution is flagged on the returned observation
        (``is_exact_date=False``) rather than performed silently, because "we
        used the 29 Feb close for a 2 Mar valuation date" is exactly the kind of
        detail a reviewer is entitled to see.

        Raises:
            DataUnavailableError: The index is unknown, or ``on`` precedes the
                first available observation so no prior level exists.
        """
        ...

    def known_sectors(self) -> list[str]:
        """Sectors this provider can screen. Powers CLI and UI discovery."""
        ...

    def describe(self) -> str:
        """One line naming the data sources in use, for the audit trail."""
        ...
