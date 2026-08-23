"""Comparable Company Analysis.

Value the company at what the public market pays for similar businesses:
screen a peer set, compute each peer's EV/Revenue, take the median, discount it
for illiquidity, and apply it to the subject's revenue.

Four choices here are the ones worth defending in review:

* **Median, not mean.** Multiples are right-skewed -- one hyper-growth peer can
  drag a mean well past anything a buyer would actually pay. The median is
  robust to exactly that.
* **Outliers are trimmed by a stated rule, not by eye.** A Tukey fence
  (1.5 x IQR) drops peers mechanically, and every dropped peer is named in the
  trail with its multiple. Discretionary exclusion is the classic way a comps
  analysis becomes unfalsifiable; a rule plus a record is the fix.
* **Rejections are reported as prominently as inclusions.** The method emits a
  funnel -- universe, dropped for missing data, dropped as not comparable,
  dropped as an outlier, valued against -- because a peer set presented without
  its rejects looks curated even when it isn't.
* **The range comes from the peer distribution.** Low and high are the peer
  quartile multiples, so the range reports real disagreement among comparables
  rather than an arbitrary +/- percentage.
"""

from __future__ import annotations

import statistics

from vc_audit.context import ValuationContext
from vc_audit.data.base import PeerScreenResult
from vc_audit.domain.errors import InsufficientEvidenceError
from vc_audit.domain.models import (
    ExcludedPeer,
    PeerCompany,
    PeerFunnel,
    PortfolioCompany,
    ValuationRange,
)
from vc_audit.methods.base import DriverSpec, MethodOutcome, ValuationMethod, bridge_to_equity

#: Below this many surviving peers, a "median" is a small-sample artefact rather
#: than a market observation, so the method declines to conclude.
MIN_PEERS = 3

#: Tukey fence coefficient for outlier trimming.
IQR_FENCE = 1.5


def _quartiles(values: list[float]) -> tuple[float, float]:
    """Tukey hinges (Q1, Q3): the medians of the lower and upper halves.

    Hand-rolled rather than taken from ``statistics.quantiles`` because that
    function interpolates, and an interpolated hinge is not a multiple any real
    peer actually trades at. Hinges keep the fence explicable to a reviewer.
    """
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    lower = ordered[:mid]
    upper = ordered[mid + 1 :] if n % 2 else ordered[mid:]
    return statistics.median(lower), statistics.median(upper)


class ComparableCompanyAnalysis(ValuationMethod):
    """Peer-median EV/Revenue applied to the subject's LTM revenue."""

    id = "comps"
    name = "Comparable Company Analysis"
    summary = "Peer-median EV/Revenue multiple, illiquidity-discounted, applied to LTM revenue."
    required_inputs = frozenset({"sector", "ltm_revenue_usd"})
    default_weight = 0.35
    weight_rationale = (
        "Anchored in observable market prices, but comparability between a private "
        "company and listed peers is always imperfect."
    )

    def drivers(self) -> list[DriverSpec]:
        return [
            DriverSpec(
                key="illiquidity_discount",
                label="Illiquidity discount",
                unit="percent",
                delta=0.10,
                min_value=0.0,
                max_value=0.60,
            ),
            DriverSpec(
                key="peer_size_band",
                label="Peer size band",
                unit="ratio",
                delta=0.90,
                mode="relative",
                min_value=1.5,
            ),
        ]

    def compute(self, company: PortfolioCompany, ctx: ValuationContext) -> MethodOutcome:
        trail = ctx.trail
        revenue = company.ltm_revenue_usd
        assert revenue is not None  # guaranteed by preflight

        screen = self._screen_peers(company, ctx)
        source = screen.source
        surviving, review_rejects = self._review_comparability(company, screen.peers, ctx)
        multiples = self._record_peer_multiples(surviving, trail, source)
        retained, trimmed = self._trim_outliers(surviving, multiples, ctx)

        retained_multiples = [peer.ev_to_revenue for peer in retained]
        median_multiple = ctx.derive(
            "peer_median_ev_revenue",
            statistics.median(retained_multiples),
            rationale=(
                f"Median EV/Revenue across the {len(retained)} peers surviving the "
                f"comparability and outlier screens."
            ),
            unit="multiple",
            source=source,
        )
        trail.record(
            label="peer_median_multiple",
            description="Take the median EV/Revenue across the retained peer set.",
            formula=f"median({', '.join(f'{p.ev_to_revenue:.2f}x' for p in retained)})",
            inputs={p.ticker: round(p.ev_to_revenue, 4) for p in retained},
            output=median_multiple,
            unit="multiple",
            sources=[source],
        )

        discount = ctx.assume(
            "illiquidity_discount",
            0.20,
            rationale=(
                "Private shares cannot be sold on demand, so they trade below "
                "otherwise-identical listed equity. 20% sits mid-range for the "
                "restricted-stock and pre-IPO studies auditors typically cite; it is "
                "a judgement call and should be revisited per company."
            ),
            unit="percent",
        )
        adjusted_multiple = trail.record(
            label="apply_illiquidity_discount",
            description="Discount the peer multiple for the subject's lack of marketability.",
            formula=f"{median_multiple:.2f}x * (1 - {discount:.0%})",
            inputs={"median_multiple": median_multiple, "illiquidity_discount": discount},
            output=median_multiple * (1 - discount),
            unit="multiple",
        )

        enterprise_value = trail.record(
            label="apply_multiple",
            description="Apply the discounted multiple to the subject's LTM revenue.",
            formula=f"{adjusted_multiple:.2f}x * ${revenue:,.0f}",
            inputs={"adjusted_multiple": adjusted_multiple, "ltm_revenue_usd": revenue},
            output=adjusted_multiple * revenue,
            unit="usd",
        )
        equity_value = bridge_to_equity(enterprise_value, company, ctx)

        value_range = self._range_from_peer_spread(
            retained_multiples, discount, revenue, company, equity_value, ctx
        )

        excluded = [*screen.excluded, *review_rejects, *trimmed]
        funnel = self._record_funnel(screen, review_rejects, trimmed, retained, ctx)

        narrative = (
            f"Valuation based on the peer-group median EV/Revenue multiple of "
            f"{median_multiple:.2f}x across {len(retained)} listed comparables in the "
            f"{company.sector} sector, discounted {discount:.0%} for illiquidity to "
            f"{adjusted_multiple:.2f}x and applied to ${revenue:,.0f} of LTM revenue. "
            f"{funnel.proposed} candidates were considered and {funnel.total_dropped} "
            f"rejected, each with a recorded reason. The range reflects the peer group's "
            f"own interquartile spread."
        )
        return MethodOutcome(
            equity_value_usd=equity_value,
            enterprise_value_usd=enterprise_value,
            value_range=value_range,
            narrative=narrative,
            peers=retained,
            excluded_peers=excluded,
            funnel=funnel,
        )

    # ---- steps -----------------------------------------------------------

    def _screen_peers(self, company: PortfolioCompany, ctx: ValuationContext) -> PeerScreenResult:
        """Fetch and record the comparability screen."""
        size_band = ctx.assume(
            "peer_size_band",
            500.0,
            rationale=(
                "Peers are kept when LTM revenue falls within a 500x band either side of "
                "the subject, which in practice retains the whole sector. Listed "
                "comparables are routinely two to three orders of magnitude larger than a "
                "venture-stage company, so a conventional size screen would empty the peer "
                "set rather than improve it. The size mismatch is addressed through the "
                "illiquidity discount, and tightening this band is available as an "
                "override where a genuine size-matched peer group exists."
            ),
            unit="ratio",
        )
        extra = tuple(ctx.overrides.get("extra_peer_tickers", ()))
        extra += self._proposed_tickers(company, ctx)

        screen = ctx.provider.get_peers(
            sector=company.sector,
            as_of=ctx.as_of,
            subject_revenue_usd=company.ltm_revenue_usd,
            size_band=size_band,
            extra_tickers=extra,
        )
        ctx.trail.record(
            label="peer_screen",
            description=(
                f"Screen the candidate universe for {company.sector} companies within "
                f"the size band."
            ),
            formula=f"filter(sector == '{company.sector}', revenue within {size_band:g}x band)",
            inputs={
                "sector": company.sector,
                "size_band": size_band,
                "universe_size": screen.universe_size,
                "screen": screen.screen_description,
                "rejected": {e.ticker: e.reason for e in screen.excluded},
            },
            output=screen.tickers,
            sources=[screen.source],
        )
        for exclusion in screen.excluded:
            if exclusion.stage == "data":
                ctx.trail.warn(
                    f"Candidate {exclusion.ticker} could not be assembled: {exclusion.reason}"
                )
        if len(screen.peers) < MIN_PEERS:
            raise InsufficientEvidenceError(
                f"comps requires at least {MIN_PEERS} comparable peers; the screen for "
                f"sector '{company.sector}' returned {len(screen.peers)} usable from a "
                f"universe of {screen.universe_size}"
            )
        return screen

    def _proposed_tickers(
        self, company: PortfolioCompany, ctx: ValuationContext
    ) -> tuple[str, ...]:
        """Candidates nominated by the research layer, if one is configured.

        Proposals are *added* to the standing universe rather than replacing it.
        A model that returns nothing therefore costs nothing, and a model that
        hallucinates a ticker cannot displace a real candidate -- the worst it
        can do is add a name that EDGAR then fails to resolve, which surfaces as
        a recorded exclusion.
        """
        researcher = ctx.researcher
        if researcher is None:
            return ()

        candidates = researcher.propose_peers(company, trail=ctx.trail)
        if candidates is None or not candidates.tickers:
            return ()

        ctx.trail.record(
            label="peer_proposals",
            description=(
                "Add model-proposed candidates to the standing sector universe. "
                "Proposals supplement the universe; they never replace it."
            ),
            formula=f"{len(candidates.tickers)} candidates proposed",
            inputs={
                "business_summary": candidates.business_summary,
                "rationales": {p.ticker.upper(): p.rationale for p in candidates.proposals},
            },
            output=list(candidates.tickers),
        )
        ctx.trail.warn(
            "Peer selection included model judgement, so the candidate set is not "
            "guaranteed identical between runs. Every proposal and rejection is "
            "recorded, and all figures remain computed from filings."
        )
        return candidates.tickers

    def _review_comparability(
        self, company: PortfolioCompany, peers: list[PeerCompany], ctx: ValuationContext
    ) -> tuple[list[PeerCompany], list[ExcludedPeer]]:
        """Optionally ask the research layer to judge each peer's comparability.

        A no-op unless a researcher is configured. When one is, the model sees
        each peer's *SEC registrant name* rather than any claimed name, so a
        ticker that resolves to the wrong company is visible to the reviewer and
        gets rejected on sight.

        A rejection that would take the set below the peer floor is *not*
        applied: the method keeps the wider set and warns, because "the model
        thought these were dissimilar" is a weaker signal than "there is no
        longer enough evidence to compute a median".
        """
        researcher = ctx.researcher
        if researcher is None or not peers:
            return list(peers), []

        review = researcher.review_peers(company, peers, trail=ctx.trail)
        if review is None:
            return list(peers), []

        rejected = review.rejected()
        accepted = review.accepted()
        kept = [p for p in peers if p.ticker.upper() not in rejected]

        if len(kept) < MIN_PEERS:
            ctx.trail.warn(
                f"Peer review would have rejected {len(peers) - len(kept)} of {len(peers)} "
                f"peers, leaving fewer than the {MIN_PEERS} needed for a median. The "
                f"review was recorded but not applied; treat the peer set with caution."
            )
            return list(peers), []

        # Frozen records: the reviewer's reason is attached by copy.
        kept = [
            peer.model_copy(
                update={
                    "inclusion_rationale": accepted.get(
                        peer.ticker.upper(), peer.inclusion_rationale
                    )
                }
            )
            for peer in kept
        ]
        rejects = [
            ExcludedPeer(
                ticker=peer.ticker,
                company_name=peer.name,
                stage="comparability",
                reason=rejected[peer.ticker.upper()],
            )
            for peer in peers
            if peer.ticker.upper() in rejected
        ]
        ctx.trail.record(
            label="peer_comparability_review",
            description=(
                "Apply the research layer's verdict on each peer's business "
                "comparability. The model judges similarity; it produces no figures."
            ),
            formula=f"kept {len(kept)} of {len(peers)} after review",
            inputs={"rejected": rejected},
            output=[p.ticker for p in kept],
        )
        return kept, rejects

    def _record_peer_multiples(self, peers: list[PeerCompany], trail, source) -> list[float]:
        """Record each peer's EV bridge, implied multiple, and filing citation."""
        rows = {}
        for peer in peers:
            rows[peer.ticker] = {
                "name": peer.name,
                "market_cap_usd": round(peer.market_cap_usd, 2),
                "total_debt_usd": round(peer.total_debt_usd, 2),
                "cash_usd": round(peer.cash_usd, 2),
                "enterprise_value_usd": round(peer.enterprise_value_usd, 2),
                "ltm_revenue_usd": round(peer.ltm_revenue_usd, 2),
                "ev_to_revenue": round(peer.ev_to_revenue, 4),
                "basis": peer.fundamentals_basis,
                "filing": peer.latest_filing.cite() if peer.latest_filing else None,
            }
        trail.record(
            label="peer_multiples",
            description=(
                "Build each peer's enterprise value (market cap + debt - cash) and divide "
                "by its LTM revenue. Computed here rather than taken as a vendor figure, "
                "so every component is individually citable."
            ),
            formula="(market_cap + total_debt - cash) / ltm_revenue, per peer",
            inputs=rows,
            output={t: round(r["ev_to_revenue"], 2) for t, r in rows.items()},
            unit="multiple",
            sources=[source],
        )
        return [peer.ev_to_revenue for peer in peers]

    def _trim_outliers(
        self, peers: list[PeerCompany], multiples: list[float], ctx: ValuationContext
    ) -> tuple[list[PeerCompany], list[ExcludedPeer]]:
        """Drop peers outside the Tukey fence, naming every exclusion."""
        trail = ctx.trail

        if len(peers) < 4:
            trail.warn(
                f"Only {len(peers)} peers in the screen; outlier trimming was skipped "
                f"because a fence computed on so few observations is not meaningful."
            )
            return list(peers), []

        q1, q3 = _quartiles(multiples)
        iqr = q3 - q1
        low_fence, high_fence = q1 - IQR_FENCE * iqr, q3 + IQR_FENCE * iqr

        retained = [p for p in peers if low_fence <= p.ev_to_revenue <= high_fence]
        dropped = [p for p in peers if not (low_fence <= p.ev_to_revenue <= high_fence)]

        trimmed = [
            ExcludedPeer(
                ticker=peer.ticker,
                company_name=peer.name,
                stage="statistical",
                reason=(
                    f"trades at {peer.ev_to_revenue:.2f}x EV/Revenue, outside the Tukey "
                    f"fence of {low_fence:.2f}x to {high_fence:.2f}x"
                ),
            )
            for peer in dropped
        ]

        trail.record(
            label="outlier_trim",
            description=(
                "Exclude peers whose multiple falls outside the Tukey fence "
                "(Q1 - 1.5*IQR, Q3 + 1.5*IQR). Applied mechanically so exclusions "
                "are reproducible rather than discretionary."
            ),
            formula=(
                f"keep {low_fence:.2f}x <= EV/Revenue <= {high_fence:.2f}x "
                f"(Q1={q1:.2f}x, Q3={q3:.2f}x, IQR={iqr:.2f})"
            ),
            inputs={
                "q1": round(q1, 4),
                "q3": round(q3, 4),
                "iqr": round(iqr, 4),
                "fence_coefficient": IQR_FENCE,
                "excluded": {p.ticker: round(p.ev_to_revenue, 2) for p in dropped},
            },
            output=[p.ticker for p in retained],
            unit="multiple",
        )
        for peer in dropped:
            trail.warn(
                f"Peer {peer.ticker} excluded as an outlier at {peer.ev_to_revenue:.2f}x "
                f"EV/Revenue (fence: {low_fence:.2f}x to {high_fence:.2f}x)."
            )
        if len(retained) < MIN_PEERS:
            raise InsufficientEvidenceError(
                f"only {len(retained)} peers survived outlier trimming; "
                f"comps requires at least {MIN_PEERS}"
            )
        return retained, trimmed

    def _record_funnel(
        self,
        screen: PeerScreenResult,
        review_rejects: list[ExcludedPeer],
        trimmed: list[ExcludedPeer],
        retained: list[PeerCompany],
        ctx: ValuationContext,
    ) -> PeerFunnel:
        """Record how the candidate universe narrowed to the valued peer set."""
        no_data = sum(1 for e in screen.excluded if e.stage == "data")
        not_comparable = (
            sum(1 for e in screen.excluded if e.stage == "comparability") + len(review_rejects)
        )
        funnel = PeerFunnel(
            proposed=screen.universe_size or (len(screen.peers) + len(screen.excluded)),
            dropped_no_data=no_data,
            dropped_not_comparable=not_comparable,
            dropped_outlier=len(trimmed),
            retained=len(retained),
        )
        ctx.trail.record(
            label="peer_funnel",
            description=(
                "Summarise the narrowing from candidate universe to valued peer set. "
                "How much judgement stood between the two is itself audit evidence."
            ),
            formula=(
                f"{funnel.proposed} proposed - {funnel.dropped_no_data} no data "
                f"- {funnel.dropped_not_comparable} not comparable "
                f"- {funnel.dropped_outlier} outliers = {funnel.retained} valued against"
            ),
            inputs=funnel.model_dump(),
            output=funnel.retained,
        )
        return funnel

    def _range_from_peer_spread(
        self,
        retained_multiples: list[float],
        discount: float,
        revenue: float,
        company: PortfolioCompany,
        point_equity: float,
        ctx: ValuationContext,
    ) -> ValuationRange:
        """Derive low/high from the peer interquartile range."""
        q1, q3 = _quartiles(retained_multiples)
        net_cash = company.cash_usd - company.debt_usd
        low = q1 * (1 - discount) * revenue + net_cash
        high = q3 * (1 - discount) * revenue + net_cash

        ctx.trail.record(
            label="peer_spread_range",
            description=(
                "Set the valuation range from the peer group's own interquartile "
                "multiples, so the range reports observed disagreement among "
                "comparables rather than an assumed tolerance."
            ),
            formula=(
                f"Q1 {q1:.2f}x and Q3 {q3:.2f}x, each discounted {discount:.0%}, "
                f"applied to ${revenue:,.0f} and bridged to equity"
            ),
            inputs={"q1_multiple": round(q1, 4), "q3_multiple": round(q3, 4)},
            output={"low_usd": round(low, 2), "high_usd": round(high, 2)},
            unit="usd",
        )
        # Clamp so an unusual peer distribution cannot invert the range.
        return ValuationRange(
            low_usd=min(low, point_equity),
            point_usd=point_equity,
            high_usd=max(high, point_equity),
        )
