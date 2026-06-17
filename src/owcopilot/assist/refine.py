"""A generic generate→critique→refine loop, reused by every assist surface that has a critic.

The quest-draft and world-seed loops predate this and keep their own richer trail types; this is the
shared default for new content kinds (characters, dialogue). Adding the loop to a new kind is just
"supply an ``assess`` and a ``regenerate`` callback" — the honest-failure rule (an unparsable
critique is never a pass; it sets ``auto_review_incomplete``) lives here, once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel

from .critic import CritiqueResult

T = TypeVar("T")


class RefineStep(BaseModel):
    """One pass of the loop, surfaced so the human reviewer sees how the draft was improved."""

    round: int
    verdict: str
    score: float
    blocking_count: int  # deterministic gaps still open this round
    auto_review_ok: bool  # False when this round's critique could not be parsed
    fixes: list[str] = []
    summary: str = ""


@dataclass
class RefineOutcome(Generic[T]):
    artifact: T
    trail: list[RefineStep] = field(default_factory=list)
    auto_review_incomplete: bool = False


def run_refine_loop(
    *,
    initial: T,
    max_rounds: int,
    assess: Callable[[T], tuple[list[str], CritiqueResult]],
    regenerate: Callable[[T, list[str]], T],
) -> RefineOutcome[T]:
    """Drive the loop. ``assess(artifact) -> (deterministic_gaps, critique)`` is the objective +
    subjective read; ``regenerate(artifact, fixes) -> artifact`` produces an improved version.

    Accept ONLY when the critic actually passed it (a parsed reply) AND there are no deterministic
    gaps. A critique that failed to parse can never satisfy that — it flags the result for human
    scrutiny instead of being waved through."""
    artifact = initial
    trail: list[RefineStep] = []
    auto_review_incomplete = False
    for round_index in range(max_rounds):
        gaps, critique = assess(artifact)
        auto_review_incomplete = not critique.parse_ok
        fixes = list(critique.actionable_fixes())
        for gap in gaps:
            marker = f"[completeness] 补全：{gap}"
            if marker not in fixes:
                fixes.append(marker)
        trail.append(
            RefineStep(
                round=round_index,
                verdict=critique.verdict,
                score=critique.score,
                blocking_count=len(gaps),
                auto_review_ok=critique.parse_ok,
                fixes=fixes,
                summary=critique.summary,
            )
        )
        if critique.parse_ok and critique.verdict == "pass" and not gaps:
            break
        if not fixes:
            break
        artifact = regenerate(artifact, fixes)
    return RefineOutcome(
        artifact=artifact, trail=trail, auto_review_incomplete=auto_review_incomplete
    )
