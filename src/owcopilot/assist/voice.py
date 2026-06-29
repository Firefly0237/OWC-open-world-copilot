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
    # C4: voice style description from entity.metadata["profile"]["voice"].
    # Empty string when the field is absent — backward-compatible default.
    profile_voice: str = ""


def build_voice_card(entity: Entity, bundle: ContentBundle) -> VoiceCard:
    faction_ids = [
        relation.target
        for relation in bundle.relations
        if relation.source == entity.id and relation.kind == "member_of"
    ]
    # C4: read voice style description from nested profile dict.
    # Falls back to "" if "profile" key is absent, "voice" key is absent,
    # or the value is None/empty — guards against KeyError and None.
    profile = entity.metadata.get("profile")
    profile_voice = str(profile.get("voice", "") or "").strip() if isinstance(profile, dict) else ""
    return VoiceCard(
        entity_id=entity.id,
        name=entity.name,
        description=entity.description,
        faction_ids=faction_ids,
        tags=list(entity.tags),
        tone=_optional_str(entity.metadata.get("tone")),
        taboo=_list(entity.metadata.get("taboo")),
        profile_voice=profile_voice,
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
