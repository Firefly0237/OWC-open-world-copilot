"""LLM reviewer for generated drafts (the critic half of a generate→critique→refine loop).

Deterministic audit answers "is it correct?" and readiness answers "is it complete?" — both are
code, so neither can judge *subjective craft*: does this quest actually deliver what the brief
asked, is it grounded in the world facts, is it specific rather than generic? That judgement is
what a human reviewer currently carries alone. The critic moves that load *before* the review
queue: it scores a draft and returns concrete, actionable fixes that feed a bounded refinement
loop, so the human receives near-production-quality content instead of a first draft.

The critic is NOT a gate. Deterministic audit stays the hard correctness gate and human review
stays the final sign-off; the critic only raises autonomous quality. "LLM judging LLM as an
automatic gate" would inherit same-source bias — but "LLM critic as a refinement signal, with
deterministic checks and a human still downstream" is defense in depth, not a single point of trust.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import Quest, Term
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from .industry import (
    BARK_RUBRIC_SOURCES,
    CHARACTER_RUBRIC_SOURCES,
    DIALOGUE_RUBRIC_SOURCES,
    FLAVOR_RUBRIC_SOURCES,
    QUEST_RUBRIC_SOURCES,
    industry_source_block,
)

# Sentinel the offline test double keys on to return a critique instead of a draft. Real providers
# ignore it; it just has to be a stable, unmistakable phrase in the system prompt.
_REVIEWER_SENTINEL = "STRICT_QUEST_REVIEWER"

# Same idea for the character/dialogue critics, but as a natural phrase. The offline doubles in
# characters.py / dialogue_trees.py import these to tell a critique request from a generate request,
# and each critic's system prompt embeds the matching one — kept here so prompt and detector can
# never drift apart (a drift would only mean the offline critic stops firing, which the refine
# tests would catch, but a single source is cheaper than relying on that).
CHARACTER_CRITIQUE_MARKER = "reviewing a character sheet"
DIALOGUE_CRITIQUE_MARKER = "reviewing a branching dialogue tree"
BARK_CRITIQUE_MARKER = "reviewing NPC bark variants"
FLAVOR_CRITIQUE_MARKER = "reviewing item/skill/achievement flavor text"

# When a deterministic check (lint) already found concrete problems, the critic message lists them
# under this header. The offline critic double keys on it to return "revise" until they are gone —
# the same flip-once-clean behaviour as the quest/character offline critiques.
DETERMINISTIC_PROBLEMS_HEADER = "确定性检查发现以下问题"

_VALID_SEVERITIES = {"blocker", "minor", "ok"}

# Whitelist for dimension values — mirrors the rubric in every critic system prompt.
# Anything the LLM invents that is not in this set gets silently normalised to "craft".
# Symmetry with _VALID_SEVERITIES: same pattern, same guarantee.
_VALID_DIMENSIONS = {
    "intent", "grounding", "completeness", "craft", "voice",
    "branching", "coherence", "function", "flavor", "style",
    "variety", "topic",
}


class CritiqueDimension(BaseModel):
    dimension: str  # intent | grounding | completeness | craft
    severity: str  # blocker | minor | ok
    issue: str = ""
    fix: str = ""


class CritiqueResult(BaseModel):
    verdict: str  # "pass" | "revise"
    score: float = 0.0  # 0..1, higher is better
    dimensions: list[CritiqueDimension] = Field(default_factory=list)
    summary: str = ""
    # False when the model's reply could not be parsed as a critique. The loop must NOT treat an
    # unparsable critique as a pass — that would silently disable the quality gate. Instead the
    # caller surfaces auto_review_incomplete so the human reviewer knows the auto-check did not run.
    parse_ok: bool = True

    def actionable_fixes(self) -> list[str]:
        """The concrete fixes worth feeding back to the generator (blockers and minors)."""
        out: list[str] = []
        for dim in self.dimensions:
            if dim.severity == "ok":
                continue
            piece = dim.fix.strip() or dim.issue.strip()
            if piece:
                out.append(f"[{dim.dimension}] {piece}")
        return out


def critique_with_retry(
    gateway: LLMGateway,
    *,
    task: str,
    system: str,
    user: str,
    evaluator_gateway: LLMGateway | None = None,
) -> CritiqueResult:
    """One critique call + a single root-cause retry if the reply won't parse. Every critic shares
    this so the honest-failure rule lives in ONE place: the usual reason a critique won't parse is
    the model wrapping the JSON in prose, so we demand a bare object once before giving up — far
    better than faking a pass. If it still fails, the unparsable result flows out (parse_ok=False)
    so the caller can flag the draft for human scrutiny instead of waving it through.

    When evaluator_gateway is provided it is used in place of the main gateway. If the evaluator
    raises LLMGatewayError, we fall back to the main gateway and mark the result with
    '[evaluator-fallback]' in the summary (honest degradation, never silently drops the feature).
    """
    from ..llm.gateway import LLMGatewayError  # avoid circular import at module level

    active = evaluator_gateway if evaluator_gateway is not None else gateway
    used_fallback = False

    def _call(gw: LLMGateway, sys_suffix: str = "") -> CritiqueResult:
        return parse_critique(gw.complete(task=task, system=system + sys_suffix, user=user))

    try:
        result = _call(active)
        if not result.parse_ok:
            result = _call(active, _JSON_ONLY_RETRY)
    except LLMGatewayError:
        if evaluator_gateway is None:
            raise
        # evaluator failed -> fallback to main gateway
        used_fallback = True
        result = _call(gateway)
        if not result.parse_ok:
            result = _call(gateway, _JSON_ONLY_RETRY)

    if used_fallback and result.parse_ok:
        result = result.model_copy(
            update={"summary": f"[evaluator-fallback] {result.summary}"}
        )
    return result


_JSON_ONLY_RETRY = (
    "\n\nYour previous reply could not be parsed. Reply with ONLY the JSON object described "
    "above — no prose before or after it, no markdown code fences."
)


class QuestCritic:
    def __init__(self, *, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def critique(
        self,
        *,
        brief: str,
        quest: Quest,
        context_lines: list[str],
        readiness_missing: list[str],
        evaluator_gateway: LLMGateway | None = None,
        terms: list[Term] | None = None,
        inject_terms: bool = True,
        lessons: list[dict] | None = None,      # IN-B3 M1: default None
        inject_lessons: bool = False,            # IN-B3 M1: default False (guard: off by default)
    ) -> CritiqueResult:
        return critique_with_retry(
            self.gateway,
            task="quest_critique",
            system=_critic_system_prompt(
                terms=terms,
                inject_terms=inject_terms,
                lessons=lessons,
                inject_lessons=inject_lessons,
            ),
            user=_critic_user_message(
                brief=brief,
                quest=quest,
                context_lines=context_lines,
                readiness_missing=readiness_missing,
            ),
            evaluator_gateway=evaluator_gateway,
        )


def _critic_system_prompt(
    terms: list[Term] | None = None,
    inject_terms: bool = True,
    lessons: list[dict] | None = None,   # IN-B3 M1
    inject_lessons: bool = False,        # IN-B3 M1: default False
) -> str:
    from .lessons import build_critic_lesson_block  # IN-B3 M1
    from .term_injection import build_term_block_for_critic
    term_block = build_term_block_for_critic(list(terms) if terms else []) if inject_terms else ""
    term_section = f"\n\n{term_block}" if term_block else ""
    # IN-B3 M1: build lesson block (empty string when inject_lessons=False or no lessons)
    lesson_block = build_critic_lesson_block(lessons or [], inject_lessons=inject_lessons)
    lesson_section = f"\n\n{lesson_block}" if lesson_block else ""
    return (
        f"You are a {_REVIEWER_SENTINEL}: a demanding senior quest designer reviewing a draft "
        "before it reaches a human approver. Judge it on four dimensions and return ONE JSON "
        "object only (no markdown):\n"
        '{"verdict": "pass" | "revise", "score": 0.0-1.0, "summary": "...", '
        '"dimensions": [{"dimension": "intent|grounding|completeness|craft", '
        '"severity": "blocker|minor|ok", "issue": "...", "fix": "..."}]}\n'
        + industry_source_block(*QUEST_RUBRIC_SOURCES)
        + "\n"
        "- intent: does the quest actually deliver what the brief asked for?\n"
        "- grounding: does it use ONLY the provided world facts (entity ids), inventing nothing?\n"
        "- completeness: objective, stages, rewards, giver, location all present and meaningful?\n"
        "- craft: is it specific and evocative rather than generic boilerplate?\n"
        "Set verdict to 'revise' if ANY dimension is a blocker. Each non-ok dimension MUST give a "
        "concrete, actionable fix the writer can apply. Be strict but fair; do not rewrite the "
        f"quest yourself.{term_section}{lesson_section}"  # IN-B3 M1: lesson appended at end
    )


def _critic_user_message(
    *, brief: str, quest: Quest, context_lines: list[str], readiness_missing: list[str]
) -> str:
    parts = [f"Brief:\n{brief.strip()}", ""]
    if context_lines:
        parts.append("World facts the quest may reference:")
        parts.extend(context_lines)
        parts.append("")
    if readiness_missing:
        parts.append(
            "A deterministic completeness check already flagged these missing pieces "
            "(treat as completeness blockers): " + "、".join(readiness_missing)
        )
        parts.append("")
    parts.append("Draft quest JSON to review:")
    parts.append(json.dumps(quest.model_dump(mode="json", exclude_none=True), ensure_ascii=False))
    return "\n".join(parts)


def parse_critique(raw: str) -> CritiqueResult:
    """Parse a critique reply. Robust to the model wrapping the JSON in prose/fences (via the shared
    ``extract_json_object``), but it MUST NOT fake a verdict: an unparsable reply means the quality
    gate failed to run, so it returns verdict='revise' with parse_ok=False — never a silent 'pass'.
    The caller (QuestCritic.critique) retries once; if that also fails, the loop surfaces
    auto_review_incomplete so a human knows to scrutinize the draft."""
    try:
        data: dict[str, Any] = extract_json_object(raw)
    except ValueError:
        return CritiqueResult(
            verdict="revise",
            score=0.0,
            summary="评审输出无法解析为合法 JSON（自动评审未完成）",
            parse_ok=False,
        )
    # Item 1: empty JSON object or missing verdict → parse failure (fail-closed, never silent pass).
    if not data:
        return CritiqueResult(
            verdict="revise",
            score=0.0,
            summary="评审输出为空对象（自动评审未完成）",
            parse_ok=False,
        )
    raw_verdict = data.get("verdict")
    if raw_verdict is None:
        return CritiqueResult(
            verdict="revise",
            score=0.0,
            summary="评审输出缺少 verdict 字段（自动评审未完成）",
            parse_ok=False,
        )
    dimensions = []
    for item in data.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "minor")).lower()
        if severity not in _VALID_SEVERITIES:
            severity = "minor"
        # Item 3: dimension whitelist — any value outside the declared rubric is normalised to
        # "craft". This is the same graceful-degradation pattern as the severity whitelist above,
        # and closes the indirect-injection vector: a crafted dimension name can no longer
        # propagate adversarial text into the lesson-text template.
        dimension = str(item.get("dimension", "craft")).lower()
        if dimension not in _VALID_DIMENSIONS:
            dimension = "craft"
        dimensions.append(
            CritiqueDimension(
                dimension=dimension,
                severity=severity,
                issue=str(item.get("issue", "")),
                fix=str(item.get("fix", "")),
            )
        )
    verdict = str(raw_verdict).lower()
    has_blocker = any(d.severity == "blocker" for d in dimensions)
    if verdict not in {"pass", "revise"}:
        verdict = "revise" if has_blocker else "pass"
    # A model that says "pass" while flagging a blocker is self-contradictory; trust the blocker.
    if verdict == "pass" and has_blocker:
        verdict = "revise"
    score = data.get("score")
    score_val = float(score) if isinstance(score, (int, float)) else 0.0
    return CritiqueResult(
        verdict=verdict,
        score=max(0.0, min(1.0, score_val)),
        dimensions=dimensions,
        summary=str(data.get("summary", "")),
    )


# --- character & dialogue critics (same Reflexion pattern, kind-specific rubric) ----------------
#
# Each is just a system rubric + a user-message builder; both route through ``critique_with_retry``
# so the parse/retry/honest-failure behaviour is identical to the quest critic. Adding a critic for
# a new content kind is "write one prompt", not "re-implement the loop".

_VERDICT_SHAPE = (
    'Return ONE JSON object only (no markdown): {"verdict": "pass" | "revise", "score": 0.0-1.0, '
    '"summary": "...", "dimensions": [{"dimension": "...", "severity": "blocker|minor|ok", '
    '"issue": "...", "fix": "..."}]}. Set verdict to "revise" if ANY dimension is a blocker; each '
    "non-ok dimension MUST give a concrete, actionable fix. Be strict but fair; do not rewrite it."
)


class CharacterCritic:
    def __init__(self, *, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def critique(
        self,
        *,
        concept: str,
        profile: dict[str, str],
        summary: str,
        context_lines: list[str],
        missing_sections: list[str],
        evaluator_gateway: LLMGateway | None = None,
        lessons: list[dict] | None = None,   # IN-B3 M1
        inject_lessons: bool = False,        # IN-B3 M1: default False (guard)
    ) -> CritiqueResult:
        from .lessons import build_critic_lesson_block  # IN-B3 M1
        lesson_block = build_critic_lesson_block(lessons or [], inject_lessons=inject_lessons)
        lesson_section = f"\n\n{lesson_block}" if lesson_block else ""
        return critique_with_retry(
            self.gateway,
            task="character_profile",
            system=(
                f"You are a demanding senior character designer {CHARACTER_CRITIQUE_MARKER} "
                "before it reaches a human approver. Judge it on four dimensions:\n"
                "- concept: does the sheet actually deliver the creator's one-line concept?\n"
                "- grounding: is it consistent with the world facts, inventing no contradictions?\n"
                "- completeness: are appearance / personality / backstory / motivation / abilities "
                "/ weakness / voice each present and substantive (not filler)?\n"
                "- voice: is the speaking style distinctive and consistent with the character?\n"
                + industry_source_block(*CHARACTER_RUBRIC_SOURCES)
                + "\n"
                + _VERDICT_SHAPE
                + lesson_section  # IN-B3 M1: lesson appended after _VERDICT_SHAPE
            ),
            user=_character_user_message(
                concept=concept,
                profile=profile,
                summary=summary,
                context_lines=context_lines,
                missing_sections=missing_sections,
            ),
            evaluator_gateway=evaluator_gateway,
        )


def _character_user_message(
    *,
    concept: str,
    profile: dict[str, str],
    summary: str,
    context_lines: list[str],
    missing_sections: list[str],
) -> str:
    parts = [f"Concept:\n{concept.strip()}", ""]
    if context_lines:
        parts.append("World facts the character must stay consistent with:")
        parts.extend(context_lines)
        parts.append("")
    if missing_sections:
        parts.append(
            "A deterministic check found these profile sections empty (completeness blockers): "
            + "、".join(missing_sections)
        )
        parts.append("")
    parts.append("Character sheet to review:")
    parts.append(json.dumps({"summary": summary, **profile}, ensure_ascii=False))
    return "\n".join(parts)


class DialogueCritic:
    def __init__(self, *, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def critique(
        self,
        *,
        brief: str,
        nodes: dict[str, Any],
        speaker_ids: list[str],
        structure_problems: list[str],
        evaluator_gateway: LLMGateway | None = None,
        lessons: list[dict] | None = None,   # IN-B3 M1
        inject_lessons: bool = False,        # IN-B3 M1: default False (guard)
    ) -> CritiqueResult:
        from .lessons import build_critic_lesson_block  # IN-B3 M1
        lesson_block = build_critic_lesson_block(lessons or [], inject_lessons=inject_lessons)
        lesson_section = f"\n\n{lesson_block}" if lesson_block else ""
        return critique_with_retry(
            self.gateway,
            task="dialogue_tree",
            system=(
                f"You are a demanding senior narrative designer {DIALOGUE_CRITIQUE_MARKER} before "
                "it reaches a human approver. Judge it on four dimensions:\n"
                "- voice: does each line sound like its speaker (distinct, in-character)?\n"
                "- branching: are the player choices meaningfully different (real decisions, not "
                "cosmetic re-phrasings of the same outcome)?\n"
                "- coherence: does the conversation advance the brief and read naturally?\n"
                "- grounding: are all speakers among the provided ids, inventing none?\n"
                + industry_source_block(*DIALOGUE_RUBRIC_SOURCES)
                + "\n"
                + _VERDICT_SHAPE
                + lesson_section  # IN-B3 M1
            ),
            user=_dialogue_user_message(
                brief=brief,
                nodes=nodes,
                speaker_ids=speaker_ids,
                structure_problems=structure_problems,
            ),
            evaluator_gateway=evaluator_gateway,
        )


def _dialogue_user_message(
    *,
    brief: str,
    nodes: dict[str, Any],
    speaker_ids: list[str],
    structure_problems: list[str],
) -> str:
    parts = [
        f"Brief:\n{brief.strip()}",
        f"Speakers (use only these ids): {', '.join(speaker_ids)}",
        "",
    ]
    if structure_problems:
        parts.append(
            "A deterministic structure check found these problems (treat as blockers): "
            + "；".join(structure_problems)
        )
        parts.append("")
    parts.append("Dialogue nodes to review:")
    parts.append(json.dumps(nodes, ensure_ascii=False))
    return "\n".join(parts)


def _problems_block(problems: list[str]) -> list[str]:
    """The shared way every critic lists deterministic (lint) findings, so the offline double can
    recognise them and the real critic treats them as blockers."""
    if not problems:
        return []
    return [f"{DETERMINISTIC_PROBLEMS_HEADER}（按 blocker 处理）：" + "；".join(problems), ""]


class BarkCritic:
    """Subjective quality reviewer for a speaker's batch of bark variants — the bark half of the
    same generate→critique→refine loop. Adding this kind to the loop was just this prompt."""

    def __init__(self, *, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def critique(
        self,
        *,
        topic: str,
        voice_card_json: str,
        variants: list[str],
        lint_problems: list[str],
        evaluator_gateway: LLMGateway | None = None,
        lessons: list[dict] | None = None,   # IN-B3 M1
        inject_lessons: bool = False,        # IN-B3 M1: default False (guard)
    ) -> CritiqueResult:
        from .lessons import build_critic_lesson_block  # IN-B3 M1
        lesson_block = build_critic_lesson_block(lessons or [], inject_lessons=inject_lessons)
        lesson_section = f"\n\n{lesson_block}" if lesson_block else ""
        parts = [
            f"Topic the barks must address:\n{topic.strip()}",
            f"Voice card the lines must stay within:\n{voice_card_json}",
            "",
            *_problems_block(lint_problems),
            "Bark variants to review:",
            json.dumps(variants, ensure_ascii=False),
        ]
        return critique_with_retry(
            self.gateway,
            task="barks_batch",
            system=(
                f"You are a demanding senior narrative designer {BARK_CRITIQUE_MARKER} — short, "
                "in-world NPC one-liners — before they reach a human approver. Judge four "
                "dimensions:\n"
                "- voice: do the lines sound like THIS speaker (the voice card), in-character?\n"
                "- topic: does each variant actually address the requested topic?\n"
                "- variety: are the variants meaningfully different, not trivial rewordings?\n"
                "- craft: punchy and natural, within length, no filler or meta narration?\n"
                + industry_source_block(*BARK_RUBRIC_SOURCES)
                + "\n"
                + _VERDICT_SHAPE
                + lesson_section  # IN-B3 M1
            ),
            user="\n".join(parts),
            evaluator_gateway=evaluator_gateway,
        )


class FlavorCritic:
    """Subjective quality reviewer for a batch of flavor entries — the flavor half of the loop."""

    def __init__(self, *, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def critique(
        self,
        *,
        category: str,
        theme: str,
        style_text: str,
        entries: list[dict[str, str]],
        lint_problems: list[str],
        evaluator_gateway: LLMGateway | None = None,
        lessons: list[dict] | None = None,   # IN-B3 M1
        inject_lessons: bool = False,        # IN-B3 M1: default False (guard)
    ) -> CritiqueResult:
        from .lessons import build_critic_lesson_block  # IN-B3 M1
        lesson_block = build_critic_lesson_block(lessons or [], inject_lessons=inject_lessons)
        lesson_section = f"\n\n{lesson_block}" if lesson_block else ""
        parts = [
            f"Category: {category}. Theme: {theme or '(none)'}.",
            f"Style guide the writing must respect:\n{style_text or '(none)'}",
            "",
            *_problems_block(lint_problems),
            "Flavor entries to review (name / description / flavor):",
            json.dumps(entries, ensure_ascii=False),
        ]
        return critique_with_retry(
            self.gateway,
            task="flavor_batch",
            system=(
                f"You are a demanding senior game writer {FLAVOR_CRITIQUE_MARKER} before it "
                "reaches a human approver. Judge four dimensions:\n"
                "- function: is each description clear about what it does / how it is earned?\n"
                "- flavor: is the flavor line atmospheric and in the world's voice, not generic?\n"
                "- style: does it respect the style guide?\n"
                "- craft: concise, within budget, no filler or meta narration?\n"
                + industry_source_block(*FLAVOR_RUBRIC_SOURCES)
                + "\n"
                + _VERDICT_SHAPE
                + lesson_section  # IN-B3 M1
            ),
            user="\n".join(parts),
            evaluator_gateway=evaluator_gateway,
        )
