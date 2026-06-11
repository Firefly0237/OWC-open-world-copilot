"""Unified content models for the v2 content hub.

These models are intentionally broader than the old WorldBible schema. They are the file-backed
facts that later pipeline stages index, audit, retrieve, patch, review and export.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Origin(str, Enum):
    HUMAN = "human"
    AI_DRAFT = "ai_draft"
    AI_PATCH = "ai_patch"


class ReviewStatus(str, Enum):
    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


class EntityType(str, Enum):
    NPC = "npc"
    LOCATION = "location"
    FACTION = "faction"
    ITEM = "item"
    EVENT = "event"
    REGION = "region"
    ORGANIZATION = "organization"
    CONCEPT = "concept"
    TERM = "term"


class SourceRef(BaseModel):
    """Where a content object came from before normalization."""

    path: str
    line: int | None = None
    sheet: str | None = None
    row: int | None = None
    column: str | None = None


class ProvenanceMixin(BaseModel):
    origin: Origin = Origin.HUMAN
    source_ref: SourceRef | None = None
    review_status: ReviewStatus = ReviewStatus.APPROVED


class Entity(ProvenanceMixin):
    id: str
    name: str
    type: EntityType
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str = "active"
    version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Relation(ProvenanceMixin):
    source: str
    target: str
    kind: str
    valid_from: int | None = None
    valid_until: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuestEventRefKind(str, Enum):
    MENTIONS_EVENT = "mentions_event"
    REFERENCES_RESULT = "references_result"


class QuestEventReference(ProvenanceMixin):
    """A quest-to-event reference with semantic intent.

    `mentions_event` means the quest may discuss or investigate an event. `references_result`
    means the quest depends on the event outcome, which is timeline-sensitive.
    """

    id: str
    quest_id: str
    event_id: str
    ref_kind: QuestEventRefKind = QuestEventRefKind.MENTIONS_EVENT
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuestStage(BaseModel):
    id: str
    summary: str
    location: str | None = None
    required_entities: list[str] = Field(default_factory=list)


class Reward(BaseModel):
    kind: str
    value: str
    amount: int | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: Any) -> str:
        return "" if value is None else str(value)


class Quest(ProvenanceMixin):
    id: str
    title: str
    giver_npc: str | None = None
    location: str | None = None
    objective: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    timeline_order: int | None = None
    stages: list[QuestStage] = Field(default_factory=list)
    rewards: list[Reward] = Field(default_factory=list)
    dialogue_refs: list[str] = Field(default_factory=list)
    localization_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegionBrief(ProvenanceMixin):
    id: str
    name: str
    level_min: int | None = None
    level_max: int | None = None
    themes: list[str] = Field(default_factory=list)
    allowed_content: list[str] = Field(default_factory=list)
    banned_content: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class POI(ProvenanceMixin):
    id: str
    name: str
    region_id: str | None = None
    purpose: str = ""
    controlling_faction: str | None = None
    level_min: int | None = None
    level_max: int | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DialogueRef(ProvenanceMixin):
    id: str
    text_key: str
    speaker_id: str | None = None
    quest_id: str | None = None
    text: str | None = None
    locale: str | None = None
    ui_max_len: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalizedText(ProvenanceMixin):
    id: str
    text_key: str
    locale: str
    text: str
    ui_max_len: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Term(ProvenanceMixin):
    id: str
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    description: str = ""


class StyleGuide(ProvenanceMixin):
    id: str = "style_guide"
    body: str = ""
    rules: list[str] = Field(default_factory=list)


class ContentBundle(BaseModel):
    """A normalized in-memory bundle loaded from the file-backed content store."""

    entities: dict[str, Entity] = Field(default_factory=dict)
    relations: list[Relation] = Field(default_factory=list)
    quest_event_refs: dict[str, QuestEventReference] = Field(default_factory=dict)
    quests: dict[str, Quest] = Field(default_factory=dict)
    regions: dict[str, RegionBrief] = Field(default_factory=dict)
    pois: dict[str, POI] = Field(default_factory=dict)
    dialogues: dict[str, DialogueRef] = Field(default_factory=dict)
    localized_texts: dict[str, LocalizedText] = Field(default_factory=dict)
    terms: dict[str, Term] = Field(default_factory=dict)
    style_guides: dict[str, StyleGuide] = Field(default_factory=dict)

    def add_entity(self, entity: Entity) -> None:
        self.entities[entity.id] = entity

    def add_relation(self, relation: Relation) -> None:
        self.relations.append(relation)

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self.entities
