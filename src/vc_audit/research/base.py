"""The peer-research contract: using a model to choose comparables.

A sector tag is a blunt instrument. "SaaS" contains both a 90%-gross-margin
infrastructure business and a services-heavy implementation shop, and no static
ticker list distinguishes them. Judging which businesses genuinely resemble each
other is the part of comps analysis a language model is actually good at.

Two rules make that usable as audit evidence rather than as a black box:

**The model judges; Python calculates.** The researcher may propose tickers and
may argue that a peer is or is not comparable. It never produces a number. Every
enterprise value, multiple, quartile and valuation is computed in code from SEC
filings, so no figure in the memo traces back to a model's arithmetic.

**Every model call is recorded as an audit step.** Model id, effort, the
SHA-256 of the exact prompt sent, token usage, and the full structured response
all land in the trail. A reviewer can see that a machine made a judgement, which
machine, on what input, and what it said -- which is the minimum bar for letting
one influence a valuation at all.

Research is **off by default**. With it off the peer set is a pure function of
the sector universe and the run is exactly reproducible. Turning it on trades
some of that reproducibility for better comparability, and the trade is recorded
as a warning rather than left for the reader to infer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from vc_audit.domain.audit import AuditTrail
from vc_audit.domain.models import PeerCompany, PortfolioCompany


class PeerProposal(BaseModel):
    """One candidate comparable put forward by the research layer."""

    ticker: str = Field(description="US exchange ticker, uppercase.")
    company_name: str = Field(description="The model's claim about who this is.")
    rationale: str = Field(description="Why this business resembles the subject.")


class PeerCandidates(BaseModel):
    """The research layer's proposed candidate set."""

    business_summary: str = Field(
        description="One-paragraph read of what the subject company actually does."
    )
    proposals: list[PeerProposal] = Field(default_factory=list)

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.ticker.strip().upper() for p in self.proposals if p.ticker))


class PeerVerdict(BaseModel):
    """A keep-or-drop decision on one candidate, with its reason."""

    ticker: str
    comparable: bool
    rationale: str = Field(description="Specific reason, not a restatement of the sector.")


class PeerReview(BaseModel):
    """The research layer's verdict on every candidate it was shown."""

    verdicts: list[PeerVerdict] = Field(default_factory=list)

    def rejected(self) -> dict[str, str]:
        return {v.ticker.upper(): v.rationale for v in self.verdicts if not v.comparable}

    def accepted(self) -> dict[str, str]:
        return {v.ticker.upper(): v.rationale for v in self.verdicts if v.comparable}


@runtime_checkable
class PeerResearcher(Protocol):
    """Optional model-driven peer selection."""

    name: str

    def propose_peers(
        self, company: PortfolioCompany, *, trail: AuditTrail
    ) -> PeerCandidates | None:
        """Suggest listed comparables for ``company``.

        Returns ``None`` when the call fails; peer selection then falls back to
        the standing sector universe rather than the run failing. Proposals are
        *added* to that universe, never substituted for it, so a model outage
        degrades the peer set rather than emptying it.
        """
        ...

    def review_peers(
        self, company: PortfolioCompany, peers: list[PeerCompany], *, trail: AuditTrail
    ) -> PeerReview | None:
        """Judge which assembled peers are genuinely comparable.

        Sees each peer's **SEC registrant name**, not the name the proposer
        claimed. That is what catches a hallucinated ticker: ``BRX`` resolves to
        Brixmor Property Group, a REIT, and the review drops the mismatch on
        sight rather than valuing a fintech against a shopping-centre landlord.
        """
        ...
