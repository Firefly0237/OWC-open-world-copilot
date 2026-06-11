"""Normalize raw imported objects into the v2 content models."""

from __future__ import annotations

import re
from typing import Any

from .importers.base import RawObject
from .models import (
    POI,
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    QuestEventReference,
    QuestEventRefKind,
    RegionBrief,
    Relation,
    SourceRef,
    StyleGuide,
    Term,
)


def slug_id(value: str, *, prefix: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not slug and value.strip():
        import hashlib

        slug = hashlib.sha1(value.strip().encode("utf-8")).hexdigest()[:10]
    if prefix and slug and not slug.startswith(f"{prefix}_"):
        return f"{prefix}_{slug}"
    return slug


def normalize_raw_objects(raw_objects: list[RawObject]) -> ContentBundle:
    bundle = ContentBundle()
    name_to_id: dict[str, str] = {}

    for raw in raw_objects:
        if raw.kind == "entity" or _looks_like_entity(raw.data):
            entity = _entity_from_raw(raw)
            bundle.add_entity(entity)
            name_to_id[entity.name] = entity.id
        elif raw.kind == "quest":
            quest = _quest_from_raw(raw)
            bundle.quests[quest.id] = quest
        elif raw.kind in {"quest_event_ref", "questeventref", "quest_event"}:
            ref = _quest_event_ref_from_raw(raw)
            bundle.quest_event_refs[ref.id] = ref
        elif raw.kind in {"region", "regionbrief", "region_brief"}:
            region = _region_from_raw(raw)
            bundle.regions[region.id] = region
        elif raw.kind == "poi":
            poi = _poi_from_raw(raw)
            bundle.pois[poi.id] = poi
        elif raw.kind in {"dialogue", "dialogueref", "dialogue_ref"}:
            dialogue = _dialogue_from_raw(raw)
            bundle.dialogues[dialogue.id] = dialogue
        elif raw.kind in {"localized_text", "localizedtext", "localization", "text"}:
            for text in _localized_texts_from_raw(raw):
                bundle.localized_texts[text.id] = text
        elif raw.kind == "term":
            term = _term_from_raw(raw)
            bundle.terms[term.id] = term
        elif raw.kind in {"style_guide", "styleguide"}:
            style = _style_guide_from_raw(raw)
            bundle.style_guides[style.id] = style

    for raw in raw_objects:
        if raw.kind == "relation":
            bundle.add_relation(_relation_from_raw(raw, name_to_id))
    _derive_relations(bundle)

    return bundle


def _looks_like_entity(data: dict[str, Any]) -> bool:
    return "name" in data and "type" in data


def _entity_from_raw(raw: RawObject) -> Entity:
    name = str(raw.data.get("name") or raw.data.get("id") or "").strip()
    prefix = str(raw.data.get("type") or "concept").lower()
    entity_id = str(raw.data.get("id") or slug_id(name, prefix=prefix)).strip()
    entity_type = _entity_type(raw, entity_id)
    tags = _list(raw.data.get("tags"))
    aliases = _list(raw.data.get("aliases"))
    metadata = _metadata(
        raw.data,
        exclude={
            "kind",
            "object_type",
            "id",
            "name",
            "type",
            "description",
            "aliases",
            "tags",
            "status",
            "version",
        },
    )
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,
        description=str(raw.data.get("description") or raw.data.get("desc") or ""),
        aliases=aliases,
        tags=tags,
        status=str(raw.data.get("status") or "active"),
        version=_optional_str(raw.data.get("version")),
        metadata=metadata,
        source_ref=SourceRef(path=raw.source_path, line=raw.line, row=raw.row, sheet=raw.sheet),
    )


def _quest_from_raw(raw: RawObject) -> Quest:
    title = str(raw.data.get("title") or raw.data.get("name") or raw.data.get("id") or "").strip()
    quest_id = str(raw.data.get("id") or slug_id(title, prefix="quest")).strip()
    return Quest(
        id=quest_id,
        title=title,
        giver_npc=_optional_str(raw.data.get("giver_npc")),
        location=_optional_str(raw.data.get("location")),
        objective=str(raw.data.get("objective") or raw.data.get("objectives") or ""),
        prerequisites=_list(raw.data.get("prerequisites")),
        timeline_order=_optional_int(raw.data.get("timeline_order")),
        dialogue_refs=_list(raw.data.get("dialogue_refs")),
        localization_keys=_list(raw.data.get("localization_keys")),
        tags=_list(raw.data.get("tags")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "title",
                "name",
                "giver_npc",
                "location",
                "objective",
                "objectives",
                "prerequisites",
                "timeline_order",
                "dialogue_refs",
                "localization_keys",
                "tags",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _quest_event_ref_from_raw(raw: RawObject) -> QuestEventReference:
    quest_id = str(raw.data.get("quest_id") or "").strip()
    event_id = str(raw.data.get("event_id") or raw.data.get("event") or "").strip()
    ref_kind = _event_ref_kind(raw.data)
    ref_id = str(
        raw.data.get("id") or f"{quest_id}:{event_id}:{ref_kind.value}"
    ).strip()
    return QuestEventReference(
        id=ref_id,
        quest_id=quest_id,
        event_id=event_id,
        ref_kind=ref_kind,
        note=str(raw.data.get("note") or raw.data.get("remark") or raw.data.get("备注") or ""),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "quest_id",
                "event_id",
                "event",
                "ref_kind",
                "note",
                "remark",
                "备注",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _region_from_raw(raw: RawObject) -> RegionBrief:
    name = str(raw.data.get("name") or raw.data.get("id") or "").strip()
    region_id = str(raw.data.get("id") or slug_id(name, prefix="region")).strip()
    return RegionBrief(
        id=region_id,
        name=name,
        level_min=_optional_int(raw.data.get("level_min")),
        level_max=_optional_int(raw.data.get("level_max")),
        themes=_list(raw.data.get("themes")),
        allowed_content=_list(raw.data.get("allowed_content")),
        banned_content=_list(raw.data.get("banned_content") or raw.data.get("forbidden_elements")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "name",
                "level_min",
                "level_max",
                "themes",
                "allowed_content",
                "banned_content",
                "forbidden_elements",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _poi_from_raw(raw: RawObject) -> POI:
    name = str(raw.data.get("name") or raw.data.get("id") or "").strip()
    poi_id = str(raw.data.get("id") or slug_id(name, prefix="poi")).strip()
    return POI(
        id=poi_id,
        name=name,
        region_id=_optional_str(raw.data.get("region_id") or raw.data.get("region")),
        purpose=str(raw.data.get("purpose") or raw.data.get("narrative_purpose") or ""),
        controlling_faction=_optional_str(raw.data.get("controlling_faction")),
        level_min=_optional_int(raw.data.get("level_min") or raw.data.get("level")),
        level_max=_optional_int(raw.data.get("level_max") or raw.data.get("level")),
        tags=_list(raw.data.get("tags")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "name",
                "region_id",
                "region",
                "purpose",
                "narrative_purpose",
                "controlling_faction",
                "level",
                "level_min",
                "level_max",
                "tags",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _dialogue_from_raw(raw: RawObject) -> DialogueRef:
    text_key = str(raw.data.get("text_key") or raw.data.get("key") or raw.data.get("id") or "")
    dialogue_id = str(raw.data.get("id") or slug_id(text_key, prefix="dialogue")).strip()
    return DialogueRef(
        id=dialogue_id,
        text_key=text_key,
        speaker_id=_optional_str(raw.data.get("speaker_id") or raw.data.get("speaker")),
        quest_id=_optional_str(raw.data.get("quest_id") or raw.data.get("quest")),
        text=_optional_str(raw.data.get("text")),
        locale=_optional_str(raw.data.get("locale")),
        ui_max_len=_optional_int(raw.data.get("ui_max_len")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "text_key",
                "key",
                "speaker_id",
                "speaker",
                "quest_id",
                "quest",
                "text",
                "locale",
                "ui_max_len",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _localized_texts_from_raw(raw: RawObject) -> list[LocalizedText]:
    text_key = str(raw.data.get("text_key") or raw.data.get("key") or raw.data.get("id") or "")
    rows: list[LocalizedText] = []
    locale_values: dict[str, Any] = {}
    if raw.data.get("locale") and raw.data.get("text"):
        locale_values[str(raw.data["locale"])] = raw.data["text"]
    for key, value in raw.data.items():
        if _looks_like_locale(key) and value not in (None, ""):
            locale_values[key] = value
    for locale, text in sorted(locale_values.items()):
        text_id = str(
            raw.data.get("id")
            if len(locale_values) == 1 and raw.data.get("id")
            else f"{text_key}:{locale}"
        )
        rows.append(
            LocalizedText(
                id=slug_id(text_id, prefix="loc"),
                text_key=text_key,
                locale=locale,
                text=str(text),
                ui_max_len=_optional_int(raw.data.get("ui_max_len")),
                metadata=_metadata(
                    raw.data,
                    exclude={
                        "kind",
                        "object_type",
                        "id",
                        "text_key",
                        "key",
                        "locale",
                        "text",
                        "ui_max_len",
                        *locale_values.keys(),
                    },
                ),
                source_ref=_source_ref(raw),
            )
        )
    return rows


def _term_from_raw(raw: RawObject) -> Term:
    canonical = str(
        raw.data.get("canonical")
        or raw.data.get("preferred")
        or raw.data.get("name")
        or raw.data.get("id")
        or ""
    )
    term_id = str(raw.data.get("id") or slug_id(canonical, prefix="term")).strip()
    return Term(
        id=term_id,
        canonical=canonical,
        aliases=_list(raw.data.get("aliases")),
        forbidden=_list(raw.data.get("forbidden")),
        description=str(raw.data.get("description") or raw.data.get("note") or ""),
        source_ref=_source_ref(raw),
    )


def _style_guide_from_raw(raw: RawObject) -> StyleGuide:
    style_id = str(raw.data.get("id") or "style_guide")
    return StyleGuide(
        id=style_id,
        body=str(raw.data.get("body") or raw.data.get("text") or ""),
        rules=_list(raw.data.get("rules")),
        source_ref=_source_ref(raw),
    )


def _relation_from_raw(raw: RawObject, name_to_id: dict[str, str]) -> Relation:
    source = str(raw.data.get("source") or "").strip()
    target = str(raw.data.get("target") or "").strip()
    return Relation(
        source=name_to_id.get(source, source),
        target=name_to_id.get(target, target),
        kind=str(raw.data.get("kind") or "").strip(),
        valid_from=_optional_int(raw.data.get("valid_from")),
        valid_until=_optional_int(raw.data.get("valid_until")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "source",
                "target",
                "valid_from",
                "valid_until",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _source_ref(raw: RawObject) -> SourceRef:
    return SourceRef(path=raw.source_path, line=raw.line, row=raw.row, sheet=raw.sheet)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [
            item.strip()
            for item in re.split(r"[;,；、|]", value)
            if item.strip()
        ]
    return [str(value).strip()] if str(value).strip() else []


def _metadata(data: dict[str, Any], *, exclude: set[str]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in exclude and value not in (None, "")
    }


def _looks_like_locale(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"zh", "zh-cn", "en", "en-us"} or bool(
        re.match(r"^[a-z]{2}(?:-[a-z]{2})?$", lowered)
    )


def _event_ref_kind(data: dict[str, Any]) -> QuestEventRefKind:
    raw = str(data.get("ref_kind") or data.get("kind") or "").lower()
    note = str(data.get("note") or data.get("remark") or data.get("备注") or "")
    if raw in {QuestEventRefKind.REFERENCES_RESULT.value, "result", "references_event_result"}:
        return QuestEventRefKind.REFERENCES_RESULT
    if "结果" in note or "剧透" in note or "新盟主" in note:
        return QuestEventRefKind.REFERENCES_RESULT
    return QuestEventRefKind.MENTIONS_EVENT


def _entity_type(raw: RawObject, entity_id: str) -> EntityType:
    raw_type = str(raw.data.get("type") or "").lower().strip()
    if raw_type:
        return EntityType(raw_type)
    lowered = " ".join([entity_id, raw.source_path, raw.sheet or ""]).lower()
    if entity_id.startswith("npc_") or "npc" in lowered:
        return EntityType.NPC
    if entity_id.startswith("fac_") or "faction" in lowered or "阵营" in lowered:
        return EntityType.FACTION
    if entity_id.startswith("evt_") or "event" in lowered or "事件" in lowered:
        return EntityType.EVENT
    if entity_id.startswith("poi_") or "poi" in lowered:
        return EntityType.LOCATION
    return EntityType.CONCEPT


def _derive_relations(bundle: ContentBundle) -> None:
    existing = {(r.source, r.kind, r.target) for r in bundle.relations}
    for entity in bundle.entities.values():
        faction = _optional_str(entity.metadata.get("faction"))
        if faction and (entity.id, "member_of", faction) not in existing:
            bundle.relations.append(
                Relation(
                    source=entity.id,
                    kind="member_of",
                    target=faction,
                    source_ref=entity.source_ref,
                )
            )
            existing.add((entity.id, "member_of", faction))
    for poi in bundle.pois.values():
        if poi.controlling_faction and (
            poi.id,
            "controlled_by",
            poi.controlling_faction,
        ) not in existing:
            bundle.relations.append(
                Relation(
                    source=poi.id,
                    kind="controlled_by",
                    target=poi.controlling_faction,
                    source_ref=poi.source_ref,
                )
            )
            existing.add((poi.id, "controlled_by", poi.controlling_faction))
