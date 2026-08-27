"""Tests for the three valuation methods.

Each method is exercised in isolation against a hand-computable expectation, so
a failure points at one piece of arithmetic rather than at "the valuation moved".
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import AS_OF, make_context
from vc_audit.domain.errors import (
    AssumptionError,
    InsufficientEvidenceError,
    MissingInputError,
)
from vc_audit.methods.comps import ComparableCompanyAnalysis, _quartiles
from vc_audit.methods.dcf import DiscountedCashFlow, _unlevered_fcf
from vc_audit.methods.last_round import LastRoundMarkToMarket

# NMBS is the median SaaS peer after outlier trimming:
# EV = 12.4B + 0.8B - 2.1B = 11.1B, over 1.35B revenue.
EXPECTED_MEDIAN_MULTIPLE = 11.1 / 1.35


class TestQuartiles:
    def test_hinges_are_the_medians_of_each_half(self):
        assert _quartiles([1, 2, 3, 4]) == (1.5, 3.5)

    def test_odd_length_excludes_the_median_from_both_halves(self):
        assert _quartiles([1, 2, 3, 4, 5]) == (1.5, 4.5)


class TestComps:
    def test_median_multiple_matches_the_peer_set(self, company, provider):
        ctx = make_context(provider, "comps")
        ComparableCompanyAnalysis().compute(company, ctx)

        median = ctx.trail.step("peer_median_multiple").output
        assert median == pytest.approx(EXPECTED_MEDIAN_MULTIPLE, rel=1e-6)

    def test_hypergrowth_peer_is_trimmed_as_an_outlier(self, company, provider):
        """KTRA trades at 28x revenue; leaving it in would drag the median."""
        ctx = make_context(provider, "comps")
        ComparableCompanyAnalysis().compute(company, ctx)

        retained = ctx.trail.step("outlier_trim").output
        assert "KTRA" not in retained
        assert "NMBS" in retained

    def test_every_excluded_peer_is_named_in_a_warning(self, company, provider):
        ctx = make_context(provider, "comps")
        ComparableCompanyAnalysis().compute(company, ctx)

        assert any("KTRA" in w for w in ctx.trail.warnings)

    def test_equity_value_is_multiple_times_revenue_bridged_for_net_cash(
        self, company, provider
    ):
        ctx = make_context(provider, "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        expected_ev = EXPECTED_MEDIAN_MULTIPLE * 0.80 * company.ltm_revenue_usd
        expected_equity = expected_ev - company.debt_usd + company.cash_usd
        assert outcome.enterprise_value_usd == pytest.approx(expected_ev)
        assert outcome.equity_value_usd == pytest.approx(expected_equity)

    def test_illiquidity_discount_override_is_honoured(self, company, provider):
        ctx = make_context(provider, "comps", overrides={"illiquidity_discount": 0.40})
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        expected_ev = EXPECTED_MEDIAN_MULTIPLE * 0.60 * company.ltm_revenue_usd
        assert outcome.enterprise_value_usd == pytest.approx(expected_ev)

    def test_override_is_recorded_as_user_provided(self, company, provider):
        ctx = make_context(provider, "comps", overrides={"illiquidity_discount": 0.40})
        ComparableCompanyAnalysis().compute(company, ctx)

        recorded = next(a for a in ctx.trail.assumptions if a.key == "illiquidity_discount")
        assert recorded.origin == "user_provided"
        assert recorded not in ctx.trail.unreviewed_defaults()

    def test_too_few_peers_declines_rather_than_concluding(self, company, provider):
        """A "median" of one or two peers is not a market observation."""
        ctx = make_context(provider, "comps", overrides={"peer_size_band": 2.0})
        with pytest.raises(InsufficientEvidenceError, match="at least 3"):
            ComparableCompanyAnalysis().compute(company, ctx)

    def test_preflight_rejects_a_company_without_revenue(self, sparse_company):
        with pytest.raises(MissingInputError, match="ltm_revenue_usd"):
            ComparableCompanyAnalysis().preflight(sparse_company)

    def test_range_comes_from_the_peer_spread_not_a_fixed_tolerance(
        self, company, provider
    ):
        ctx = make_context(provider, "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)
        spread = ctx.trail.step("peer_spread_range")

        assert spread.inputs["q1_multiple"] < spread.inputs["q3_multiple"]
        assert outcome.value_range.low_usd < outcome.value_range.point_usd
        assert outcome.value_range.point_usd < outcome.value_range.high_usd


class TestCompsFunnel:
    def test_the_funnel_reconciles_to_the_universe(self, company, provider):
        ctx = make_context(provider, "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)
        f = outcome.funnel

        assert f.proposed == f.retained + f.total_dropped
        assert f.retained == len(outcome.peers)

    def test_the_trimmed_outlier_is_reported_as_a_rejection(self, company, provider):
        ctx = make_context(provider, "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)
        ktra = next(e for e in outcome.excluded_peers if e.ticker == "KTRA")

        assert ktra.stage == "statistical"
        assert "Tukey fence" in ktra.reason
        assert outcome.funnel.dropped_outlier == 1

    def test_size_band_rejections_are_staged_as_comparability(self, company, provider):
        """A 300x band puts the ceiling at $3B, which drops the largest peer only."""
        ctx = make_context(provider, "comps", overrides={"peer_size_band": 300.0})
        outcome = ComparableCompanyAnalysis().compute(company, ctx)
        dropped = [e for e in outcome.excluded_peers if e.stage == "comparability"]

        assert [e.ticker for e in dropped] == ["ATLS"]
        assert "size band" in dropped[0].reason
        assert outcome.funnel.dropped_not_comparable == 1

    def test_every_retained_peer_carries_a_reason_for_being_there(self, company, provider):
        ctx = make_context(provider, "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert all(p.inclusion_rationale for p in outcome.peers)

    def test_the_narrative_reports_both_sides_of_the_funnel(self, company, provider):
        ctx = make_context(provider, "comps")
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert "candidates were considered" in outcome.narrative
        assert "rejected" in outcome.narrative


class TestDcf:
    def test_unlevered_fcf_matches_the_line_item_build(self, projections):
        # 2026: EBIT -1.5M at 21% tax, +0.6M D&A, -0.9M capex, -0.4M NWC.
        assert _unlevered_fcf(projections[0]) == pytest.approx(-1_885_000.0)

    def test_terminal_value_uses_gordon_growth_on_the_final_year(self, company, provider):
        ctx = make_context(provider, "dcf")
        DiscountedCashFlow().compute(company, ctx)

        final_fcf = _unlevered_fcf(company.projections[-1])
        expected = final_fcf * 1.03 / (0.15 - 0.03)
        assert ctx.trail.step("terminal_value").output == pytest.approx(expected)

    def test_enterprise_value_is_the_sum_of_both_present_values(self, company, provider):
        ctx = make_context(provider, "dcf")
        outcome = DiscountedCashFlow().compute(company, ctx)

        parts = (
            ctx.trail.step("pv_forecast_period").output
            + ctx.trail.step("pv_terminal_value").output
        )
        assert outcome.enterprise_value_usd == pytest.approx(parts)

    def test_a_higher_discount_rate_lowers_the_value(self, company, provider):
        base = DiscountedCashFlow().compute(company, make_context(provider, "dcf"))
        strict = DiscountedCashFlow().compute(
            company, make_context(provider, "dcf", overrides={"wacc": 0.25})
        )
        assert strict.equity_value_usd < base.equity_value_usd

    def test_terminal_growth_at_or_above_wacc_is_fatal(self, company, provider):
        ctx = make_context(provider, "dcf", overrides={"wacc": 0.03, "terminal_growth": 0.05})
        with pytest.raises(AssumptionError, match="must exceed terminal growth"):
            DiscountedCashFlow().compute(company, ctx)

    def test_terminal_value_concentration_is_flagged(self, company, provider):
        """This company's forecast is back-loaded, so the warning must fire."""
        ctx = make_context(provider, "dcf")
        DiscountedCashFlow().compute(company, ctx)

        assert any("Terminal value accounts for" in w for w in ctx.trail.warnings)

    def test_preflight_rejects_a_company_without_projections(self, sparse_company):
        with pytest.raises(MissingInputError, match="projections"):
            DiscountedCashFlow().preflight(sparse_company)


class TestLastRound:
    def test_value_is_the_round_price_moved_by_the_index(self, company, provider):
        ctx = make_context(provider, "last_round")
        outcome = LastRoundMarkToMarket().compute(company, ctx)

        change = ctx.trail.step("index_change").output
        expected = company.last_post_money_valuation_usd * (1 + change)
        assert outcome.equity_value_usd == pytest.approx(expected)

    def test_saas_companies_mark_against_the_cloud_index(self, company, provider):
        ctx = make_context(provider, "last_round")
        LastRoundMarkToMarket().compute(company, ctx)

        index = next(a for a in ctx.trail.assumptions if a.key == "market_index")
        assert index.value == "WCLD"

    def test_index_choice_can_be_overridden(self, company, provider):
        ctx = make_context(provider, "last_round", overrides={"market_index": "^IXIC"})
        LastRoundMarkToMarket().compute(company, ctx)

        assert ctx.trail.step("index_levels").inputs["index_id"] == "^IXIC"

    def test_beta_scales_the_adjustment(self, company, provider):
        ctx = make_context(provider, "last_round", overrides={"index_beta": 0.0})
        outcome = LastRoundMarkToMarket().compute(company, ctx)

        assert outcome.equity_value_usd == pytest.approx(company.last_post_money_valuation_usd)

    def test_a_stale_round_is_flagged(self, company, provider):
        """The round is over four years old, so the mark carries the conclusion."""
        ctx = make_context(provider, "last_round")
        LastRoundMarkToMarket().compute(company, ctx)

        assert any("Last round closed" in w for w in ctx.trail.warnings)

    def test_substituted_observation_dates_are_disclosed(self, company, provider):
        """The fixture series is month-end, so 2026-08-22 has no close of its own
        and the mark falls back to the previous session. The disclosure names the
        date actually used rather than asserting why the requested one was
        unavailable, which is not something the tool can determine."""
        ctx = make_context(provider, "last_round")
        LastRoundMarkToMarket().compute(company, ctx)

        assert any("had no published close" in w for w in ctx.trail.warnings)
        assert any("previous session" in w for w in ctx.trail.warnings)

    def test_a_round_dated_after_the_valuation_date_is_rejected(self, company, provider):
        ctx = make_context(provider, "last_round", as_of=date(2021, 1, 31))
        with pytest.raises(InsufficientEvidenceError, match="after the valuation date"):
            LastRoundMarkToMarket().compute(company, ctx)

    def test_range_brackets_the_unadjusted_round_value(self, company, provider):
        ctx = make_context(provider, "last_round")
        outcome = LastRoundMarkToMarket().compute(company, ctx)
        post_money = company.last_post_money_valuation_usd

        assert outcome.value_range.low_usd <= post_money <= outcome.value_range.high_usd


class TestMethodContract:
    @pytest.mark.parametrize(
        "method",
        [ComparableCompanyAnalysis(), DiscountedCashFlow(), LastRoundMarkToMarket()],
        ids=lambda m: m.id,
    )
    def test_every_method_records_steps_and_declares_drivers(
        self, method, company, provider
    ):
        ctx = make_context(provider, method.id)
        outcome = method.compute(company, ctx)

        assert ctx.trail.steps, "a method that records nothing is not auditable"
        assert method.drivers(), "a method with no drivers cannot be stress-tested"
        assert outcome.equity_value_usd > 0
        assert outcome.narrative

    @pytest.mark.parametrize(
        "method",
        [ComparableCompanyAnalysis(), DiscountedCashFlow(), LastRoundMarkToMarket()],
        ids=lambda m: m.id,
    )
    def test_every_step_carries_a_formula_and_a_description(
        self, method, company, provider
    ):
        ctx = make_context(provider, method.id)
        method.compute(company, ctx)

        for step in ctx.trail.steps:
            assert step.formula.strip(), f"{method.id}:{step.label} has no formula"
            assert step.description.strip(), f"{method.id}:{step.label} has no description"

    @pytest.mark.parametrize(
        "method",
        [ComparableCompanyAnalysis(), DiscountedCashFlow(), LastRoundMarkToMarket()],
        ids=lambda m: m.id,
    )
    def test_methods_are_stateless_across_runs(self, method, company, provider):
        """Registry instances are shared, so a method must not retain run state."""
        first = method.compute(company, make_context(provider, method.id))
        second = method.compute(company, make_context(provider, method.id))
        assert first.equity_value_usd == pytest.approx(second.equity_value_usd)

    def test_valuation_date_is_never_read_from_the_clock(self, company, provider):
        """Same inputs at a different as_of must move the answer, not stay put."""
        early = LastRoundMarkToMarket().compute(
            company, make_context(provider, "last_round", as_of=date(2023, 6, 30))
        )
        late = LastRoundMarkToMarket().compute(
            company, make_context(provider, "last_round", as_of=AS_OF)
        )
        assert early.equity_value_usd != pytest.approx(late.equity_value_usd)


class TestDcfTerminalYearGuard:
    """A venture company still burning cash in its final forecast year is the
    normal case here, not the exotic one. Gordon growth capitalises that flow
    into a negative perpetuity, so the method must decline rather than return a
    confident negative number."""

    def _burning(self, company, final_ebit: float):
        years = list(company.projections)
        years[-1] = years[-1].model_copy(update={"ebit_usd": final_ebit})
        return company.model_copy(update={"projections": years})

    def test_a_negative_terminal_cash_flow_is_declined(self, company, provider):
        burning = self._burning(company, -8_000_000)
        with pytest.raises(InsufficientEvidenceError, match="terminal year"):
            DiscountedCashFlow().compute(burning, make_context(provider, "dcf"))

    def test_the_refusal_names_the_year_and_the_figure(self, company, provider):
        burning = self._burning(company, -8_000_000)
        with pytest.raises(InsufficientEvidenceError) as caught:
            DiscountedCashFlow().compute(burning, make_context(provider, "dcf"))

        assert "2030" in str(caught.value)
        assert "Gordon" in str(caught.value)

    def test_the_other_methods_still_run(self, company, provider):
        """Recoverable, not fatal: the forecast is unusable, the company is not."""
        from vc_audit.engine import value_company

        report = value_company(
            self._burning(company, -8_000_000), provider=provider, as_of=AS_OF
        )
        assert {r.method_id for r in report.method_results} == {"comps", "last_round"}
        skipped = next(s for s in report.skipped_methods if s.method_id == "dcf")
        assert skipped.error_type == "InsufficientEvidenceError"

    def test_a_positive_terminal_year_is_untouched(self, company, provider):
        outcome = DiscountedCashFlow().compute(company, make_context(provider, "dcf"))
        assert outcome.equity_value_usd > 0
