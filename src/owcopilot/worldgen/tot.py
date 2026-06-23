"""Tree of Thoughts (Yao et al., 2023) for the premise / dramatic-spine stage.

The premise is the highest-commitment decision in the staged chain: every later stage (factions,
places, cast, quests) is grounded in it, so a weak or flat spine drags the whole world down and is
expensive to undo. That is exactly where ToT pays off — explore several candidate spines, evaluate
each, and keep the best before committing the rest of the chain to it.

This module has two parts:

* :func:`tree_of_thoughts` — the generic, canonical ToT-BFS primitive (propose → evaluate → prune
  to a beam, for ``steps`` layers). It is plain and reusable; the premise application below uses it
  at ``steps=1`` with breadth N (the pragmatic, cost-linear case), but the primitive itself is a
  real multi-step beam search.
* :func:`score_premise` — the deterministic value function for a premise. Keeping the evaluator
  deterministic means the selection is reproducible and $0; a model-backed evaluator can be passed
  to :func:`tree_of_thoughts` instead when a sharper judge is wanted.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object

T = TypeVar("T")

# A premise value function: score a candidate spine, returning (score, rationale). The default is
# the deterministic ``score_premise``; ``LLMPremiseEvaluator`` is the canonical LLM-backed one.
PremiseEvaluator = Callable[[dict[str, Any]], tuple[float, str]]


@dataclass
class ToTCandidate(Generic[T]):
    state: T
    score: float
    rationale: str = ""


@dataclass
class ToTResult(Generic[T]):
    best: T
    best_score: float
    # The final-layer candidates, highest score first (so callers can surface the runners-up).
    evaluated: list[ToTCandidate[T]] = field(default_factory=list)


def tree_of_thoughts(
    *,
    root: T,
    expand: Callable[[T], list[T]],
    evaluate: Callable[[T], tuple[float, str]],
    steps: int = 1,
    beam_width: int = 1,
) -> ToTResult[T]:
    """Canonical ToT breadth-first search with value-based pruning.

    ``expand(state) -> [child, ...]`` proposes the candidate thoughts from a state (its length is
    the breadth ``b``); ``evaluate(state) -> (score, rationale)`` is the value function. Each layer
    keeps the top ``beam_width`` candidates and expands those into the next layer, for ``steps``
    layers. Returns the best leaf found.
    """
    steps = max(1, steps)
    beam_width = max(1, beam_width)
    beam: list[T] = [root]
    final: list[ToTCandidate[T]] = []
    for _ in range(steps):
        scored: list[ToTCandidate[T]] = []
        for state in beam:
            for child in expand(state):
                score, rationale = evaluate(child)
                scored.append(ToTCandidate(state=child, score=score, rationale=rationale))
        if not scored:
            break
        # Stable sort by descending score: ties keep generation order (so the result is fully
        # determined by the inputs, not by sort nondeterminism).
        scored.sort(key=lambda candidate: -candidate.score)
        final = scored
        beam = [candidate.state for candidate in scored[:beam_width]]
    if not final:
        return ToTResult(best=root, best_score=0.0, evaluated=[])
    return ToTResult(best=final[0].state, best_score=final[0].score, evaluated=final)


# --- the premise value function ------------------------------------------------------------------
# A strong dramatic spine is concrete and complete: it names the central conflict, the open
# question, the stakes, the opposing faction axes and the themes. We reward each present, specific
# piece — a flat premise that only has a summary scores low, so ToT pushes the chain toward a world
# with a real engine of conflict.
_SPINE_TEXT_FIELDS = ("central_conflict", "dramatic_question", "stakes")


def score_premise(premise: dict[str, Any]) -> tuple[float, str]:
    score = 0.0
    present: list[str] = []
    for field_name in _SPINE_TEXT_FIELDS:
        if str(premise.get(field_name) or "").strip():
            score += 1.0
            present.append(field_name)
    axes = [a for a in _as_list(premise.get("faction_axes")) if str(a).strip()]
    themes = [t for t in _as_list(premise.get("themes")) if str(t).strip()]
    score += min(len(axes), 4) * 0.5
    score += min(len(themes), 4) * 0.25
    # Specificity: a longer central_conflict is (up to a point) a more concrete engine of conflict.
    conflict_len = len(str(premise.get("central_conflict") or ""))
    score += min(conflict_len / 120.0, 1.0)
    rationale = (
        f"spine[{','.join(present) or 'none'}] axes={len(axes)} themes={len(themes)} "
        f"conflict_chars={conflict_len}"
    )
    return score, rationale


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --- the LLM value function (canonical ToT state evaluation, with a deterministic floor) ---------
#
# Real-LLM testing showed the deterministic score saturating: a strong model reliably returns a
# complete spine, so every candidate scored the same and the search could not discriminate. That is
# exactly the case ToT's LLM evaluator is for — judging the *subjective* quality (specificity,
# tension, originality) that structure alone cannot. We keep the deterministic score as a floor so a
# structurally incomplete spine still can't win, then add the model's rating on top.

# Stable phrase so an offline test double can tell an evaluation call from a generation call. A real
# model treats it as an inert role label.
_PREMISE_EVAL_SENTINEL = "PREMISE_VALUE_EVALUATOR"

_PREMISE_EVAL_SYSTEM = (
    f"You are a {_PREMISE_EVAL_SENTINEL}: a senior open-world narrative director scoring ONE "
    "candidate dramatic spine before the rest of the world is built on it. Judge three dimensions: "
    "specificity (a concrete engine of conflict, not vague), dramatic tension (forces that must "
    "collide now), and originality (not generic boilerplate). Return ONE JSON object only, no "
    'markdown: {"score": <number 0-10>, "reason": "<one sentence>"}.'
)


class LLMPremiseEvaluator:
    """A premise value function that asks a model to rate dramatic quality, over a deterministic
    floor. ``__call__(premise)`` returns ``score_premise``'s structural score PLUS ``weight`` × the
    model's 0–10 rating — so among already-complete candidates (where the structural score ties) the
    model's judgement is the tie-breaker. An unparsable rating degrades to the deterministic score
    alone, never crashing the chain (the same honest-failure rule as the critics)."""

    def __init__(
        self, gateway: LLMGateway, *, task: str = "world_seed", weight: float = 1.0
    ) -> None:
        self.gateway = gateway
        self.task = task
        self.weight = weight

    def __call__(self, premise: dict[str, Any]) -> tuple[float, str]:
        base, base_rationale = score_premise(premise)
        raw = self.gateway.complete(
            task=self.task,
            system=_PREMISE_EVAL_SYSTEM,
            user=json.dumps(premise, ensure_ascii=False),
        )
        try:
            data = extract_json_object(raw)
            score_value: Any = data.get("score")  # None/non-numeric -> caught below
            rating = max(0.0, min(10.0, float(score_value)))
        except (ValueError, TypeError):
            return base, f"{base_rationale}; llm=unparsable→deterministic-only"
        reason = str(data.get("reason", "")).strip()[:70]
        return base + self.weight * rating, f"{base_rationale}; llm={rating:.1f}({reason})"
