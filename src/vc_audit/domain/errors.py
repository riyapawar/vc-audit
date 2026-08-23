"""Typed failure modes.

Every error an auditor could hit is named, so the engine can decide whether a
problem *skips one method* (recoverable) or *invalidates the run* (fatal), and
so the reason lands in the report instead of a stack trace.
"""

from __future__ import annotations


class VcAuditError(Exception):
    """Base class for every error this package raises deliberately."""


class RecoverableError(VcAuditError):
    """A single method cannot run, but the overall valuation can still proceed.

    The engine catches these, records the reason on the report, and continues
    with the remaining methods.
    """


class FatalError(VcAuditError):
    """The run cannot produce a defensible answer and must stop."""


class MissingInputError(RecoverableError):
    """A method's required inputs were absent from the company record."""

    def __init__(self, method_id: str, missing: list[str]) -> None:
        self.method_id = method_id
        self.missing = missing
        super().__init__(
            f"method '{method_id}' requires {', '.join(sorted(missing))}, "
            f"which {'is' if len(missing) == 1 else 'are'} not present on the company record"
        )


class DataUnavailableError(RecoverableError):
    """An upstream data source could not serve the data a method needs."""

    def __init__(self, provider: str, dataset: str, reason: str) -> None:
        self.provider = provider
        self.dataset = dataset
        self.reason = reason
        super().__init__(f"{provider} could not serve '{dataset}': {reason}")


class InsufficientEvidenceError(RecoverableError):
    """Data was available but too thin to support a defensible conclusion.

    Example: only one peer survives the comparability screen, so a "peer median"
    would be a single observation dressed up as a distribution.
    """


class AssumptionError(FatalError):
    """An assumption is internally inconsistent (e.g. terminal growth >= WACC)."""
