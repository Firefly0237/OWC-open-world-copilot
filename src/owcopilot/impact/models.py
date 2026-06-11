"""Impact analysis models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChangeType(str, Enum):
    ENTITY_RENAME = "entity_rename"
    ENTITY_DELETE = "entity_delete"
    ENTITY_FIELD_CHANGE = "entity_field_change"
    RELATION_CHANGE = "relation_change"
    CONTENT_CHANGE = "content_change"


class ImpactLevel(str, Enum):
    MUST_CHANGE = "must_change"
    SUGGEST_CHECK = "suggest_check"


class Change(BaseModel):
    change_type: ChangeType
    target_ref: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class ChangeSet(BaseModel):
    changes: list[Change] = Field(default_factory=list)


class ImpactItem(BaseModel):
    target_ref: str
    level: ImpactLevel
    distance: int
    reason: str
    source_change: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ImpactResult(BaseModel):
    items: list[ImpactItem] = Field(default_factory=list)

    def by_level(self, level: ImpactLevel) -> list[ImpactItem]:
        return [item for item in self.items if item.level is level]
