"""Tests for the optional model-driven peer selection.

The point of these is not that the model is right -- it is that whatever the
model says is recorded, bounded, and never allowed to break the run:

* every call lands in the audit trail with its model id and prompt hash;
* a failure degrades to the deterministic universe instead of propagating;
* a rejection that would leave too few peers is recorded but not applied.

A stub client stands in for the API, so the suite stays hermetic and needs no
credentials.
"""

from __future__ import annotations

import pytest

from tests.conftest import AS_OF, make_context
from vc_audit.domain.audit import AuditTrail
from vc_audit.methods.comps import ComparableCompanyAnalysis
from vc_audit.research import build_researcher
from vc_audit.research.base import PeerCandidates, PeerProposal, PeerReview, PeerVerdict
from vc_audit.research.claude import ClaudePeerResearcher


class StubResponse:
    def __init__(self, parsed):
        self.parsed_output = parsed
        self.usage = type("U", (), {"input_tokens": 120, "output_tokens": 45})()


class StubMessages:
    def __init__(self, outputs, fail_with=None):
        self.outputs = list(outputs)
        self.fail_with = fail_with
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return StubResponse(self.outputs.pop(0))


class StubClient:
    def __init__(self, outputs=(), fail_with=None):
        self.messages = StubMessages(outputs, fail_with)


def trail() -> AuditTrail:
    return AuditTrail(run_id="r1", as_of=AS_OF, scope="method:comps")


CANDIDATES = PeerCandidates(
    business_summary="Enterprise subscription software sold per seat.",
    proposals=[
        PeerProposal(ticker="crm", company_name="Salesforce", rationale="Enterprise SaaS."),
        PeerProposal(ticker="NOW", company_name="ServiceNow", rationale="Workflow SaaS."),
        PeerProposal(ticker="CRM", company_name="Salesforce", rationale="duplicate"),
    ],
)


class TestProposal:
    def test_returns_deduplicated_uppercase_tickers(self, company):
        researcher = ClaudePeerResearcher(client=StubClient([CANDIDATES]))
        result = researcher.propose_peers(company, trail=trail())

        assert result.tickers == ("CRM", "NOW")

    def test_records_the_call_with_model_and_prompt_hash(self, company):
        t = trail()
        ClaudePeerResearcher(client=StubClient([CANDIDATES])).propose_peers(company, trail=t)
        step = t.step("research.propose_peers")

        assert step.inputs["model"] == "claude-opus-5"
        assert len(step.inputs["prompt_sha256"]) == 64
        assert step.inputs["output_tokens"] == 45
        assert "prompt sha256=" in step.formula

    def test_the_same_inputs_hash_identically(self, company):
        first, second = trail(), trail()
        ClaudePeerResearcher(client=StubClient([CANDIDATES])).propose_peers(company, trail=first)
        ClaudePeerResearcher(client=StubClient([CANDIDATES])).propose_peers(company, trail=second)

        assert (
            first.step("research.propose_peers").inputs["prompt_sha256"]
            == second.step("research.propose_peers").inputs["prompt_sha256"]
        )

    def test_the_business_description_reaches_the_prompt(self, company):
        client = StubClient([CANDIDATES])
        ClaudePeerResearcher(client=client).propose_peers(company, trail=trail())
        prompt = client.messages.calls[0]["messages"][0]["content"]

        assert company.business_description in prompt

    def test_the_prompt_forbids_the_model_producing_figures(self, company):
        client = StubClient([CANDIDATES])
        ClaudePeerResearcher(client=client).propose_peers(company, trail=trail())

        assert "must not estimate, calculate" in client.messages.calls[0]["system"]


class TestFailureHandling:
    def test_a_failed_call_returns_none_and_is_recorded(self, company):
        t = trail()
        researcher = ClaudePeerResearcher(client=StubClient(fail_with=RuntimeError("429 quota")))

        assert researcher.propose_peers(company, trail=t) is None
        assert t.step("research.propose_peers.failed").inputs["error"].endswith("429 quota")
        assert any("did not complete" in w for w in t.warnings)

    def test_a_failure_never_propagates_to_the_caller(self, company, provider):
        """A model outage must not be able to stop an audit."""
        ctx = make_context(provider, "comps")
        ctx.researcher = ClaudePeerResearcher(client=StubClient(fail_with=TimeoutError("slow")))
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert outcome.equity_value_usd > 0


class TestReviewApplied:
    def _ctx(self, provider, verdicts):
        ctx = make_context(provider, "comps")
        ctx.researcher = ClaudePeerResearcher(
            client=StubClient([PeerCandidates(business_summary="s", proposals=[]),
                               PeerReview(verdicts=verdicts)])
        )
        return ctx

    def test_rejected_peers_leave_the_set_with_their_reason(self, company, provider):
        ctx = self._ctx(provider, [
            PeerVerdict(ticker="KTRA", comparable=False, rationale="Pre-revenue AI lab, not SaaS."),
            PeerVerdict(
                ticker="OBSD", comparable=False, rationale="Decelerating; different profile."
            ),
        ])
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert "KTRA" not in [p.ticker for p in outcome.peers]
        rejected = {e.ticker: e for e in outcome.excluded_peers}
        assert rejected["KTRA"].stage == "comparability"
        assert rejected["KTRA"].reason == "Pre-revenue AI lab, not SaaS."

    def test_accepted_peers_carry_the_reviewers_rationale(self, company, provider):
        ctx = self._ctx(provider, [
            PeerVerdict(ticker="NMBS", comparable=True, rationale="Closest revenue model match."),
        ])
        outcome = ComparableCompanyAnalysis().compute(company, ctx)
        nmbs = next(p for p in outcome.peers if p.ticker == "NMBS")

        assert nmbs.inclusion_rationale == "Closest revenue model match."

    def test_the_review_lands_in_the_funnel_as_not_comparable(self, company, provider):
        ctx = self._ctx(provider, [
            PeerVerdict(ticker="OBSD", comparable=False, rationale="Different profile."),
        ])
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert outcome.funnel.dropped_not_comparable == 1

    def test_a_run_using_research_warns_that_it_is_not_reproducible(self, company, provider):
        ctx = make_context(provider, "comps")
        ctx.researcher = ClaudePeerResearcher(
            client=StubClient([CANDIDATES, PeerReview(verdicts=[])])
        )
        ComparableCompanyAnalysis().compute(company, ctx)

        assert any("not guaranteed identical between runs" in w for w in ctx.trail.warnings)


class TestReviewGuardrails:
    def test_a_review_that_would_starve_the_peer_set_is_not_applied(self, company, provider):
        """Too few peers is a worse failure than a peer the model disliked."""
        ctx = make_context(provider, "comps")
        ctx.researcher = ClaudePeerResearcher(
            client=StubClient([
                PeerCandidates(business_summary="s", proposals=[]),
                PeerReview(verdicts=[
                    PeerVerdict(ticker=t, comparable=False, rationale="no")
                    for t in ("ATLS", "HLIO", "NMBS", "OBSD", "QDRA", "SGNL")
                ]),
            ])
        )
        outcome = ComparableCompanyAnalysis().compute(company, ctx)

        assert len(outcome.peers) >= 3
        assert any("was recorded but not applied" in w for w in ctx.trail.warnings)


class TestBuildResearcher:
    def test_disabled_returns_nothing(self):
        assert build_researcher(False) is None

    def test_enabled_without_a_key_fails_loudly(self, monkeypatch):
        """Silently producing a non-researched valuation would misrepresent it."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            build_researcher(True)


class TestSensitivityIsolation:
    def test_sensitivity_reruns_do_not_repeat_model_calls(self, provider):
        """Perturbation runs must not cost API calls or move the peer set."""
        ctx = make_context(provider, "comps")
        ctx.researcher = ClaudePeerResearcher(client=StubClient([CANDIDATES]))
        scratch = AuditTrail(run_id="r1", as_of=AS_OF, scope="sensitivity")

        assert ctx.with_overrides({"x": 1}, scratch).researcher is None
