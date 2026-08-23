"""Execution context handed to every valuation method.

The context carries the three things a method is allowed to depend on -- the
frozen valuation date, the market data provider, and the auditor's assumption
overrides -- and exposes exactly one way to read an assumption:
:meth:`ValuationContext.assume`.

Routing every judgement call through a single accessor is what makes two
otherwise-awkward features fall out for free:

* **Assumption provenance.** ``assume`` knows whether a value came from the
  auditor or from an engine default, so it stamps the origin as it records it.
  Nobody has to remember to.
* **Sensitivity analysis.** Perturbing an assumption is just re-running the
  method with a different override map. The sensitivity engine therefore needs
  no knowledge of any particular method -- see :mod:`vc_audit.sensitivity`.

Overrides may be keyed bare (``wacc``, applies to every method that reads it) or
qualified (``dcf.wacc``, applies only within that method). Qualified wins, so an
auditor can set a house-wide default and then override one method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from vc_audit.data.base import MarketDataProvider
from vc_audit.domain.audit import AuditTrail, SourceRef


@dataclass
class ValuationContext:
    """Per-method execution scope."""

    as_of: date
    provider: MarketDataProvider
    trail: AuditTrail
    method_id: str = "engine"
    overrides: dict[str, Any] = field(default_factory=dict)

    def for_method(self, method_id: str, trail: AuditTrail) -> ValuationContext:
        """Derive a context scoped to one method, sharing overrides and provider."""
        return ValuationContext(
            as_of=self.as_of,
            provider=self.provider,
            trail=trail,
            method_id=method_id,
            overrides=self.overrides,
        )

    def with_overrides(self, extra: dict[str, Any], trail: AuditTrail) -> ValuationContext:
        """Copy this context with additional overrides applied.

        Used by the sensitivity sweep to re-run a method with one driver moved.
        The originals are not mutated, so a sweep cannot contaminate the base run.
        """
        return ValuationContext(
            as_of=self.as_of,
            provider=self.provider,
            trail=trail,
            method_id=self.method_id,
            overrides={**self.overrides, **extra},
        )

    # ---- the single assumption accessor ---------------------------------

    def assume(
        self,
        key: str,
        default: Any,
        *,
        rationale: str,
        unit: str | None = None,
        source: SourceRef | None = None,
    ) -> Any:
        """Resolve an assumption, record it with its origin, and return it.

        Args:
            key: Assumption name, e.g. ``"wacc"``.
            default: Value to use when the auditor has not overridden it.
            rationale: Why this default is defensible. Reproduced in the memo,
                so write it for a reviewer, not for a maintainer.
            unit: One of ``percent``, ``multiple``, ``usd``, ``ratio``; controls
                formatting only.
            source: Citation, when the default is drawn from data rather than
                from judgement.
        """
        qualified = f"{self.method_id}.{key}"
        if qualified in self.overrides:
            value, origin = self.overrides[qualified], "user_provided"
        elif key in self.overrides:
            value, origin = self.overrides[key], "user_provided"
        else:
            value, origin = default, "engine_default"

        return self.trail.assume(
            key=key,
            value=value,
            origin=origin,
            rationale=rationale,
            unit=unit,
            source=source,
        )

    def derive(
        self,
        key: str,
        value: Any,
        *,
        rationale: str,
        unit: str | None = None,
        source: SourceRef | None = None,
    ) -> Any:
        """Record a value computed from data rather than chosen, then return it.

        Distinct from :meth:`assume` because a derived figure -- a peer median,
        say -- is not something an auditor can override, and lumping it in with
        judgement calls would inflate the list of things needing sign-off.
        """
        return self.trail.assume(
            key=key,
            value=value,
            origin="derived",
            rationale=rationale,
            unit=unit,
            source=source,
        )

    def is_overridden(self, key: str) -> bool:
        """True when the auditor explicitly supplied this assumption."""
        return key in self.overrides or f"{self.method_id}.{key}" in self.overrides
