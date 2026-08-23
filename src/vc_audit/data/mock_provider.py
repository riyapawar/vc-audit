"""Fixture-backed market data, standing in for a real vendor feed.

The brief blesses mocking, so the interesting question is not "how do we fake
the data" but "does the seam hold up when the real feed is plugged in". This
implementation therefore behaves like a network client in the ways that matter:
it fails with typed errors, it can be told to go down (``simulate_outage_for``)
so the graceful-degradation path is exercised in tests rather than assumed, and
it stamps provenance on everything it returns.

Fixture files live in ``data/fixtures/`` and carry an ``as_of`` field. That date
flows into every :class:`SourceRef`, so a stale fixture shows up as a stale
citation in the memo instead of quietly ageing.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from vc_audit.domain.audit import SourceRef
from vc_audit.domain.errors import DataUnavailableError
from vc_audit.domain.models import IndexObservation, PeerCompany

FIXTURES_DIR = Path(__file__).parent / "fixtures"

PEERS_DATASET = "public_comps"
INDEX_DATASET = "index_history"


class MockMarketDataProvider:
    """A :class:`~vc_audit.data.base.MarketDataProvider` backed by JSON fixtures."""

    name = "yahoo_finance_mock"

    def __init__(
        self,
        fixtures_dir: Path | None = None,
        *,
        simulate_outage_for: set[str] | None = None,
    ) -> None:
        """Args:
        fixtures_dir: Override the bundled fixture directory.
        simulate_outage_for: Dataset names that should raise
            :class:`DataUnavailableError` on access. Used to prove the
            engine degrades gracefully instead of crashing.
        """
        self._dir = fixtures_dir or FIXTURES_DIR
        self._outages = simulate_outage_for or set()
        self._cache: dict[str, Any] = {}

    # ---- fixture plumbing ------------------------------------------------

    def _load(self, dataset: str) -> dict[str, Any]:
        if dataset in self._outages:
            raise DataUnavailableError(
                self.name, dataset, "simulated upstream outage (HTTP 503)"
            )
        if dataset in self._cache:
            return self._cache[dataset]

        path = self._dir / f"{dataset}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DataUnavailableError(self.name, dataset, f"fixture not found at {path}") from exc
        except json.JSONDecodeError as exc:
            raise DataUnavailableError(self.name, dataset, f"malformed fixture: {exc}") from exc

        self._cache[dataset] = raw
        return raw

    def _source(self, dataset: str, payload: dict[str, Any], note: str | None = None) -> SourceRef:
        return SourceRef(
            provider=self.name,
            dataset=dataset,
            as_of=date.fromisoformat(payload["as_of"]),
            note=note,
        )

    # ---- MarketDataProvider ----------------------------------------------

    def get_peers(
        self,
        *,
        sector: str,
        subject_revenue_usd: float | None = None,
        size_band: float = 500.0,
    ) -> tuple[list[PeerCompany], SourceRef]:
        payload = self._load(PEERS_DATASET)
        universe = [PeerCompany(**row) for row in payload["companies"]]

        sector_key = sector.strip().lower()
        peers = [p for p in universe if p.sector.lower() == sector_key]

        note = f"screened on sector='{sector_key}'"
        if subject_revenue_usd is not None and size_band > 0:
            floor = subject_revenue_usd / size_band
            ceiling = subject_revenue_usd * size_band
            peers = [p for p in peers if floor <= p.ltm_revenue_usd <= ceiling]
            note += f", revenue within [${floor:,.0f}, ${ceiling:,.0f}]"

        # Deterministic ordering. Fixture order is an accident of authoring, and
        # an accident must not be able to change a published valuation.
        peers.sort(key=lambda p: p.ticker)
        return peers, self._source(PEERS_DATASET, payload, note)

    def get_index_level(self, *, index_id: str, on: date) -> tuple[IndexObservation, SourceRef]:
        payload = self._load(INDEX_DATASET)
        series = payload["indices"].get(index_id)
        if series is None:
            known = ", ".join(sorted(payload["indices"]))
            raise DataUnavailableError(
                self.name, INDEX_DATASET, f"unknown index '{index_id}'; available: {known}"
            )

        observations = sorted(
            ((date.fromisoformat(o["date"]), float(o["level"])) for o in series["observations"]),
            key=lambda pair: pair[0],
        )

        # Nearest prior observation: never look forward, which would leak
        # information that did not exist on the valuation date.
        candidates = [pair for pair in observations if pair[0] <= on]
        if not candidates:
            earliest = observations[0][0].isoformat()
            raise DataUnavailableError(
                self.name,
                INDEX_DATASET,
                f"no observation for '{index_id}' on or before {on.isoformat()} "
                f"(series begins {earliest})",
            )

        observed_on, level = candidates[-1]
        is_exact = observed_on == on
        note = None if is_exact else f"{on.isoformat()} not a trading day; used prior close"

        observation = IndexObservation(
            index_id=index_id,
            observed_on=observed_on,
            level=level,
            is_exact_date=is_exact,
            requested_date=on,
        )
        return observation, self._source(INDEX_DATASET, payload, note)

    # ---- convenience -----------------------------------------------------

    def known_sectors(self) -> list[str]:
        """Sectors present in the peer universe. Powers CLI discovery."""
        payload = self._load(PEERS_DATASET)
        return sorted({row["sector"] for row in payload["companies"]})

    def known_indices(self) -> dict[str, str]:
        """Map of index id to display name."""
        payload = self._load(INDEX_DATASET)
        return {key: value["name"] for key, value in payload["indices"].items()}
