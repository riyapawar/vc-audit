"""Orchestration: pick the methods, run them, reconcile the answers.

The engine is deliberately thin. It knows how to choose methods for a company,
how to survive one of them failing, and how to combine what survives into a
single conclusion. It knows nothing about how any individual valuation works.

The reconciliation step is the part worth reading. Running three methods and
quoting the average is not analysis; the useful signal is whether independent
methods *agree*. Convergence is corroboration. Divergence is a finding that
needs an explanation before anyone books a number -- so the engine measures the
dispersion, classifies it, and says so in the report rather than averaging the
disagreement away.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Sequence
from datetime import date
from typing import Any

from vc_audit.context import ValuationContext
from vc_audit.data.base import MarketDataProvider
from vc_audit.domain.audit import AuditTrail
from vc_audit.domain.errors import FatalError, RecoverableError
from vc_audit.domain.models import (
    ConcordanceReport,
    MethodResult,
    MethodSkip,
    PortfolioCompany,
    ValuationRange,
    ValuationReport,
)
from vc_audit.methods.base import ValuationMethod
from vc_audit.methods.registry import all_methods, eligible_methods, get_method
from vc_audit.sensitivity import analyse

#: Coefficient-of-variation bands for classifying cross-method agreement.
TIGHT_CV = 0.10
MODERATE_CV = 0.25


def value_company(
    company: PortfolioCompany,
    *,
    provider: MarketDataProvider,
    as_of: date,
    methods: Sequence[str] | None = None,
    overrides: dict[str, Any] | None = None,
    run_sensitivity: bool = True,
    researcher: Any | None = None,
) -> ValuationReport:
    """Produce a complete, auditable fair value estimate.

    Args:
        company: The subject company.
        provider: Market data source.
        as_of: Valuation date. Injected rather than defaulted to today so that
            re-running an archived valuation reproduces it exactly.
        methods: Method ids to attempt. ``None`` runs every method the
            company's data supports.
        overrides: Assumption overrides, keyed bare (``wacc``) or qualified
            (``dcf.wacc``).
        run_sensitivity: Whether to stress each method's drivers.
        researcher: Optional model-driven peer selection. ``None`` keeps the
            run fully deterministic; see :mod:`vc_audit.research`.

    Returns:
        A :class:`ValuationReport` carrying the conclusion and every trail
        behind it.

    Raises:
        FatalError: No method could produce a value, or an assumption set was
            internally inconsistent.
        ValueError: An explicitly requested method id does not exist.
    """
    overrides = dict(overrides or {})
    trail = AuditTrail.start(
        as_of=as_of,
        scope="engine",
        seed={
            "company": company.model_dump(mode="json"),
            "as_of": as_of.isoformat(),
            "methods": sorted(methods) if methods else None,
            "overrides": overrides,
            "researcher": getattr(researcher, "name", None),
        },
    )
    base_ctx = ValuationContext(
        as_of=as_of,
        provider=provider,
        trail=trail,
        overrides=overrides,
        researcher=researcher,
    )

    _record_data_sources(provider, trail)
    _warn_if_research_cannot_land(provider, researcher, trail)
    selected, skipped = _select_methods(company, methods, trail)
    results, run_skips = _run_methods(
        selected, company, base_ctx, trail, run_sensitivity=run_sensitivity
    )
    skipped.extend(run_skips)
    _record_data_degradations(provider, trail)

    if not results:
        reasons = "; ".join(f"{s.method_id}: {s.reason}" for s in skipped) or "no methods selected"
        raise FatalError(f"no valuation method could be applied to '{company.name}' -- {reasons}")

    concluded, weights = _conclude(results, trail)
    concluded_range = _combine_ranges(results, concluded, trail)
    concordance = _assess_concordance(results, trail)
    _queue_unreviewed_assumptions(results, trail)

    narrative = _narrate(company, results, weights, concluded, concordance, as_of)

    return ValuationReport(
        run_id=trail.run_id,
        company_name=company.name,
        sector=company.sector,
        as_of=as_of,
        concluded_value_usd=concluded,
        concluded_range=concluded_range,
        method_results=results,
        skipped_methods=skipped,
        concordance=concordance,
        narrative=narrative,
        engine_trail=trail,
        fingerprint=_fingerprint(trail, results),
    )


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _record_data_sources(provider: MarketDataProvider, trail: AuditTrail) -> None:
    """Open the trail by naming where the market data comes from.

    First question a reviewer asks of any figure sourced outside the company.
    """
    trail.record(
        label="data_sources",
        description="Identify the market data sources this run drew on.",
        formula="provider configuration",
        inputs={"provider": provider.name},
        output=provider.describe(),
    )


def _warn_if_research_cannot_land(
    provider: MarketDataProvider, researcher: Any | None, trail: AuditTrail
) -> None:
    """Flag the one provider and researcher combination that quietly does nothing.

    The research layer proposes real tickers; the fixture universe contains
    invented ones. Pointing one at the other costs an API call and changes
    nothing, and the only visible symptom is a peer set that ignored every
    proposal. Better to say so than to let a reviewer conclude the model was
    consulted when in practice it was not.
    """
    if researcher is None or not getattr(provider, "synthetic_universe", False):
        return
    trail.warn(
        "Peer research ran against the synthetic fixture universe, so its proposed "
        "tickers could not resolve and did not affect the peer set. Use live data "
        "for research to have any effect."
    )


def _record_data_degradations(provider: MarketDataProvider, trail: AuditTrail) -> None:
    """Surface any silent substitution the provider made during the run.

    A resilient provider may fall back from live filings to fixtures. That is
    the right behaviour, but it must never be invisible: fixture figures
    presented with the authority of filed ones would be the worst outcome
    available. Read defensively, since not every provider degrades at all.
    """
    degradations: list[str] = list(getattr(provider, "degradations", []))
    if not degradations:
        return
    trail.record(
        label="data_degradation",
        description=(
            "Record where a live data source was unavailable and fixture data was "
            "substituted."
        ),
        formula="fallbacks taken during this run",
        inputs={"count": len(degradations)},
        output=degradations,
    )
    for message in degradations:
        trail.warn(message)


def _select_methods(
    company: PortfolioCompany,
    requested: Sequence[str] | None,
    trail: AuditTrail,
) -> tuple[list[ValuationMethod], list[MethodSkip]]:
    """Decide which methods to attempt, recording why each was in or out."""
    skips: list[MethodSkip] = []

    if requested is None:
        selected = eligible_methods(company)
        ineligible = [m for m in all_methods() if m not in selected]
    else:
        # get_method raises on an unknown id: a typo in a method name should
        # stop the run, not silently narrow the analysis.
        asked = [get_method(mid) for mid in requested]
        selected = [m for m in asked if m.is_eligible(company)]
        ineligible = [m for m in asked if not m.is_eligible(company)]

    for method in ineligible:
        missing = ", ".join(sorted(method.missing_inputs(company)))
        skips.append(
            MethodSkip(
                method_id=method.id,
                method_name=method.name,
                reason=f"company record does not supply {missing}",
                error_type="MissingInputError",
            )
        )

    trail.record(
        label="method_selection",
        description=(
            "Select the valuation methods the available data supports. Methods that "
            "cannot run are recorded with their reason rather than dropped silently."
        ),
        formula="methods whose required inputs are all present on the company record",
        inputs={
            "available_inputs": sorted(company.available_inputs()),
            "requested": list(requested) if requested else "all",
        },
        output={
            "selected": [m.id for m in selected],
            "skipped": {s.method_id: s.reason for s in skips},
        },
    )
    return selected, skips


def _run_methods(
    selected: list[ValuationMethod],
    company: PortfolioCompany,
    base_ctx: ValuationContext,
    trail: AuditTrail,
    *,
    run_sensitivity: bool,
) -> tuple[list[MethodResult], list[MethodSkip]]:
    """Run each method in isolation so one failure cannot take down the rest.

    Recoverable failures (a data outage, too few peers) skip the method and are
    reported. Fatal failures -- an internally inconsistent assumption set --
    propagate, because continuing would mean publishing a conclusion drawn from
    a model the engine knows to be invalid.
    """
    results: list[MethodResult] = []
    skips: list[MethodSkip] = []

    for method in selected:
        method_trail = trail.child(f"method:{method.id}")
        ctx = base_ctx.for_method(method.id, method_trail)
        try:
            result = method.run(company, ctx)
        except RecoverableError as exc:
            skips.append(
                MethodSkip(
                    method_id=method.id,
                    method_name=method.name,
                    reason=str(exc),
                    error_type=type(exc).__name__,
                )
            )
            trail.warn(f"{method.name} could not be applied: {exc}")
            continue

        if run_sensitivity:
            result.sensitivity = analyse(method, company, ctx, result)
        results.append(result)

    return results, skips


def _conclude(results: list[MethodResult], trail: AuditTrail) -> tuple[float, dict[str, float]]:
    """Blend method results into a single value using normalised weights.

    Weights express relative confidence in each methodology and are declared on
    the methods themselves. They are renormalised over whichever methods
    actually ran, so a skipped method redistributes its weight rather than
    quietly dragging the conclusion toward zero.
    """
    raw = {r.method_id: _weight_for(r.method_id) for r in results}
    total = sum(raw.values())
    weights = {mid: w / total for mid, w in raw.items()}

    concluded = sum(r.equity_value_usd * weights[r.method_id] for r in results)

    trail.record(
        label="weighted_conclusion",
        description=(
            "Blend the method results using confidence weights, renormalised over the "
            "methods that actually ran."
        ),
        formula=" + ".join(
            f"${r.equity_value_usd:,.0f} * {weights[r.method_id]:.0%}" for r in results
        ),
        inputs={
            "method_values_usd": {r.method_id: round(r.equity_value_usd, 2) for r in results},
            "weights": {mid: round(w, 4) for mid, w in weights.items()},
            "weight_rationale": {
                r.method_id: get_method(r.method_id).weight_rationale for r in results
            },
        },
        output=concluded,
        unit="usd",
    )
    return concluded, weights


def _weight_for(method_id: str) -> float:
    return get_method(method_id).default_weight


def _combine_ranges(
    results: list[MethodResult], concluded: float, trail: AuditTrail
) -> ValuationRange:
    """Span every method's range, so the reported range hides no method's view."""
    low = min(r.value_range.low_usd for r in results)
    high = max(r.value_range.high_usd for r in results)

    trail.record(
        label="combined_range",
        description=(
            "Take the union of the individual method ranges. A narrower combined range "
            "would suppress a method's stated uncertainty."
        ),
        formula=(
            f"min(low) = ${low:,.0f}, max(high) = ${high:,.0f} across "
            f"{len(results)} method(s)"
        ),
        inputs={
            r.method_id: {
                "low_usd": round(r.value_range.low_usd, 2),
                "high_usd": round(r.value_range.high_usd, 2),
            }
            for r in results
        },
        output={"low_usd": round(low, 2), "high_usd": round(high, 2)},
        unit="usd",
    )
    return ValuationRange(
        low_usd=min(low, concluded), point_usd=concluded, high_usd=max(high, concluded)
    )


def _assess_concordance(results: list[MethodResult], trail: AuditTrail) -> ConcordanceReport:
    """Measure and classify how far the methods disagree."""
    values = {r.method_id: r.equity_value_usd for r in results}
    points = list(values.values())
    mean = statistics.fmean(points)
    spread = max(points) - min(points)

    if len(points) == 1:
        cv = 0.0
        agreement = "single-method"
        commentary = (
            "Only one methodology could be applied, so there is no corroborating "
            "evidence. The conclusion rests entirely on that method's assumptions."
        )
    else:
        cv = statistics.stdev(points) / mean if mean else 0.0
        if cv <= TIGHT_CV:
            agreement = "tight"
            commentary = (
                f"The {len(points)} methods agree closely (coefficient of variation "
                f"{cv:.1%}). Independent approaches converging is meaningful "
                f"corroboration of the estimate."
            )
        elif cv <= MODERATE_CV:
            agreement = "moderate"
            commentary = (
                f"The methods differ moderately (coefficient of variation {cv:.1%}), "
                f"which is normal for a private company. The weighted conclusion is "
                f"reasonable, but the range should be quoted alongside it."
            )
        else:
            agreement = "wide"
            commentary = (
                f"The methods disagree materially (coefficient of variation {cv:.1%}, "
                f"spread ${spread:,.0f}). Reconcile the drivers behind the divergence "
                f"before relying on the blended figure -- a weighted average of "
                f"conflicting evidence is not itself evidence."
            )
            trail.warn(
                f"Cross-method dispersion is wide (CV {cv:.1%}); the blended conclusion "
                f"should not be booked without explaining the divergence."
            )

    trail.record(
        label="concordance",
        description="Measure dispersion across the independent method conclusions.",
        formula=(
            "stdev(method values) / mean(method values)" if len(points) > 1 else "single method"
        ),
        inputs={mid: round(v, 2) for mid, v in values.items()},
        output={
            "mean_usd": round(mean, 2),
            "spread_usd": round(spread, 2),
            "coefficient_of_variation": round(cv, 4),
            "agreement": agreement,
        },
    )
    return ConcordanceReport(
        values_by_method=values,
        mean_usd=mean,
        spread_usd=spread,
        coefficient_of_variation=cv,
        agreement=agreement,
        commentary=commentary,
    )


def _queue_unreviewed_assumptions(results: list[MethodResult], trail: AuditTrail) -> None:
    """List the assumptions the tool chose that nobody has signed off on.

    This is the review queue: everything the engine defaulted because the
    auditor did not specify it. Presenting it explicitly is the difference
    between a tool that hides its assumptions and one that hands them over.
    """
    queue = {}
    for result in results:
        for assumption in result.trail.unreviewed_defaults():
            queue.setdefault(f"{result.method_id}.{assumption.key}", assumption.display_value())

    trail.record(
        label="assumption_review_queue",
        description=(
            "Assumptions applied from engine defaults rather than auditor input. These "
            "are the inputs still requiring sign-off."
        ),
        formula="assumptions where origin == 'engine_default'",
        inputs={"count": len(queue)},
        output=queue,
    )
    if queue:
        trail.warn(
            f"{len(queue)} assumption(s) were applied from engine defaults and have not "
            f"been reviewed: {', '.join(sorted(queue))}."
        )


def _narrate(company, results, weights, concluded, concordance, as_of) -> str:
    """Compose the report-level summary an auditor reads first."""
    contributions = ", ".join(
        f"{r.method_name} ${r.equity_value_usd:,.0f} ({weights[r.method_id]:.0%})"
        for r in results
    )
    return (
        f"Estimated fair value of {company.name} at {as_of.isoformat()} is "
        f"${concluded:,.0f}, derived from {len(results)} independent "
        f"method{'s' if len(results) != 1 else ''}: {contributions}. "
        f"{concordance.commentary}"
    )


def _fingerprint(trail: AuditTrail, results: list[MethodResult]) -> str:
    """Hash every trail in the report into one tamper-evident digest.

    Composed from the per-trail fingerprints rather than from the assembled
    report, so a reviewer can localise a mismatch to a single method instead of
    only learning that something, somewhere, changed.
    """
    digest = hashlib.sha256()
    digest.update(trail.fingerprint().encode("utf-8"))
    for result in sorted(results, key=lambda r: r.method_id):
        digest.update(result.method_id.encode("utf-8"))
        digest.update(result.trail.fingerprint().encode("utf-8"))
    return digest.hexdigest()
