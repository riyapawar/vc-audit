"""The audit ledger: the spine of this system.

An auditor's question is never "what is the number?" -- it is "where did the
number come from?". So no valuation code in this package is allowed to compute a
value and simply return it. Every arithmetic operation is written into an
:class:`AuditTrail` as a :class:`Step` carrying its formula, its inputs, its
output, and the sources those inputs came from.

That constraint is enforced ergonomically rather than by policy:
:meth:`AuditTrail.record` *returns* the value it recorded, so the natural way to
write the calculation is also the audited way::

    ev = trail.record(
        label="apply_multiple",
        description="Apply peer median EV/Revenue to subject LTM revenue.",
        formula="8.20x * $10,000,000",
        inputs={"multiple": 8.2, "ltm_revenue_usd": 10_000_000},
        output=82_000_000,
    )

Two properties make the resulting document usable as a workpaper:

* **Reproducibility.** The valuation date is frozen on the trail and injected
  into every method. Nothing in this package calls ``date.today()`` inside a
  calculation, so re-running last quarter's inputs reproduces last quarter's
  answer.
* **Tamper evidence.** :meth:`AuditTrail.fingerprint` hashes the canonical JSON
  form of the whole trail. Identical inputs and identical code produce an
  identical fingerprint, so a reviewer can tell at a glance whether an archived
  memo still matches the run that produced it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Stable namespace for deterministic run ids. This constant must never be
# regenerated: changing it would break the link between archived memos and any
# future re-run of the same inputs.
_RUN_ID_NAMESPACE = uuid.UUID("6f1b6d1e-2a4f-5c9b-9f3a-0f2c7a51d8e4")

AssumptionOrigin = Literal["user_provided", "engine_default", "derived"]


class SourceRef(BaseModel):
    """Provenance for a single piece of input data.

    This is what turns a number into a citation. ``as_of`` is the date the data
    itself describes -- not the date it was fetched -- because that is the date a
    reviewer needs in order to judge whether an input was stale.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(description="System of record, e.g. 'yahoo_finance_mock'.")
    dataset: str = Field(description="Specific table or file within the provider.")
    as_of: date = Field(description="Date the data describes.")
    note: str | None = Field(default=None, description="Caveats a reviewer should see.")

    def cite(self) -> str:
        """Render a one-line citation for the memo."""
        base = f"{self.provider}:{self.dataset} (as of {self.as_of.isoformat()})"
        return f"{base} -- {self.note}" if self.note else base


class Assumption(BaseModel):
    """A judgement call, recorded with its origin and rationale.

    ``origin`` is the field reviewers actually care about: it separates numbers
    the auditor chose (``user_provided``) from numbers the tool chose on their
    behalf (``engine_default``) from numbers computed off other data
    (``derived``). Un-reviewed engine defaults are precisely what a valuation
    review hunts for, so they get their own accessor below.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    value: Any
    origin: AssumptionOrigin
    rationale: str
    unit: str | None = None
    source: SourceRef | None = None

    def display_value(self) -> str:
        """Format the value for human-facing output, respecting ``unit``."""
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            if self.unit == "percent":
                return f"{self.value:.2%}"
            if self.unit == "multiple":
                return f"{self.value:.2f}x"
            if self.unit == "usd":
                return f"${self.value:,.0f}"
        return str(self.value)


class Step(BaseModel):
    """One recorded operation in a calculation.

    ``formula`` is deliberately a human-readable string with the numbers already
    substituted in, not a symbolic expression. A reviewer should be able to
    re-key it into a calculator without consulting anything else.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    label: str = Field(description="Stable machine-readable id, e.g. 'peer_median'.")
    description: str = Field(description="What this step does, in a sentence.")
    formula: str = Field(description="The arithmetic, with values substituted in.")
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    unit: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)


class AuditTrail(BaseModel):
    """An append-only record of how one valuation was produced."""

    run_id: str
    as_of: date = Field(description="Frozen valuation date; injected, never inferred.")
    scope: str = Field(description="What this trail covers, e.g. 'method:dcf'.")
    steps: list[Step] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # ---- construction ----------------------------------------------------

    @classmethod
    def start(cls, *, as_of: date, scope: str, seed: dict[str, Any]) -> AuditTrail:
        """Open a trail whose ``run_id`` is a pure function of its inputs.

        Deterministic ids mean re-running the same valuation lands in the same
        output directory and overwrites it, rather than accumulating near-duplicate
        workpapers that a reviewer then has to reconcile by hand.
        """
        canonical = json.dumps(seed, sort_keys=True, default=str)
        return cls(run_id=str(uuid.uuid5(_RUN_ID_NAMESPACE, canonical)), as_of=as_of, scope=scope)

    def child(self, scope: str) -> AuditTrail:
        """Open a sub-trail (one per method) sharing this run's id and date."""
        return AuditTrail(run_id=self.run_id, as_of=self.as_of, scope=scope)

    # ---- recording -------------------------------------------------------

    def record(
        self,
        *,
        label: str,
        description: str,
        formula: str,
        output: Any,
        inputs: dict[str, Any] | None = None,
        unit: str | None = None,
        sources: list[SourceRef] | None = None,
    ) -> Any:
        """Append a step and return ``output``, so the call reads as an expression."""
        self.steps.append(
            Step(
                seq=len(self.steps) + 1,
                label=label,
                description=description,
                formula=formula,
                inputs=inputs or {},
                output=output,
                unit=unit,
                sources=sources or [],
            )
        )
        return output

    def assume(
        self,
        *,
        key: str,
        value: Any,
        origin: AssumptionOrigin,
        rationale: str,
        unit: str | None = None,
        source: SourceRef | None = None,
    ) -> Any:
        """Register an assumption and return its value."""
        self.assumptions.append(
            Assumption(
                key=key,
                value=value,
                origin=origin,
                rationale=rationale,
                unit=unit,
                source=source,
            )
        )
        return value

    def warn(self, message: str) -> None:
        """Record something a reviewer should look at that does not stop the run."""
        self.warnings.append(message)

    # ---- inspection ------------------------------------------------------

    @property
    def sources(self) -> list[SourceRef]:
        """Every distinct source cited in this trail, in first-seen order."""
        seen: dict[tuple[str, str, date], SourceRef] = {}
        for step in self.steps:
            for src in step.sources:
                seen.setdefault((src.provider, src.dataset, src.as_of), src)
        for assumption in self.assumptions:
            if assumption.source is not None:
                src = assumption.source
                seen.setdefault((src.provider, src.dataset, src.as_of), src)
        return list(seen.values())

    def step(self, label: str) -> Step:
        """Look up a recorded step by label. Raises ``KeyError`` if absent."""
        for candidate in self.steps:
            if candidate.label == label:
                return candidate
        raise KeyError(f"no step labelled '{label}' in trail scope '{self.scope}'")

    def unreviewed_defaults(self) -> list[Assumption]:
        """Assumptions the engine picked that the auditor never explicitly set.

        Surfacing these is the whole point of tracking origin: they are the
        inputs nobody has signed off on yet.
        """
        return [a for a in self.assumptions if a.origin == "engine_default"]

    def fingerprint(self) -> str:
        """SHA-256 over the canonical JSON form of this trail."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
