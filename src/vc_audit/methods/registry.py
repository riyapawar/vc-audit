"""Method registry: the one place that knows which methodologies exist.

The engine, CLI, API and reporting layers all resolve methods through here, so
adding a methodology is a two-line change confined to this file.
"""

from __future__ import annotations

from vc_audit.domain.models import PortfolioCompany
from vc_audit.methods.base import ValuationMethod
from vc_audit.methods.comps import ComparableCompanyAnalysis
from vc_audit.methods.dcf import DiscountedCashFlow
from vc_audit.methods.last_round import LastRoundMarkToMarket

#: Instantiated once. Methods are stateless -- all per-run state lives on the
#: ValuationContext -- so sharing instances is safe and keeps the registry flat.
_REGISTERED: tuple[ValuationMethod, ...] = (
    ComparableCompanyAnalysis(),
    DiscountedCashFlow(),
    LastRoundMarkToMarket(),
)

METHODS: dict[str, ValuationMethod] = {method.id: method for method in _REGISTERED}


def all_methods() -> list[ValuationMethod]:
    """Every registered method, in a stable order."""
    return list(_REGISTERED)


def get_method(method_id: str) -> ValuationMethod:
    """Resolve a method by id.

    Raises:
        ValueError: No such method, listing what is available.
    """
    try:
        return METHODS[method_id]
    except KeyError:
        available = ", ".join(sorted(METHODS))
        raise ValueError(f"unknown method '{method_id}'; available: {available}") from None


def eligible_methods(company: PortfolioCompany) -> list[ValuationMethod]:
    """Methods whose required inputs this company actually supplies."""
    return [method for method in _REGISTERED if method.is_eligible(company)]


def eligibility_report(company: PortfolioCompany) -> dict[str, set[str]]:
    """Map each method id to the inputs it is missing. Empty set means eligible."""
    return {method.id: method.missing_inputs(company) for method in _REGISTERED}
