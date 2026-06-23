"""Constrained draft generation.

Draft generation is no longer the project axis. It is a sidecar assist feature: produce a
structured draft, mark it pending review, audit it immediately, and let humans decide.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.models import Issue, Severity
from ..audit.runner import AuditRunner
from ..content.lang import detect_language, language_directive
from ..content.models import ContentBundle, Origin, Quest, ReviewStatus
from ..content.normalize import slug_id
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..readiness import assess_quest
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack
from .critic import CritiqueResult, QuestCritic
from .industry import QUEST_RUBRIC_SOURCES, industry_source_block
from .refine import summarize_reflection, with_reflection_memory


class RefineRound(BaseModel):
    """One pass of the generate→critique→refine loop, surfaced so the human reviewer can see how
    the draft was improved before it reached them (review is a step, not the quality source)."""

    round: int
    verdict: str
    score: float
    readiness_score: float
    new_error_count: int
    fixes: list[str] = Field(default_factory=list)
    summary: str = ""
    auto_review_ok: bool = True  # False when this round's critique could not be parsed
    # Verbal self-reflection distilled from this round (Reflexion memory), carried forward.
    reflection: str = ""


class DraftResult(BaseModel):
    quest: Quest
    issues: list[Issue] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    refine_trail: list[RefineRound] = Field(default_factory=list)
    # True when the final draft did NOT clear an auto-review (the critic's reply was unparsable even
    # after a retry). The draft still goes to human review — but flagged for extra scrutiny rather
    # than silently treated as if it had passed.
    auto_review_incomplete: bool = False


class QuestDraftService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        context_builder: ContextPackBuilder,
        audit_runner: AuditRunner,
        bundle: ContentBundle,
        critic: QuestCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.audit_runner = audit_runner
        self.bundle = bundle
        # The loop is opt-in: without a critic the service is the original single-shot draft.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

    def draft_quest(self, brief: str, *, budget_tokens: int = 800) -> DraftResult:
        pack = self.context_builder.build(brief, budget_tokens=budget_tokens)
        context_lines = [f"[{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits]
        # The pre-draft audit depends only on the (fixed) existing bundle, so compute its
        # fingerprints once instead of re-auditing the whole world on every refine round.
        baseline_fingerprints = {
            issue_fingerprint(issue)
            for issue in self.audit_runner.run(AuditContext.from_bundle(self.bundle)).issues
        }
        quest = self._generate(brief, pack)
        trail: list[RefineRound] = []
        reflections: list[str] = []  # accumulated Reflexion memory across rounds
        auto_review_incomplete = False

        for round_index in range(self.max_refine_rounds):
            assert self.critic is not None  # max_refine_rounds == 0 when critic is None
            new_errors = sum(
                1
                for issue in self._new_issues(quest, baseline_fingerprints)
                if issue.severity is Severity.ERROR
            )
            readiness = assess_quest(quest)
            critique = self.critic.critique(
                brief=brief,
                quest=quest,
                context_lines=context_lines,
                readiness_missing=readiness.missing,
            )
            # An unparsable critique is NOT a pass — the quality gate failed to run. Track it so the
            # final draft is flagged for human scrutiny instead of being waved through.
            auto_review_incomplete = not critique.parse_ok
            fixes = _merge_fixes(critique, readiness.missing, new_errors_present=new_errors > 0)
            reflection = summarize_reflection(round_index, critique, readiness.missing)
            trail.append(
                RefineRound(
                    round=round_index,
                    verdict=critique.verdict,
                    score=critique.score,
                    readiness_score=readiness.score,
                    new_error_count=new_errors,
                    fixes=fixes,
                    summary=critique.summary,
                    auto_review_ok=critique.parse_ok,
                    reflection=reflection,
                )
            )
            # Accept ONLY when the critic actually passed it (a parsed reply), the subjective bar is
            # met, and there are no new audit errors. A failed parse can never satisfy this.
            if critique.parse_ok and critique.verdict == "pass" and new_errors == 0:
                break
            if not fixes:
                break
            # Feed the whole reflection history forward (Reflexion), not just this round's fixes.
            reflections.append(reflection)
            quest = self._generate(
                brief, pack, prior=quest, feedback=with_reflection_memory(fixes, reflections)
            )

        quest = _with_unique_quest_id(quest, existing_ids=set(self.bundle.quests))
        quest = quest.model_copy(
            update={
                "origin": Origin.AI_DRAFT,
                "review_status": ReviewStatus.PENDING_REVIEW,
                "metadata": {
                    **quest.metadata,
                    "context_refs": pack.refs,
                    **({"refine_rounds": len(trail)} if trail else {}),
                    **({"auto_review_incomplete": True} if auto_review_incomplete else {}),
                },
            }
        )
        new_issues = self._new_issues(quest, baseline_fingerprints)
        return DraftResult(
            quest=quest,
            issues=new_issues,
            context_refs=pack.refs,
            refine_trail=trail,
            auto_review_incomplete=auto_review_incomplete,
        )

    def revise(self, prior: Quest, feedback: str, *, budget_tokens: int = 800) -> DraftResult:
        """Regenerate the draft to address a reviewer's feedback (feedback-driven revision).

        Reuses the same prior+feedback path the refine loop uses; the quest's own objective grounds
        the retrieval since the original brief is not stored. The result re-enters review."""
        brief = (prior.objective or prior.title or "").strip()
        pack = self.context_builder.build(brief, budget_tokens=budget_tokens)
        baseline_fingerprints = {
            issue_fingerprint(issue)
            for issue in self.audit_runner.run(AuditContext.from_bundle(self.bundle)).issues
        }
        quest = self._generate(brief, pack, prior=prior, feedback=[feedback.strip()])
        quest = _with_unique_quest_id(quest, existing_ids=set(self.bundle.quests))
        quest = quest.model_copy(
            update={
                "origin": Origin.AI_DRAFT,
                "review_status": ReviewStatus.PENDING_REVIEW,
                "metadata": {**quest.metadata, "revised_from_feedback": True},
            }
        )
        return DraftResult(
            quest=quest,
            issues=self._new_issues(quest, baseline_fingerprints),
            context_refs=pack.refs,
        )

    def _generate(
        self,
        brief: str,
        pack: ContextPack,
        *,
        prior: Quest | None = None,
        feedback: list[str] | None = None,
    ) -> Quest:
        system = _system_prompt(pack, brief=brief)
        user = _draft_user_message(brief, prior=prior, feedback=feedback)
        raw = self.gateway.complete(task="quest_draft", system=system, user=user)
        try:
            return parse_quest_draft(raw)
        except ValueError:
            # One honest retry with a strict JSON-only nudge before failing: richer prose can run
            # long / occasionally wrap in stray text. Still raises if the retry is unparseable too.
            strict = user + "\n\n严格只返回一个完整的 JSON 对象，不要任何额外文字、不要省略。"
            raw = self.gateway.complete(task="quest_draft", system=system, user=strict)
            return parse_quest_draft(raw)

    def _candidate_bundle(self, quest: Quest) -> ContentBundle:
        audit_bundle = self.bundle.model_copy(deep=True)
        audit_bundle.quests[quest.id] = quest
        return audit_bundle

    def _new_issues(self, quest: Quest, baseline_fingerprints: set[str]) -> list[Issue]:
        """Issues the draft INTRODUCES, diffed against the pre-draft baseline computed once by the
        caller (so a refine loop doesn't re-audit the unchanged existing world every round)."""
        after = self.audit_runner.run(AuditContext.from_bundle(self._candidate_bundle(quest)))
        return [
            issue for issue in after.issues if issue_fingerprint(issue) not in baseline_fingerprints
        ]


def _merge_fixes(
    critique: CritiqueResult, readiness_missing: list[str], *, new_errors_present: bool
) -> list[str]:
    fixes = list(critique.actionable_fixes())
    for missing in readiness_missing:
        marker = f"[completeness] 补全：{missing}"
        if marker not in fixes:
            fixes.append(marker)
    if new_errors_present:
        fixes.append("[grounding] 修正引用/世界观错误：只引用世界事实中存在的实体 id。")
    return fixes


def _draft_user_message(
    brief: str, *, prior: Quest | None = None, feedback: list[str] | None = None
) -> str:
    if prior is None or not feedback:
        return brief
    prior_json = json.dumps(prior.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    fix_lines = "\n".join(f"- {fix}" for fix in feedback)
    return (
        f"{brief}\n\n"
        "[REFINE] 这是上一版草稿。请产出改进后的完整 JSON：保留可用部分，逐条解决下列问题，"
        "补全缺失字段，使任务更具体、可量产。\n"
        f"上一版草稿：\n{prior_json}\n\n"
        f"必须解决的问题：\n{fix_lines}"
    )


def parse_quest_draft(raw: str) -> Quest:
    data = extract_json_object(raw)
    if "id" not in data:
        data["id"] = slug_id(str(data.get("title") or "quest_draft"), prefix="quest")
    return Quest.model_validate(_normalize_quest_payload(data))


def _with_unique_quest_id(quest: Quest, *, existing_ids: set[str]) -> Quest:
    if quest.id not in existing_ids:
        return quest
    base_id = quest.id.strip() or slug_id(quest.title or "quest_draft", prefix="quest")
    if not base_id:
        base_id = "quest_draft"
    candidate = f"{base_id}_draft"
    index = 2
    while candidate in existing_ids:
        candidate = f"{base_id}_draft_{index}"
        index += 1
    return quest.model_copy(
        update={
            "id": candidate,
            "metadata": {
                **quest.metadata,
                "model_requested_id": quest.id,
                "id_collision_resolved": True,
            },
        }
    )


def _as_str_list(value: Any) -> list[str]:
    """Coerce a ref-list field to ``list[str]``, whatever shape the model emitted.

    Real models drift: ``None``, a scalar, a ``{}`` object, or — seen in a round-29 live run —
    a list of OBJECTS (``dialogue_refs: [{"id": "dial_001", "speaker": ...}, ...]``) where the
    schema wants a list of id strings. We pull the id out and keep what the model meant; whether
    that id resolves is the audit's job, not the parser's. A draft must never 500 on shape."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [s for item in value.values() if (s := str(item).strip())]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                ref = str(item.get("id") or item.get("ref") or item.get("text_key") or "").strip()
            else:
                ref = str(item).strip()
            if ref:
                out.append(ref)
        return out
    return []


def _normalize_quest_payload(data: dict) -> dict:
    """Tolerate the list/dict shape drift real models actually produce.

    Round-2 real-LLM testing showed deepseek emitting `"rewards": {}` (empty object) and
    occasionally scalar strings where the schema wants lists. Strictness belongs to the audit
    layer, not JSON shape parsing — mirror the QA-side lesson."""
    normalized = dict(data)
    # Scalar string fields sometimes arrive as numbers (round-10 live rerun: stage ids
    # came back as ints). Pydantic v2 rightly refuses int->str, so stringify here.
    for key in ("id", "title", "giver_npc", "location", "objective"):
        value = normalized.get(key)
        if value is not None and not isinstance(value, str):
            normalized[key] = str(value)
    for key in ("prerequisites", "dialogue_refs", "localization_keys", "tags"):
        if key in normalized:
            normalized[key] = _as_str_list(normalized[key])
    order = normalized.get("timeline_order")
    if order is not None and not isinstance(order, bool) and not isinstance(order, int):
        # Round-10 live run: the model answered `"timeline_order": "side"`. Coerce numeric
        # strings; anything else degrades to None with the raw value kept in metadata so
        # the reviewer still sees what the model meant — a draft must never 500 on this.
        try:
            normalized["timeline_order"] = int(str(order).strip())
        except ValueError:
            normalized["timeline_order"] = None
            meta = normalized.get("metadata")
            normalized["metadata"] = {
                **(meta if isinstance(meta, dict) else {}),
                "model_timeline_order": str(order),
            }
    rewards = normalized.get("rewards")
    if rewards is None and "rewards" in normalized:
        normalized["rewards"] = []
    elif isinstance(rewards, dict):
        if not rewards:
            normalized["rewards"] = []
        elif "kind" in rewards or "value" in rewards or "type" in rewards:
            normalized["rewards"] = [rewards]
        else:  # {"gold": 75} style
            normalized["rewards"] = [
                {"kind": str(kind), "value": str(value)} for kind, value in rewards.items()
            ]
    if isinstance(normalized.get("rewards"), list):
        normalized["rewards"] = [
            _normalize_reward(reward)
            for reward in normalized["rewards"]
            if isinstance(reward, dict)
        ]
    stages = normalized.get("stages")
    if stages is None and "stages" in normalized:
        normalized["stages"] = []
    elif isinstance(stages, dict):
        stages = list(stages.values()) if stages else []
        normalized["stages"] = stages
    if isinstance(normalized.get("stages"), list):
        normalized["stages"] = [
            _normalize_stage(stage, index)
            for index, stage in enumerate(normalized["stages"], start=1)
            if isinstance(stage, dict)
        ]
    if not isinstance(normalized.get("metadata"), dict):
        normalized.pop("metadata", None)
    return normalized


def _normalize_reward(reward: dict) -> dict:
    """Models describe rewards loosely: `{"type": "experience", "amount": 100}` (round-3) or, once
    the quality bar made drafts richer, `{"id":..., "description":"亲和力", "value":""}` with no
    `kind` at all. Alias whatever names the reward kind so the draft lands for human review instead
    of crashing; `value` falls back to `amount`."""
    normalized = dict(reward)
    if "kind" not in normalized or not str(normalized.get("kind") or "").strip():
        for alias in ("type", "description", "name", "label", "title", "id"):
            value = normalized.get(alias)
            if isinstance(value, str) and value.strip():
                normalized["kind"] = value.strip()
                break
        else:
            normalized["kind"] = "reward"
    normalized.pop("type", None)
    if "value" not in normalized:
        normalized["value"] = str(normalized.get("amount", ""))
    return normalized


def _normalize_stage(stage: dict, index: int) -> dict:
    """Models call the stage text `description`/`text`/`name` interchangeably; the schema wants
    `summary`. Keep whatever id they gave, default one otherwise."""
    normalized = dict(stage)
    raw_id = normalized.get("id")
    if raw_id is not None and not isinstance(raw_id, str):
        normalized["id"] = str(raw_id)
    if not str(normalized.get("id") or "").strip():
        normalized["id"] = f"stage_{index}"
    if not str(normalized.get("summary") or "").strip():
        for alias in ("description", "text", "name", "objective"):
            value = normalized.get(alias)
            if isinstance(value, str) and value.strip():
                normalized["summary"] = value.strip()
                break
        else:
            normalized["summary"] = str(normalized["id"])
    entities = normalized.get("required_entities")
    if isinstance(entities, str):
        normalized["required_entities"] = [entities] if entities.strip() else []
    elif entities is None and "required_entities" in normalized:
        normalized["required_entities"] = []
    return normalized


def _system_prompt(pack: ContextPack, *, brief: str = "") -> str:
    context_lines = [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits]
    # Keep the output in the brief's language — the (English) quality bar below was drifting the
    # model to English on Chinese briefs; the directive pins player-facing text back to the source.
    lang = language_directive(detect_language(brief)) if brief.strip() else ""
    lang_line = f"{lang}\n" if lang else ""
    return (
        "Draft one structured Quest as JSON using only the provided content context. "
        "Return keys compatible with owcopilot.content.models.Quest: id, title, giver_npc, "
        "location, objective, prerequisites, timeline_order, localization_keys, dialogue_refs, "
        "stages, rewards, tags, metadata. Use entity ids, not display names, for references. "
        "The draft is not approved content; it will enter human review.\n\n"
        + industry_source_block(*QUEST_RUBRIC_SOURCES)
        + "\n"
        # Quality bar (from the 二游剧情 rubric): a quest is a small drama, not a checklist. Thin,
        # abstract stages ("learn the history", "make a choice") are the #1 failure mode to avoid.
        "QUALITY BAR — write stages as a small drama, not a to-do list:\n"
        "1. Each stage is a CONCRETE SCENE: where it happens + what the player does/sees + what is "
        "at stake right there. Never an abstract step like '了解历史' or '做出选择'.\n"
        "2. Arc across stages: setup -> rising conflict -> climax or a real choice "
        "(铺垫->冲突->高潮/抉择). Build ONE escalating spine. The choice and its outcomes belong "
        "INSIDE the climax stage (描述抉择与各分支后果在同一阶段内), NOT split into one stage per "
        "ending — never write parallel 'choose A' / 'choose B' / 'outcome A' / 'outcome B' as "
        "separate sibling stages.\n"
        "3. Give the key choice a real COST and at least one non-obvious consequence.\n"
        "4. Prefer a reversal, a hidden truth, or a moral dilemma over a fetch errand. Match the "
        "world's tone; use concrete imagery, not abstract sentiment.\n"
        "5. No filler — every stage must advance stakes or reveal something. Stay grounded: only "
        "reference entity ids that appear in the context below.\n"
        f"{lang_line}\n"
        "Content context:\n" + "\n".join(context_lines)
    )
