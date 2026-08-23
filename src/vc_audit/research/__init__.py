"""Optional model-driven peer selection.

Off by default. See :mod:`vc_audit.research.base` for why, and for the two rules
that make a model's judgement admissible as audit evidence.
"""

from __future__ import annotations

import os

from vc_audit.research.base import (
    PeerCandidates,
    PeerProposal,
    PeerResearcher,
    PeerReview,
    PeerVerdict,
)

API_KEY_ENV = "ANTHROPIC_API_KEY"

__all__ = [
    "API_KEY_ENV",
    "PeerCandidates",
    "PeerProposal",
    "PeerResearcher",
    "PeerReview",
    "PeerVerdict",
    "build_researcher",
    "research_available",
]


def research_available() -> bool:
    """True when peer research could run: package installed and key present."""
    if not os.environ.get(API_KEY_ENV):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def build_researcher(enabled: bool, **kwargs) -> PeerResearcher | None:
    """Construct a researcher, or explain why one cannot be built.

    Args:
        enabled: Whether the caller asked for research.
        **kwargs: Passed through to the researcher.

    Returns:
        A researcher, or ``None`` when research was not requested.

    Raises:
        RuntimeError: Research was requested but cannot run. Failing loudly here
            is deliberate -- silently producing a non-researched valuation when
            the auditor asked for a researched one would misrepresent the
            evidence behind the number.
    """
    if not enabled:
        return None
    if not os.environ.get(API_KEY_ENV):
        raise RuntimeError(
            f"peer research needs {API_KEY_ENV} to be set. Run without --research to use "
            f"the deterministic sector universe instead."
        )
    from vc_audit.research.claude import ClaudePeerResearcher

    return ClaudePeerResearcher(**kwargs)
