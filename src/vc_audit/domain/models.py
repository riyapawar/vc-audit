"""Domain vocabulary: the things this system reasons about.

Everything here is a Pydantic model, which buys three things at once: input
validation with structured error messages (an auditor gets "ltm_revenue_usd must
be > 0", not a ``TypeError`` from three frames down), JSON serialisation for the
evidence pack, and an OpenAPI schema for the HTTP service for free.

**Money is represented as ``float``, not ``Decimal``.** That is a deliberate
tradeoff. ``Decimal`` is the right call when a system moves money, because
binary rounding error is a real defect there. This system *estimates* a value
whose honest precision is roughly two significant figures -- the uncertainty in
a discount rate assumption swamps float error by many orders of magnitude. The
cost of ``Decimal`` (constant ``Decimal * float`` type friction across every
statistical operation) buys precision that the underlying analysis does not
have. Presentation rounding happens once, at the reporting boundary.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vc_audit.domain.audit import AuditTrail, SourceRef

# --------------------------------------------------------------------------
# Subject company
# --------------------------------------------------------------------------


class FinancialProjection(BaseModel):
    """One forecast year for a portfolio company.

    Modelled at the line-item level rather than as a pre-computed free cash
    flow so the DCF can show its work: an auditor reviewing the memo sees the
    tax, capex and working-capital steps individually rather than a single
    unexplained cash flow figure.
    """

    model_config = ConfigDict(frozen=True)

    year: int = Field(description="Calendar year this projection covers.")
    revenue_usd: float = Field(gt=0)
    ebit_usd: float = Field(description="Operating profit; may be negative for early stage.")
    depreciation_amortization_usd: float = Field(ge=0, default=0.0)
    capex_usd: float = Field(ge=0, default=0.0)
    change_in_nwc_usd: float = Field(
        default=0.0,
        description="Increase in net working capital; positive consumes cash.",
    )
    tax_rate: float = Field(ge=0, le=1, default=0.21)


class PortfolioCompany(BaseModel):
    """The private company being valued.

    Fields are optional because sparse data is the defining constraint of this
    problem. Rather than demand a complete record, each valuation method
    declares what *it* needs and the engine runs whichever methods the available
    data supports.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    sector: str = Field(description="Comparability screen key, e.g. 'saas'.")
    currency: str = Field(default="USD")

    # Operating data (drives Comps)
    ltm_revenue_usd: float | None = Field(default=None, gt=0)
    ltm_ebitda_usd: float | None = Field(default=None)

    # Balance sheet (drives the enterprise-to-equity bridge)
    cash_usd: float = Field(default=0.0, ge=0)
    debt_usd: float = Field(default=0.0, ge=0)

    # Last financing round (drives Last Round)
    last_post_money_valuation_usd: float | None = Field(default=None, gt=0)
    last_round_date: date | None = None
    last_round_series: str | None = None

    # Forecasts (drives DCF)
    projections: list[FinancialProjection] = Field(default_factory=list)
    projections_source: SourceRef | None = Field(
        default=None,
        description="Where the forecast came from; cited verbatim in the memo.",
    )

    @property
    def net_debt_usd(self) -> float:
        """Debt less cash. Bridges enterprise value to equity value."""
        return self.debt_usd - self.cash_usd

    @model_validator(mode="after")
    def _validate_internal_consistency(self) -> PortfolioCompany:
        if (self.last_post_money_valuation_usd is None) != (self.last_round_date is None):
            raise ValueError(
                "last_post_money_valuation_usd and last_round_date must be provided "
                "together: a round valuation without its date cannot be mark-adjusted"
            )
        years = [p.year for p in self.projections]
        if len(years) != len(set(years)):
            raise ValueError("projections contain duplicate years")
        if years != sorted(years):
            raise ValueError("projections must be ordered by ascending year")
        return self

    def available_inputs(self) -> set[str]:
        """Names of the inputs actually populated, for method preflight checks."""
        present: set[str] = set()
        if self.ltm_revenue_usd is not None:
            present.add("ltm_revenue_usd")
        if self.ltm_ebitda_usd is not None:
            present.add("ltm_ebitda_usd")
        if self.last_post_money_valuation_usd is not None:
            present.add("last_post_money_valuation_usd")
        if self.last_round_date is not None:
            present.add("last_round_date")
        if self.projections:
            present.add("projections")
        if self.sector:
            present.add("sector")
        return present


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


class PeerCompany(BaseModel):
    """A publicly traded comparable.

    Enterprise value is computed here rather than stored, so the comps method
    can record the bridge as an explicit step instead of trusting an opaque
    vendor field.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str
    sector: str
    market_cap_usd: float = Field(gt=0)
    total_debt_usd: float = Field(ge=0)
    cash_usd: float = Field(ge=0)
    ltm_revenue_usd: float = Field(gt=0)
    ltm_ebitda_usd: float | None = None
    revenue_growth_yoy: float | None = None

    @property
    def enterprise_value_usd(self) -> float:
        return self.market_cap_usd + self.total_debt_usd - self.cash_usd

    @property
    def ev_to_revenue(self) -> float:
        return self.enterprise_value_usd / self.ltm_revenue_usd


class IndexObservation(BaseModel):
    """A single closing level of a public index."""

    model_config = ConfigDict(frozen=True)

    index_id: str
    observed_on: date
    level: float = Field(gt=0)
    is_exact_date: bool = Field(
        default=True,
        description="False when the requested date was not a trading day and the "
        "nearest prior observation was substituted.",
    )
    requested_date: date | None = None


class IndexSeries(BaseModel):
    """A time series of index levels, as served by a market data provider."""

    index_id: str
    name: str
    observations: list[IndexObservation]


# --------------------------------------------------------------------------
# Valuation outputs
# --------------------------------------------------------------------------


class ValuationRange(BaseModel):
    """A low/point/high estimate.

    A single number implies a precision this exercise does not have. Every
    method returns a range; the point estimate is the one you would book.
    """

    model_config = ConfigDict(frozen=True)

    low_usd: float
    point_usd: float
    high_usd: float

    @model_validator(mode="after")
    def _validate_ordering(self) -> ValuationRange:
        if not (self.low_usd <= self.point_usd <= self.high_usd):
            raise ValueError(
                f"range must be ordered low <= point <= high, got "
                f"{self.low_usd} / {self.point_usd} / {self.high_usd}"
            )
        return self

    @property
    def width_pct(self) -> float:
        """Range width as a fraction of the point estimate."""
        if self.point_usd == 0:
            return 0.0
        return (self.high_usd - self.low_usd) / self.point_usd


class Driver(BaseModel):
    """An assumption a method exposes for sensitivity testing.

    Methods declare their drivers; the sensitivity engine re-runs the method
    with each driver perturbed. This is why sensitivity analysis needs no
    per-method special-casing.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Assumption key the method reads via ctx.assume().")
    label: str
    base: float
    low: float
    high: float
    unit: Literal["percent", "multiple", "usd", "ratio"] = "percent"


class SensitivityRow(BaseModel):
    """The effect of moving one driver across its low/high bounds."""

    model_config = ConfigDict(frozen=True)

    driver_key: str
    label: str
    unit: str
    low_input: float
    high_input: float
    low_value_usd: float
    high_value_usd: float
    base_value_usd: float

    @property
    def swing_usd(self) -> float:
        """Absolute spread in output value. Sorting by this makes a tornado chart."""
        return abs(self.high_value_usd - self.low_value_usd)

    @property
    def swing_pct(self) -> float:
        if self.base_value_usd == 0:
            return 0.0
        return self.swing_usd / abs(self.base_value_usd)


class SensitivityReport(BaseModel):
    """One-at-a-time sensitivity across all of a method's drivers."""

    method_id: str
    base_value_usd: float
    rows: list[SensitivityRow]

    @property
    def ranked_rows(self) -> list[SensitivityRow]:
        """Drivers ordered by influence -- the tornado ordering."""
        return sorted(self.rows, key=lambda r: r.swing_usd, reverse=True)

    @property
    def dominant_driver(self) -> SensitivityRow | None:
        ranked = self.ranked_rows
        return ranked[0] if ranked else None


class MethodResult(BaseModel):
    """What one valuation method concluded, with its full audit trail attached."""

    method_id: str
    method_name: str
    equity_value_usd: float
    enterprise_value_usd: float | None = None
    value_range: ValuationRange
    narrative: str = Field(description="Plain-English summary of how this was derived.")
    trail: AuditTrail
    sensitivity: SensitivityReport | None = None


class MethodSkip(BaseModel):
    """A method that was requested or eligible but could not run.

    Recorded rather than silently dropped: "we did not run a DCF" is itself an
    audit finding, and the reason belongs in the workpaper.
    """

    model_config = ConfigDict(frozen=True)

    method_id: str
    method_name: str
    reason: str
    error_type: str


class ConcordanceReport(BaseModel):
    """Do the methods agree?

    Independent methods landing in the same place is corroborating evidence.
    Methods landing far apart is a finding that needs explaining before anyone
    books a number -- so the spread is computed and classified rather than left
    for a reader to eyeball.
    """

    model_config = ConfigDict(frozen=True)

    values_by_method: dict[str, float]
    mean_usd: float
    spread_usd: float = Field(description="Max minus min across method point estimates.")
    coefficient_of_variation: float = Field(description="Std dev / mean; unitless dispersion.")
    agreement: Literal["single-method", "tight", "moderate", "wide"]
    commentary: str


class ValuationReport(BaseModel):
    """The complete, self-contained deliverable for one company at one date."""

    run_id: str
    company_name: str
    sector: str
    as_of: date
    concluded_value_usd: float
    concluded_range: ValuationRange
    method_results: list[MethodResult]
    skipped_methods: list[MethodSkip] = Field(default_factory=list)
    concordance: ConcordanceReport
    narrative: str
    engine_trail: AuditTrail
    fingerprint: str = Field(description="SHA-256 over every trail in this report.")

    @property
    def all_sources(self) -> list[SourceRef]:
        """Every source cited anywhere in the report, deduplicated."""
        seen: dict[tuple[str, str, date], SourceRef] = {}
        for trail in [self.engine_trail, *(r.trail for r in self.method_results)]:
            for src in trail.sources:
                seen.setdefault((src.provider, src.dataset, src.as_of), src)
        return list(seen.values())

    @property
    def all_warnings(self) -> list[str]:
        warnings = list(self.engine_trail.warnings)
        for result in self.method_results:
            warnings.extend(f"[{result.method_id}] {w}" for w in result.trail.warnings)
        return warnings
