"""World-level reviewer for the quests stage (the critic half of a generate→critique→refine loop).

This is the same Reflexion / Self-Refine pattern as ``assist/critic.py``'s ``QuestCritic`` — and it
reuses that module's tolerant ``parse_critique`` and ``CritiqueResult`` so a broken critique can
never wedge the loop — but it reviews the *whole quests batch* the staged world-seed chain produced,
not a single approved-content quest. The capstone stage is the one most likely to drift: quests are
generated last and must actually connect the cast and places the earlier stages established, so a
review-and-refine pass here is where coherence is won or lost.

Discipline (identical to the round-22 quest loop): the critic is a *signal*, not a gate. The
deterministic grounding check (:func:`quest_grounding_gaps`) is the objective bar; the human review
queue is the final sign-off. The critic only raises autonomous quality before a human looks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..assist.critic import CritiqueResult, critique_with_retry
from ..assist.industry import QUEST_RUBRIC_SOURCES, industry_source_block
from ..assist.refine import summarize_reflection, with_reflection_memory
from ..llm.gateway import LLMGateway
from . import stages
from .models import WorldRefineRound


def quest_grounding_gaps(
    quests: list[dict[str, Any]],
    *,
    npc_refs: set[str],
    place_refs: set[str],
) -> list[str]:
    """Deterministic completeness/grounding check over the quests batch — the objective signal the
    refine loop closes on (the world-level analogue of readiness ``assess_quest``).

    A gap is anything a level designer could not build from: a quest with no concrete objective,
    fewer than two stages, or a giver/location that is not one of the cast/places the world actually
    contains (``npc_refs`` / ``place_refs`` hold both the ids and display names earlier stages
    emitted, so a quest may reference either form).
    """
    gaps: list[str] = []
    for index, raw in enumerate(quests, start=1):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("title") or raw.get("id") or f"任务{index}")
        if not str(raw.get("objective") or raw.get("description") or "").strip():
            gaps.append(f"「{label}」缺少一句话目标（谁要做什么、为何此刻重要）。")
        stages_value = raw.get("stages")
        stage_count = len(stages_value) if isinstance(stages_value, list) else 0
        if stage_count < 2:
            gaps.append(f"「{label}」至少需要 2 个阶段，且每个阶段点明地点与玩家行动。")
        giver = str(raw.get("giver_npc") or "").strip()
        if not giver or (npc_refs and giver not in npc_refs):
            gaps.append(f"「{label}」的 giver_npc 必须引用已确立角色之一（{_sample(npc_refs)}）。")
        location = str(raw.get("location") or "").strip()
        if not location or (place_refs and location not in place_refs):
            gaps.append(f"「{label}」的 location 必须引用已确立地点之一（{_sample(place_refs)}）。")
    return gaps


def _sample(refs: set[str]) -> str:
    return "、".join(sorted(refs)[:4]) if refs else "无可用 id"


class WorldQuestCritic:
    def __init__(self, *, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def critique(
        self,
        *,
        brief: str,
        quests: list[dict[str, Any]],
        context_lines: list[str],
        gaps: list[str],
    ) -> CritiqueResult:
        return critique_with_retry(
            self.gateway,
            task="world_seed",
            system=_critic_system_prompt(),
            user=_critic_user_message(
                brief=brief, quests=quests, context_lines=context_lines, gaps=gaps
            ),
        )


def _critic_system_prompt() -> str:
    return (
        f"{stages.stage_marker(stages.QUEST_CRITIQUE)}\n"
        "You are a demanding senior open-world quest director reviewing a freshly generated quest "
        "batch before it reaches a human approver. Judge it on four dimensions and return ONE JSON "
        "object only (no markdown):\n"
        '{"verdict": "pass" | "revise", "score": 0.0-1.0, "summary": "...", '
        '"dimensions": [{"dimension": "intent|grounding|completeness|craft", '
        '"severity": "blocker|minor|ok", "issue": "...", "fix": "..."}]}\n'
        + industry_source_block(*QUEST_RUBRIC_SOURCES)
        + "\n"
        "- intent: do the quests deliver the brief's premise and central conflict?\n"
        "- grounding: do giver_npc / location / stages reference ONLY the cast and places listed "
        "below, by the ids given, inventing nothing?\n"
        "- completeness: objective, >=2 concrete stages, a grounded giver and location present?\n"
        "- craft: specific, world-specific hooks rather than generic fetch-quest boilerplate?\n"
        "Set verdict to 'revise' if ANY dimension is a blocker. Each non-ok dimension MUST give a "
        "concrete, actionable fix the writer can apply. Be strict but fair; do not rewrite quests."
    )


def _critic_user_message(
    *, brief: str, quests: list[dict[str, Any]], context_lines: list[str], gaps: list[str]
) -> str:
    parts = [f"Brief:\n{brief.strip()}", ""]
    if context_lines:
        parts.append("Cast and places the quests may reference (use these ids exactly):")
        parts.extend(context_lines)
        parts.append("")
    if gaps:
        parts.append("确定性接地检查已发现下列待修正问题（视为 completeness/grounding blocker）：")
        parts.extend(f"- {gap}" for gap in gaps)
        parts.append("")
    parts.append("待评审的任务批次 JSON：")
    parts.append(json.dumps(quests, ensure_ascii=False))
    return "\n".join(parts)


@dataclass
class QuestRefineOutcome:
    quests: list[Any]
    relations: list[Any]
    reference_rows: list[Any]
    trail: list[WorldRefineRound]
    auto_review_incomplete: bool


def run_quest_refine_loop(
    *,
    critic: WorldQuestCritic,
    max_rounds: int,
    quests: list[Any],
    relations: list[Any],
    reference_rows: list[Any],
    npc_refs: set[str],
    place_refs: set[str],
    context_lines: list[str],
    brief: str,
    regenerate: Callable[[list[Any], list[str]], tuple[list[Any], list[Any], list[Any]]],
    emit: Callable[[str], None],
) -> QuestRefineOutcome:
    """The single generate→critique→refine loop shared by genesis (``service``) and expansion
    (``expand``) capstone stages — one implementation so the honesty rules below live in one place.

    Objective gate = deterministic grounding gaps; subjective signal = the critic. Accept ONLY when
    the critic actually passed it (a parsed reply) AND there are no grounding gaps. An unparsable
    critique is never a pass — it sets ``auto_review_incomplete`` so the caller flags the batch for
    human scrutiny. ``regenerate(prior_quests, fixes)`` (returns quests, relations, reference_rows)
    is the stage-specific re-call the caller supplies."""
    trail: list[WorldRefineRound] = []
    reflections: list[str] = []  # accumulated Reflexion memory across rounds
    auto_review_incomplete = False
    for round_index in range(max_rounds):
        gaps = quest_grounding_gaps(quests, npc_refs=npc_refs, place_refs=place_refs)
        critique = critic.critique(
            brief=brief, quests=quests, context_lines=context_lines, gaps=gaps
        )
        auto_review_incomplete = not critique.parse_ok
        fixes = merge_quest_fixes(critique.actionable_fixes(), gaps)
        reflection = summarize_reflection(round_index, critique, gaps)
        trail.append(
            WorldRefineRound(
                round=round_index,
                verdict=critique.verdict,
                score=critique.score,
                gap_count=len(gaps),
                fixes=fixes,
                summary=critique.summary,
                auto_review_ok=critique.parse_ok,
                reflection=reflection,
            )
        )
        if critique.parse_ok and critique.verdict == "pass" and not gaps:
            break
        if not fixes:
            break
        emit("refining")
        # Feed the whole reflection history forward (Reflexion), not just this round's fixes.
        reflections.append(reflection)
        quests, relations, reference_rows = regenerate(
            quests, with_reflection_memory(fixes, reflections)
        )
    return QuestRefineOutcome(quests, relations, reference_rows, trail, auto_review_incomplete)


def merge_quest_fixes(critique_fixes: list[str], gaps: list[str]) -> list[str]:
    """Combine the critic's subjective fixes with the deterministic grounding gaps (the single
    definition both genesis and expansion use — was copy-pasted as ``_merge_world_fixes`` /
    ``_merge_fixes`` in two files)."""
    fixes = list(critique_fixes)
    for gap in gaps:
        marker = f"[completeness] 补全：{gap}"
        if marker not in fixes:
            fixes.append(marker)
    return fixes
