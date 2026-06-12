"""World seed generation service."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..content.models import (
    POI,
    ContentBundle,
    Entity,
    EntityType,
    Origin,
    Quest,
    QuestStage,
    RegionBrief,
    Relation,
    ReviewStatus,
    StyleGuide,
    Term,
)
from ..inspiration.retrieval import ReferenceContextBuilder
from ..llm.gateway import LLMGateway
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack, RetrievalHit
from .models import ReferenceReportItem, WorldSeedBrief, WorldSeedDraft


class WorldSeedService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        project_context_builder: ContextPackBuilder,
        reference_context_builder: ReferenceContextBuilder,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.project_context_builder = project_context_builder
        self.reference_context_builder = reference_context_builder

    def generate(self, brief: WorldSeedBrief, *, budget_tokens: int = 1800) -> WorldSeedDraft:
        query = _brief_query(brief)
        project_pack = (
            self.project_context_builder.build(query, budget_tokens=budget_tokens // 2, limit=6)
            if brief.use_project_facts
            else ContextPack(query=query, budget_tokens=budget_tokens // 2)
        )
        reference_query = brief.reference_query.strip() or query
        inspiration_pack = self.reference_context_builder.build(
            reference_query, budget_tokens=budget_tokens, limit=8
        )
        raw = self.gateway.complete(
            task="world_seed",
            system=_system_prompt(brief, project_pack, inspiration_pack),
            user=_brief_user_message(brief),
        )
        payload = parse_world_seed_payload(raw)
        draft_id = "world_seed_" + hashlib.sha256(f"{brief.idea}\n{raw}".encode()).hexdigest()[:12]
        bundle = _bundle_from_payload(
            payload,
            draft_id=draft_id,
            brief=brief,
            existing=self.bundle,
            inspiration_pack=inspiration_pack,
            project_pack=project_pack,
        )
        report = _reference_report(payload, inspiration_pack, brief)
        return WorldSeedDraft(
            id=draft_id,
            brief=brief,
            summary=str(payload.get("summary") or brief.idea),
            bundle=bundle,
            reference_report=report,
            project_context_refs=project_pack.refs,
            inspiration_context_refs=inspiration_pack.refs,
        )


def parse_world_seed_payload(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.find("{") : text.rfind("}") + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("world seed provider returned non-object JSON")
    return payload


_BRIEF_OPTIONAL_LABELS: list[tuple[str, str]] = [
    ("medium", "载体/媒介"),
    ("game_genre", "玩法/类型"),
    ("tone", "基调"),
    ("era", "时代/技术水平"),
    ("player_fantasy", "主角/玩家身份"),
    ("core_conflict", "核心冲突"),
    ("notes", "补充要求"),
]


def _brief_user_message(brief: WorldSeedBrief) -> str:
    """Compose the user message from the FILLED brief fields only.

    Never serialize the whole brief: an empty `"player_fantasy": ""` in the prompt reads
    as "this dimension exists, fill it" and the model invents a protagonist for a
    worldview-only request. Absent means absent.
    """
    lines = [f"核心想法：{brief.idea.strip()}"]
    if brief.world_styles:
        styles = "、".join(s.strip() for s in brief.world_styles if s.strip())
        if styles:
            lines.append(f"世界风格：{styles}")
    for field, label in _BRIEF_OPTIONAL_LABELS:
        value = str(getattr(brief, field) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    lines.append(
        "以上是创作者提供的全部设定。未提及的维度由你根据核心想法自行裁量，"
        "保持内在一致即可——不要为未提及的维度强加具体设定（例如未提及主角就不要设计主角）。"
    )
    return "\n".join(lines)


def _section_plan(brief: WorldSeedBrief) -> str:
    """Counts speak only for requested sections; a 0-count section is an explicit
    'return an empty array', not a smaller target."""
    wanted: list[str] = []
    skipped: list[str] = []
    for key, count in (
        ("factions", brief.faction_count),
        ("regions", brief.region_count),
        ("npcs", brief.npc_count),
        ("quests", brief.quest_count),
        ("terms", brief.term_count),
    ):
        if count > 0:
            wanted.append(f"{key}={count}")
        else:
            skipped.append(key)
    plan = "Target counts: " + (", ".join(wanted) if wanted else "(none)") + ". "
    if skipped:
        plan += (
            "The creator explicitly does NOT want these sections — return [] for: "
            + ", ".join(skipped)
            + ". "
        )
    if brief.region_count <= 0:
        plan += "Also return [] for locations. "
    return plan


def _system_prompt(
    brief: WorldSeedBrief,
    project_pack: ContextPack,
    inspiration_pack: ContextPack,
) -> str:
    project_lines = _context_lines(project_pack.hits)
    inspiration_lines = _context_lines(inspiration_pack.hits)
    return (
        "You are a senior worldbuilding and narrative designer. Create an original "
        "structured world seed from the creator's brief, in the brief's own genre, medium "
        "and language — do not assume a default genre or audience. "
        "Return ONE JSON object only. Do not wrap it in markdown. "
        "The JSON keys must be: summary, style_guide, factions, regions, locations, npcs, "
        "quests, terms, relations, reference_report. "
        "Use uploaded references only as inspiration or structure according to reference_mode; "
        "do not treat them as canonical lore facts. If project facts are provided, preserve them "
        "as higher-priority facts. Avoid long verbatim reuse from references unless the brief "
        "explicitly asks for quotation. "
        "Each reference_report item must include source_ref, source_title, used_for, "
        "transformation, excluded. "
        + _section_plan(brief)
        + "\n\nProject facts context:\n"
        + ("\n".join(project_lines) if project_lines else "(none)")
        + "\n\nInspiration reference context:\n"
        + ("\n".join(inspiration_lines) if inspiration_lines else "(none)")
    )


def _context_lines(hits: list[RetrievalHit]) -> list[str]:
    return [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in hits]


def _brief_query(brief: WorldSeedBrief) -> str:
    parts = [
        brief.idea,
        " ".join(brief.world_styles),
        brief.tone,
        brief.era,
        brief.game_genre,
        brief.player_fantasy,
        brief.core_conflict,
        brief.notes,
    ]
    return " ".join(part for part in parts if part.strip())


def _bundle_from_payload(
    payload: dict[str, Any],
    *,
    draft_id: str,
    brief: WorldSeedBrief,
    existing: ContentBundle,
    inspiration_pack: ContextPack,
    project_pack: ContextPack,
) -> ContentBundle:
    bundle = ContentBundle()
    id_map: dict[str, str] = {}
    used_entities = set(existing.entities)
    used_regions = set(existing.regions)
    used_pois = set(existing.pois)
    used_quests = set(existing.quests)
    used_terms = set(existing.terms)
    refs = ",".join(inspiration_pack.refs[:8])
    common_meta: dict[str, Any] = {
        "world_seed_id": draft_id,
        "brief": brief.idea,
        "reference_mode": brief.reference_mode,
        "inspiration_refs": refs,
        "project_context_refs": ",".join(project_pack.refs[:8]),
    }

    style = _dict(payload.get("style_guide"))
    body = str(style.get("body") or payload.get("summary") or brief.idea)
    bundle.style_guides["style_guide"] = StyleGuide(
        body=body,
        rules=[str(item) for item in _list(style.get("rules"))],
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )

    for item in _ensure_count(_list(payload.get("factions")), brief.faction_count, "阵营"):
        raw = _dict(item)
        entity = _entity_from_item(
            raw,
            entity_type=EntityType.FACTION,
            prefix="fac",
            used=used_entities,
            metadata=common_meta,
        )
        _remember(id_map, raw, entity.id)
        bundle.entities[entity.id] = entity

    for item in _ensure_count(_list(payload.get("regions")), brief.region_count, "区域"):
        raw = _dict(item)
        region_id = _unique_id(
            "region",
            str(raw.get("id") or raw.get("name") or "region"),
            used_regions,
        )
        _remember(id_map, raw, region_id)
        bundle.regions[region_id] = RegionBrief(
            id=region_id,
            name=str(raw.get("name") or region_id),
            level_min=_int(raw.get("level_min"), 1),
            level_max=_int(raw.get("level_max"), 20),
            themes=[str(item) for item in _list(raw.get("themes"))],
            allowed_content=[str(item) for item in _list(raw.get("allowed_content"))],
            banned_content=[str(item) for item in _list(raw.get("banned_content"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    locations = _ensure_count(
        _list(payload.get("locations")),
        max(brief.region_count + 2, 3) if brief.region_count > 0 else 0,
        "地点",
    )
    region_ids = list(bundle.regions) or list(existing.regions)
    faction_ids = _ids_by_type(bundle, EntityType.FACTION) or _ids_by_type(
        existing, EntityType.FACTION
    )
    used_locations = used_pois | used_entities
    for index, item in enumerate(locations):
        raw = _dict(item)
        loc_id = _unique_id(
            "loc",
            str(raw.get("id") or raw.get("name") or "location"),
            used_locations,
        )
        used_pois.add(loc_id)
        used_entities.add(loc_id)
        _remember(id_map, raw, loc_id)
        poi_region_id = _known_or(
            _resolve(raw.get("region_id"), id_map), region_ids, _round_robin(region_ids, index)
        )
        faction_id = _known_or(
            _resolve(raw.get("controlling_faction"), id_map),
            faction_ids,
            _round_robin(faction_ids, index),
        )
        # No preset filler text: a missing description stays minimal and theme-neutral.
        description = str(raw.get("description") or raw.get("purpose") or raw.get("name") or "")
        bundle.entities[loc_id] = Entity(
            id=loc_id,
            name=str(raw.get("name") or loc_id),
            type=EntityType.LOCATION,
            description=description,
            tags=[str(item) for item in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        bundle.pois[loc_id] = POI(
            id=loc_id,
            name=str(raw.get("name") or loc_id),
            region_id=poi_region_id,
            purpose=str(raw.get("purpose") or description),
            controlling_faction=faction_id,
            level_min=_int(raw.get("level_min"), None),
            level_max=_int(raw.get("level_max"), None),
            tags=[str(item) for item in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        if faction_id:
            bundle.relations.append(_relation(loc_id, faction_id, "controlled_by", common_meta))

    location_ids = list(bundle.pois) or list(existing.pois)
    for index, item in enumerate(_ensure_count(_list(payload.get("npcs")), brief.npc_count, "NPC")):
        raw = _dict(item)
        entity = _entity_from_item(
            raw,
            entity_type=EntityType.NPC,
            prefix="npc",
            used=used_entities,
            metadata=common_meta,
        )
        _remember(id_map, raw, entity.id)
        bundle.entities[entity.id] = entity
        faction_id = _known_or(
            _resolve(raw.get("faction_id"), id_map),
            faction_ids,
            _round_robin(faction_ids, index),
        )
        location_id = _known_or(
            _resolve(raw.get("location_id"), id_map),
            location_ids,
            _round_robin(location_ids, index),
        )
        if faction_id:
            bundle.relations.append(_relation(entity.id, faction_id, "member_of", common_meta))
        if location_id:
            bundle.relations.append(_relation(entity.id, location_id, "located_in", common_meta))

    npc_ids = _ids_by_type(bundle, EntityType.NPC) or _ids_by_type(existing, EntityType.NPC)
    for index, item in enumerate(
        _ensure_count(_list(payload.get("quests")), brief.quest_count, "任务")
    ):
        raw = _dict(item)
        quest_id = _unique_id(
            "quest",
            str(raw.get("id") or raw.get("title") or "quest"),
            used_quests,
        )
        _remember(id_map, raw, quest_id)
        quest_location = _known_or(
            _resolve(raw.get("location"), id_map),
            location_ids,
            _round_robin(location_ids, index),
        )
        quest_giver = _known_or(
            _resolve(raw.get("giver_npc"), id_map),
            npc_ids,
            _round_robin(npc_ids, index),
        )
        # When the model omits stages, derive ONE stage from the quest's own objective —
        # never inject preset beats (确认线索/作出选择 was steering every fallback quest
        # toward the same investigation-shaped arc).
        raw_stages = _list(raw.get("stages")) or [
            str(raw.get("objective") or raw.get("title") or quest_id)
        ]
        stages = [
            QuestStage(
                id=f"{quest_id}_stage_{stage_index + 1}",
                summary=str(stage),
                location=quest_location,
            )
            for stage_index, stage in enumerate(raw_stages)
        ]
        bundle.quests[quest_id] = Quest(
            id=quest_id,
            title=str(raw.get("title") or quest_id),
            giver_npc=quest_giver,
            location=quest_location,
            objective=str(raw.get("objective") or raw.get("title") or quest_id),
            timeline_order=index + 1,
            stages=stages,
            localization_keys=[f"quest.{quest_id}.objective"],
            tags=[str(item) for item in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    for item in _ensure_count(_list(payload.get("terms")), brief.term_count, "术语"):
        raw = _dict(item)
        term_id = _unique_id(
            "term",
            str(raw.get("id") or raw.get("canonical") or "term"),
            used_terms,
        )
        _remember(id_map, raw, term_id)
        bundle.terms[term_id] = Term(
            id=term_id,
            canonical=str(raw.get("canonical") or raw.get("name") or term_id),
            aliases=[str(item) for item in _list(raw.get("aliases"))],
            forbidden=[str(item) for item in _list(raw.get("forbidden"))],
            description=str(raw.get("description") or ""),
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    for item in _list(payload.get("relations")):
        raw = _dict(item)
        source = _resolve(raw.get("source"), id_map)
        target = _resolve(raw.get("target"), id_map)
        kind = str(raw.get("kind") or "").strip()
        known_relation_ids = set(bundle.entities) | set(bundle.pois) | set(bundle.regions)
        if source in known_relation_ids and target in known_relation_ids and kind:
            bundle.relations.append(_relation(source, target, kind, common_meta))
    bundle.relations = _dedupe_relations(bundle.relations)
    return bundle


def _reference_report(
    payload: dict[str, Any],
    inspiration_pack: ContextPack,
    brief: WorldSeedBrief,
) -> list[ReferenceReportItem]:
    rows: list[ReferenceReportItem] = []
    by_ref = {hit.ref: hit for hit in inspiration_pack.hits}
    for item in _list(payload.get("reference_report")):
        raw = _dict(item)
        source_ref = str(raw.get("source_ref") or "")
        hit = by_ref.get(source_ref)
        if not source_ref or hit is None:
            continue
        rows.append(
            ReferenceReportItem(
                source_ref=source_ref,
                source_title=str(
                    raw.get("source_title") or hit.metadata.get("source_title") or hit.title
                ),
                used_for=str(raw.get("used_for") or brief.reference_mode),
                transformation=str(raw.get("transformation") or "转化为新的世界设定元素。"),
                excluded=[str(entry) for entry in _list(raw.get("excluded"))],
            )
        )
    if rows:
        return rows
    return [
        ReferenceReportItem(
            source_ref=hit.ref,
            source_title=hit.metadata.get("source_title") or hit.title,
            used_for=f"{brief.reference_mode}：主题、人物关系或任务节奏参考",
            transformation="转化为新的阵营关系、地点功能和任务目标；参考资料不进入正式设定事实库。",
            excluded=["未复用参考材料中的专有名词", "未复用长段原文"],
        )
        for hit in inspiration_pack.hits[:5]
    ]


def _entity_from_item(
    raw: dict[str, Any],
    *,
    entity_type: EntityType,
    prefix: str,
    used: set[str],
    metadata: dict[str, Any],
) -> Entity:
    entity_id = _unique_id(prefix, str(raw.get("id") or raw.get("name") or entity_type.value), used)
    return Entity(
        id=entity_id,
        name=str(raw.get("name") or entity_id),
        type=entity_type,
        description=str(raw.get("description") or ""),
        aliases=[str(item) for item in _list(raw.get("aliases"))],
        tags=[str(item) for item in _list(raw.get("tags"))],
        metadata=metadata,
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def _relation(source: str, target: str, kind: str, metadata: dict[str, Any]) -> Relation:
    return Relation(
        source=source,
        target=target,
        kind=kind,
        metadata=metadata,
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def _dedupe_relations(relations: list[Relation]) -> list[Relation]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Relation] = []
    for relation in relations:
        key = (relation.source, relation.kind, relation.target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(relation)
    return deduped


def _remember(id_map: dict[str, str], raw: dict[str, Any], canonical_id: str) -> None:
    for value in (raw.get("id"), raw.get("name"), raw.get("title"), raw.get("canonical")):
        if value:
            id_map[str(value)] = canonical_id
    id_map[canonical_id] = canonical_id


def _resolve(value: Any, id_map: dict[str, str]) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return id_map.get(raw) or raw or None


def _unique_id(prefix: str, raw: str, used: set[str]) -> str:
    stem = _slug(raw)
    if stem.startswith(prefix + "_"):
        base = stem
    else:
        base = f"{prefix}_{stem or 'item'}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _slug(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "_", text)
    return text.strip("_")


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ensure_count(items: list[Any], count: int, label: str) -> list[Any]:
    result = list(items)
    while len(result) < count:
        index = len(result) + 1
        result.append(
            {
                "name": f"{label}{index}",
                "description": f"由一句话世界种子补全的{label}。",
            }
        )
    return result[:count]


def _int(value: Any, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ids_by_type(bundle: ContentBundle, entity_type: EntityType) -> list[str]:
    return [entity.id for entity in bundle.entities.values() if entity.type is entity_type]


def _round_robin(values: list[str], index: int) -> str | None:
    if not values:
        return None
    return values[index % len(values)]


def _known_or(value: str | None, known_values: list[str], fallback: str | None) -> str | None:
    return value if value in set(known_values) else fallback
