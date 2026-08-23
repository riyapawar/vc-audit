"""Tests for the audit ledger.

These are the most important tests in the suite. If the ledger silently drops a
step or produces an unstable fingerprint, every downstream guarantee this tool
makes is void.
"""

from __future__ import annotations

from datetime import date

import pytest

from vc_audit.domain.audit import AuditTrail, SourceRef

AS_OF = date(2026, 8, 22)


def new_trail(scope: str = "test") -> AuditTrail:
    return AuditTrail(run_id="r1", as_of=AS_OF, scope=scope)


class TestRecording:
    def test_record_returns_its_output_so_calls_read_as_expressions(self):
        trail = new_trail()
        result = trail.record(
            label="add", description="Add two numbers.", formula="2 + 3", output=5
        )
        assert result == 5

    def test_steps_are_sequenced_in_call_order(self):
        trail = new_trail()
        for i in range(3):
            trail.record(label=f"s{i}", description="d", formula="f", output=i)
        assert [s.seq for s in trail.steps] == [1, 2, 3]
        assert [s.label for s in trail.steps] == ["s0", "s1", "s2"]

    def test_step_lookup_by_label(self):
        trail = new_trail()
        trail.record(label="target", description="d", formula="f", output=42)
        assert trail.step("target").output == 42

    def test_step_lookup_raises_for_unknown_label(self):
        trail = new_trail("method:dcf")
        with pytest.raises(KeyError, match="method:dcf"):
            trail.step("nope")


class TestAssumptions:
    def test_unreviewed_defaults_excludes_user_and_derived_values(self):
        trail = new_trail()
        trail.assume(key="wacc", value=0.15, origin="engine_default", rationale="r")
        trail.assume(key="growth", value=0.03, origin="user_provided", rationale="r")
        trail.assume(key="median", value=8.2, origin="derived", rationale="r")

        assert [a.key for a in trail.unreviewed_defaults()] == ["wacc"]

    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            (0.205, "percent", "20.50%"),
            (8.2222, "multiple", "8.22x"),
            (1_500_000, "usd", "$1,500,000"),
            ("EMCLOUD", None, "EMCLOUD"),
        ],
    )
    def test_display_value_formats_by_unit(self, value, unit, expected):
        trail = new_trail()
        trail.assume(key="k", value=value, origin="engine_default", rationale="r", unit=unit)
        assert trail.assumptions[0].display_value() == expected


class TestSources:
    def test_sources_are_deduplicated_across_steps_and_assumptions(self):
        trail = new_trail()
        source = SourceRef(provider="p", dataset="d", as_of=AS_OF)
        other = SourceRef(provider="p", dataset="other", as_of=AS_OF)

        trail.record(label="a", description="d", formula="f", output=1, sources=[source])
        trail.record(label="b", description="d", formula="f", output=2, sources=[source, other])
        trail.assume(key="k", value=1, origin="derived", rationale="r", source=source)

        assert [s.dataset for s in trail.sources] == ["d", "other"]

    def test_citation_includes_the_note_when_present(self):
        source = SourceRef(provider="p", dataset="d", as_of=AS_OF, note="stale")
        assert source.cite() == "p:d (as of 2026-08-22) -- stale"


class TestDeterminism:
    def test_run_id_is_a_pure_function_of_the_seed(self):
        seed = {"company": "Basis AI", "as_of": "2026-08-22"}
        first = AuditTrail.start(as_of=AS_OF, scope="engine", seed=seed)
        second = AuditTrail.start(as_of=AS_OF, scope="engine", seed=seed)
        assert first.run_id == second.run_id

    def test_run_id_is_insensitive_to_seed_key_ordering(self):
        forward = AuditTrail.start(as_of=AS_OF, scope="e", seed={"a": 1, "b": 2})
        reversed_ = AuditTrail.start(as_of=AS_OF, scope="e", seed={"b": 2, "a": 1})
        assert forward.run_id == reversed_.run_id

    def test_different_inputs_produce_different_run_ids(self):
        first = AuditTrail.start(as_of=AS_OF, scope="e", seed={"wacc": 0.15})
        second = AuditTrail.start(as_of=AS_OF, scope="e", seed={"wacc": 0.18})
        assert first.run_id != second.run_id

    def test_child_trail_inherits_run_id_and_date(self):
        parent = AuditTrail.start(as_of=AS_OF, scope="engine", seed={"x": 1})
        child = parent.child("method:dcf")
        assert (child.run_id, child.as_of) == (parent.run_id, parent.as_of)
        assert child.scope == "method:dcf"


class TestFingerprint:
    def test_identical_trails_fingerprint_identically(self):
        def build() -> AuditTrail:
            trail = new_trail()
            trail.record(label="a", description="d", formula="f", output=1)
            trail.assume(key="k", value=2, origin="engine_default", rationale="r")
            return trail

        assert build().fingerprint() == build().fingerprint()

    def test_fingerprint_changes_when_a_step_changes(self):
        trail = new_trail()
        trail.record(label="a", description="d", formula="f", output=1)
        before = trail.fingerprint()

        trail.record(label="b", description="d", formula="f", output=2)
        assert trail.fingerprint() != before

    def test_fingerprint_changes_when_only_a_warning_is_added(self):
        """A warning is audit evidence, so it must be inside the tamper seal."""
        trail = new_trail()
        trail.record(label="a", description="d", formula="f", output=1)
        before = trail.fingerprint()

        trail.warn("terminal value dominates")
        assert trail.fingerprint() != before
