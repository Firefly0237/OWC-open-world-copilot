"""A generic generate→critique→refine loop, reused by every assist surface that has a critic.

The quest-draft and world-seed loops predate this and keep their own richer trail types; this is the
shared default for new content kinds (characters, dialogue). Adding the loop to a new kind is just
"supply an ``assess`` and a ``regenerate`` callback" — the honest-failure rule (an unparsable
critique is never a pass; it sets ``auto_review_incomplete``) lives here, once.

This is the **Reflexion** form (Shinn et al., 2023), not just Self-Refine: each round's critique is
distilled into a short verbal *reflection* that is accumulated in memory and fed forward into EVERY
later attempt — so the generator sees the whole history of what went wrong, not only the latest
round's fixes. The reflection memory rides the existing ``fixes`` channel (so no call site changes),
while the per-round reflection is surfaced separately on the trail.
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
    # The verbal self-reflection distilled from this round's critique (Reflexion memory). Carried
    # forward into every later attempt so the generator learns from the whole history.
    reflection: str = ""


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
    reflections: list[str] = []  # accumulated verbal reflections (Reflexion episodic memory)
    auto_review_incomplete = False
    for round_index in range(max_rounds):
        gaps, critique = assess(artifact)
        auto_review_incomplete = not critique.parse_ok
        fixes = list(critique.actionable_fixes())
        for gap in gaps:
            marker = f"[completeness] 补全：{gap}"
            if marker not in fixes:
                fixes.append(marker)
        reflection = summarize_reflection(round_index, critique, gaps)
        trail.append(
            RefineStep(
                round=round_index,
                verdict=critique.verdict,
                score=critique.score,
                blocking_count=len(gaps),
                auto_review_ok=critique.parse_ok,
                fixes=fixes,
                summary=critique.summary,
                reflection=reflection,
            )
        )
        if critique.parse_ok and critique.verdict == "pass" and not gaps:
            break
        if not fixes:
            break
        reflections.append(reflection)
        # Feed the WHOLE reflection history forward (not just this round's fixes) — the Reflexion
        # distinction. It rides the fixes channel so existing regenerate callbacks render it.
        artifact = regenerate(artifact, with_reflection_memory(fixes, reflections))
    return RefineOutcome(
        artifact=artifact, trail=trail, auto_review_incomplete=auto_review_incomplete
    )


def summarize_reflection(round_index: int, critique: CritiqueResult, gaps: list[str]) -> str:
    """Distil one round's critique into a short verbal reflection (the Reflexion 'self-reflection').

    Reused by the quest-draft and world-seed loops so every surface phrases its memory identically.
    """
    summary = critique.summary.strip() or "（无评语）"
    note = f"第{round_index + 1}轮（{critique.verdict}/{critique.score:.2f}）：{summary}"
    if gaps:
        note += f"；仍缺：{'、'.join(gaps[:3])}"
    return note


def with_reflection_memory(fixes: list[str], reflections: list[str]) -> list[str]:
    """Prepend the accumulated reflections as one clearly-marked memory entry ahead of this round's
    fixes. Empty memory returns the fixes unchanged (round 0 has nothing to reflect on yet)."""
    if not reflections:
        return fixes
    memory = "[reflexion-memory] 过往尝试与教训（请勿重复同类问题）：\n" + "\n".join(
        f"  · {line}" for line in reflections
    )
    return [memory, *fixes]
