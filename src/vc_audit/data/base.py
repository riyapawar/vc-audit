"""The boundary between this system and the outside world.

Valuation methods never touch a file, an HTTP client, or a vendor SDK. They talk
to :class:`MarketDataProvider`, which is a ``Protocol`` -- so swapping the bundled
fixture-backed implementation for a real Yahoo Finance or CapIQ client is a
constructor argument, not a refactor. That boundary is also what makes the
methods testable without a network.

Two rules the interface enforces on any implementation:

1. **Every returned record carries its own :class:`SourceRef`.** Provenance is
   produced where the data enters the system, not reconstructed downstream.
2. **Failures are typed.** An implementation raises
   :class:`~vc_audit.domain.errors.DataUnavailableError` rather than returning an
   empty list, so the engine can distinguish "the vendor is down" from "no peers
   matched the screen" -- which are different audit findings.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from vc_audit.domain.audit import SourceRef
from vc_audit.domain.models import IndexObservation, PeerCompany


@runtime_checkable
class MarketDataProvider(Protocol):
    """Read-only access to public market data."""

    name: str

    def get_peers(
        self,
        *,
        sector: str,
        subject_revenue_usd: float | None = None,
        size_band: float = 500.0,
    ) -> tuple[list[PeerCompany], SourceRef]:
        """Return public comparables for ``sector``, plus the citation for that set.

        Args:
            sector: Comparability screen key.
            subject_revenue_usd: Subject's LTM revenue, used to apply a size
                band. ``None`` disables the size screen.
            size_band: Peers are kept when their revenue falls within
                ``[subject / size_band, subject * size_band]``. Very wide by
                default because private companies are usually orders of
                magnitude smaller than their listed comparables; narrowing it is
                an auditor's judgement call.

        Raises:
            DataUnavailableError: The peer dataset could not be read.
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
