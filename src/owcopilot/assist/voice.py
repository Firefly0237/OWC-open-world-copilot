"""Voice card assembly for constrained short-text generation."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Entity


class VoiceCard(BaseModel):
    entity_id: str
    name: str
    description: str = ""
    faction_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tone: str | None = None
    taboo: list[str] = Field(default_factory=list)


def build_voice_card(entity: Entity, bundle: ContentBundle) -> VoiceCard:
    faction_ids = [
        relation.target
        for relation in bundle.relations
        if relation.source == entity.id and relation.kind == "member_of"
    ]
    return VoiceCard(
        entity_id=entity.id,
        name=entity.name,
        description=entity.description,
        faction_ids=faction_ids,
        tags=list(entity.tags),
        tone=_optional_str(entity.metadata.get("tone")),
        taboo=_list(entity.metadata.get("taboo")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []
