"""Last Round, market-adjusted.

Start from the price set by the company's most recent priced financing round --
the only observable transaction in the company's own securities -- and roll it
forward by the move in a relevant public index since that date.

Why this method carries real weight despite its crudeness: the last round is
the one price a real buyer actually paid for this specific asset. Comps and DCF
are inferences; this is an observation. Its weakness is staleness, and the index
adjustment is precisely an attempt to correct for that.

Design notes:

* **The index is chosen by sector, and the choice is recorded as an overridable
  assumption.** Marking a SaaS company to the broad Nasdaq understates the
  2022 software drawdown badly; a cloud index tracks it far better. Which index
  to use is a judgement call, so it is recorded as one.
* **Beta scales the index move.** A private company is not the index. Beta is
  exposed as the method's sensitivity driver because it is the single number
  most open to challenge.
* **The range brackets the adjustment itself.** One end is the unadjusted last
  round (beta = 0, "the round still holds"); the other is the fully
  market-adjusted mark (beta = 1). The point estimate sits between them. That
  is a more honest bracket than an arbitrary tolerance, because it spans the
  actual disagreement: whether to mark to market at all.
"""

from __future__ import annotations

from vc_audit.context import ValuationContext
from vc_audit.domain.errors import InsufficientEvidenceError
from vc_audit.domain.models import PortfolioCompany, ValuationRange
from vc_audit.methods.base import DriverSpec, MethodOutcome, ValuationMethod

#: Sector to public index. Falls back to the broad market where unmapped.
SECTOR_INDEX_MAP = {
    "saas": "WCLD",
    "fintech": "^IXIC",
    "marketplace": "^IXIC",
    "healthtech": "^IXIC",
}
DEFAULT_INDEX = "^IXIC"

#: Rounds older than this are flagged: the index adjustment is carrying more
#: weight than the transaction it is adjusting.
STALE_ROUND_DAYS = 730


class LastRoundMarkToMarket(ValuationMethod):
    """Most recent post-money valuation, rolled forward on a public index."""

    id = "last_round"
    name = "Last Round (Market-Adjusted)"
    summary = (
        "Most recent post-money valuation adjusted for public market movement since the round."
    )
    required_inputs = frozenset({"last_post_money_valuation_usd", "last_round_date"})
    default_weight = 0.35
    weight_rationale = (
        "The only observed transaction price in the company's own securities, but it "
        "ages quickly and the index adjustment is a proxy, not a measurement."
    )

    def drivers(self) -> list[DriverSpec]:
        return [
            DriverSpec(
                key="index_beta",
                label="Index sensitivity (beta)",
                unit="ratio",
                delta=0.25,
                min_value=0.0,
            ),
        ]

    def compute(self, company: PortfolioCompany, ctx: ValuationContext) -> MethodOutcome:
        trail = ctx.trail
        post_money = company.last_post_money_valuation_usd
        round_date = company.last_round_date
        assert post_money is not None and round_date is not None  # preflight guarantees

        if round_date > ctx.as_of:
            raise InsufficientEvidenceError(
                f"last round date {round_date.isoformat()} is after the valuation date "
                f"{ctx.as_of.isoformat()}; there is no period to mark over"
            )

        age_days = (ctx.as_of - round_date).days
        if age_days > STALE_ROUND_DAYS:
            trail.warn(
                f"Last round closed {age_days / 365.25:.1f} years ago "
                f"({round_date.isoformat()}). Beyond roughly two years the index "
                f"adjustment carries more of the answer than the transaction does, and "
                f"company-specific performance since the round is unreflected."
            )

        trail.record(
            label="last_round_anchor",
            description=(
                f"Take the {company.last_round_series or 'most recent'} post-money "
                f"valuation as the starting mark."
            ),
            formula=f"${post_money:,.0f} on {round_date.isoformat()}",
            inputs={
                "last_post_money_valuation_usd": post_money,
                "last_round_date": round_date.isoformat(),
                "series": company.last_round_series,
                "age_days": age_days,
            },
            output=post_money,
            unit="usd",
        )

        index_id = self._select_index(company, ctx)
        pct_change = self._index_change(index_id, round_date, ctx)

        beta = ctx.assume(
            "index_beta",
            1.0,
            rationale=(
                "Sensitivity of this company's value to the reference index. Held at "
                "1.0 because the index is chosen to match the company's sector; a "
                "company materially more or less volatile than its sector should carry "
                "a beta reflecting that."
            ),
            unit="ratio",
        )
        adjustment = trail.record(
            label="beta_adjusted_change",
            description="Scale the index move by the company's assumed index sensitivity.",
            formula=f"{pct_change:.4f} * {beta:.2f}",
            inputs={"index_change": pct_change, "index_beta": beta},
            output=pct_change * beta,
            unit="percent",
        )

        # A mark cannot fall below zero, and an index move steep enough to imply
        # that is a signal the beta assumption has broken down.
        if adjustment <= -1.0:
            trail.warn(
                f"Beta-adjusted market move of {adjustment:.1%} implies a non-positive "
                f"valuation; floored at zero. Review the beta assumption before relying "
                f"on this method."
            )
            adjustment = -1.0

        fair_value = trail.record(
            label="adjusted_valuation",
            description="Apply the beta-adjusted market move to the last round valuation.",
            formula=f"${post_money:,.0f} * (1 + {adjustment:.4f})",
            inputs={"last_post_money_valuation_usd": post_money, "adjustment": adjustment},
            output=post_money * (1 + adjustment),
            unit="usd",
        )

        value_range = self._range_from_adjustment_bracket(
            post_money, pct_change, fair_value, ctx
        )

        direction = "appreciated" if pct_change >= 0 else "depreciated"
        narrative = (
            f"Valuation based on the {company.last_round_series or 'last'} post-money "
            f"valuation of ${post_money:,.0f} ({round_date.isoformat()}), marked to market "
            f"using {index_id}, which {direction} {abs(pct_change):.1%} between the round "
            f"date and {ctx.as_of.isoformat()}. Applied at a beta of {beta:.2f}. The range "
            f"spans the unadjusted round value and the fully market-adjusted mark."
        )
        return MethodOutcome(
            equity_value_usd=fair_value,
            enterprise_value_usd=None,
            value_range=value_range,
            narrative=narrative,
        )

    # ---- steps -----------------------------------------------------------

    def _select_index(self, company: PortfolioCompany, ctx: ValuationContext) -> str:
        """Choose the reference index, recording the choice as an assumption."""
        default_index = SECTOR_INDEX_MAP.get(company.sector.lower(), DEFAULT_INDEX)
        index_id = ctx.assume(
            "market_index",
            default_index,
            rationale=(
                f"Reference index for marking a {company.sector} company. Selected by "
                f"sector so the benchmark reflects the company's own market: a broad "
                f"index would materially understate sector-specific moves."
            ),
        )
        return index_id

    def _index_change(self, index_id: str, round_date, ctx: ValuationContext) -> float:
        """Percentage move in the index between the round date and the valuation date."""
        trail = ctx.trail
        start_obs, start_source = ctx.provider.get_index_level(index_id=index_id, on=round_date)
        end_obs, end_source = ctx.provider.get_index_level(index_id=index_id, on=ctx.as_of)

        for observation, label in ((start_obs, "round date"), (end_obs, "valuation date")):
            if not observation.is_exact_date:
                trail.warn(
                    f"{index_id} had no published close for the {label} "
                    f"({observation.requested_date.isoformat()}); the mark uses the "
                    f"previous session, {observation.observed_on.isoformat()}."
                )

        trail.record(
            label="index_levels",
            description=f"Read {index_id} levels at the round date and the valuation date.",
            formula=(
                f"{index_id} on {start_obs.observed_on.isoformat()} = {start_obs.level:,.2f}; "
                f"on {end_obs.observed_on.isoformat()} = {end_obs.level:,.2f}"
            ),
            inputs={
                "index_id": index_id,
                "start_date": start_obs.observed_on.isoformat(),
                "start_level": start_obs.level,
                "end_date": end_obs.observed_on.isoformat(),
                "end_level": end_obs.level,
                "exact_dates": start_obs.is_exact_date and end_obs.is_exact_date,
            },
            output={"start_level": start_obs.level, "end_level": end_obs.level},
            sources=[start_source, end_source],
        )
        return trail.record(
            label="index_change",
            description="Compute the percentage move in the index over the holding period.",
            formula=f"({end_obs.level:,.2f} / {start_obs.level:,.2f}) - 1",
            inputs={"start_level": start_obs.level, "end_level": end_obs.level},
            output=(end_obs.level / start_obs.level) - 1,
            unit="percent",
        )

    def _range_from_adjustment_bracket(
        self, post_money: float, pct_change: float, point_value: float, ctx: ValuationContext
    ) -> ValuationRange:
        """Bracket the point estimate by the unadjusted and fully adjusted marks."""
        unadjusted = post_money
        fully_adjusted = post_money * (1 + max(pct_change, -1.0))

        low = min(unadjusted, fully_adjusted, point_value)
        high = max(unadjusted, fully_adjusted, point_value)

        ctx.trail.record(
            label="adjustment_bracket_range",
            description=(
                "Bound the conclusion by the two defensible extremes: holding the round "
                "valuation unadjusted, and marking it fully to the index."
            ),
            formula=(
                f"min/max of unadjusted ${unadjusted:,.0f} and fully adjusted "
                f"${fully_adjusted:,.0f}"
            ),
            inputs={
                "unadjusted_usd": unadjusted,
                "fully_adjusted_usd": round(fully_adjusted, 2),
            },
            output={"low_usd": round(low, 2), "high_usd": round(high, 2)},
            unit="usd",
        )
        return ValuationRange(low_usd=low, point_usd=point_value, high_usd=high)
