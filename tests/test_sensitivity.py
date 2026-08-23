"""Tests for the sensitivity sweep."""

from __future__ import annotations

import pytest

from tests.conftest import make_context
from vc_audit.methods.base import DriverSpec
from vc_audit.methods.dcf import DiscountedCashFlow
from vc_audit.methods.last_round import LastRoundMarkToMarket
from vc_audit.sensitivity import analyse


def run_with_sensitivity(method, company, provider, **override_kwargs):
    ctx = make_context(provider, method.id, **override_kwargs)
    result = method.run(company, ctx)
    return ctx, result, analyse(method, company, ctx, result)


class TestDriverBounds:
    def test_absolute_mode_shifts_by_the_delta(self):
        spec = DriverSpec(key="wacc", label="WACC", delta=0.02)
        assert spec.bounds(0.15) == pytest.approx((0.13, 0.17))

    def test_relative_mode_scales_by_the_delta(self):
        spec = DriverSpec(key="band", label="Band", delta=0.5, mode="relative")
        assert spec.bounds(100.0) == pytest.approx((50.0, 150.0))

    def test_clamps_prevent_nonsensical_inputs(self):
        spec = DriverSpec(key="beta", label="Beta", delta=0.5, min_value=0.0)
        assert spec.bounds(0.25) == pytest.approx((0.0, 0.75))


class TestSweep:
    def test_every_declared_driver_produces_a_row(self, company, provider):
        method = DiscountedCashFlow()
        _, _, report = run_with_sensitivity(method, company, provider)

        assert {row.driver_key for row in report.rows} == {
            spec.key for spec in method.drivers()
        }

    def test_rows_rank_by_influence(self, company, provider):
        _, _, report = run_with_sensitivity(DiscountedCashFlow(), company, provider)
        swings = [row.swing_usd for row in report.ranked_rows]

        assert swings == sorted(swings, reverse=True)
        assert report.dominant_driver.driver_key == "wacc"

    def test_discount_rate_moves_value_inversely(self, company, provider):
        _, _, report = run_with_sensitivity(DiscountedCashFlow(), company, provider)
        wacc = next(row for row in report.rows if row.driver_key == "wacc")

        assert wacc.low_input < wacc.high_input
        assert wacc.low_value_usd > wacc.high_value_usd

    def test_sweep_is_centred_on_the_auditors_value_not_the_default(
        self, company, provider
    ):
        _, _, report = run_with_sensitivity(
            DiscountedCashFlow(), company, provider, overrides={"wacc": 0.25}
        )
        wacc = next(row for row in report.rows if row.driver_key == "wacc")

        assert (wacc.low_input, wacc.high_input) == pytest.approx((0.23, 0.27))

    def test_beta_band_moves_the_last_round_mark(self, company, provider):
        """Beta scales the entire adjustment, so a +/-0.25 band must bite."""
        _, result, report = run_with_sensitivity(
            LastRoundMarkToMarket(), company, provider
        )
        beta = next(row for row in report.rows if row.driver_key == "index_beta")

        assert beta.swing_usd > 0
        assert beta.low_value_usd != pytest.approx(result.equity_value_usd)

    def test_swing_percentage_is_relative_to_the_base_value(self, company, provider):
        _, result, report = run_with_sensitivity(DiscountedCashFlow(), company, provider)

        for row in report.rows:
            assert row.base_value_usd == pytest.approx(result.equity_value_usd)
            assert row.swing_pct == pytest.approx(row.swing_usd / abs(row.base_value_usd))


class TestUnstressableDrivers:
    def test_one_broken_driver_drops_its_row_and_keeps_the_rest(
        self, company, provider
    ):
        """At a 4.5% WACC, the low bound (2.5%) crosses terminal growth of 3%."""
        _, result, report = run_with_sensitivity(
            DiscountedCashFlow(), company, provider, overrides={"wacc": 0.045}
        )

        assert {row.driver_key for row in report.rows} == {"terminal_growth"}
        assert any("Discount rate" in w and "does not solve" in w for w in result.trail.warnings)

    def test_when_no_driver_can_be_stressed_the_report_is_omitted(
        self, company, provider
    ):
        """At a 4% WACC both bands cross terminal growth; an empty table is worse
        than no table, so the section is dropped entirely."""
        _, result, report = run_with_sensitivity(
            DiscountedCashFlow(), company, provider, overrides={"wacc": 0.04}
        )

        assert report is None
        assert sum("does not solve" in w for w in result.trail.warnings) == 2

    def test_a_non_numeric_assumption_is_skipped_cleanly(self, company, provider):
        """market_index is a string; it has no band and must not be stressed."""
        method = LastRoundMarkToMarket()
        _, _, report = run_with_sensitivity(method, company, provider)

        assert "market_index" not in {row.driver_key for row in report.rows}
