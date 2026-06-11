"""The World Bible: the single source of truth the consistency hub validates against."""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    NPC = "npc"
    LOCATION = "location"
    FACTION = "faction"
    ITEM = "item"
    EVENT = "event"


class Entity(BaseModel):
    id: str
    name: str
    type: EntityType
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class Relation(BaseModel):
    source: str  # entity id
    target: str  # entity id
    kind: str  # e.g. "located_in", "allied_with", "enemy_of", "requires"


class WorldBible(BaseModel):
    entities: dict[str, Entity] = Field(default_factory=dict)
    relations: list[Relation] = Field(default_factory=list)

    def add_entity(self, e: Entity) -> None:
        self.entities[e.id] = e

    def add_relation(self, r: Relation) -> None:
        self.relations.append(r)

    def has(self, entity_id: str) -> bool:
        return entity_id in self.entities

    def names(self) -> set[str]:
        return {e.name for e in self.entities.values()}

    def by_type(self, t: EntityType) -> list[Entity]:
        return [e for e in self.entities.values() if e.type == t]


def world_bible_hash(wb: WorldBible) -> str:
    """Stable content hash for traceability and cache/debug namespaces."""
    payload = wb.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
