"""Tests for orchestration: selection, degradation, reconciliation, determinism."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import AS_OF
from vc_audit.data.mock_provider import MockMarketDataProvider
from vc_audit.data.resilient import ResilientMarketDataProvider
from vc_audit.domain.errors import DataUnavailableError, FatalError
from vc_audit.domain.models import PortfolioCompany
from vc_audit.engine import value_company


def value(company, provider, **kwargs):
    return value_company(company, provider=provider, as_of=AS_OF, **kwargs)


class TestMethodSelection:
    def test_complete_data_runs_every_method(self, company, provider):
        report = value(company, provider)
        assert {r.method_id for r in report.method_results} == {"comps", "dcf", "last_round"}
        assert report.skipped_methods == []

    def test_sparse_data_runs_what_it_can_and_records_the_rest(
        self, sparse_company, provider
    ):
        report = value(sparse_company, provider)

        assert [r.method_id for r in report.method_results] == ["last_round"]
        assert {s.method_id for s in report.skipped_methods} == {"comps", "dcf"}
        assert all(s.error_type == "MissingInputError" for s in report.skipped_methods)

    def test_skip_reasons_name_the_missing_field(self, sparse_company, provider):
        report = value(sparse_company, provider)
        reasons = {s.method_id: s.reason for s in report.skipped_methods}
        assert "ltm_revenue_usd" in reasons["comps"]
        assert "projections" in reasons["dcf"]

    def test_explicit_method_restriction_is_honoured(self, company, provider):
        report = value(company, provider, methods=["dcf"])
        assert [r.method_id for r in report.method_results] == ["dcf"]

    def test_an_unknown_method_id_stops_the_run(self, company, provider):
        """A typo must not silently narrow the analysis."""
        with pytest.raises(ValueError, match="unknown method 'dcff'"):
            value(company, provider, methods=["dcff"])

    def test_a_company_no_method_supports_is_fatal(self, provider):
        bare = PortfolioCompany(name="Stealth Co", sector="saas")
        with pytest.raises(FatalError, match="no valuation method could be applied"):
            value(bare, provider)


class TestGracefulDegradation:
    def test_a_data_outage_skips_only_the_affected_method(self, company):
        provider = MockMarketDataProvider(simulate_outage_for={"public_comps"})
        report = value(company, provider)

        assert {r.method_id for r in report.method_results} == {"dcf", "last_round"}
        outage = next(s for s in report.skipped_methods if s.method_id == "comps")
        assert outage.error_type == "DataUnavailableError"
        assert "503" in outage.reason

    def test_the_outage_is_surfaced_as_an_exception_not_swallowed(self, company):
        provider = MockMarketDataProvider(simulate_outage_for={"public_comps"})
        report = value(company, provider)
        assert any("could not be applied" in w for w in report.all_warnings)

    def test_losing_every_data_dependent_method_still_returns_the_dcf(self, company):
        provider = MockMarketDataProvider(
            simulate_outage_for={"public_comps", "index_history"}
        )
        report = value(company, provider)
        assert [r.method_id for r in report.method_results] == ["dcf"]


class TestDataProvenance:
    def test_the_run_opens_by_naming_its_data_sources(self, company, provider):
        report = value(company, provider)
        step = report.engine_trail.step("data_sources")

        assert step.inputs["provider"] == "yahoo_finance_mock"
        assert "fixtures" in step.output

    def test_a_fixture_substitution_is_disclosed_as_an_exception(self, company):
        """Fixture figures must never be presented with the authority of filed ones."""

        class DeadPrimary:
            name = "dead"
            degradations: list[str] = []

            def describe(self):
                return "unreachable"

            def known_sectors(self):
                return []

            def get_peers(self, **kwargs):
                raise DataUnavailableError("dead", "peers", "connection refused")

            def get_index_level(self, **kwargs):
                raise DataUnavailableError("dead", "index", "connection refused")

        provider = ResilientMarketDataProvider(DeadPrimary(), MockMarketDataProvider())
        report = value(company, provider)

        assert report.engine_trail.step("data_degradation").output
        assert any("fell back to checked-in fixtures" in w for w in report.all_warnings)

    def test_no_degradation_step_when_nothing_degraded(self, company, provider):
        report = value(company, provider)
        with pytest.raises(KeyError):
            report.engine_trail.step("data_degradation")


class TestConclusion:
    def test_weights_are_renormalised_over_the_methods_that_ran(
        self, sparse_company, provider
    ):
        report = value(sparse_company, provider)
        weights = report.engine_trail.step("weighted_conclusion").inputs["weights"]

        assert sum(weights.values()) == pytest.approx(1.0)
        assert weights == {"last_round": 1.0}

    def test_the_conclusion_lies_between_the_method_values(self, company, provider):
        report = value(company, provider)
        values = [r.equity_value_usd for r in report.method_results]

        assert min(values) <= report.concluded_value_usd <= max(values)

    def test_the_combined_range_spans_every_method_range(self, company, provider):
        report = value(company, provider)

        assert report.concluded_range.low_usd <= min(
            r.value_range.low_usd for r in report.method_results
        )
        assert report.concluded_range.high_usd >= max(
            r.value_range.high_usd for r in report.method_results
        )


class TestConcordance:
    def test_divergent_methods_are_classified_and_warned(self, company, provider):
        report = value(company, provider)

        assert report.concordance.agreement == "wide"
        assert any("dispersion is wide" in w for w in report.engine_trail.warnings)

    def test_convergent_methods_are_classified_as_tight(self, provider):
        inflo = PortfolioCompany(
            name="Inflo",
            sector="fintech",
            ltm_revenue_usd=42_000_000,
            cash_usd=30_000_000,
            debt_usd=6_000_000,
            last_post_money_valuation_usd=165_000_000,
            last_round_date=date(2024, 11, 30),
        )
        report = value(inflo, provider)

        assert report.concordance.agreement == "tight"
        assert report.concordance.coefficient_of_variation < 0.10

    def test_a_lone_method_is_reported_as_uncorroborated(self, sparse_company, provider):
        report = value(sparse_company, provider)

        assert report.concordance.agreement == "single-method"
        assert "no corroborating evidence" in report.concordance.commentary


class TestReviewQueue:
    def test_engine_defaults_are_queued_for_sign_off(self, company, provider):
        report = value(company, provider)
        queued = report.engine_trail.step("assumption_review_queue").output

        assert "dcf.wacc" in queued
        assert "comps.illiquidity_discount" in queued

    def test_supplying_an_assumption_removes_it_from_the_queue(self, company, provider):
        report = value(company, provider, overrides={"wacc": 0.18})
        queued = report.engine_trail.step("assumption_review_queue").output

        assert "dcf.wacc" not in queued
        assert "dcf.terminal_growth" in queued

    def test_a_fully_specified_run_leaves_an_empty_queue(self, company, provider):
        report = value(
            company,
            provider,
            methods=["dcf"],
            overrides={"wacc": 0.18, "terminal_growth": 0.025},
        )
        assert report.engine_trail.step("assumption_review_queue").output == {}


class TestDeterminism:
    def test_identical_inputs_reproduce_the_run_id_and_fingerprint(self, company, provider):
        first = value(company, provider)
        second = value(company, MockMarketDataProvider())

        assert first.run_id == second.run_id
        assert first.fingerprint == second.fingerprint

    def test_changing_an_assumption_changes_both_identifiers(self, company, provider):
        base = value(company, provider)
        overridden = value(company, provider, overrides={"wacc": 0.18})

        assert base.run_id != overridden.run_id
        assert base.fingerprint != overridden.fingerprint

    def test_changing_the_valuation_date_changes_the_run_id(self, company, provider):
        base = value(company, provider)
        later = value_company(
            company, provider=provider, as_of=date(2026, 6, 30)
        )
        assert base.run_id != later.run_id

    def test_sensitivity_runs_do_not_leak_into_the_audit_trail(self, company, provider):
        """Hypothetical arithmetic must not sit beside the booked arithmetic."""
        with_sensitivity = value(company, provider)
        without = value(company, provider, run_sensitivity=False)

        for left, right in zip(
            with_sensitivity.method_results, without.method_results, strict=True
        ):
            assert [s.label for s in left.trail.steps] == [s.label for s in right.trail.steps]
            assert left.equity_value_usd == pytest.approx(right.equity_value_usd)


class TestUnsettledValuationDate:
    """Every other run is reproducible forever, because filed accounts and past
    closes do not change. A valuation dated today is the exception: the run id is
    seeded on the date, so a morning run and an evening run share an identifier
    while producing different figures, and the later silently overwrites the
    earlier one's evidence pack."""

    def test_a_settled_past_date_says_nothing(self, company, provider):
        report = value(company, provider, today=date(2026, 9, 1))
        assert not any("have not settled" in w for w in report.all_warnings)

    def test_valuing_as_of_today_is_flagged(self, company, provider):
        report = value(company, provider, today=AS_OF)
        warning = next(w for w in report.all_warnings if "have not settled" in w)

        assert "today" in warning
        assert "same run id" in warning

    def test_a_future_date_is_flagged_and_named(self, company, provider):
        report = value(company, provider, today=date(2026, 8, 1))
        warning = next(w for w in report.all_warnings if "have not settled" in w)

        assert "in the future (2026-08-22)" in warning

    def test_the_engine_still_never_reads_the_clock(self, company, provider):
        """Omitting the date skips the check rather than calling date.today(),
        so this layer stays a pure function of its arguments."""
        report = value(company, provider)
        assert not any("have not settled" in w for w in report.all_warnings)

    def test_the_warning_does_not_change_the_conclusion(self, company, provider):
        quiet = value(company, provider)
        flagged = value(company, provider, today=AS_OF)

        assert flagged.concluded_value_usd == pytest.approx(quiet.concluded_value_usd)
