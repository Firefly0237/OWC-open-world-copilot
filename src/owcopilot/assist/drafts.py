"""Constrained draft generation.

Draft generation is no longer the project axis. It is a sidecar assist feature: produce a
structured draft, mark it pending review, audit it immediately, and let humans decide.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.models import Issue
from ..audit.runner import AuditRunner
from ..content.models import ContentBundle, Origin, Quest, ReviewStatus
from ..content.normalize import slug_id
from ..llm.gateway import LLMGateway
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack


class DraftResult(BaseModel):
    quest: Quest
    issues: list[Issue] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)


class QuestDraftService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        context_builder: ContextPackBuilder,
        audit_runner: AuditRunner,
        bundle: ContentBundle,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.audit_runner = audit_runner
        self.bundle = bundle

    def draft_quest(self, brief: str, *, budget_tokens: int = 800) -> DraftResult:
        pack = self.context_builder.build(brief, budget_tokens=budget_tokens)
        raw = self.gateway.complete(
            task="quest_draft",
            system=_system_prompt(pack),
            user=brief,
        )
        quest = parse_quest_draft(raw)
        quest = _with_unique_quest_id(quest, existing_ids=set(self.bundle.quests))
        quest = quest.model_copy(
            update={
                "origin": Origin.AI_DRAFT,
                "review_status": ReviewStatus.PENDING_REVIEW,
                "metadata": {**quest.metadata, "context_refs": pack.refs},
            }
        )
        audit_bundle = self.bundle.model_copy(deep=True)
        audit_bundle.quests[quest.id] = quest
        before = self.audit_runner.run(AuditContext.from_bundle(self.bundle))
        audit = self.audit_runner.run(AuditContext.from_bundle(audit_bundle))
        before_fingerprints = {issue_fingerprint(issue) for issue in before.issues}
        new_issues = [
            issue for issue in audit.issues if issue_fingerprint(issue) not in before_fingerprints
        ]
        return DraftResult(quest=quest, issues=new_issues, context_refs=pack.refs)


def parse_quest_draft(raw: str) -> Quest:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
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
        value = normalized.get(key)
        if value is None and key in normalized:
            normalized[key] = []
        elif isinstance(value, str):
            normalized[key] = [value] if value.strip() else []
        elif isinstance(value, dict):
            normalized[key] = [str(item) for item in value.values() if str(item).strip()]
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
    """Round-3 real run: models say `{"type": "experience", "amount": 100}` — alias `type` to
    `kind` and fall back to `amount` when `value` is absent."""
    normalized = dict(reward)
    if "kind" not in normalized and "type" in normalized:
        normalized["kind"] = str(normalized.pop("type"))
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


def _system_prompt(pack: ContextPack) -> str:
    context_lines = [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits]
    return (
        "Draft one structured Quest as JSON using only the provided content context. "
        "Return keys compatible with owcopilot.content.models.Quest: id, title, giver_npc, "
        "location, objective, prerequisites, timeline_order, localization_keys, dialogue_refs, "
        "stages, rewards, tags, metadata. Use entity ids, not display names, for references. "
        "The draft is not approved content; it will enter human review.\n\n"
        "Content context:\n" + "\n".join(context_lines)
    )
