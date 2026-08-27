"""Live market data, assembled from primary sources.

This provider builds every peer's enterprise value from components it can cite
individually:

    market cap  =  shares outstanding (SEC XBRL, dei)  x  close (public quote)
    EV          =  market cap  +  debt (SEC XBRL)  -  cash (SEC XBRL)
    EV/Revenue  =  EV  /  LTM revenue (SEC XBRL, four trailing quarters)

That is deliberately more work than reading a vendor's pre-computed
``enterpriseValue`` field. The payoff is that no step of the multiple rests on
an unauditable number: a reviewer can open the linked 10-Q and find the cash
balance the calculation used.

Candidates that cannot be assembled are **excluded with a specific reason**
rather than dropped, so the funnel from universe to valued peer set is visible
in the memo. Every lookup is bounded by the valuation date, so a historical run
cannot see figures filed after the mark was struck.

Needs no API key. SEC EDGAR asks only for a descriptive User-Agent.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from vc_audit.data.base import PeerScreenResult
from vc_audit.data.http import TransientDataError
from vc_audit.data.quotes import QuoteClient
from vc_audit.data.sec import SECClient
from vc_audit.data.universe import candidates_for, known_sectors
from vc_audit.domain.audit import SourceRef
from vc_audit.domain.errors import DataUnavailableError
from vc_audit.domain.models import ExcludedPeer, IndexObservation, PeerCompany

#: EDGAR requires a descriptive agent. Overridable, with a working default so
#: the tool runs out of the box; a real deployment should set its own.
DEFAULT_USER_AGENT = "vc-audit-tool contact@example.com"
USER_AGENT_ENV = "SEC_USER_AGENT"

#: Modest: EDGAR is a free public service and the rate limiter serialises us
#: anyway, so more threads would only queue.
_MAX_WORKERS = 6

#: Logical index ids mapped to quotable symbols. The fixture provider uses the
#: same ids, so switching providers never changes which index a method asks for.
INDEX_SYMBOLS = {
    "^IXIC": "^IXIC",
    "WCLD": "WCLD",
    "IGV": "IGV",
}

#: A peer whose implied multiple falls outside this band is almost certainly a
#: data error rather than a market fact: no listed operating company trades at a
#: twentieth of revenue or four hundred times it for reasons a peer screen should
#: respect. Deliberately far wider than any plausible valuation, because this is
#: a corruption detector and not a comparability judgement -- narrowing it would
#: start silently discarding real companies. Genuine but extreme multiples are
#: handled downstream by the Tukey fence, and are recorded at a different funnel
#: stage so the two causes never get confused.
MIN_PLAUSIBLE_EV_REVENUE = 0.05
MAX_PLAUSIBLE_EV_REVENUE = 400.0

#: How far back to pull index history. Comfortably covers any plausible round
#: date without asking for decades of data on every call.
_INDEX_HISTORY_YEARS = 12


def _reconciliation_failure(facts) -> str | None:
    """Check the selected cash and debt tags against the filed balance sheet.

    XBRL tag mapping is the least certain step in this pipeline: filers use
    different concepts for the same line, and picking the wrong one moves every
    multiple derived from it without changing anything a reader would notice.
    Cash cannot exceed total assets and debt cannot exceed total liabilities, so
    a breach means the mapping selected something that is not what we think it
    is, and the peer is unusable rather than merely imprecise.

    Skipped rather than failed when the filer does not report the totals: an
    absent control is not evidence of a fault.
    """
    if facts.total_assets_usd and facts.cash_usd > facts.total_assets_usd:
        return (
            f"balance sheet does not reconcile: cash and equivalents of "
            f"${facts.cash_usd:,.0f} exceed total assets of "
            f"${facts.total_assets_usd:,.0f}, so the cash tag mapping "
            f"({facts.components.get('cash', 'unknown')}) selected the wrong concept"
        )
    if facts.total_liabilities_usd and facts.debt_usd > facts.total_liabilities_usd:
        return (
            f"balance sheet does not reconcile: debt of ${facts.debt_usd:,.0f} "
            f"exceeds total liabilities of ${facts.total_liabilities_usd:,.0f}, so "
            f"the debt tag mapping ({facts.components.get('debt', 'unknown')}) "
            f"selected the wrong concept"
        )
    return None


def _implausible_multiple(peer: PeerCompany) -> str | None:
    """Reject a multiple that can only be a data fault, not a market price.

    Distinct from outlier trimming, and recorded at a different funnel stage. A
    peer at 800x revenue is not an aggressive valuation, it is a broken revenue
    figure, and letting it into the set for the Tukey fence to catch would both
    mislabel the cause and, with few peers, sometimes fail to catch it at all.
    """
    multiple = peer.ev_to_revenue
    if MIN_PLAUSIBLE_EV_REVENUE <= multiple <= MAX_PLAUSIBLE_EV_REVENUE:
        return None
    return (
        f"implied EV/Revenue of {multiple:,.2f}x falls outside the plausible band "
        f"({MIN_PLAUSIBLE_EV_REVENUE:g}x to {MAX_PLAUSIBLE_EV_REVENUE:g}x), which "
        f"indicates a corrupt or mis-scaled figure rather than a market price"
    )


class LiveMarketDataProvider:
    """Peers and index levels built from SEC filings plus public quotes."""

    name = "sec_edgar+public_quotes"

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        sec: SECClient | None = None,
        quotes: QuoteClient | None = None,
    ) -> None:
        agent = user_agent or os.environ.get(USER_AGENT_ENV) or DEFAULT_USER_AGENT
        self._sec = sec or SECClient(agent)
        self._quotes = quotes or QuoteClient()

    def describe(self) -> str:
        return (
            "SEC EDGAR XBRL company facts (revenue, cash, debt, shares outstanding) "
            "with closing prices from a public quote endpoint; enterprise value "
            "computed in-process from those components."
        )

    def known_sectors(self) -> list[str]:
        return known_sectors()

    # ---- peers -----------------------------------------------------------

    def get_peers(
        self,
        *,
        sector: str,
        as_of: date,
        subject_revenue_usd: float | None = None,
        size_band: float = 500.0,
        extra_tickers: tuple[str, ...] = (),
    ) -> PeerScreenResult:
        tickers = list(dict.fromkeys([*candidates_for(sector), *extra_tickers]))
        if not tickers:
            raise DataUnavailableError(
                self.name,
                "sector_universe",
                f"no candidate universe for sector '{sector}'; "
                f"known: {', '.join(self.known_sectors())}",
            )

        peers: list[PeerCompany] = []
        excluded: list[ExcludedPeer] = []

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            outcomes = list(
                pool.map(lambda t: self._build_peer(t, sector=sector, as_of=as_of), tickers)
            )

        for outcome in outcomes:
            if isinstance(outcome, ExcludedPeer):
                excluded.append(outcome)
            else:
                peers.append(outcome)

        if not peers:
            raise DataUnavailableError(
                self.name,
                "xbrl_companyfacts",
                f"no usable fundamentals for any of the {len(tickers)} candidates in "
                f"sector '{sector}'",
            )

        screen = f"sector='{sector}' universe of {len(tickers)} candidates"
        if subject_revenue_usd is not None and size_band > 0:
            floor = subject_revenue_usd / size_band
            ceiling = subject_revenue_usd * size_band
            kept = []
            for peer in peers:
                if floor <= peer.ltm_revenue_usd <= ceiling:
                    kept.append(peer)
                else:
                    excluded.append(
                        ExcludedPeer(
                            ticker=peer.ticker,
                            company_name=peer.name,
                            stage="comparability",
                            reason=(
                                f"LTM revenue ${peer.ltm_revenue_usd:,.0f} falls outside the "
                                f"{size_band:g}x size band around the subject"
                            ),
                        )
                    )
            peers = kept
            screen += f", revenue within [${floor:,.0f}, ${ceiling:,.0f}]"

        # Deterministic ordering: fetch completion order is an accident of
        # network timing, and an accident must not move a published valuation.
        peers.sort(key=lambda p: p.ticker)
        excluded.sort(key=lambda e: e.ticker)

        return PeerScreenResult(
            peers=peers,
            source=SourceRef(
                provider=self.name,
                dataset="xbrl_companyfacts",
                as_of=as_of,
                note=screen,
            ),
            universe_size=len(tickers),
            excluded=excluded,
            screen_description=screen,
        )

    def _build_peer(
        self, ticker: str, *, sector: str, as_of: date
    ) -> PeerCompany | ExcludedPeer:
        """Assemble one peer, or explain precisely why it could not be assembled."""
        try:
            facts = self._sec.fundamentals(ticker, as_of=as_of)
        except TransientDataError as exc:
            # Distinct from "no such filer". A peer lost this way would have been
            # in the set on a different day, so the median is not reproducible and
            # the reason has to say so rather than reading like a finding about
            # the company.
            return ExcludedPeer(
                ticker=ticker,
                stage="unreachable",
                reason=f"SEC EDGAR {exc.reason}. Re-run before relying on this peer set",
            )
        except DataUnavailableError as exc:
            return ExcludedPeer(
                ticker=ticker, stage="data", reason=f"SEC filings unavailable: {exc.reason}"
            )

        if facts.ltm_revenue_usd is None or facts.ltm_revenue_usd <= 0:
            return ExcludedPeer(
                ticker=ticker,
                company_name=facts.company_name,
                stage="data",
                reason="no usable revenue figure in the XBRL facts on or before the valuation date",
            )
        if facts.shares_outstanding is None or facts.shares_outstanding <= 0:
            return ExcludedPeer(
                ticker=ticker,
                company_name=facts.company_name,
                stage="data",
                reason="shares outstanding not reported, so market capitalisation cannot be built",
            )

        try:
            quote = self._quotes.close_on(ticker, on=as_of)
        except TransientDataError as exc:
            return ExcludedPeer(
                ticker=ticker,
                company_name=facts.company_name,
                stage="unreachable",
                reason=f"price source {exc.reason}. Re-run before relying on this peer set",
            )
        except DataUnavailableError as exc:
            return ExcludedPeer(
                ticker=ticker,
                company_name=facts.company_name,
                stage="data",
                reason=f"no closing price on or before the valuation date: {exc.reason}",
            )

        market_cap = facts.shares_outstanding * quote.close
        peer = PeerCompany(
            ticker=facts.ticker,
            name=facts.company_name,
            sector=sector,
            market_cap_usd=market_cap,
            total_debt_usd=facts.debt_usd,
            cash_usd=facts.cash_usd,
            ltm_revenue_usd=facts.ltm_revenue_usd,
            latest_filing=facts.filing,
            fundamentals_basis=(
                f"revenue: {facts.revenue_basis} ({facts.components.get('revenue', 'n/a')}); "
                f"cash: {facts.components.get('cash', 'n/a')}; "
                f"debt: {facts.components.get('debt', 'n/a')}; "
                f"market cap: {facts.shares_outstanding:,.0f} shares at "
                f"${quote.close:,.2f} ({quote.observed_on.isoformat()})"
            ),
            inclusion_rationale=f"Listed {sector} comparable in the standing candidate universe.",
        )
        if peer.enterprise_value_usd <= 0:
            return ExcludedPeer(
                ticker=ticker,
                company_name=facts.company_name,
                stage="data",
                reason=(
                    f"net cash exceeds market capitalisation, giving a non-positive "
                    f"enterprise value of ${peer.enterprise_value_usd:,.0f}"
                ),
            )

        failure = _reconciliation_failure(facts) or _implausible_multiple(peer)
        if failure is not None:
            return ExcludedPeer(
                ticker=ticker,
                company_name=facts.company_name,
                stage="data",
                reason=failure,
            )
        return peer

    # ---- index -----------------------------------------------------------

    def get_index_level(self, *, index_id: str, on: date) -> tuple[IndexObservation, SourceRef]:
        """The index close on ``on``, or the nearest prior trading day.

        Reads the **daily** series rather than the monthly one. Monthly bars are
        labelled by the period they open, so a bar labelled 2021-11-01 carries
        the 30 November close. Using them meant the memo reported an observation
        date a month earlier than the figure actually came from: the number was
        right and its provenance was wrong, which on this tool is the worse of
        the two.
        """
        symbol = INDEX_SYMBOLS.get(index_id, index_id)
        quote = self._quotes.close_on(symbol, on=on)

        observation = IndexObservation(
            index_id=index_id,
            observed_on=quote.observed_on,
            level=quote.close,
            is_exact_date=quote.is_exact_date,
            requested_date=on,
        )
        source = SourceRef(
            provider=self.name,
            dataset=f"index:{symbol}",
            as_of=quote.observed_on,
            note=quote.substitution_note(),
        )
        return observation, source
