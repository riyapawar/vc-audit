"""Tests for .env loading.

The security-relevant behaviour is that a real environment variable always wins,
so a stale file on disk can never silently override a deliberately exported
value or a CI secret. The rest is tolerance: a convenience file should never be
able to stop a valuation.
"""

from __future__ import annotations

import pytest

from vc_audit.env import load_env_file, parse_env_text

KEY = "VC_AUDIT_TEST_KEY"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)


def write(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    def test_reads_a_plain_pair(self):
        assert parse_env_text("A=1") == {"A": "1"}

    def test_ignores_comments_and_blank_lines(self):
        assert parse_env_text("# note\n\n  \nA=1\n") == {"A": "1"}

    def test_tolerates_a_leading_export(self):
        """People paste from shell history."""
        assert parse_env_text('export A="1"') == {"A": "1"}

    @pytest.mark.parametrize("line", ['A="sk-x"', "A='sk-x'", "A=sk-x", "  A = sk-x  "])
    def test_strips_surrounding_quotes_and_whitespace(self, line):
        assert parse_env_text(line) == {"A": "sk-x"}

    def test_keeps_equals_signs_inside_the_value(self):
        assert parse_env_text("A=a=b=c") == {"A": "a=b=c"}

    def test_an_empty_value_is_still_a_key(self):
        assert parse_env_text("A=") == {"A": ""}

    @pytest.mark.parametrize("line", ["no equals sign here", "=orphan", "bad key!=1"])
    def test_malformed_lines_are_skipped_not_raised(self, line):
        assert parse_env_text(line) == {}

    def test_a_later_line_wins_within_the_file(self):
        assert parse_env_text("A=1\nA=2") == {"A": "2"}


class TestLoading:
    def test_applies_a_key_that_is_not_already_set(self, tmp_path, monkeypatch):
        import os

        applied = load_env_file(write(tmp_path, f"{KEY}=from-file"))

        assert applied == [KEY]
        assert os.environ[KEY] == "from-file"

    def test_a_real_environment_variable_always_wins(self, tmp_path, monkeypatch):
        """A stale file must never override a deliberately exported value."""
        import os

        monkeypatch.setenv(KEY, "from-environment")
        applied = load_env_file(write(tmp_path, f"{KEY}=from-file"))

        assert applied == []
        assert os.environ[KEY] == "from-environment"

    def test_returns_names_only_so_logging_it_cannot_leak_a_secret(self, tmp_path):
        applied = load_env_file(write(tmp_path, f"{KEY}=sk-ant-secret-value"))

        assert applied == [KEY]
        assert "sk-ant-secret-value" not in str(applied)

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert load_env_file(tmp_path / "absent") == []

    def test_a_directory_is_not_an_error(self, tmp_path):
        assert load_env_file(tmp_path) == []

    def test_a_binary_file_is_not_an_error(self, tmp_path):
        path = tmp_path / ".env"
        path.write_bytes(b"\xff\xfe\x00\x01binary")
        assert load_env_file(path) == []


class TestExampleFile:
    def test_the_committed_example_parses_and_leaks_nothing(self):
        """.env.example is committed, so it must never carry a real value."""
        from pathlib import Path

        parsed = parse_env_text(Path(".env.example").read_text(encoding="utf-8"))

        assert "ANTHROPIC_API_KEY" in parsed
        assert all(value == "" for value in parsed.values()), "a placeholder has a value in it"
