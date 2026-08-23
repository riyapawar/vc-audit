"""Peer research backed by Claude.

Uses structured outputs, so the response is validated against a Pydantic schema
at the API boundary and arrives as typed objects rather than as prose that has
to be parsed hopefully.

Every call writes an audit step carrying the model id, the effort setting, the
SHA-256 of the exact prompt sent, and the token usage. Recording the prompt hash
rather than the prompt itself keeps the trail compact while still letting a
reviewer prove that two runs asked the same question -- and prove that a memo's
recorded judgement belongs to the prompt that is in the repository today.

Failures never propagate. If the model is unreachable, out of quota, or returns
something unusable, the researcher records the failure and returns ``None``; the
valuation continues on the deterministic sector universe. A model outage must
not be able to stop an audit.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from vc_audit.domain.audit import AuditTrail
from vc_audit.domain.models import PeerCompany, PortfolioCompany
from vc_audit.research.base import PeerCandidates, PeerReview

DEFAULT_MODEL = "claude-opus-5"

#: Peer selection is a judgement call over a short list, not a research project.
#: Low effort keeps it fast and cheap without measurably hurting the choice.
DEFAULT_EFFORT = "low"

MAX_TOKENS = 8000

#: Sized well above the three-peer floor: candidates are lost to missing filings
#: and to review, so proposing a dozen leaves room for both.
TARGET_PROPOSALS = 16

PROPOSE_SYSTEM = """\
You are assisting a financial auditor with a Comparable Company Analysis of a \
private venture-backed company.

Your only job is to name US-listed public companies whose *business* most \
resembles the subject, and to say why. You must not estimate, calculate, or \
assert any financial figure: every valuation number is computed separately from \
SEC filings. Do not state revenues, multiples, market caps, or valuations.

Judge comparability on business model, revenue model, customer type, and growth \
profile -- not on sector label alone. A company that sells subscription software \
to enterprises is not comparable to one that sells implementation services, even \
though both are called "software".

Only propose companies you are confident are currently listed on a US exchange \
and file with the SEC. A ticker you are unsure of is worse than one fewer \
candidate, because a wrong ticker silently resolves to a different company.\
"""

REVIEW_SYSTEM = """\
You are assisting a financial auditor reviewing a comparable-company peer set.

For each candidate you are shown, decide whether it is genuinely comparable to \
the subject company and give a specific reason. The names shown are the SEC \
registrant names, taken from EDGAR -- if a name does not match the business you \
would expect for that ticker, that is itself grounds to reject it.

Reject a peer when its business model, customer type, or revenue model differs \
materially from the subject. Do not reject on size alone: listed comparables are \
routinely far larger than a private company, and that is handled elsewhere.

Give a reason a reviewer could disagree with. "Not comparable" is not a reason; \
"consumer marketplace rather than enterprise software" is.\
"""


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ClaudePeerResearcher:
    """Model-driven peer proposal and review, fully recorded in the audit trail."""

    name = "claude"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        """Args:
        model: Claude model id.
        effort: Reasoning effort -- ``low`` through ``max``.
        api_key: Overrides ``ANTHROPIC_API_KEY``.
        client: Pre-built Anthropic client, for tests.

        Raises:
            RuntimeError: The ``anthropic`` package is not installed.
        """
        self.model = model
        self.effort = effort
        if client is not None:
            self._client = client
            return
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise RuntimeError(
                "peer research needs the 'anthropic' package; install it with "
                "`pip install -e \".[research]\"`"
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    # ---- shared plumbing -------------------------------------------------

    def _ask(
        self,
        *,
        label: str,
        description: str,
        system: str,
        prompt: str,
        schema: type,
        trail: AuditTrail,
    ) -> Any | None:
        """Make one structured call and record it as an audit step.

        Returns ``None`` on any failure, after recording why.
        """
        prompt_hash = _fingerprint(prompt)
        system_hash = _fingerprint(system)
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": self.effort},
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
            parsed = response.parsed_output
        except Exception as exc:  # noqa: BLE001 - any client failure degrades identically
            trail.warn(
                f"Peer research step '{label}' did not complete ({type(exc).__name__}: {exc}); "
                f"the valuation continued on the standing sector universe."
            )
            trail.record(
                label=f"research.{label}.failed",
                description=f"{description} The call failed and was not used.",
                formula=f"{self.model} @ effort={self.effort}",
                inputs={
                    "model": self.model,
                    "effort": self.effort,
                    "prompt_sha256": prompt_hash,
                    "system_sha256": system_hash,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                output=None,
            )
            return None

        usage = getattr(response, "usage", None)
        trail.record(
            label=f"research.{label}",
            description=(
                f"{description} Judgement made by a language model; the prompt hash "
                f"below identifies exactly what it was asked."
            ),
            formula=f"{self.model} @ effort={self.effort}, prompt sha256={prompt_hash[:16]}…",
            inputs={
                "model": self.model,
                "effort": self.effort,
                "prompt_sha256": prompt_hash,
                "system_sha256": system_hash,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            },
            output=parsed.model_dump() if hasattr(parsed, "model_dump") else parsed,
        )
        return parsed

    # ---- PeerResearcher --------------------------------------------------

    def propose_peers(
        self, company: PortfolioCompany, *, trail: AuditTrail
    ) -> PeerCandidates | None:
        scale = (
            f"LTM revenue of about ${company.ltm_revenue_usd:,.0f}"
            if company.ltm_revenue_usd
            else "not supplied"
        )
        prompt = (
            f"Subject company: {company.name}\n"
            f"Sector: {company.sector}\n"
            f"Business description: {company.business_description or 'not supplied'}\n"
            f"Approximate scale: {scale}\n\n"
            f"Propose up to {TARGET_PROPOSALS} US-listed companies to use as comparables, "
            f"each with a one-sentence reason grounded in business model rather than sector "
            f"label. Also give a one-paragraph read of what the subject actually does."
        )
        return self._ask(
            label="propose_peers",
            description="Ask the model to nominate listed comparables for the subject.",
            system=PROPOSE_SYSTEM,
            prompt=prompt,
            schema=PeerCandidates,
            trail=trail,
        )

    def review_peers(
        self, company: PortfolioCompany, peers: list[PeerCompany], *, trail: AuditTrail
    ) -> PeerReview | None:
        roster = "\n".join(
            f"- {peer.ticker}: {peer.name} (SEC registrant name)" for peer in peers
        )
        prompt = (
            f"Subject company: {company.name}\n"
            f"Sector: {company.sector}\n"
            f"Business description: {company.business_description or 'not supplied'}\n\n"
            f"Candidate peers assembled from SEC filings:\n{roster}\n\n"
            f"For each ticker above, decide whether it belongs in this peer set and give a "
            f"specific reason. Return a verdict for every ticker listed."
        )
        return self._ask(
            label="review_peers",
            description="Ask the model to judge each assembled peer's comparability.",
            system=REVIEW_SYSTEM,
            prompt=prompt,
            schema=PeerReview,
            trail=trail,
        )
