"""Tests for SEC XBRL extraction.

This is the subtlest code in the project. XBRL is not a clean data source:
filers migrate between tags, periods overlap, and a naive read produces numbers
that look plausible and are wrong. These tests pin the specific failure modes
that were found by inspecting real filings.

Everything runs against in-memory payloads -- no network.
"""

from __future__ import annotations

from datetime import date

import pytest

from vc_audit.data.sec import MAX_BALANCE_SHEET_AGE_DAYS, SECClient
from vc_audit.domain.errors import DataUnavailableError

AS_OF = date(2026, 8, 21)


def instant(tag: str, end: str, val: float, form: str = "10-Q") -> dict:
    return {"end": end, "val": val, "form": form, "accn": f"acc-{end}"}


def duration(start: str, end: str, val: float, form: str = "10-Q") -> dict:
    return {"start": start, "end": end, "val": val, "form": form, "accn": f"acc-{end}"}


def facts(**tags: dict) -> dict:
    """Build a companyfacts payload from ``tag=[entries]`` pairs."""
    gaap, dei = {}, {}
    for tag, entries in tags.items():
        unit = "shares" if tag == "EntityCommonStockSharesOutstanding" else "USD"
        target = dei if unit == "shares" else gaap
        target[tag] = {"units": {unit: entries}}
    return {"facts": {"us-gaap": gaap, "dei": dei}}


@pytest.fixture
def client(monkeypatch):
    """A client whose HTTP layer is replaced by a stub registry."""
    sec = SECClient("test-agent test@example.com")
    sec._cik_map = {"ACME": ("0000000001", "Acme Corp"), "GONE": ("0000000002", "Gone Inc")}
    return sec


def load(client, payload: dict, cik: str = "0000000001") -> None:
    client._facts_cache[cik] = payload


class TestTickerResolution:
    def test_resolves_to_the_registrant_name_from_edgar(self, client):
        assert client.resolve("acme") == ("0000000001", "Acme Corp")

    def test_an_unregistered_ticker_is_a_typed_failure(self, client):
        with pytest.raises(DataUnavailableError, match="not a registered filer"):
            client.resolve("NOPE")


class TestLtmRevenue:
    def test_prefers_four_non_overlapping_quarters(self, client):
        load(client, facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
            duration("2025-08-01", "2025-10-31", 100),
            duration("2025-11-01", "2026-01-31", 110),
            duration("2026-02-01", "2026-04-30", 120),
            duration("2026-05-01", "2026-07-31", 130),
        ]))
        revenue, basis, tag = client._ltm_revenue(client._company_facts("0000000001"), as_of=AS_OF)

        assert revenue == 460
        assert basis == "trailing four quarters"
        assert tag == "RevenueFromContractWithCustomerExcludingAssessedTax"

    def test_does_not_double_count_overlapping_periods(self, client):
        """A 10-K's 12-month figure spans the same period as its quarters."""
        load(client, facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
            duration("2025-08-01", "2025-10-31", 100),
            duration("2025-11-01", "2026-01-31", 110),
            duration("2025-08-01", "2026-07-31", 460, form="10-K"),
            duration("2026-02-01", "2026-04-30", 120),
            duration("2026-05-01", "2026-07-31", 130),
        ]))
        revenue, _, _ = client._ltm_revenue(client._company_facts("0000000001"), as_of=AS_OF)

        assert revenue == 460, "the annual figure must not be added to its own quarters"

    def test_falls_back_to_the_annual_period_and_says_so(self, client):
        load(client, facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
            duration("2025-02-01", "2026-01-31", 900, form="10-K"),
        ]))
        revenue, basis, _ = client._ltm_revenue(client._company_facts("0000000001"), as_of=AS_OF)

        assert revenue == 900
        assert "latest annual period" in basis

    def test_tries_the_next_tag_when_the_preferred_one_is_absent(self, client):
        load(client, facts(Revenues=[duration("2025-02-01", "2026-01-31", 750, form="10-K")]))
        revenue, _, tag = client._ltm_revenue(client._company_facts("0000000001"), as_of=AS_OF)

        assert (revenue, tag) == (750, "Revenues")

    def test_facts_after_the_valuation_date_are_invisible(self, client):
        """A historical run must not see figures filed after the mark was struck."""
        load(client, facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
            duration("2025-02-01", "2026-01-31", 900, form="10-K"),
            duration("2026-02-01", "2027-01-31", 1500, form="10-K"),
        ]))
        revenue, _, _ = client._ltm_revenue(client._company_facts("0000000001"), as_of=AS_OF)

        assert revenue == 900

    def test_no_revenue_tag_at_all_returns_none(self, client):
        load(client, facts(CashAndCashEquivalentsAtCarryingValue=[instant("c", "2026-04-30", 5)]))
        assert client._ltm_revenue(client._company_facts("0000000001"), as_of=AS_OF) == (
            None,
            None,
            None,
        )


class TestBalanceSheetFreshness:
    def test_the_freshest_tag_wins_over_the_preferred_one(self, client):
        """ServiceNow stopped filing LongTermDebtNoncurrent in 2021 and moved to
        LongTermDebt. First-tag-wins would value a 2026 company on a 2021 balance."""
        load(client, facts(
            LongTermDebtNoncurrent=[instant("d", "2021-09-30", 1_484)],
            LongTermDebt=[instant("d", "2026-06-30", 5_435)],
        ))
        fact = client._latest_instant(
            client._company_facts("0000000001"),
            ("LongTermDebtNoncurrent", "LongTermDebt"),
            as_of=AS_OF,
        )
        assert (fact.tag, fact.value) == ("LongTermDebt", 5_435)

    def test_a_stale_fact_is_treated_as_absent(self, client):
        """Salesforce's last MarketableSecuritiesCurrent fact is from 2014."""
        load(client, facts(MarketableSecuritiesCurrent=[instant("s", "2014-10-31", 79)]))
        fact = client._latest_instant(
            client._company_facts("0000000001"), ("MarketableSecuritiesCurrent",), as_of=AS_OF
        )
        assert fact is None

    def test_the_freshness_window_is_the_documented_one(self, client):
        inside = (AS_OF.toordinal() - MAX_BALANCE_SHEET_AGE_DAYS + 5)
        outside = (AS_OF.toordinal() - MAX_BALANCE_SHEET_AGE_DAYS - 5)
        load(client, facts(LongTermDebt=[
            instant("d", date.fromordinal(outside).isoformat(), 999),
            instant("d", date.fromordinal(inside).isoformat(), 111),
        ]))
        fact = client._latest_instant(
            client._company_facts("0000000001"), ("LongTermDebt",), as_of=AS_OF
        )
        assert fact.value == 111

    def test_preference_order_breaks_ties_on_the_same_date(self, client):
        load(client, facts(
            LongTermDebtNoncurrent=[instant("d", "2026-06-30", 100)],
            LongTermDebt=[instant("d", "2026-06-30", 200)],
        ))
        fact = client._latest_instant(
            client._company_facts("0000000001"),
            ("LongTermDebtNoncurrent", "LongTermDebt"),
            as_of=AS_OF,
        )
        assert fact.tag == "LongTermDebtNoncurrent"

    def test_duration_facts_are_never_read_as_balances(self, client):
        load(client, facts(LongTermDebt=[duration("2026-01-01", "2026-06-30", 500)]))
        fact = client._latest_instant(
            client._company_facts("0000000001"), ("LongTermDebt",), as_of=AS_OF
        )
        assert fact is None


class TestFundamentals:
    @pytest.fixture
    def payload(self):
        return facts(
            RevenueFromContractWithCustomerExcludingAssessedTax=[
                duration("2025-08-01", "2025-10-31", 100),
                duration("2025-11-01", "2026-01-31", 110),
                duration("2026-02-01", "2026-04-30", 120),
                duration("2026-05-01", "2026-07-31", 130),
            ],
            CashAndCashEquivalentsAtCarryingValue=[instant("c", "2026-07-31", 40)],
            AvailableForSaleSecuritiesDebtSecuritiesCurrent=[instant("s", "2026-07-31", 25)],
            LongTermDebt=[instant("d", "2026-07-31", 60)],
            ShortTermBorrowings=[instant("d", "2026-07-31", 10)],
            EntityCommonStockSharesOutstanding=[instant("q", "2026-07-31", 1_000)],
        )

    def test_assembles_every_component(self, client, payload, monkeypatch):
        monkeypatch.setattr(SECClient, "latest_filing", lambda *a, **k: None)
        load(client, payload)
        f = client.fundamentals("ACME", as_of=AS_OF)

        assert f.ltm_revenue_usd == 460
        assert f.cash_usd == 65, "cash is core cash plus short-term investments"
        assert f.debt_usd == 70, "debt is long-term plus short-term"
        assert f.shares_outstanding == 1_000
        assert f.company_name == "Acme Corp"

    def test_records_which_tag_produced_each_component(self, client, payload, monkeypatch):
        """Debt and cash decomposition varies by filer, so the mapping is disclosed
        rather than assumed."""
        monkeypatch.setattr(SECClient, "latest_filing", lambda *a, **k: None)
        load(client, payload)
        components = client.fundamentals("ACME", as_of=AS_OF).components

        assert "us-gaap:LongTermDebt @2026-07-31" in components["debt"]
        assert "us-gaap:ShortTermBorrowings @2026-07-31" in components["debt"]
        assert "us-gaap:CashAndCashEquivalentsAtCarryingValue" in components["cash"]
        assert components["revenue"].startswith("us-gaap:RevenueFromContract")

    def test_missing_components_are_zero_and_labelled(self, client, monkeypatch):
        monkeypatch.setattr(SECClient, "latest_filing", lambda *a, **k: None)
        load(client, facts(Revenues=[duration("2025-02-01", "2026-01-31", 900, form="10-K")]))
        f = client.fundamentals("ACME", as_of=AS_OF)

        assert (f.cash_usd, f.debt_usd) == (0.0, 0.0)
        assert f.components["debt"] == "not reported"


class TestFilingSelection:
    def test_picks_the_most_recent_periodic_filing_on_or_before_the_date(self, client):
        client._get_json = lambda url: {  # type: ignore[method-assign]
            "filings": {"recent": {
                "form": ["8-K", "10-Q", "10-K"],
                "filingDate": ["2026-08-01", "2026-06-05", "2026-03-01"],
                "accessionNumber": ["0-1", "0-2", "0-3"],
                "primaryDocument": ["a.htm", "b.htm", "c.htm"],
                "reportDate": ["", "2026-04-30", "2026-01-31"],
            }}
        }
        filing = client.latest_filing("0000000001", as_of=AS_OF)

        assert filing.form == "10-Q"
        assert filing.accession_number == "0-2"
        assert filing.url.endswith("/02/b.htm")

    def test_filings_after_the_valuation_date_are_ignored(self, client):
        client._get_json = lambda url: {  # type: ignore[method-assign]
            "filings": {"recent": {
                "form": ["10-Q", "10-K"],
                "filingDate": ["2026-09-30", "2026-03-01"],
                "accessionNumber": ["0-1", "0-2"],
                "primaryDocument": ["a.htm", "b.htm"],
                "reportDate": ["2026-07-31", "2026-01-31"],
            }}
        }
        assert client.latest_filing("0000000001", as_of=AS_OF).accession_number == "0-2"

    def test_a_provenance_failure_loses_the_citation_not_the_peer(self, client):
        def boom(url):
            raise DataUnavailableError("sec_edgar", url, "down")

        client._get_json = boom  # type: ignore[method-assign]
        assert client.latest_filing("0000000001", as_of=AS_OF) is None


class TestMalformedData:
    def test_one_bad_fact_does_not_lose_the_series(self, client):
        load(client, facts(LongTermDebt=[
            {"end": "not-a-date", "val": 1},
            instant("d", "2026-06-30", 500),
        ]))
        fact = client._latest_instant(
            client._company_facts("0000000001"), ("LongTermDebt",), as_of=AS_OF
        )
        assert fact.value == 500
