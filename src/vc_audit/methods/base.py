"""The valuation method contract.

A method is a plugin. It declares what inputs it needs, what judgement calls it
exposes for sensitivity testing, and how to turn the former into a value while
writing every step into the audit trail. Adding a fourth methodology means
adding one file and one registry entry -- no changes to the engine, the
reporting layer, the CLI, or the API.

The base class owns the parts every method must do identically (preflight,
opening the trail, assembling the result) so that a method implementation
contains arithmetic and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from vc_audit.context import ValuationContext
from vc_audit.domain.errors import MissingInputError
from vc_audit.domain.models import (
    ExcludedPeer,
    MethodResult,
    PeerCompany,
    PeerFunnel,
    PortfolioCompany,
    ValuationRange,
)


class DriverSpec(BaseModel):
    """Declaration of an assumption that sensitivity analysis should perturb.

    Declarative rather than imperative: the method says *which* assumption is
    worth stressing and by how much, and :mod:`vc_audit.sensitivity` resolves
    the base value from the recorded trail and re-runs. No method contains any
    sensitivity logic.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Assumption key, matching the ctx.assume() call.")
    label: str
    unit: Literal["percent", "multiple", "usd", "ratio"] = "percent"
    delta: float = Field(gt=0, description="Perturbation magnitude.")
    mode: Literal["absolute", "relative"] = "absolute"
    min_value: float | None = None
    max_value: float | None = None

    def bounds(self, base: float) -> tuple[float, float]:
        """Resolve low/high input values around ``base``, respecting clamps."""
        if self.mode == "relative":
            low, high = base * (1 - self.delta), base * (1 + self.delta)
        else:
            low, high = base - self.delta, base + self.delta
        if self.min_value is not None:
            low, high = max(low, self.min_value), max(high, self.min_value)
        if self.max_value is not None:
            low, high = min(low, self.max_value), min(high, self.max_value)
        return low, high


@dataclass
class MethodOutcome:
    """What a method's arithmetic produces, before the trail is attached."""

    equity_value_usd: float
    value_range: ValuationRange
    narrative: str
    enterprise_value_usd: float | None = None
    #: Comparables evidence, for methods that value against a peer set.
    peers: list[PeerCompany] = field(default_factory=list)
    excluded_peers: list[ExcludedPeer] = field(default_factory=list)
    funnel: PeerFunnel | None = None


class ValuationMethod(ABC):
    """Base class for every valuation methodology."""

    id: ClassVar[str]
    name: ClassVar[str]
    summary: ClassVar[str]
    required_inputs: ClassVar[frozenset[str]]
    #: Relative confidence when blending methods. See ``engine.conclude`` for
    #: how these are normalised over whichever methods actually ran.
    default_weight: ClassVar[float] = 1.0
    weight_rationale: ClassVar[str] = ""

    # ---- contract --------------------------------------------------------

    @abstractmethod
    def compute(self, company: PortfolioCompany, ctx: ValuationContext) -> MethodOutcome:
        """Do the valuation, recording every step on ``ctx.trail``."""

    @abstractmethod
    def drivers(self) -> list[DriverSpec]:
        """Assumptions worth stressing in sensitivity analysis."""

    # ---- shared machinery ------------------------------------------------

    def missing_inputs(self, company: PortfolioCompany) -> set[str]:
        """Required inputs this company does not supply."""
        return set(self.required_inputs) - company.available_inputs()

    def is_eligible(self, company: PortfolioCompany) -> bool:
        return not self.missing_inputs(company)

    def preflight(self, company: PortfolioCompany) -> None:
        """Fail fast and specifically before any arithmetic runs.

        Raises:
            MissingInputError: A required input is absent.
        """
        missing = self.missing_inputs(company)
        if missing:
            raise MissingInputError(self.id, sorted(missing))

    def run(self, company: PortfolioCompany, ctx: ValuationContext) -> MethodResult:
        """Validate, compute, and package the result with its audit trail."""
        self.preflight(company)
        outcome = self.compute(company, ctx)
        return MethodResult(
            method_id=self.id,
            method_name=self.name,
            equity_value_usd=outcome.equity_value_usd,
            enterprise_value_usd=outcome.enterprise_value_usd,
            value_range=outcome.value_range,
            narrative=outcome.narrative,
            trail=ctx.trail,
            peers=outcome.peers,
            excluded_peers=outcome.excluded_peers,
            funnel=outcome.funnel,
        )


# --------------------------------------------------------------------------
# Shared calculation helpers
# --------------------------------------------------------------------------


def bridge_to_equity(
    enterprise_value_usd: float,
    company: PortfolioCompany,
    ctx: ValuationContext,
) -> float:
    """Convert enterprise value to equity value, recording the bridge.

    Shared by every method that values the *business* rather than the *shares*,
    so the bridge is written and reviewed once. Equity = EV - debt + cash.
    """
    equity = enterprise_value_usd - company.debt_usd + company.cash_usd
    return ctx.trail.record(
        label="equity_bridge",
        description=(
            "Bridge enterprise value to equity value by deducting debt and adding cash."
        ),
        formula=(
            f"${enterprise_value_usd:,.0f} - ${company.debt_usd:,.0f} "
            f"+ ${company.cash_usd:,.0f}"
        ),
        inputs={
            "enterprise_value_usd": enterprise_value_usd,
            "debt_usd": company.debt_usd,
            "cash_usd": company.cash_usd,
        },
        output=equity,
        unit="usd",
    )


def apply_range(point: float, *, down: float, up: float) -> ValuationRange:
    """Build a range by flexing a point estimate down and up.

    A last resort for methods with no natural distribution of their own. Methods
    that *do* have one -- comps has a peer quartile spread -- should use it
    instead, because an empirical range is evidence and a flexed one is only a
    convention.
    """
    return ValuationRange(
        low_usd=point * (1 - down),
        point_usd=point,
        high_usd=point * (1 + up),
    )
