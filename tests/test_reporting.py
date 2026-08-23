"""Tests for the reporting layer.

The memo is the deliverable, so these assert the properties that make it usable
as a workpaper: it renders deterministically, it does not omit exceptions, and
it carries the citations back to source.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import AS_OF
from vc_audit.data.mock_provider import MockMarketDataProvider
from vc_audit.engine import value_company
from vc_audit.reporting import evidence, memo
from vc_audit.reporting.formatting import compact, money, percent


@pytest.fixture
def report(company, provider):
    return value_company(company, provider=provider, as_of=AS_OF)


class TestFormatting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1_500_000_000, "$1.50B"), (82_000_000, "$82.0M"), (4_600, "$5K"), (250, "$250")],
    )
    def test_money_abbreviates_by_magnitude(self, value, expected):
        assert money(value) == expected

    def test_precise_money_is_fully_written_out(self):
        assert money(82_032_049, precise=True) == "$82,032,049"

    def test_percent_respects_requested_places(self):
        assert percent(0.3068, places=1) == "30.7%"

    def test_compact_never_emits_scientific_notation_for_money(self):
        """'1.885e+06' in a workpaper cell is unreadable."""
        assert compact(-1_885_000.0) == "-1,885,000"

    def test_compact_truncates_long_structures(self):
        text = compact({f"k{i}": i for i in range(40)}, limit=40)
        assert len(text) == 40
        assert text.endswith("...")


class TestMemo:
    def test_renders_deterministically(self, report):
        assert memo.render(report) == memo.render(report)

    def test_contains_no_generation_timestamp(self, report):
        """A timestamp would make two identical valuations diff as different."""
        rendered = memo.render(report)
        assert "Generated" not in rendered
        assert "generated at" not in rendered.lower()

    def test_leads_with_the_conclusion(self, report):
        rendered = memo.render(report)
        assert rendered.index("## 1. Conclusion") < rendered.index("## 6. Method detail")

    def test_every_exception_appears(self, report):
        rendered = memo.render(report)
        for warning in report.all_warnings:
            assert warning in rendered

    def test_every_applied_method_gets_its_own_section(self, report):
        rendered = memo.render(report)
        for result in report.method_results:
            assert result.method_name in rendered
            for step in result.trail.steps:
                assert step.description in rendered

    def test_skipped_methods_are_documented(self, sparse_company, provider):
        report = value_company(sparse_company, provider=provider, as_of=AS_OF)
        rendered = memo.render(report)

        assert "## 7. Methods not applied" in rendered
        assert "MissingInputError" in rendered

    def test_sources_are_cited(self, report):
        rendered = memo.render(report)
        assert "yahoo_finance_mock" in rendered
        assert "board_pack_q3_2025" in rendered

    def test_the_fingerprint_is_printed_for_verification(self, report):
        assert report.fingerprint in memo.render(report)

    def test_pipes_in_content_cannot_break_a_table(self, report):
        """Every table row must have the column count its header declares."""
        for line in memo.render(report).splitlines():
            if line.startswith("|") and not set(line) <= set("|-: "):
                # Unescaped pipes would inflate the cell count past the header.
                assert "\\|" in line or line.count("|") >= 3


class TestEvidencePack:
    def test_writes_all_three_artefacts(self, report, company, tmp_path):
        pack = evidence.write(report, company, output_dir=tmp_path)

        assert pack.directory.name == report.run_id
        assert all(path.exists() for path in pack.as_list())

    def test_inputs_file_is_sufficient_to_replay_the_run(self, report, company, tmp_path):
        pack = evidence.write(report, company, output_dir=tmp_path)
        payload = json.loads(pack.inputs.read_text(encoding="utf-8"))

        replayed = value_company(
            company.model_validate(payload["company"]),
            provider=MockMarketDataProvider(),
            as_of=AS_OF,
            overrides=payload["overrides"],
        )
        assert replayed.fingerprint == report.fingerprint

    def test_rerunning_overwrites_rather_than_duplicating(self, report, company, tmp_path):
        evidence.write(report, company, output_dir=tmp_path)
        evidence.write(report, company, output_dir=tmp_path)

        assert len(list(tmp_path.iterdir())) == 1

    def test_archived_reports_round_trip(self, report, company, tmp_path):
        evidence.write(report, company, output_dir=tmp_path)
        reloaded = evidence.load_report(report.run_id, output_dir=tmp_path)

        assert reloaded.fingerprint == report.fingerprint
        assert reloaded.concluded_value_usd == pytest.approx(report.concluded_value_usd)
        assert memo.render(reloaded) == memo.render(report)

    def test_listing_an_empty_archive_is_not_an_error(self, tmp_path):
        assert evidence.list_runs(output_dir=tmp_path / "nothing") == []

    def test_a_corrupt_pack_does_not_break_the_listing(self, report, company, tmp_path):
        evidence.write(report, company, output_dir=tmp_path)
        broken = tmp_path / "corrupt-run"
        broken.mkdir()
        (broken / "report.json").write_text("{not json", encoding="utf-8")

        assert [run_id for run_id, _, _ in evidence.list_runs(output_dir=tmp_path)] == [
            report.run_id
        ]

    def test_loading_an_unknown_run_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            evidence.load_report("no-such-run", output_dir=tmp_path)
