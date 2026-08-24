"""Tests for input ingestion.

Bad input is the most common failure mode for this tool, so the error messages
are treated as a feature and tested as one.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from vc_audit.domain.errors import FatalError
from vc_audit.domain.models import PortfolioCompany
from vc_audit.loader import load_company, parse_company, parse_overrides

EXAMPLES = ["basis_ai", "inflo", "northwind_labs", "basis_ai_linked"]


class TestLoadCompany:
    @pytest.mark.parametrize("name", EXAMPLES)
    def test_bundled_examples_all_load(self, name):
        company = load_company(f"examples/{name}.json")
        assert isinstance(company, PortfolioCompany)
        assert company.name

    def test_a_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(FatalError, match="company file not found"):
            load_company(tmp_path / "absent.json")

    def test_malformed_json_reports_the_line(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text('{"name": "X",}', encoding="utf-8")

        with pytest.raises(FatalError, match="not valid JSON"):
            load_company(path)

    def test_validation_errors_name_the_offending_field(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"name": "X", "sector": "saas", "ltm_revenue_usd": -5}', encoding="utf-8")

        with pytest.raises(FatalError, match="ltm_revenue_usd"):
            load_company(path)


class TestProjectionsByReference:
    """The brief allows projections as "a path to a file or a JSON object"."""

    def _company(self, projections):
        return {
            "name": "X",
            "sector": "saas",
            "projections": projections,
        }

    def _year(self, year=2026):
        return {"year": year, "revenue_usd": 1_000, "ebit_usd": 100}

    def test_a_referenced_file_is_loaded(self, tmp_path):
        (tmp_path / "forecast.json").write_text(
            json.dumps({"projections": [self._year()]}), encoding="utf-8"
        )
        (tmp_path / "co.json").write_text(
            json.dumps(self._company("forecast.json")), encoding="utf-8"
        )
        company = load_company(tmp_path / "co.json")

        assert [p.year for p in company.projections] == [2026]

    def test_a_bare_array_file_is_also_accepted(self, tmp_path):
        (tmp_path / "forecast.json").write_text(json.dumps([self._year()]), encoding="utf-8")
        (tmp_path / "co.json").write_text(
            json.dumps(self._company("forecast.json")), encoding="utf-8"
        )
        assert len(load_company(tmp_path / "co.json").projections) == 1

    def test_the_path_resolves_relative_to_the_company_record(self, tmp_path):
        """So a pair of files can be moved together."""
        nested = tmp_path / "sub"
        nested.mkdir()
        (nested / "forecast.json").write_text(json.dumps([self._year()]), encoding="utf-8")
        (nested / "co.json").write_text(
            json.dumps(self._company("forecast.json")), encoding="utf-8"
        )
        assert len(load_company(nested / "co.json").projections) == 1

    def test_the_referenced_file_is_cited_as_the_projections_source(self, tmp_path):
        (tmp_path / "forecast.json").write_text(json.dumps([self._year()]), encoding="utf-8")
        (tmp_path / "co.json").write_text(
            json.dumps(self._company("forecast.json")), encoding="utf-8"
        )
        source = load_company(tmp_path / "co.json").projections_source

        assert source is not None
        assert source.dataset == "forecast.json"

    def test_an_explicit_source_is_not_overwritten(self, tmp_path):
        (tmp_path / "forecast.json").write_text(json.dumps([self._year()]), encoding="utf-8")
        payload = self._company("forecast.json")
        payload["projections_source"] = {
            "provider": "internal",
            "dataset": "board_pack",
            "as_of": "2025-09-30",
        }
        (tmp_path / "co.json").write_text(json.dumps(payload), encoding="utf-8")

        assert load_company(tmp_path / "co.json").projections_source.dataset == "board_pack"

    def test_a_missing_referenced_file_names_it(self, tmp_path):
        (tmp_path / "co.json").write_text(
            json.dumps(self._company("absent.json")), encoding="utf-8"
        )
        with pytest.raises(FatalError, match="projections file not found"):
            load_company(tmp_path / "co.json")

    def test_a_file_of_the_wrong_shape_is_rejected(self, tmp_path):
        (tmp_path / "forecast.json").write_text(json.dumps("nope"), encoding="utf-8")
        (tmp_path / "co.json").write_text(
            json.dumps(self._company("forecast.json")), encoding="utf-8"
        )
        with pytest.raises(FatalError, match="list of projected years"):
            load_company(tmp_path / "co.json")

    def test_the_bundled_linked_example_loads(self):
        company = load_company("examples/basis_ai_linked.json")
        assert len(company.projections) == 5

    def test_a_company_file_that_is_not_an_object_is_rejected(self, tmp_path):
        (tmp_path / "co.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(FatalError, match="should contain a company object"):
            load_company(tmp_path / "co.json")


class TestValidationRules:
    def test_a_round_valuation_without_its_date_is_rejected(self):
        with pytest.raises(FatalError, match="must be provided together"):
            parse_company(
                {"name": "X", "sector": "saas", "last_post_money_valuation_usd": 1_000_000}
            )

    def test_unordered_projections_are_rejected(self):
        with pytest.raises(FatalError, match="ascending year"):
            parse_company(
                {
                    "name": "X",
                    "sector": "saas",
                    "projections": [
                        {"year": 2027, "revenue_usd": 1, "ebit_usd": 1},
                        {"year": 2026, "revenue_usd": 1, "ebit_usd": 1},
                    ],
                }
            )

    def test_duplicate_projection_years_are_rejected(self):
        with pytest.raises(FatalError, match="duplicate years"):
            parse_company(
                {
                    "name": "X",
                    "sector": "saas",
                    "projections": [
                        {"year": 2026, "revenue_usd": 1, "ebit_usd": 1},
                        {"year": 2026, "revenue_usd": 2, "ebit_usd": 1},
                    ],
                }
            )

    def test_unknown_fields_are_rejected_rather_than_ignored(self):
        """A typo'd field name must not be silently dropped along with its value."""
        with pytest.raises(FatalError, match="ltm_revenu_usd"):
            parse_company({"name": "X", "sector": "saas", "ltm_revenu_usd": 1_000_000})


class TestAvailableInputs:
    def test_reports_only_populated_fields(self):
        company = parse_company(
            {
                "name": "X",
                "sector": "saas",
                "ltm_revenue_usd": 1_000_000,
                "last_post_money_valuation_usd": 5_000_000,
                "last_round_date": "2024-01-31",
            }
        )
        assert company.available_inputs() == {
            "sector",
            "ltm_revenue_usd",
            "last_post_money_valuation_usd",
            "last_round_date",
        }

    def test_net_debt_nets_cash_against_debt(self):
        company = parse_company(
            {"name": "X", "sector": "saas", "cash_usd": 18_000_000, "debt_usd": 4_000_000}
        )
        assert company.net_debt_usd == -14_000_000


class TestOverrideParsing:
    def test_numeric_values_are_coerced(self):
        assert parse_overrides(["wacc=0.18"]) == {"wacc": 0.18}

    def test_non_numeric_values_stay_strings(self):
        assert parse_overrides(["market_index=^IXIC"]) == {"market_index": "^IXIC"}

    def test_qualified_keys_are_preserved(self):
        assert parse_overrides(["dcf.wacc=0.2"]) == {"dcf.wacc": 0.2}

    @pytest.mark.parametrize("bad", ["wacc", "=0.18"])
    def test_malformed_overrides_are_rejected(self, bad):
        with pytest.raises(FatalError, match="malformed override"):
            parse_overrides([bad])


class TestExampleContents:
    def test_the_complete_example_supports_every_method(self):
        company = load_company("examples/basis_ai.json")
        assert {"sector", "ltm_revenue_usd", "projections", "last_round_date"} <= (
            company.available_inputs()
        )

    def test_the_sparse_example_supports_only_the_last_round(self):
        company = load_company("examples/northwind_labs.json")
        assert "projections" not in company.available_inputs()
        assert company.last_round_date == date(2021, 11, 30)


class TestCurrency:
    """Peer fundamentals are USD. A non-USD subject would be valued against USD
    multiples and reported in USD with no sign of the mismatch, so the record is
    refused rather than silently mis-valued."""

    def test_usd_is_accepted(self):
        assert parse_company({"name": "X", "sector": "saas", "currency": "USD"}).currency == "USD"

    def test_the_default_is_usd(self):
        assert parse_company({"name": "X", "sector": "saas"}).currency == "USD"

    def test_case_and_whitespace_do_not_defeat_the_check(self):
        assert parse_company({"name": "X", "sector": "saas", "currency": " usd "}).currency

    @pytest.mark.parametrize("currency", ["EUR", "GBP", "eur", "JPY"])
    def test_any_other_currency_is_refused(self, currency):
        with pytest.raises(FatalError, match="is not supported"):
            parse_company({"name": "X", "sector": "saas", "currency": currency})

    def test_the_refusal_explains_the_consequence_not_just_the_rule(self):
        with pytest.raises(FatalError) as caught:
            parse_company({"name": "X", "sector": "saas", "currency": "EUR"})
        message = str(caught.value)

        assert "SEC filings in USD" in message
        assert "Convert the record to USD" in message
