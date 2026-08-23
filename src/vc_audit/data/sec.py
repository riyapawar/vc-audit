"""SEC EDGAR: fundamentals straight from the primary source.

Most valuation tooling takes enterprise value as a single pre-computed number
from a data vendor. That is convenient and unauditable: when a reviewer asks
"where did the $39.5B come from", the honest answer is "the vendor said so".

This module refuses that shortcut. Every component of a peer's enterprise value
is pulled as a tagged XBRL fact from a filed 10-K or 10-Q -- shares outstanding,
cash, debt -- and the bridge is computed in our own code, where the audit trail
can record it. Revenue likewise comes from the filings rather than being implied
by dividing a vendor's EV by its EV/Revenue ratio.

The whole module needs **no API key**. EDGAR asks only for a descriptive
User-Agent and a courteous request rate, both of which are honoured below.

Two facts about XBRL shape the design:

* **Companies use different tags for the same concept.** Revenue may be tagged
  ``RevenueFromContractWithCustomerExcludingAssessedTax``, ``Revenues``, or
  ``SalesRevenueNet`` depending on filer and era. Candidate tags are therefore
  tried in preference order, and *which* tag matched is recorded.
* **Facts overlap.** A 10-K carries a 12-month period while 10-Qs carry 3- and
  9-month periods over the same span. Summing naively double-counts, so LTM
  revenue is assembled from non-overlapping quarters and falls back to the
  latest annual figure, recording which basis was used.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, timedelta
from typing import Any

from vc_audit.domain.errors import DataUnavailableError
from vc_audit.domain.models import FilingReference

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

PERIODIC_FORMS = ("10-K", "10-Q")

#: EDGAR asks for no more than 10 requests/second. We stay well inside that:
#: a portfolio review is not latency-sensitive, and being a good citizen of a
#: free public service is worth more than a few hundred milliseconds.
_MIN_REQUEST_INTERVAL_S = 0.15

#: Tag preference order. Earlier tags are more specific and more reliable; the
#: later ones catch older filings and unusual filers.
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueServicesNet",
)
CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
SHORT_TERM_INVESTMENT_TAGS = (
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
)
LONG_TERM_DEBT_TAGS = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
)
SHORT_TERM_DEBT_TAGS = (
    "LongTermDebtCurrent",
    "ShortTermBorrowings",
    "DebtCurrent",
)
SHARES_TAGS = ("EntityCommonStockSharesOutstanding",)

#: A balance-sheet fact older than this is treated as absent rather than as
#: current. Fifteen months covers an annual cycle plus filing lag; beyond it a
#: figure describes a different company.
MAX_BALANCE_SHEET_AGE_DAYS = 460


class _RateLimiter:
    """Serialises outbound requests so parallel peer fetches stay polite."""

    def __init__(self, min_interval_s: float) -> None:
        self._min_interval = min_interval_s
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


@dataclass(frozen=True)
class Fact:
    """One XBRL fact, with the filing it came from."""

    tag: str
    value: float
    period_start: date | None
    period_end: date
    form: str
    accession: str

    @property
    def period_months(self) -> int | None:
        if self.period_start is None:
            return None
        return round((self.period_end - self.period_start).days / 30.44)


@dataclass(frozen=True)
class Fundamentals:
    """Everything needed to build one peer's enterprise value, with provenance."""

    ticker: str
    cik: str
    company_name: str
    shares_outstanding: float | None
    ltm_revenue_usd: float | None
    revenue_basis: str | None
    revenue_tag: str | None
    cash_usd: float
    debt_usd: float
    period_end: date | None
    filing: FilingReference | None
    #: Which XBRL tag and period produced each component, e.g.
    #: ``{"debt": "us-gaap:LongTermDebt @2026-06-30 + us-gaap:ShortTermBorrowings @2026-06-30"}``.
    #: Debt and cash decomposition varies by filer, so the tags actually used are
    #: reported rather than assumed -- a reviewer can then judge the mapping.
    components: dict[str, str] = dc_field(default_factory=dict)


class SECClient:
    """Read-only EDGAR access. Caches aggressively; EDGAR data changes quarterly."""

    name = "sec_edgar"

    def __init__(self, user_agent: str, *, timeout_s: float = 20.0) -> None:
        """Args:
        user_agent: EDGAR requires a descriptive agent identifying the
            caller, conventionally ``"Name email@example.com"``.
        timeout_s: Per-request timeout.
        """
        # No gzip: urllib does not transparently decompress, and a compressed
        # body would surface as a decode error rather than as a data failure.
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
        self._timeout = timeout_s
        self._limiter = _RateLimiter(_MIN_REQUEST_INTERVAL_S)
        self._cik_map: dict[str, tuple[str, str]] | None = None
        self._facts_cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---- transport -------------------------------------------------------

    def _get_json(self, url: str) -> dict[str, Any]:
        self._limiter.wait()
        request = urllib.request.Request(url, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise DataUnavailableError(
                self.name, url, f"HTTP {exc.code} from EDGAR"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DataUnavailableError(self.name, url, f"network error: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DataUnavailableError(self.name, url, f"malformed response: {exc}") from exc

    # ---- ticker resolution -----------------------------------------------

    def _load_cik_map(self) -> dict[str, tuple[str, str]]:
        with self._lock:
            if self._cik_map is not None:
                return self._cik_map
        payload = self._get_json(SEC_TICKERS_URL)
        mapping = {
            str(row["ticker"]).upper(): (str(row["cik_str"]).zfill(10), str(row["title"]))
            for row in payload.values()
        }
        with self._lock:
            self._cik_map = mapping
        return mapping

    def resolve(self, ticker: str) -> tuple[str, str]:
        """Map a ticker to ``(cik, registrant name)``.

        The registrant name comes from EDGAR, never from the caller. That is
        what catches a mistyped or hallucinated ticker: ``BRX`` resolves to
        Brixmor Property Group, a REIT, and the mismatch becomes visible instead
        of silently entering the peer set as whatever the caller called it.
        """
        mapping = self._load_cik_map()
        try:
            return mapping[ticker.upper()]
        except KeyError:
            raise DataUnavailableError(
                self.name, "company_tickers", f"ticker '{ticker}' is not a registered filer"
            ) from None

    # ---- facts -----------------------------------------------------------

    def _company_facts(self, cik: str) -> dict[str, Any]:
        with self._lock:
            if cik in self._facts_cache:
                return self._facts_cache[cik]
        payload = self._get_json(SEC_FACTS_URL.format(cik=cik))
        with self._lock:
            self._facts_cache[cik] = payload
        return payload

    @staticmethod
    def _facts_for(payload: dict[str, Any], taxonomy: str, tag: str, unit: str) -> list[Fact]:
        raw = ((payload.get("facts") or {}).get(taxonomy) or {}).get(tag)
        if not raw:
            return []
        facts: list[Fact] = []
        for entry in (raw.get("units") or {}).get(unit, []):
            try:
                facts.append(
                    Fact(
                        tag=tag,
                        value=float(entry["val"]),
                        period_start=(
                            date.fromisoformat(entry["start"]) if entry.get("start") else None
                        ),
                        period_end=date.fromisoformat(entry["end"]),
                        form=str(entry.get("form", "")),
                        accession=str(entry.get("accn", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue  # A single malformed fact must not lose the whole series.
        facts.sort(key=lambda f: f.period_end)
        return facts

    def _latest_instant(
        self, payload: dict[str, Any], tags: tuple[str, ...], *, as_of: date
    ) -> Fact | None:
        """Freshest point-in-time value at or before ``as_of``, across all candidate tags.

        Deliberately *not* "first tag that has any data". Filers migrate between
        tags: ServiceNow stopped reporting ``LongTermDebtNoncurrent`` in 2021 and
        switched to ``LongTermDebt``, and Salesforce's last
        ``MarketableSecuritiesCurrent`` fact is from 2014. Taking the first tag
        with a hit would silently value a 2026 company on a 2021 balance sheet.

        So every candidate tag is gathered, anything staler than
        :data:`MAX_BALANCE_SHEET_AGE_DAYS` is discarded outright, and the most
        recent remaining fact wins -- ties broken by the tag preference order.
        """
        oldest_acceptable = as_of - timedelta(days=MAX_BALANCE_SHEET_AGE_DAYS)
        candidates: list[tuple[date, int, Fact]] = []
        for rank, tag in enumerate(tags):
            for fact in self._facts_for(payload, "us-gaap", tag, "USD"):
                if fact.period_start is not None:
                    continue  # duration fact, not a balance
                if not (oldest_acceptable <= fact.period_end <= as_of):
                    continue
                candidates.append((fact.period_end, -rank, fact))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[-1][2]

    def _ltm_revenue(
        self, payload: dict[str, Any], *, as_of: date
    ) -> tuple[float | None, str | None, str | None]:
        """Trailing-twelve-month revenue, with the basis and tag that produced it.

        Prefers four non-overlapping quarters, because that is genuinely
        trailing. Falls back to the most recent annual period, which is correct
        but staler -- and says which one it used, because a reviewer comparing
        two peers needs to know they are not on the same basis.
        """
        for tag in REVENUE_TAGS:
            facts = [
                f
                for f in self._facts_for(payload, "us-gaap", tag, "USD")
                if f.period_start is not None
                and f.period_end <= as_of
                and f.form in PERIODIC_FORMS
            ]
            if not facts:
                continue

            quarters: list[Fact] = []
            cursor: date | None = None
            for fact in reversed(facts):
                if fact.period_months not in (3, 4):
                    continue
                if cursor is not None and fact.period_end > cursor:
                    continue  # overlaps a quarter already taken
                quarters.append(fact)
                cursor = fact.period_start
                if len(quarters) == 4:
                    break
            if len(quarters) == 4:
                return sum(f.value for f in quarters), "trailing four quarters", tag

            annual = [f for f in facts if f.period_months in (12, 11, 13)]
            if annual:
                return (
                    annual[-1].value,
                    f"latest annual period ending {annual[-1].period_end.isoformat()}",
                    tag,
                )
        return None, None, None

    def _shares(self, payload: dict[str, Any], *, as_of: date) -> float | None:
        for tag in SHARES_TAGS:
            facts = [
                f
                for f in self._facts_for(payload, "dei", tag, "shares")
                if f.period_end <= as_of
            ]
            if facts:
                return facts[-1].value
        return None

    def _component(
        self, payload: dict[str, Any], tags: tuple[str, ...], *, as_of: date
    ) -> tuple[float, str | None]:
        """Resolve one balance-sheet component and describe where it came from."""
        fact = self._latest_instant(payload, tags, as_of=as_of)
        if fact is None:
            return 0.0, None
        return fact.value, f"us-gaap:{fact.tag} @{fact.period_end.isoformat()}"

    # ---- public API ------------------------------------------------------

    def fundamentals(self, ticker: str, *, as_of: date) -> Fundamentals:
        """Assemble one company's balance-sheet and revenue facts as of a date.

        Facts dated after ``as_of`` are excluded throughout, so a historical
        valuation cannot be contaminated by information that did not exist on
        the valuation date.
        """
        cik, name = self.resolve(ticker)
        payload = self._company_facts(cik)

        revenue, basis, revenue_tag = self._ltm_revenue(payload, as_of=as_of)

        cash_core, cash_tag = self._component(payload, CASH_TAGS, as_of=as_of)
        investments, investments_tag = self._component(
            payload, SHORT_TERM_INVESTMENT_TAGS, as_of=as_of
        )
        long_debt, long_debt_tag = self._component(payload, LONG_TERM_DEBT_TAGS, as_of=as_of)
        short_debt, short_debt_tag = self._component(payload, SHORT_TERM_DEBT_TAGS, as_of=as_of)

        cash = cash_core + investments
        debt = long_debt + short_debt
        components = {
            "cash": " + ".join(t for t in (cash_tag, investments_tag) if t) or "not reported",
            "debt": " + ".join(t for t in (long_debt_tag, short_debt_tag) if t) or "not reported",
            "revenue": f"us-gaap:{revenue_tag}" if revenue_tag else "not reported",
        }

        anchor = self._latest_instant(payload, CASH_TAGS, as_of=as_of)
        period_end = anchor.period_end if anchor is not None else None

        return Fundamentals(
            ticker=ticker.upper(),
            cik=cik,
            company_name=name,
            shares_outstanding=self._shares(payload, as_of=as_of),
            ltm_revenue_usd=revenue,
            revenue_basis=basis,
            revenue_tag=revenue_tag,
            cash_usd=cash,
            debt_usd=debt,
            period_end=period_end,
            filing=self.latest_filing(cik, as_of=as_of),
            components=components,
        )

    def latest_filing(self, cik: str, *, as_of: date) -> FilingReference | None:
        """Most recent 10-K or 10-Q filed on or before ``as_of``.

        Provenance is a nice-to-have: losing it must not lose the peer, so every
        failure here returns ``None`` rather than propagating.
        """
        try:
            payload = self._get_json(SEC_SUBMISSIONS_URL.format(cik=cik))
            recent = payload["filings"]["recent"]
            for form, filed, accession, document, period in zip(
                recent["form"],
                recent["filingDate"],
                recent["accessionNumber"],
                recent["primaryDocument"],
                recent.get("reportDate", [""] * len(recent["form"])),
                strict=False,
            ):
                if form not in PERIODIC_FORMS:
                    continue
                filed_at = date.fromisoformat(filed)
                if filed_at > as_of:
                    continue
                return FilingReference(
                    form=form,
                    filed_at=filed_at,
                    period_end=date.fromisoformat(period) if period else None,
                    accession_number=accession,
                    url=SEC_ARCHIVE_URL.format(
                        cik=int(cik),
                        accession=accession.replace("-", ""),
                        document=document,
                    ),
                )
        except (DataUnavailableError, KeyError, TypeError, ValueError):
            return None
        return None
