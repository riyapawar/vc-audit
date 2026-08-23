"""Shared fixtures.

Every test runs against a fixed valuation date. Nothing in the test suite may
depend on the wall clock, for the same reason nothing in the engine may: a test
that passes today and fails in March is worse than no test.
"""

from __future__ import annotations

from datetime import date

import pytest

from vc_audit.context import ValuationContext
from vc_audit.data.mock_provider import MockMarketDataProvider
from vc_audit.domain.audit import AuditTrail, SourceRef
from vc_audit.domain.models import FinancialProjection, PortfolioCompany

AS_OF = date(2026, 8, 22)


@pytest.fixture
def provider() -> MockMarketDataProvider:
    return MockMarketDataProvider()


@pytest.fixture
def projections() -> list[FinancialProjection]:
    return [
        FinancialProjection(
            year=2026,
            revenue_usd=14_000_000,
            ebit_usd=-1_500_000,
            depreciation_amortization_usd=600_000,
            capex_usd=900_000,
            change_in_nwc_usd=400_000,
        ),
        FinancialProjection(
            year=2027,
            revenue_usd=20_000_000,
            ebit_usd=1_200_000,
            depreciation_amortization_usd=800_000,
            capex_usd=1_100_000,
            change_in_nwc_usd=550_000,
        ),
        FinancialProjection(
            year=2028,
            revenue_usd=28_000_000,
            ebit_usd=4_500_000,
            depreciation_amortization_usd=1_000_000,
            capex_usd=1_400_000,
            change_in_nwc_usd=700_000,
        ),
        FinancialProjection(
            year=2029,
            revenue_usd=37_000_000,
            ebit_usd=8_200_000,
            depreciation_amortization_usd=1_200_000,
            capex_usd=1_700_000,
            change_in_nwc_usd=800_000,
        ),
        FinancialProjection(
            year=2030,
            revenue_usd=46_000_000,
            ebit_usd=12_400_000,
            depreciation_amortization_usd=1_400_000,
            capex_usd=2_000_000,
            change_in_nwc_usd=900_000,
        ),
    ]


@pytest.fixture
def company(projections) -> PortfolioCompany:
    """A company with complete data, so every method is eligible."""
    return PortfolioCompany(
        name="Basis AI",
        sector="saas",
        ltm_revenue_usd=10_000_000,
        ltm_ebitda_usd=-2_000_000,
        cash_usd=18_000_000,
        debt_usd=4_000_000,
        last_post_money_valuation_usd=120_000_000,
        last_round_date=date(2022, 3, 31),
        last_round_series="Series B",
        projections=projections,
        projections_source=SourceRef(
            provider="internal", dataset="board_pack_q3_2025", as_of=date(2025, 9, 30)
        ),
    )


@pytest.fixture
def sparse_company() -> PortfolioCompany:
    """Only a priced round: exercises the method-skipping path."""
    return PortfolioCompany(
        name="Northwind Labs",
        sector="healthtech",
        cash_usd=9_500_000,
        last_post_money_valuation_usd=45_000_000,
        last_round_date=date(2021, 11, 30),
        last_round_series="Series A",
    )


def make_context(
    provider: MockMarketDataProvider,
    method_id: str,
    *,
    as_of: date = AS_OF,
    overrides: dict | None = None,
) -> ValuationContext:
    """Build a standalone context for exercising one method in isolation."""
    trail = AuditTrail(run_id="test-run", as_of=as_of, scope=f"method:{method_id}")
    return ValuationContext(
        as_of=as_of,
        provider=provider,
        trail=trail,
        method_id=method_id,
        overrides=overrides or {},
    )
