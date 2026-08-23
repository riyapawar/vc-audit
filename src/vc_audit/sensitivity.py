"""One-at-a-time sensitivity analysis.

A point estimate invites false confidence. This module answers the question a
reviewer actually asks -- "how much of this number is the assumption?" -- by
re-running each method with one driver moved at a time and reporting the swing.
Ranking the results gives a tornado ordering: the driver at the top is the one
worth spending review time on.

The module contains no knowledge of any specific methodology. It works because
methods declare their drivers (:class:`~vc_audit.methods.base.DriverSpec`) and
read every assumption through :meth:`~vc_audit.context.ValuationContext.assume`,
so perturbing an assumption is nothing more than re-running with a different
override map.

Perturbed runs write into a throwaway trail. Hypothetical arithmetic must never
appear in the workpaper next to the arithmetic that produced the booked number.
"""

from __future__ import annotations

from vc_audit.context import ValuationContext
from vc_audit.domain.audit import AuditTrail
from vc_audit.domain.errors import VcAuditError
from vc_audit.domain.models import (
    MethodResult,
    PortfolioCompany,
    SensitivityReport,
    SensitivityRow,
)
from vc_audit.methods.base import DriverSpec, ValuationMethod


def _recorded_value(trail: AuditTrail, key: str) -> float | None:
    """Find the value a completed run actually used for ``key``.

    Reads from the trail rather than from the method's defaults, so an auditor
    override is stressed around *their* value, not around ours.
    """
    for assumption in reversed(trail.assumptions):
        if assumption.key == key:
            value = assumption.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            return None
    return None


def _revalue(
    method: ValuationMethod,
    company: PortfolioCompany,
    base_ctx: ValuationContext,
    key: str,
    value: float,
) -> float | None:
    """Re-run one method with a single driver overridden. ``None`` if it cannot run.

    The override is method-qualified so a shared key (``illiquidity_discount``,
    say) is stressed only within the method under test.
    """
    scratch = AuditTrail(
        run_id=base_ctx.trail.run_id,
        as_of=base_ctx.as_of,
        scope=f"sensitivity:{method.id}:{key}",
    )
    ctx = base_ctx.with_overrides({f"{method.id}.{key}": value}, scratch)
    try:
        return method.compute(company, ctx).equity_value_usd
    except VcAuditError:
        # A perturbation can be individually invalid -- pushing WACC below the
        # terminal growth rate, or narrowing the size band until too few peers
        # survive. That is information about the driver, not a failure of the run.
        return None


def _row(
    method: ValuationMethod,
    company: PortfolioCompany,
    base_ctx: ValuationContext,
    base_result: MethodResult,
    spec: DriverSpec,
) -> SensitivityRow | None:
    """Build one tornado row, or ``None`` if the driver cannot be stressed."""
    base = _recorded_value(base_result.trail, spec.key)
    if base is None:
        base_result.trail.warn(
            f"Sensitivity skipped for '{spec.key}': the base run recorded no numeric "
            f"value for it."
        )
        return None

    low_input, high_input = spec.bounds(base)
    low_value = _revalue(method, company, base_ctx, spec.key, low_input)
    high_value = _revalue(method, company, base_ctx, spec.key, high_input)

    if low_value is None or high_value is None:
        base_result.trail.warn(
            f"Sensitivity incomplete for '{spec.label}': the model does not solve at "
            f"one or both bounds ({low_input:.4g} / {high_input:.4g})."
        )
        return None

    return SensitivityRow(
        driver_key=spec.key,
        label=spec.label,
        unit=spec.unit,
        low_input=low_input,
        high_input=high_input,
        low_value_usd=low_value,
        high_value_usd=high_value,
        base_value_usd=base_result.equity_value_usd,
    )


def analyse(
    method: ValuationMethod,
    company: PortfolioCompany,
    base_ctx: ValuationContext,
    base_result: MethodResult,
) -> SensitivityReport | None:
    """Stress every driver a method declares.

    Returns ``None`` when no driver could be stressed, so callers can omit the
    section entirely rather than print an empty table.
    """
    rows = [
        row
        for spec in method.drivers()
        if (row := _row(method, company, base_ctx, base_result, spec)) is not None
    ]
    if not rows:
        return None

    report = SensitivityReport(
        method_id=method.id,
        base_value_usd=base_result.equity_value_usd,
        rows=rows,
    )

    dominant = report.dominant_driver
    if dominant is not None and dominant.swing_pct > 0.5:
        base_result.trail.warn(
            f"'{dominant.label}' alone moves the {method.name} conclusion by "
            f"{dominant.swing_pct:.0%} of its value across the tested band. The estimate "
            f"is driven more by this assumption than by the underlying data."
        )
    return report
