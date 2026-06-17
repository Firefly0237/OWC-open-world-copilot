"""Detailed character-sheet generation, grounded in the existing world.

The brief mirrors what narrative designers actually fill in for a character document
(public template consensus: basics / appearance / personality / backstory / motivation /
abilities & weakness / voice / relationships / dramatic role). Only name and concept are
required; empty dimensions never reach the prompt. The output is one rich NPC entity
(profile sections live in metadata) plus relations wired ONLY to entities that already
exist in the world — unknown targets are kept as suggestions for the human, never
fabricated into the graph.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..content.models import ContentBundle, Entity, EntityType, Origin, Relation, ReviewStatus
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..retrieval.context_pack import ContextPackBuilder
from ..util import slugify
from .critic import CHARACTER_CRITIQUE_MARKER, CharacterCritic
from .refine import RefineStep, run_refine_loop

PROFILE_SECTIONS: list[tuple[str, str]] = [
    ("appearance", "外貌"),
    ("personality", "性格"),
    ("backstory", "背景故事"),
    ("motivation", "动机与目标"),
    ("abilities", "能力与专长"),
    ("weakness", "弱点与恐惧"),
    ("voice", "说话方式"),
]

_BRIEF_OPTIONAL_LABELS: list[tuple[str, str]] = [
    ("age_gender", "年龄/性别"),
    ("species", "种族/族裔"),
    ("role_function", "戏剧定位"),
    ("faction_id", "所属阵营（既有 id）"),
    ("location_id", "常驻地点（既有 id）"),
    ("notes", "补充要求"),
]
# hint fields are list[str]; rendered joined (kept out of the str-only optional-labels loop above)
_BRIEF_HINT_LABELS: list[tuple[str, str]] = [
    ("personality_hints", "性格倾向"),
    ("voice_hints", "说话方式"),
]

_SYSTEM_PROMPT = (
    "You are a senior character designer for narrative worlds. Create ONE detailed "
    "character sheet from the creator's brief, in the brief's own language and genre. "
    "Ground the character in the provided world facts; do not contradict them. "
    "Return ONE JSON object only, no markdown. Keys: name, summary (one sentence), "
    "appearance, personality, backstory, motivation, abilities, weakness, voice, "
    "relationships (list of {target, kind, note} — target MUST be an entity id that "
    "appears in the world facts context; if no suitable entity exists, leave the list "
    "empty rather than inventing ids). Honor every constraint the brief states.\n"
    # Quality bar (二游 character rubric — same bar applied to quests/dialogue):
    "QUALITY BAR — a memorable character, not an archetype with a label:\n"
    "1. MOTIVATION has an inner contradiction: a want PLUS a fear/cost that pulls against it "
    "(动机要有内在矛盾/反差), so the character could change — not a static trait list.\n"
    "2. VOICE is distinctive and concrete: name a verbal tic / rhythm / signature imagery in "
    "'voice' so their dialogue would be recognizable without the name.\n"
    "3. Use concrete sensory detail (objects, scars, habits), never generic adjectives like "
    "'mysterious/strong/kind' alone. weakness must be real and exploitable, not cosmetic."
)


class CharacterBrief(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    concept: str = Field(min_length=1, max_length=2000)
    age_gender: str = ""
    species: str = ""
    role_function: str = ""
    faction_id: str = ""
    location_id: str = ""
    # All three hint fields are list[str] for a consistent API; a plain string is still accepted and
    # split on common separators (so callers/UI sending one string don't break).
    personality_hints: list[str] = Field(default_factory=list)
    voice_hints: list[str] = Field(default_factory=list)
    relationship_hints: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("personality_hints", "voice_hints", "relationship_hints", mode="before")
    @classmethod
    def _coerce_hint_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [p.strip() for p in re.split(r"[、，,；;\n]+", value) if p.strip()]
        if isinstance(value, list):
            return [str(p).strip() for p in value if str(p).strip()]
        return [str(value).strip()]


class CharacterDraft(BaseModel):
    entity: Entity
    relations: list[Relation] = Field(default_factory=list)
    profile: dict[str, str] = Field(default_factory=dict)
    suggested_relations: list[str] = Field(default_factory=list)
    refine_trail: list[RefineStep] = Field(default_factory=list)
    auto_review_incomplete: bool = False


def _brief_user_message(
    brief: CharacterBrief,
    *,
    prior: CharacterDraft | None = None,
    feedback: list[str] | None = None,
) -> str:
    lines = [f"名字：{brief.name.strip()}", f"一句话概念：{brief.concept.strip()}"]
    for field, label in _BRIEF_OPTIONAL_LABELS:
        value = str(getattr(brief, field) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    for field, label in _BRIEF_HINT_LABELS:
        items = [h.strip() for h in getattr(brief, field) if h.strip()]
        if items:
            lines.append(f"{label}：{'、'.join(items)}")
    hints = [h.strip() for h in brief.relationship_hints if h.strip()]
    if hints:
        lines.append("人际关系提示（尽量落到既有实体上）：")
        lines.extend(f"- {hint}" for hint in hints)
    lines.append("未提及的维度由你根据概念与世界事实自行设计，保持内在一致。")
    if prior is not None and feedback:
        prior_json = json.dumps(
            {"summary": prior.entity.description, **prior.profile}, ensure_ascii=False
        )
        fix_lines = "\n".join(f"- {fix}" for fix in feedback)
        lines.append(
            "\n[REFINE] 这是上一版人设。请产出改进后的完整 JSON，逐条解决下列意见、补全空缺小节、"
            f"让人物更具体可用：\n上一版：\n{prior_json}\n\n必须解决：\n{fix_lines}"
        )
    return "\n".join(lines)


def _slug(name: str) -> str:
    return slugify(name, fallback="character")


class CharacterProfileService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        context_builder: ContextPackBuilder,
        critic: CharacterCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.context_builder = context_builder
        # Opt-in critique→refine loop: without a critic the service is the original single shot.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

    def generate(self, brief: CharacterBrief, *, budget_tokens: int = 1200) -> CharacterDraft:
        query = " ".join(
            part
            for part in (
                brief.name,
                brief.concept,
                brief.faction_id,
                brief.location_id,
                *brief.relationship_hints,
            )
            if part.strip()
        )
        pack = self.context_builder.build(query, budget_tokens=budget_tokens, limit=8)
        context_lines = [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits]
        draft = self._draft(brief, context_lines)
        if self.critic is None:
            return draft

        def assess(d: CharacterDraft) -> tuple[list[str], Any]:
            assert self.critic is not None
            missing = [
                label for key, label in PROFILE_SECTIONS if not d.profile.get(key, "").strip()
            ]
            critique = self.critic.critique(
                concept=brief.concept,
                profile=d.profile,
                summary=d.entity.description,
                context_lines=context_lines,
                missing_sections=missing,
            )
            return missing, critique

        outcome = run_refine_loop(
            initial=draft,
            max_rounds=self.max_refine_rounds,
            assess=assess,
            regenerate=lambda d, fixes: self._draft(brief, context_lines, prior=d, feedback=fixes),
        )
        final = outcome.artifact
        if outcome.trail:
            final.entity.metadata["refine_rounds"] = len(outcome.trail)
        if outcome.auto_review_incomplete:
            final.entity.metadata["auto_review_incomplete"] = True
        final.refine_trail = outcome.trail
        final.auto_review_incomplete = outcome.auto_review_incomplete
        return final

    def revise(
        self, prior: CharacterDraft, feedback: str, *, budget_tokens: int = 1200
    ) -> CharacterDraft:
        """Regenerate the sheet to address reviewer feedback; the stored brief grounds retrieval."""
        brief_data = dict(prior.entity.metadata.get("character_brief") or {})
        brief_data.setdefault("name", prior.entity.name)
        brief_data.setdefault("concept", prior.entity.description or prior.entity.name)
        brief = CharacterBrief.model_validate(brief_data)
        query = " ".join(
            part
            for part in (
                brief.name,
                brief.concept,
                brief.faction_id,
                brief.location_id,
                *brief.relationship_hints,
            )
            if part.strip()
        )
        pack = self.context_builder.build(query, budget_tokens=budget_tokens, limit=8)
        context_lines = [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits]
        revised = self._draft(brief, context_lines, prior=prior, feedback=[feedback.strip()])
        revised.entity.metadata["revised_from_feedback"] = True
        return revised

    def _draft(
        self,
        brief: CharacterBrief,
        context_lines: list[str],
        *,
        prior: CharacterDraft | None = None,
        feedback: list[str] | None = None,
    ) -> CharacterDraft:
        raw = self.gateway.complete(
            task="character_profile",
            system=_SYSTEM_PROMPT
            + "\n\nWorld facts context:\n"
            + ("\n".join(context_lines) if context_lines else "(none)"),
            user=_brief_user_message(brief, prior=prior, feedback=feedback),
        )
        return self._draft_from_payload(_parse_payload(raw), brief)

    def _draft_from_payload(self, payload: dict[str, Any], brief: CharacterBrief) -> CharacterDraft:
        name = str(payload.get("name") or brief.name).strip() or brief.name
        summary = str(payload.get("summary") or brief.concept).strip()
        profile = {
            key: str(payload.get(key) or "").strip()
            for key, _label in PROFILE_SECTIONS
            if str(payload.get(key) or "").strip()
        }
        entity_id = f"npc_{_slug(name)}"
        index = 2
        while entity_id in self.bundle.entities:
            entity_id = f"npc_{_slug(name)}_{index}"
            index += 1
        known_ids = set(self.bundle.entities) | set(self.bundle.pois) | set(self.bundle.regions)
        relations: list[Relation] = []
        suggestions: list[str] = []
        for item in payload.get("relationships") or []:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or "").strip()
            kind = str(item.get("kind") or "").strip() or "related_to"
            note = str(item.get("note") or "").strip()
            if target in known_ids:
                relations.append(
                    Relation(
                        source=entity_id,
                        target=target,
                        kind=kind,
                        metadata={"note": note} if note else {},
                    )
                )
            elif target:
                suggestions.append(f"{target}（{kind}）{note}".strip())
        tags = [t for t in (brief.role_function.strip(),) if t]
        if brief.faction_id.strip() and brief.faction_id in known_ids:
            relations.append(
                Relation(source=entity_id, target=brief.faction_id.strip(), kind="member_of")
            )
        if brief.location_id.strip() and brief.location_id in known_ids:
            relations.append(
                Relation(source=entity_id, target=brief.location_id.strip(), kind="located_in")
            )
        entity = Entity(
            id=entity_id,
            name=name,
            type=EntityType.NPC,
            description=summary,
            tags=tags,
            metadata={
                "profile": profile,
                "character_brief": brief.model_dump(mode="json", exclude_defaults=True),
                **({"suggested_relations": suggestions} if suggestions else {}),
            },
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        deduped: list[Relation] = []
        seen: set[tuple[str, str, str]] = set()
        for relation in relations:
            key = (relation.source, relation.target, relation.kind)
            if key not in seen:
                seen.add(key)
                deduped.append(relation)
        return CharacterDraft(
            entity=entity, relations=deduped, profile=profile, suggested_relations=suggestions
        )


def _parse_payload(raw: str) -> dict[str, Any]:
    return extract_json_object(raw)


class OfflineCharacterProvider:
    """Deterministic double following the production message contract: echoes the brief's
    name/concept/hints into a full sheet and wires relationship hints whose targets are
    present in the world-facts context (by id token match)."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if CHARACTER_CRITIQUE_MARKER in system:
            text = _offline_character_critique(user)
            return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)
        fields: dict[str, str] = {}
        hints: list[str] = []
        in_hints = False
        for line in user.splitlines():
            stripped = line.strip()
            if in_hints:
                if stripped.startswith("- "):
                    hints.append(stripped[2:])
                    continue
                in_hints = False
            if stripped.startswith("人际关系提示"):
                in_hints = True
                continue
            label, separator, value = stripped.partition("：")
            if separator:
                fields[label.strip()] = value.strip()
        name = fields.get("名字", "无名者")
        concept = fields.get("一句话概念", "")
        known_ids = re.findall(r"\[((?:entity|poi|region):([^\]]+))\]", system)
        known = {short for _full, short in known_ids}
        relationships = []
        for hint in hints:
            target = next((kid for kid in known if kid in hint), None)
            if target:
                relationships.append({"target": target, "kind": "牵连", "note": hint})
        payload = {
            "name": name,
            "summary": concept or f"{name}的设定待补。",
            "appearance": (
                f"{name}的外貌与其身份相称，细节随{fields.get('种族/族裔', '常人')}而定。"
            ),
            "personality": fields.get("性格倾向", "沉静观察，谨慎行事。"),
            "backstory": f"{name}因「{concept[:24]}」而走到今天。",
            "motivation": "完成概念中的未竟之事。",
            "abilities": "与概念匹配的一技之长。",
            "weakness": "对过往的执念。",
            "voice": fields.get("说话方式", "言简意赅。"),
            "relationships": relationships,
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _offline_character_critique(user: str) -> str:
    """The critic only lists "completeness blockers" when a profile section is empty; the offline
    sheet fills every section, so the default verdict is pass (the loop converges at round 0)."""
    if "completeness blockers" in user:
        result = {
            "verdict": "revise",
            "score": 0.5,
            "summary": "人设小节有缺。",
            "dimensions": [
                {
                    "dimension": "completeness",
                    "severity": "blocker",
                    "issue": "部分人设小节为空。",
                    "fix": "补全空缺的人设小节。",
                }
            ],
        }
    else:
        result = {
            "verdict": "pass",
            "score": 0.9,
            "summary": "人设完整、与世界一致。",
            "dimensions": [{"dimension": "completeness", "severity": "ok", "issue": "", "fix": ""}],
        }
    return json.dumps(result, ensure_ascii=False)
