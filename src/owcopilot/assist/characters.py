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

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Entity, EntityType, Origin, Relation, ReviewStatus
from ..llm.gateway import LLMGateway
from ..retrieval.context_pack import ContextPackBuilder

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
    ("personality_hints", "性格倾向"),
    ("voice_hints", "说话方式"),
    ("notes", "补充要求"),
]

_SYSTEM_PROMPT = (
    "You are a senior character designer for narrative worlds. Create ONE detailed "
    "character sheet from the creator's brief, in the brief's own language and genre. "
    "Ground the character in the provided world facts; do not contradict them. "
    "Return ONE JSON object only, no markdown. Keys: name, summary (one sentence), "
    "appearance, personality, backstory, motivation, abilities, weakness, voice, "
    "relationships (list of {target, kind, note} — target MUST be an entity id that "
    "appears in the world facts context; if no suitable entity exists, leave the list "
    "empty rather than inventing ids). Honor every constraint the brief states."
)


class CharacterBrief(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    concept: str = Field(min_length=1, max_length=2000)
    age_gender: str = ""
    species: str = ""
    role_function: str = ""
    faction_id: str = ""
    location_id: str = ""
    personality_hints: str = ""
    voice_hints: str = ""
    relationship_hints: list[str] = Field(default_factory=list)
    notes: str = ""


class CharacterDraft(BaseModel):
    entity: Entity
    relations: list[Relation] = Field(default_factory=list)
    profile: dict[str, str] = Field(default_factory=dict)
    suggested_relations: list[str] = Field(default_factory=list)


def _brief_user_message(brief: CharacterBrief) -> str:
    lines = [f"名字：{brief.name.strip()}", f"一句话概念：{brief.concept.strip()}"]
    for field, label in _BRIEF_OPTIONAL_LABELS:
        value = str(getattr(brief, field) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    hints = [h.strip() for h in brief.relationship_hints if h.strip()]
    if hints:
        lines.append("人际关系提示（尽量落到既有实体上）：")
        lines.extend(f"- {hint}" for hint in hints)
    lines.append("未提及的维度由你根据概念与世界事实自行设计，保持内在一致。")
    return "\n".join(lines)


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", name).strip("_").lower()
    return cleaned or "character"


class CharacterProfileService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        context_builder: ContextPackBuilder,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.context_builder = context_builder

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
        raw = self.gateway.complete(
            task="character_profile",
            system=_SYSTEM_PROMPT
            + "\n\nWorld facts context:\n"
            + ("\n".join(context_lines) if context_lines else "(none)"),
            user=_brief_user_message(brief),
        )
        payload = _parse_payload(raw)
        return self._draft_from_payload(payload, brief)

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
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.find("{") : text.rfind("}") + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("character provider returned non-object JSON")
    return payload


class OfflineCharacterProvider:
    """Deterministic double following the production message contract: echoes the brief's
    name/concept/hints into a full sheet and wires relationship hints whose targets are
    present in the world-facts context (by id token match)."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
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
