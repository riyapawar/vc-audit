"""Discounted Cash Flow.

Project unlevered free cash flow, discount it at a WACC, add a Gordon-growth
terminal value, and bridge the resulting enterprise value to equity.

Notes on the modelling choices a reviewer will ask about:

* **Unlevered FCF, discounted at WACC.** Financing effects live in the discount
  rate, not the cash flows, so the method values the business independently of
  its current capital structure. The debt is then removed once, in the equity
  bridge.
* **End-of-period discounting.** Year 1 is discounted a full period. The
  mid-year convention is arguably more realistic for a business generating cash
  continuously, but end-of-period is the conservative and more common audit
  default, and it is stated here rather than buried.
* **Terminal value concentration is checked.** When the terminal value carries
  most of the enterprise value, the "discounted cash flow" is really a
  perpetuity assumption wearing a forecast. The method computes that share and
  raises a warning past a threshold, because that is a finding, not a detail.
* **WACC must exceed terminal growth.** Otherwise the Gordon formula returns a
  negative or infinite value. This is a fatal error, not a warning: there is no
  defensible number on the other side of it.
"""

from __future__ import annotations

from vc_audit.context import ValuationContext
from vc_audit.domain.errors import AssumptionError, InsufficientEvidenceError
from vc_audit.domain.models import FinancialProjection, PortfolioCompany, ValuationRange
from vc_audit.methods.base import DriverSpec, MethodOutcome, ValuationMethod, bridge_to_equity

#: Above this share of enterprise value, the terminal value dominates the
#: conclusion and the forecast is doing little work. Warned, not blocked.
TERMINAL_VALUE_CONCENTRATION_WARN = 0.75

#: Width of the WACC band used to derive the reported valuation range.
WACC_RANGE_DELTA = 0.02


def _unlevered_fcf(projection: FinancialProjection) -> float:
    """Unlevered free cash flow for one projected year.

    NOPAT + D&A - capex - increase in net working capital. Tax is applied to
    EBIT rather than to pre-tax income precisely because this is the *unlevered*
    figure: the interest tax shield belongs in the WACC.
    """
    nopat = projection.ebit_usd * (1 - projection.tax_rate)
    return (
        nopat
        + projection.depreciation_amortization_usd
        - projection.capex_usd
        - projection.change_in_nwc_usd
    )


def _enterprise_value(fcfs: list[float], wacc: float, terminal_growth: float) -> float:
    """Pure DCF arithmetic, with no trail writes.

    Kept side-effect free so the range calculation and the sensitivity sweep can
    re-run it at other discount rates without polluting the audit trail with
    hypothetical steps.
    """
    if wacc <= terminal_growth:
        raise AssumptionError(
            f"WACC ({wacc:.2%}) must exceed terminal growth ({terminal_growth:.2%}); "
            f"the Gordon growth formula has no finite solution otherwise"
        )
    pv_explicit = sum(fcf / (1 + wacc) ** (i + 1) for i, fcf in enumerate(fcfs))
    terminal_value = fcfs[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / (1 + wacc) ** len(fcfs)
    return pv_explicit + pv_terminal


class DiscountedCashFlow(ValuationMethod):
    """Intrinsic value from projected unlevered free cash flow."""

    id = "dcf"
    name = "Discounted Cash Flow"
    summary = "Projected unlevered free cash flow discounted at WACC, plus a Gordon terminal value."
    required_inputs = frozenset({"projections"})
    default_weight = 0.30
    weight_rationale = (
        "Most theoretically complete, but wholly dependent on management forecasts "
        "and highly sensitive to two unobservable assumptions."
    )

    def drivers(self) -> list[DriverSpec]:
        return [
            DriverSpec(
                key="wacc",
                label="Discount rate (WACC)",
                unit="percent",
                delta=0.02,
                min_value=0.01,
            ),
            DriverSpec(
                key="terminal_growth",
                label="Terminal growth rate",
                unit="percent",
                delta=0.01,
                min_value=0.0,
            ),
        ]

    def compute(self, company: PortfolioCompany, ctx: ValuationContext) -> MethodOutcome:
        trail = ctx.trail
        projections = company.projections
        source = company.projections_source

        if len(projections) < 2:
            trail.warn(
                f"Only {len(projections)} projected year(s) supplied; the terminal value "
                f"will carry nearly all of the conclusion."
            )

        wacc = ctx.assume(
            "wacc",
            0.15,
            rationale=(
                "Weighted average cost of capital for a venture-stage private company. "
                "15% reflects the equity risk premium a growth-stage business carries "
                "over listed peers; company-specific evidence should replace it where "
                "available."
            ),
            unit="percent",
        )
        terminal_growth = ctx.assume(
            "terminal_growth",
            0.03,
            rationale=(
                "Perpetual growth beyond the forecast horizon, held at roughly long-run "
                "nominal GDP. A rate above that implies the company eventually "
                "outgrows the economy, which no business does indefinitely."
            ),
            unit="percent",
        )
        if wacc <= terminal_growth:
            raise AssumptionError(
                f"WACC ({wacc:.2%}) must exceed terminal growth ({terminal_growth:.2%}); "
                f"the Gordon growth formula has no finite solution otherwise"
            )

        fcfs = self._record_cash_flows(projections, trail, source)
        self._reject_uncapitalisable_terminal_year(projections, fcfs, trail)
        pv_explicit = self._record_present_values(projections, fcfs, wacc, trail)
        pv_terminal, terminal_value = self._record_terminal_value(
            projections, fcfs, wacc, terminal_growth, trail
        )

        enterprise_value = trail.record(
            label="enterprise_value",
            description="Sum the present value of the forecast period and the terminal value.",
            formula=f"${pv_explicit:,.0f} + ${pv_terminal:,.0f}",
            inputs={"pv_forecast_period_usd": pv_explicit, "pv_terminal_value_usd": pv_terminal},
            output=pv_explicit + pv_terminal,
            unit="usd",
        )

        concentration = ctx.derive(
            "terminal_value_share",
            pv_terminal / enterprise_value if enterprise_value else 0.0,
            rationale="Share of enterprise value attributable to the terminal value.",
            unit="percent",
        )
        if concentration > TERMINAL_VALUE_CONCENTRATION_WARN:
            trail.warn(
                f"Terminal value accounts for {concentration:.0%} of enterprise value "
                f"(threshold {TERMINAL_VALUE_CONCENTRATION_WARN:.0%}). The conclusion "
                f"rests on the perpetuity assumption more than on the forecast."
            )

        equity_value = bridge_to_equity(enterprise_value, company, ctx)
        value_range = self._range_from_wacc_band(
            fcfs, wacc, terminal_growth, company, equity_value, ctx
        )

        narrative = (
            f"Valuation based on {len(projections)} years of projected unlevered free cash "
            f"flow discounted at a {wacc:.1%} WACC, plus a Gordon terminal value at "
            f"{terminal_growth:.1%} perpetual growth (${terminal_value:,.0f} undiscounted, "
            f"{concentration:.0%} of enterprise value). The range flexes the discount rate "
            f"by +/-{WACC_RANGE_DELTA:.0%}."
        )
        return MethodOutcome(
            equity_value_usd=equity_value,
            enterprise_value_usd=enterprise_value,
            value_range=value_range,
            narrative=narrative,
        )

    # ---- steps -----------------------------------------------------------

    def _record_cash_flows(self, projections, trail, source) -> list[float]:
        """Build unlevered FCF per year, showing the full line-item build."""
        rows = {}
        fcfs = []
        for projection in projections:
            fcf = _unlevered_fcf(projection)
            fcfs.append(fcf)
            rows[str(projection.year)] = {
                "ebit_usd": projection.ebit_usd,
                "tax_rate": projection.tax_rate,
                "nopat_usd": round(projection.ebit_usd * (1 - projection.tax_rate), 2),
                "depreciation_amortization_usd": projection.depreciation_amortization_usd,
                "capex_usd": projection.capex_usd,
                "change_in_nwc_usd": projection.change_in_nwc_usd,
                "unlevered_fcf_usd": round(fcf, 2),
            }
        trail.record(
            label="unlevered_free_cash_flow",
            description=(
                "Build unlevered free cash flow per forecast year: EBIT after tax, plus "
                "depreciation and amortisation, less capital expenditure and the increase "
                "in net working capital."
            ),
            formula="EBIT * (1 - tax_rate) + D&A - capex - change_in_NWC, per year",
            inputs=rows,
            output={str(p.year): round(f, 2) for p, f in zip(projections, fcfs, strict=True)},
            unit="usd",
            sources=[source] if source else [],
        )
        return fcfs

    def _reject_uncapitalisable_terminal_year(self, projections, fcfs, trail) -> None:
        """Decline when the terminal year cannot be capitalised into a perpetuity.

        Gordon growth multiplies the final year's cash flow by a positive factor,
        so a negative terminal flow produces a negative terminal value and, very
        often, a negative enterprise value overall. Arithmetically that is what
        the formula says; as a valuation it is meaningless, because it assumes
        the company burns cash forever while simultaneously being a going
        concern worth valuing.

        This matters more here than in general practice: a venture-stage company
        still consuming cash in the final forecast year is the normal case, not
        the exotic one. Returning a confident negative number would be the worst
        available outcome, so the method declines instead.

        Recoverable rather than fatal: the forecast is unusable for a DCF, but
        comps and the last-round mark are unaffected and should still run.
        """
        final = fcfs[-1]
        if final > 0:
            return
        raise InsufficientEvidenceError(
            f"year-{projections[-1].year} unlevered free cash flow is "
            f"${final:,.0f}. Gordon growth capitalises the terminal year into a "
            f"perpetuity, so a non-positive terminal flow yields a negative "
            f"terminal value and no meaningful enterprise value. Extend the "
            f"forecast to a cash-generative year, or value this company on the "
            f"other methods."
        )

    def _record_present_values(self, projections, fcfs, wacc, trail) -> float:
        """Discount each forecast year, recording the factor and the result."""
        rows = {}
        total = 0.0
        for period, (projection, fcf) in enumerate(zip(projections, fcfs, strict=True), start=1):
            factor = 1 / (1 + wacc) ** period
            present_value = fcf * factor
            total += present_value
            rows[str(projection.year)] = {
                "period": period,
                "unlevered_fcf_usd": round(fcf, 2),
                "discount_factor": round(factor, 6),
                "present_value_usd": round(present_value, 2),
            }
        return trail.record(
            label="pv_forecast_period",
            description=(
                "Discount each forecast year's cash flow to present value at the WACC, "
                "using an end-of-period convention."
            ),
            formula=f"sum(FCF_t / (1 + {wacc:.4f})^t) for t = 1..{len(fcfs)}",
            inputs=rows,
            output=total,
            unit="usd",
        )

    def _record_terminal_value(self, projections, fcfs, wacc, terminal_growth, trail):
        """Gordon-growth terminal value and its present value."""
        final_year = projections[-1].year
        final_fcf = fcfs[-1]
        periods = len(fcfs)

        terminal_value = trail.record(
            label="terminal_value",
            description=(
                f"Capitalise the year-{final_year} cash flow into perpetuity using the "
                f"Gordon growth model."
            ),
            formula=(
                f"${final_fcf:,.0f} * (1 + {terminal_growth:.4f}) / "
                f"({wacc:.4f} - {terminal_growth:.4f})"
            ),
            inputs={
                "final_year_fcf_usd": final_fcf,
                "terminal_growth": terminal_growth,
                "wacc": wacc,
            },
            output=final_fcf * (1 + terminal_growth) / (wacc - terminal_growth),
            unit="usd",
        )
        pv_terminal = trail.record(
            label="pv_terminal_value",
            description="Discount the terminal value back to present.",
            formula=f"${terminal_value:,.0f} / (1 + {wacc:.4f})^{periods}",
            inputs={"terminal_value_usd": terminal_value, "wacc": wacc, "periods": periods},
            output=terminal_value / (1 + wacc) ** periods,
            unit="usd",
        )
        return pv_terminal, terminal_value

    def _range_from_wacc_band(
        self, fcfs, wacc, terminal_growth, company, point_equity, ctx
    ) -> ValuationRange:
        """Flex the discount rate to bound the conclusion.

        A higher discount rate produces a lower value, so the bounds invert.
        """
        high_wacc = wacc + WACC_RANGE_DELTA
        low_wacc = max(wacc - WACC_RANGE_DELTA, terminal_growth + 0.005)
        net_cash = company.cash_usd - company.debt_usd

        low = _enterprise_value(fcfs, high_wacc, terminal_growth) + net_cash
        high = _enterprise_value(fcfs, low_wacc, terminal_growth) + net_cash

        ctx.trail.record(
            label="wacc_band_range",
            description=(
                "Bound the conclusion by re-running the model at the edges of a "
                "discount-rate band. The higher rate produces the low value."
            ),
            formula=f"re-run at WACC {low_wacc:.2%} and {high_wacc:.2%}",
            inputs={"low_wacc": low_wacc, "high_wacc": high_wacc},
            output={"low_usd": round(low, 2), "high_usd": round(high, 2)},
            unit="usd",
        )
        return ValuationRange(
            low_usd=min(low, point_equity),
            point_usd=point_equity,
            high_usd=max(high, point_equity),
        )
