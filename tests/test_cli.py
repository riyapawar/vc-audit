"""Tests for the command line surface.

An auditor must never see a traceback, so alongside the happy paths these assert
that every failure mode exits non-zero with a readable message.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from vc_audit.cli import app

runner = CliRunner()
AS_OF = ["--as-of", "2026-08-22"]


def invoke(*args):
    return runner.invoke(app, list(args))


class TestValue:
    def test_reports_the_conclusion(self):
        result = invoke("value", "examples/basis_ai.json", *AS_OF)

        assert result.exit_code == 0
        assert "Basis AI" in result.output
        assert "Cross-method agreement" in result.output

    def test_json_output_is_machine_readable(self):
        result = invoke("value", "examples/inflo.json", *AS_OF, "--json")
        payload = json.loads(result.output)

        assert payload["company_name"] == "Inflo"
        assert payload["fingerprint"]

    def test_markdown_output_is_the_memo(self):
        result = invoke("value", "examples/inflo.json", *AS_OF, "--markdown")

        assert result.output.startswith("# Fair Value Memorandum")
        assert "## 9. Reproducing this valuation" in result.output

    def test_detail_prints_the_calculation_trail(self):
        plain = invoke("value", "examples/inflo.json", *AS_OF)
        detailed = invoke("value", "examples/inflo.json", *AS_OF, "--detail")

        assert "calculation trail" in detailed.output.lower()
        assert len(detailed.output) > len(plain.output)

    def test_method_restriction(self):
        result = invoke("value", "examples/basis_ai.json", *AS_OF, "-m", "dcf", "--json")
        payload = json.loads(result.output)

        assert [m["method_id"] for m in payload["method_results"]] == ["dcf"]

    def test_assumption_override(self):
        result = invoke(
            "value", "examples/basis_ai.json", *AS_OF, "-s", "wacc=0.25", "-m", "dcf", "--json"
        )
        payload = json.loads(result.output)
        wacc = next(
            a for a in payload["method_results"][0]["trail"]["assumptions"] if a["key"] == "wacc"
        )

        assert (wacc["value"], wacc["origin"]) == (0.25, "user_provided")

    def test_simulated_outage_degrades_instead_of_crashing(self):
        result = invoke(
            "value", "examples/basis_ai.json", *AS_OF, "--simulate-outage", "public_comps"
        )

        assert result.exit_code == 0
        assert "Methods not applied" in result.output

    def test_writes_an_evidence_pack(self, tmp_path):
        result = invoke("value", "examples/inflo.json", *AS_OF, "--out", str(tmp_path))

        assert result.exit_code == 0
        run_dir = next(tmp_path.iterdir())
        assert {p.name for p in run_dir.iterdir()} == {
            "inputs.json",
            "report.json",
            "memo.md",
        }


class TestValueFailures:
    def test_a_missing_file_exits_non_zero_without_a_traceback(self):
        result = invoke("value", "examples/nope.json", *AS_OF)

        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_a_bad_date_is_reported_plainly(self):
        result = invoke("value", "examples/inflo.json", "--as-of", "22-08-2026")

        assert result.exit_code == 1
        assert "not a valid date" in result.output

    def test_a_malformed_override_is_reported_plainly(self):
        result = invoke("value", "examples/inflo.json", *AS_OF, "-s", "wacc")

        assert result.exit_code == 1
        assert "malformed override" in result.output

    def test_an_unknown_method_is_reported_plainly(self):
        result = invoke("value", "examples/inflo.json", *AS_OF, "-m", "montecarlo")

        assert result.exit_code == 1
        assert "unknown method" in result.output


class TestMethods:
    def test_lists_every_method(self):
        result = invoke("methods")

        assert result.exit_code == 0
        for method_id in ("comps", "dcf", "last_round"):
            assert method_id in result.output

    def test_shows_eligibility_against_a_company(self):
        result = invoke("methods", "examples/northwind_labs.json")

        assert result.exit_code == 0
        assert "projections" in result.output  # named as the missing input


class TestPeers:
    def test_lists_the_screened_universe(self):
        result = invoke("peers", "saas")

        assert result.exit_code == 0
        assert "KTRA" in result.output
        assert "EV/Revenue" in result.output

    def test_an_unknown_sector_lists_what_is_available(self):
        result = invoke("peers", "widgets")

        assert result.exit_code == 1
        assert "fintech" in result.output


class TestArchive:
    @pytest.fixture
    def archived(self, tmp_path):
        result = invoke("value", "examples/inflo.json", *AS_OF, "--out", str(tmp_path))
        assert result.exit_code == 0
        return tmp_path, next(tmp_path.iterdir()).name

    def test_runs_lists_the_archive(self, archived):
        out, run_id = archived
        result = invoke("runs", "--out", str(out))

        assert run_id in result.output
        assert "Inflo" in result.output

    def test_explain_reopens_a_run(self, archived):
        out, run_id = archived
        result = invoke("explain", run_id, "--out", str(out))

        assert result.exit_code == 0
        assert "Inflo" in result.output

    def test_explain_can_emit_the_archived_memo(self, archived):
        out, run_id = archived
        result = invoke("explain", run_id, "--out", str(out), "--markdown")

        assert result.output.startswith("# Fair Value Memorandum")

    def test_explaining_an_unknown_run_fails_plainly(self, tmp_path):
        result = invoke("explain", "no-such-run", "--out", str(tmp_path))

        assert result.exit_code == 1
        assert "no archived report" in result.output

    def test_listing_an_empty_archive_is_not_an_error(self, tmp_path):
        result = invoke("runs", "--out", str(tmp_path / "empty"))

        assert result.exit_code == 0
        assert "No archived runs" in result.output
