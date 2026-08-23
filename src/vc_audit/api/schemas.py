"""HTTP request and response shapes.

Requests reuse the domain models directly, so the API validates on exactly the
same rules as the CLI and the OpenAPI schema at ``/docs`` is generated from the
same definitions the engine runs on. There is no second source of truth about
what a valid company record is.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from vc_audit.domain.models import PortfolioCompany


class ValuationRequest(BaseModel):
    """A request to value one company."""

    company: PortfolioCompany
    as_of: date | None = Field(
        default=None,
        description="Valuation date. Defaults to today when omitted.",
    )
    methods: list[str] | None = Field(
        default=None,
        description="Restrict to these method ids. Omit to run everything the data supports.",
    )
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Assumption overrides, keyed bare ('wacc') or qualified ('dcf.wacc').",
    )
    run_sensitivity: bool = True
    persist: bool = Field(
        default=True,
        description="Write an evidence pack so the run can be reopened by run_id.",
    )


class MethodInfo(BaseModel):
    """Metadata about one registered valuation method."""

    id: str
    name: str
    summary: str
    required_inputs: list[str]
    default_weight: float
    weight_rationale: str
    drivers: list[str]


class RunSummary(BaseModel):
    """One row in the archive listing."""

    run_id: str
    company_name: str
    as_of: date


class PeerInfo(BaseModel):
    """A public comparable, with its derived multiple."""

    ticker: str
    name: str
    sector: str
    market_cap_usd: float
    enterprise_value_usd: float
    ltm_revenue_usd: float
    ev_to_revenue: float


class PeerScreenResponse(BaseModel):
    """Result of a standalone comparability screen."""

    sector: str
    peers: list[PeerInfo]
    citation: str


class ErrorResponse(BaseModel):
    """A failure an auditor can act on."""

    error: str
    detail: str
