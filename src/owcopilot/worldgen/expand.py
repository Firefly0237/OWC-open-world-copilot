"""World EXPANSION: grow more grounded content on an EXISTING world.

A single creation is a *seed* — a dozen entities, a handful of quests. To reach the volume a
long-form open world needs, you cannot keep cold-starting new worlds; you have to grow the one you
have. :class:`WorldExpandService` takes an existing world plus one focus (a region, a faction or a
main quest) and grows a batch of new content — N new locations, M secondary NPCs, K side quests —
every piece grounded in the *existing canon* by id, nothing that overwrites or contradicts it.

It reuses creation's discipline verbatim:
  * the same staged, grounded chain (focus → pois → cast → quests), each stage stamped with the
    stage marker so the offline double rides the same multi-call contract (``worldgen/stages``)
  * the same critic→refine quality loop on the capstone quests stage (``WorldQuestCritic`` and the
    deterministic ``quest_grounding_gaps`` are generic — creation or expansion alike);
  * the same deterministic id normalisation (``_unique_id`` seeded with the existing ids, so a new
    object can never collide with canon) and the same review-queue write path.

The one thing creation does NOT have is the honest grounding ledger: every reference the model wrote
is checked against canon ∪ this-batch's-own-new ids, and anything pointing at nothing real is
recorded as a dangling ref — the deterministic gate that must read empty before a batch is trusted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..content.models import (
    POI,
    ContentBundle,
    Entity,
    EntityType,
    Origin,
    Quest,
    QuestStage,
    ReviewStatus,
)
from ..inspiration.retrieval import ReferenceContextBuilder
from ..llm.gateway import LLMGateway
from ..llm.spotlight import spotlight_references
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack
from . import stages
from .critic import (
    QuestRefineOutcome,
    WorldQuestCritic,
    quest_grounding_gaps,
    run_quest_refine_loop,
)
from .models import (
    DensitySignal,
    ExpandGrounding,
    WorldExpandBrief,
    WorldExpandDraft,
)
from .service import (
    _JSON_ONLY_RETRY,
    _context_lines,
    _dedupe_relations,
    _dict,
    _entity_from_item,
    _int,
    _list,
    _reference_report,
    _refine_user_message,
    _refs,
    _relation,
    _remember,
    _resolve,
    _unique_id,
    parse_world_seed_payload,
)


@dataclass
class _Focus:
    """The resolved canon anchor the whole batch hangs from. ``region_id`` / ``faction_id`` are the
    natural defaults a new POI / NPC falls back to when the focus is of that kind."""

    kind: str  # region | faction | quest
    ref_id: str
    label: str
    region_id: str | None
    faction_id: str | None


class WorldExpandService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        project_context_builder: ContextPackBuilder,
        reference_context_builder: ReferenceContextBuilder,
        critic: WorldQuestCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.project_context_builder = project_context_builder
        self.reference_context_builder = reference_context_builder
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

    def expand(
        self,
        brief: WorldExpandBrief,
        *,
        budget_tokens: int = 1800,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
        feedback: str = "",
    ) -> WorldExpandDraft:
        """Grow a batch at ``brief.focus_ref``. ``feedback`` (set on a reviewer-requested revision)
        is woven into every stage's user message so the whole cohesive batch — places, cast and
        quests interlock — is re-grown to address the note, re-grounded on the existing world."""

        def emit(name: str) -> None:
            if progress is not None:
                progress("stage", {"name": name})

        focus = _resolve_focus(self.bundle, brief.focus_ref)

        emit("retrieving")
        query = _expand_query(brief, focus)
        project_pack = (
            self.project_context_builder.build(query, budget_tokens=budget_tokens // 2, limit=6)
            if brief.use_project_facts
            else ContextPack(query=query, budget_tokens=budget_tokens // 2)
        )
        reference_query = brief.reference_query.strip() or query
        inspiration_pack = self.reference_context_builder.build(
            reference_query, budget_tokens=budget_tokens, limit=8
        )

        canon_lines = _canon_lines(self.bundle, focus)
        prefix = _common_prefix(project_pack, inspiration_pack, brief)
        base_user = _expand_user_message(brief, focus)
        if feedback.strip():
            base_user += (
                "\n\n[审阅意见] 请在重做这一批扩写时满足以下修订意见，并继续沿用既有 canon id："
                + feedback.strip()
            )

        # --- stage 1: focus & angle (the mini-spine, grounded in the existing conflict) ---
        emit(stages.EXPAND_FOCUS)
        focus_result = self._stage(
            stages.EXPAND_FOCUS,
            prefix + _focus_suffix(brief, focus, canon_lines, new_lines=[]),
            base_user,
        )
        angle = str(focus_result.get("angle") or brief.angle).strip()

        payload: dict[str, Any] = {}
        relations: list[Any] = []
        reference_rows: list[Any] = []
        new_lines: list[str] = []

        # --- stage 2: new locations, grounded in existing regions + factions ---
        if brief.poi_count > 0:
            emit(stages.EXPAND_POIS)
            pois = self._stage(
                stages.EXPAND_POIS,
                prefix + _pois_suffix(brief, angle, canon_lines, new_lines),
                base_user,
            )
            payload["pois"] = _list(pois.get("pois"))
            reference_rows += _list(pois.get("reference_report"))
            new_lines += _proposed_place_lines(payload["pois"])
        else:
            payload["pois"] = []

        # --- stage 3: new secondary cast, grounded in existing factions + new/existing places ---
        if brief.npc_count > 0:
            emit(stages.EXPAND_CAST)
            cast = self._stage(
                stages.EXPAND_CAST,
                prefix + _cast_suffix(brief, angle, canon_lines, new_lines),
                base_user,
            )
            payload["npcs"] = _list(cast.get("npcs"))
            relations += _list(cast.get("relations"))
            reference_rows += _list(cast.get("reference_report"))
            new_lines += _proposed_cast_lines(payload["npcs"])
        else:
            payload["npcs"] = []

        # --- stage 4: new side quests, grounded in cast + places (+ optional refine loop) ---
        outcome = QuestRefineOutcome([], [], [], [], False)
        if brief.quest_count > 0:
            outcome = self._quests_stage(
                brief, focus, angle, prefix, base_user, canon_lines, new_lines, payload, emit
            )
            payload["quests"] = outcome.quests
            relations += outcome.relations
            reference_rows += outcome.reference_rows
        else:
            payload["quests"] = []

        payload["relations"] = relations
        payload["reference_report"] = reference_rows

        emit("assembling")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        draft_id = (
            "world_expand_"
            + hashlib.sha256(f"{brief.focus_ref}\n{serialized}".encode()).hexdigest()[:12]
        )
        bundle, grounding = _expand_bundle_from_payload(
            payload,
            draft_id=draft_id,
            brief=brief,
            focus=focus,
            existing=self.bundle,
            inspiration_pack=inspiration_pack,
            project_pack=project_pack,
        )
        report = _reference_report(payload, inspiration_pack, brief.reference_mode)
        return WorldExpandDraft(
            id=draft_id,
            brief=brief,
            focus_label=focus.label,
            angle=angle,
            bundle=bundle,
            grounding=grounding,
            density=expansion_density(self.bundle, bundle),
            reference_report=report,
            project_context_refs=project_pack.refs,
            inspiration_context_refs=inspiration_pack.refs,
            refine_trail=outcome.trail,
            auto_review_incomplete=outcome.auto_review_incomplete,
        )

    def _stage(self, stage: str, system: str, user: str) -> dict[str, Any]:
        # Same robustness as creation: a stage that wraps/truncates its JSON is re-asked once for a
        # bare object before the failure is surfaced — never a silently half-built expansion.
        raw = self.gateway.complete(task="world_seed", system=system, user=user)
        try:
            return parse_world_seed_payload(raw)
        except ValueError:
            retry = self.gateway.complete(
                task="world_seed", system=system + _JSON_ONLY_RETRY, user=user
            )
            return parse_world_seed_payload(retry)

    def _quests_stage(
        self,
        brief: WorldExpandBrief,
        focus: _Focus,
        angle: str,
        prefix: str,
        base_user: str,
        canon_lines: list[str],
        new_lines: list[str],
        payload: dict[str, Any],
        emit: Callable[[str], None],
    ) -> QuestRefineOutcome:
        """Generate the new side quests, then (if a critic is wired) run the SHARED critique→refine
        loop genesis and expansion both use — the objective grounding check is over canon ∪ this
        batch's new cast/places, so a quest that references a real id (old or new) passes."""
        emit(stages.EXPAND_QUESTS)
        system = prefix + _quests_suffix(brief, angle, canon_lines, new_lines)
        result = self._stage(stages.EXPAND_QUESTS, system, base_user)
        quests = _list(result.get("quests"))
        relations = _list(result.get("relations"))
        reference_rows = _list(result.get("reference_report"))
        if self.critic is None:
            return QuestRefineOutcome(quests, relations, reference_rows, [], False)

        canon = _canon_id_sets(self.bundle)

        def regenerate(
            prior: list[Any], fixes: list[str]
        ) -> tuple[list[Any], list[Any], list[Any]]:
            r = self._stage(
                stages.EXPAND_QUESTS, system, _refine_user_message(base_user, prior, fixes)
            )
            return (
                _list(r.get("quests")),
                _list(r.get("relations")),
                _list(r.get("reference_report")),
            )

        return run_quest_refine_loop(
            critic=self.critic,
            max_rounds=self.max_refine_rounds,
            quests=quests,
            relations=relations,
            reference_rows=reference_rows,
            npc_refs=canon["npc"] | _refs(payload.get("npcs")),
            place_refs=canon["location"] | _refs(payload.get("pois")),
            context_lines=canon_lines + new_lines,
            brief=f"{focus.label}｜{angle}".strip("｜"),
            regenerate=regenerate,
            emit=emit,
        )


# --- focus resolution --------------------------------------------------------------------------
def _resolve_focus(bundle: ContentBundle, focus_ref: str) -> _Focus:
    """Resolve ``region:<id>`` / ``faction:<id>`` / ``quest:<id>`` (or a bare id) against the world.

    A focus that names nothing the world contains is a hard error — you cannot ground an expansion
    on a canon anchor that does not exist."""
    kind, sep, ref_id = focus_ref.partition(":")
    kind = kind.strip().lower()
    ref_id = ref_id.strip()
    if not sep:
        # tolerate a bare id with no "type:" prefix — match whatever canon collection holds it
        ref_id, kind = kind, ""
    if ref_id in bundle.regions and kind in {"region", ""}:
        region = bundle.regions[ref_id]
        return _Focus("region", ref_id, region.name, ref_id, None)
    entity = bundle.entities.get(ref_id)
    if entity is not None and entity.type is EntityType.FACTION and kind in {"faction", ""}:
        return _Focus("faction", ref_id, entity.name, None, ref_id)
    if ref_id in bundle.quests and kind in {"quest", ""}:
        quest = bundle.quests[ref_id]
        region_id = None
        if quest.location and quest.location in bundle.pois:
            region_id = bundle.pois[quest.location].region_id
        return _Focus("quest", ref_id, quest.title, region_id, None)
    raise ValueError(
        f"focus_ref {focus_ref!r} 不在当前世界里。请用 region:<id> / faction:<id> / quest:<id> "
        "指向一个既有的区域、阵营或主线任务。"
    )


def _canon_id_sets(bundle: ContentBundle) -> dict[str, set[str]]:
    return {
        "faction": {e.id for e in bundle.entities.values() if e.type is EntityType.FACTION},
        "region": set(bundle.regions)
        | {e.id for e in bundle.entities.values() if e.type is EntityType.REGION},
        "location": set(bundle.pois)
        | {e.id for e in bundle.entities.values() if e.type is EntityType.LOCATION},
        "npc": {e.id for e in bundle.entities.values() if e.type is EntityType.NPC},
        "quest": set(bundle.quests),
    }


# --- canon grounding lines (what the model is shown; the offline double parses the same block) ---
_CANON_CAP = 14


def _canon_lines(bundle: ContentBundle, focus: _Focus) -> list[str]:
    """A bounded, focus-centred digest of the existing world the new content must reference by id.

    All factions (few, and any new content may pick a side) plus the focus region's places and the
    cast that lives/operates around the focus. Lines use the same ``- 阵营 <id>（…）`` shapes the
    offline double keys on, so the double exercises the real grounding contract."""
    lines: list[str] = []
    spine = _spine_line(bundle)
    if spine:
        lines.append(spine)

    factions = [e for e in bundle.entities.values() if e.type is EntityType.FACTION]
    for entity in factions:
        lines.append(f"- 阵营 {entity.id}（{entity.name}）：{_trim(entity.description)}".strip())

    # Region(s): focus region first so a downstream POI's "first region" default lands on it.
    region_ids = _ordered_region_ids(bundle, focus)
    for region_id in region_ids:
        region = bundle.regions[region_id]
        themes = "、".join(region.themes)
        lines.append(f"- 区域 {region_id}（{region.name}）：{themes}".strip())

    in_focus_region = set(region_ids[:1]) if focus.kind == "region" else set(region_ids)
    places = [
        poi
        for poi in bundle.pois.values()
        if (poi.region_id in in_focus_region)
        or (focus.kind == "faction" and poi.controlling_faction == focus.faction_id)
    ] or list(bundle.pois.values())
    place_ids = {poi.id for poi in places[:_CANON_CAP]}
    for poi in places[:_CANON_CAP]:
        lines.append(
            f"- 地点 {poi.id}（{poi.name}，区域 {poi.region_id or '?'}，"
            f"控制方 {poi.controlling_faction or '?'}）：{_trim(poi.purpose)}".strip()
        )

    member_of, located_in = _npc_links(bundle)
    npcs = [
        entity
        for entity in bundle.entities.values()
        if entity.type is EntityType.NPC
        and (
            located_in.get(entity.id) in place_ids
            or (focus.kind == "faction" and member_of.get(entity.id) == focus.faction_id)
        )
    ] or [e for e in bundle.entities.values() if e.type is EntityType.NPC]
    for entity in npcs[:_CANON_CAP]:
        lines.append(
            f"- 角色 {entity.id}（{entity.name}，阵营 {member_of.get(entity.id) or '?'}，"
            f"所在 {located_in.get(entity.id) or '?'}）：{_trim(entity.description)}".strip()
        )
    return lines


def _ordered_region_ids(bundle: ContentBundle, focus: _Focus) -> list[str]:
    ids = list(bundle.regions)
    if focus.region_id and focus.region_id in ids:
        ids = [focus.region_id] + [rid for rid in ids if rid != focus.region_id]
    return ids


def _npc_links(bundle: ContentBundle) -> tuple[dict[str, str], dict[str, str]]:
    """First faction (member_of) and place (located_in) each NPC is wired to, from relations."""
    member_of: dict[str, str] = {}
    located_in: dict[str, str] = {}
    for relation in bundle.relations:
        if relation.kind == "member_of":
            member_of.setdefault(relation.source, relation.target)
        elif relation.kind == "located_in":
            located_in.setdefault(relation.source, relation.target)
    return member_of, located_in


def _spine_line(bundle: ContentBundle) -> str:
    for guide in bundle.style_guides.values():
        body = (guide.body or "").strip()
        if body:
            return f"- 世界主轴/风格：{_trim(body, 400)}"
    return ""


def _proposed_place_lines(pois: list[Any]) -> list[str]:
    lines: list[str] = []
    for raw in pois:
        item = _dict(raw)
        ident = str(item.get("id") or item.get("name") or "").strip()
        if not ident:
            continue
        name = str(item.get("name") or ident)
        purpose = str(item.get("purpose") or item.get("description") or "")
        lines.append(f"- 地点 {ident}（{name}）：{_trim(purpose)}".strip())
    return lines


def _proposed_cast_lines(npcs: list[Any]) -> list[str]:
    lines: list[str] = []
    for raw in npcs:
        item = _dict(raw)
        ident = str(item.get("id") or item.get("name") or "").strip()
        if not ident:
            continue
        name = str(item.get("name") or ident)
        faction = str(item.get("faction_id") or "?")
        desc = _trim(str(item.get("description") or ""))
        lines.append(f"- 角色 {ident}（{name}，阵营 {faction}）：{desc}".strip())
    return lines


def _trim(text: str, limit: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


# --- prompts -----------------------------------------------------------------------------------
_ROLE = (
    "You are a senior open-world content designer EXTENDING an existing game world — never "
    "starting a new one. You grow more content (locations, secondary cast, side quests) anchored "
    "to one focus, in the world's own genre and language. You build it in grounded stages; at each "
    "stage you return ONE JSON object only, no markdown, no prose outside the JSON. Every new "
    "object must reference EXISTING canon ids EXACTLY as written — never rename, never invent a "
    "faction/region that is not listed, never restate or overwrite existing settings. New content "
    "must be consistent with the established conflict and deepen the focus, not contradict it. "
    "Descriptions must be concrete enough for a level designer to build from, with a current "
    "tension or hook. Use uploaded references only as inspiration per reference_mode, never as "
    "canon facts. Each reference_report item must include source_ref, source_title, used_for, "
    "transformation, excluded."
)


def _common_prefix(
    project_pack: ContextPack, inspiration_pack: ContextPack, brief: WorldExpandBrief
) -> str:
    project_lines = _context_lines(project_pack.hits)
    inspiration_lines = _context_lines(inspiration_pack.hits)
    return (
        _ROLE
        + "\n\nExpansion plan (this stage produces only its own slice):\n"
        + _section_plan(brief)
        + "\n\nProject facts context (existing canon retrieved for this focus):\n"
        + ("\n".join(project_lines) if project_lines else "(none)")
        # Untrusted reference text is the OWASP LLM01 indirect-injection vector — fence it as data
        # so an uploaded document can't smuggle instructions into the expansion prompt.
        + "\n\nInspiration reference context:\n"
        + spotlight_references(inspiration_lines)
    )


def _section_plan(brief: WorldExpandBrief) -> str:
    wanted = []
    for key, count in (
        ("pois", brief.poi_count),
        ("npcs", brief.npc_count),
        ("quests", brief.quest_count),
    ):
        if count > 0:
            wanted.append(f"{key}={count}")
    return "Target new-content counts: " + (", ".join(wanted) if wanted else "(none)") + ". "


def _stage_header(stage: str, ordinal: str, title: str) -> str:
    return f"{stages.stage_marker(stage)}\n\nEXPAND STAGE {ordinal} — {title}\n"


def _focus_suffix(
    brief: WorldExpandBrief, focus: _Focus, canon_lines: list[str], new_lines: list[str]
) -> str:
    return (
        _stage_header(stages.EXPAND_FOCUS, "1/4", "FOCUS & ANGLE")
        + f"The focus to deepen is {focus.kind} `{focus.ref_id}` ({focus.label}). Read the canon "
        "below and decide the ANGLE for this batch. Return keys: angle, central_ids.\n"
        "- angle: 2-3 sentences naming the unrealised tension at this focus that new locations, "
        "cast and quests will dramatise — grounded in the established conflict, deepening it, "
        "never contradicting it.\n"
        "- central_ids: the existing ids (factions / places / cast) the batch should orbit.\n"
        + _grounding_block(brief, "", canon_lines, new_lines)
    )


def _pois_suffix(
    brief: WorldExpandBrief, angle: str, canon_lines: list[str], new_lines: list[str]
) -> str:
    return (
        _stage_header(stages.EXPAND_POIS, "2/4", "NEW LOCATIONS")
        + f"Design exactly {brief.poi_count} NEW locations (POIs) that deepen the focus. Each must "
        "sit in an EXISTING region and name an EXISTING controlling faction — reference their ids "
        "EXACTLY from the canon below; do NOT invent new regions or factions. Return keys: pois, "
        "reference_report.\n"
        "- pois: each {id (a NEW 'loc_*' id not already in canon), name, description, purpose, "
        "region_id (an EXISTING region id below), controlling_faction (an EXISTING faction id "
        "below), tags []}.\n" + _grounding_block(brief, angle, canon_lines, new_lines)
    )


def _cast_suffix(
    brief: WorldExpandBrief, angle: str, canon_lines: list[str], new_lines: list[str]
) -> str:
    return (
        _stage_header(stages.EXPAND_CAST, "3/4", "NEW CAST")
        + f"Design exactly {brief.npc_count} NEW secondary NPCs for the focus. Each belongs to an "
        "EXISTING faction and lives in an existing OR new location below; each has a stake in "
        "the focus's tension. Return keys: npcs, relations, reference_report.\n"
        "- npcs: each {id (a NEW 'npc_*' id not already in canon), name, description (role, what "
        "they want, their stake in the focus tension), faction_id (an EXISTING faction id below), "
        "location_id (an existing or newly-added location id below)}.\n"
        "- relations: ties among the new npcs and to existing factions/cast, using the ids below "
        "{source, target, kind}.\n" + _grounding_block(brief, angle, canon_lines, new_lines)
    )


def _quests_suffix(
    brief: WorldExpandBrief, angle: str, canon_lines: list[str], new_lines: list[str]
) -> str:
    return (
        _stage_header(stages.EXPAND_QUESTS, "4/4", "NEW SIDE QUESTS")
        + f"Design exactly {brief.quest_count} NEW side quests that dramatise the focus's tension. "
        "Each puts the player inside a real choice and references existing OR newly-added cast and "
        "places by id. Return keys: quests, relations, reference_report.\n"
        "- quests: each {title (a player-facing headline, never an id), objective (one concrete "
        "sentence: who wants what done and why it matters now), giver_npc (an existing or new NPC "
        "id below), location (an existing or new location id below), stages (2-4 stage summaries, "
        "each naming where it happens and what the player does, referencing the cast/places), "
        "tags []}.\n" + _grounding_block(brief, angle, canon_lines, new_lines)
    )


def _grounding_block(
    brief: WorldExpandBrief, angle: str, canon_lines: list[str], new_lines: list[str]
) -> str:
    parts: list[str] = []
    if angle:
        parts.append(f"本次扩写角度：{angle}")
    if brief.notes.strip():
        parts.append(f"补充要求：{brief.notes.strip()}")
    parts.append("已有正典（必须按既有 id 引用，不得改写或新造同名条目）：")
    parts.extend(canon_lines)
    if new_lines:
        parts.append("本批已新增（下游可引用其 id）：")
        parts.extend(new_lines)
    return "\n" + "\n".join(parts)


def _expand_query(brief: WorldExpandBrief, focus: _Focus) -> str:
    parts = [focus.label, brief.angle, brief.notes, brief.reference_query]
    return " ".join(part for part in parts if part and part.strip()) or focus.ref_id


def _expand_user_message(brief: WorldExpandBrief, focus: _Focus) -> str:
    lines = [f"扩写目标（focus）：{focus.label}（{brief.focus_ref}）"]
    if brief.angle.strip():
        lines.append(f"扩写角度：{brief.angle.strip()}")
    if brief.notes.strip():
        lines.append(f"补充要求：{brief.notes.strip()}")
    lines.append(
        "只产出新内容；所有引用必须指向上文已有正典的 id（或本批新增的 id）。"
        "不要重述、覆盖或矛盾既有设定。"
    )
    return "\n".join(lines)


# --- deterministic assembly + grounding ledger -------------------------------------------------
def _expand_bundle_from_payload(
    payload: dict[str, Any],
    *,
    draft_id: str,
    brief: WorldExpandBrief,
    focus: _Focus,
    existing: ContentBundle,
    inspiration_pack: ContextPack,
    project_pack: ContextPack,
) -> tuple[ContentBundle, ExpandGrounding]:
    """Normalise the new-content payload into a bundle of ONLY new objects, wiring their references
    to canon ∪ this-batch ids. Like creation's normaliser the assembled bundle is always buildable
    (a reference that fails to resolve falls back to the focus anchor / a new sibling), but the
    *grounding ledger* is measured on the raw payload — the honest count of how many references the
    model actually pointed at a real id, and which ones dangled."""
    bundle = ContentBundle()
    id_map: dict[str, str] = {}
    canon = _canon_id_sets(existing)
    used_entities = set(existing.entities) | set(existing.pois) | set(existing.regions)
    used_quests = set(existing.quests)
    common_meta: dict[str, Any] = {
        "world_seed_id": draft_id,
        "expand_focus": brief.focus_ref,
        "reference_mode": brief.reference_mode,
        "inspiration_refs": ",".join(inspiration_pack.refs[:8]),
        "project_context_refs": ",".join(project_pack.refs[:8]),
    }

    proposed = {
        "location": _refs(payload.get("pois")),
        "npc": _refs(payload.get("npcs")),
    }
    grounding = ExpandGrounding(canon_anchor=f"{focus.kind}:{focus.ref_id}")
    ledger = _GroundingLedger(grounding, canon)

    # --- new locations ---
    new_loc_ids: list[str] = []
    used_locations = used_entities.copy()
    for raw_item in _list(payload.get("pois"))[: brief.poi_count]:
        raw = _dict(raw_item)
        loc_id = _unique_id(
            "loc", str(raw.get("id") or raw.get("name") or "location"), used_locations
        )
        used_locations.add(loc_id)
        used_entities.add(loc_id)
        _remember(id_map, raw, loc_id)
        new_loc_ids.append(loc_id)
        region_id = ledger.land(
            raw.get("region_id"),
            id_map,
            "region",
            anchor=focus.region_id,
            fallback=_first(canon["region"]),
        )
        faction_id = ledger.land(
            raw.get("controlling_faction"),
            id_map,
            "faction",
            anchor=focus.faction_id,
            fallback=_first(canon["faction"]),
        )
        description = str(raw.get("description") or raw.get("purpose") or raw.get("name") or "")
        bundle.entities[loc_id] = Entity(
            id=loc_id,
            name=str(raw.get("name") or loc_id),
            type=EntityType.LOCATION,
            description=description,
            tags=[str(tag) for tag in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        bundle.pois[loc_id] = POI(
            id=loc_id,
            name=str(raw.get("name") or loc_id),
            region_id=region_id,
            purpose=str(raw.get("purpose") or description),
            controlling_faction=faction_id,
            level_min=_int(raw.get("level_min"), None),
            level_max=_int(raw.get("level_max"), None),
            tags=[str(tag) for tag in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        if faction_id:
            bundle.relations.append(_relation(loc_id, faction_id, "controlled_by", common_meta))

    # --- new secondary cast ---
    new_npc_ids: list[str] = []
    canon_and_new_loc = canon["location"] | set(new_loc_ids) | proposed["location"]
    for raw_item in _list(payload.get("npcs"))[: brief.npc_count]:
        raw = _dict(raw_item)
        entity = _entity_from_item(
            raw, entity_type=EntityType.NPC, prefix="npc", used=used_entities, metadata=common_meta
        )
        _remember(id_map, raw, entity.id)
        new_npc_ids.append(entity.id)
        bundle.entities[entity.id] = entity
        faction_id = ledger.land(
            raw.get("faction_id"),
            id_map,
            "faction",
            anchor=focus.faction_id,
            fallback=_first(canon["faction"]),
        )
        location_id = ledger.land(
            raw.get("location_id"),
            id_map,
            "location",
            anchor=_first(new_loc_ids),
            fallback=_first(canon_and_new_loc),
            valid=canon_and_new_loc,
        )
        if faction_id:
            bundle.relations.append(_relation(entity.id, faction_id, "member_of", common_meta))
        if location_id:
            bundle.relations.append(_relation(entity.id, location_id, "located_in", common_meta))

    # --- new side quests ---
    canon_and_new_npc = canon["npc"] | set(new_npc_ids) | proposed["npc"]
    for index, raw_item in enumerate(_list(payload.get("quests"))[: brief.quest_count]):
        raw = _dict(raw_item)
        quest_id = _unique_id(
            "quest", str(raw.get("id") or raw.get("title") or "quest"), used_quests
        )
        _remember(id_map, raw, quest_id)
        quest_location = ledger.land(
            raw.get("location"),
            id_map,
            "location",
            anchor=_first(new_loc_ids),
            fallback=_first(canon_and_new_loc),
            valid=canon_and_new_loc,
        )
        quest_giver = ledger.land(
            raw.get("giver_npc"),
            id_map,
            "npc",
            anchor=_first(new_npc_ids),
            fallback=_first(canon_and_new_npc),
            valid=canon_and_new_npc,
        )
        title = str(raw.get("title") or raw.get("name") or quest_id)
        objective = str(raw.get("objective") or raw.get("description") or title)
        raw_stages = _list(raw.get("stages")) or [objective]
        quest_stages = [
            QuestStage(
                id=f"{quest_id}_stage_{stage_index + 1}",
                summary=(
                    str(
                        _dict(stage).get("summary")
                        or _dict(stage).get("description")
                        or _dict(stage).get("name")
                        or stage
                    )
                    if isinstance(stage, dict)
                    else str(stage)
                ),
                location=quest_location,
            )
            for stage_index, stage in enumerate(raw_stages)
        ]
        bundle.quests[quest_id] = Quest(
            id=quest_id,
            title=title,
            giver_npc=quest_giver,
            location=quest_location,
            objective=objective,
            timeline_order=len(existing.quests) + index + 1,
            stages=quest_stages,
            localization_keys=[f"quest.{quest_id}.objective"],
            tags=[str(tag) for tag in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    # --- explicit relations: keep only those wiring real ids (canon ∪ this batch) ---
    known_ids = (
        set(bundle.entities)
        | set(bundle.pois)
        | canon["faction"]
        | canon["region"]
        | canon["location"]
        | canon["npc"]
    )
    for raw_item in _list(payload.get("relations")):
        raw = _dict(raw_item)
        source = _resolve(raw.get("source"), id_map)
        target = _resolve(raw.get("target"), id_map)
        kind = str(raw.get("kind") or "").strip()
        if source in known_ids and target in known_ids and kind:
            bundle.relations.append(_relation(source, target, kind, common_meta))
    bundle.relations = _dedupe_relations(bundle.relations)
    grounding.canon_ids_referenced = sorted(ledger.referenced)
    return bundle, grounding


class _GroundingLedger:
    """Resolve one reference and record honestly which of three buckets it falls in. ``land`` always
    returns a *buildable* id (resolved → focus anchor → fallback sibling) so the bundle is never
    broken, but the ledger never hides what the model actually did:
      * the model's value resolved to a real id  → ``grounded_refs += 1`` (a genuine grounding);
      * a non-empty value resolved to nothing     → ``dangling_refs`` (the model invented an id);
      * the value was empty/blank                 → ``unspecified_refs`` (the model omitted it; the
        assembly auto-anchored it to the focus, but that is a gap to fill, not a silent success).
    ``referenced`` is the distinct set of CANON ids the batch ends up touching (incl. auto-anchors)
    — that is deliberately a different notion from ``grounded_refs`` (a count of the model's own
    hits) and powers the canon-anchor ratio, not the trustworthiness gate."""

    def __init__(self, grounding: ExpandGrounding, canon: dict[str, set[str]]) -> None:
        self.grounding = grounding
        self.canon = canon
        self.referenced: set[str] = set()

    def land(
        self,
        value: Any,
        id_map: dict[str, str],
        kind: str,
        *,
        anchor: str | None,
        fallback: str | None,
        valid: set[str] | None = None,
    ) -> str | None:
        valid_set = valid if valid is not None else self.canon.get(kind, set())
        resolved = _resolve(value, id_map)
        raw_value = str(value or "").strip()
        if resolved and resolved in valid_set:
            self.grounding.grounded_refs += 1
            if resolved in self.canon.get(kind, set()):
                self.referenced.add(resolved)
            return resolved
        # Not a real reference the model made: classify it honestly (invented vs omitted), then keep
        # the bundle buildable by anchoring to the focus or a new sibling.
        if raw_value:
            self.grounding.dangling_refs.append(f"{kind}:{raw_value}")
        else:
            self.grounding.unspecified_refs.append(kind)
        if anchor:
            if anchor in self.canon.get(kind, set()):
                self.referenced.add(anchor)
            return anchor
        return fallback


def _first(values: Any) -> str | None:
    for value in values:
        return str(value)
    return None


def expand_grounding_gaps(
    payload: dict[str, Any],
    *,
    canon: dict[str, set[str]],
) -> list[str]:
    """Deterministic completeness/grounding check over a whole expansion payload (the objective
    signal a reviewer and the report read). Mirrors :func:`quest_grounding_gaps`, widened to the new
    locations and cast — a gap is any reference a level designer could not resolve."""
    gaps: list[str] = []
    proposed_loc = canon["location"] | _refs(payload.get("pois"))
    proposed_npc = canon["npc"] | _refs(payload.get("npcs"))
    for raw_item in _list(payload.get("pois")):
        raw = _dict(raw_item)
        label = str(raw.get("name") or raw.get("id") or "新地点")
        if str(raw.get("region_id") or "").strip() not in canon["region"]:
            gaps.append(f"「{label}」的 region_id 必须引用既有区域之一。")
        if str(raw.get("controlling_faction") or "").strip() not in canon["faction"]:
            gaps.append(f"「{label}」的 controlling_faction 必须引用既有阵营之一。")
    for raw_item in _list(payload.get("npcs")):
        raw = _dict(raw_item)
        label = str(raw.get("name") or raw.get("id") or "新角色")
        if str(raw.get("faction_id") or "").strip() not in canon["faction"]:
            gaps.append(f"「{label}」的 faction_id 必须引用既有阵营之一。")
        if str(raw.get("location_id") or "").strip() not in proposed_loc:
            gaps.append(f"「{label}」的 location_id 必须引用既有或本批新增地点之一。")
    gaps.extend(
        quest_grounding_gaps(
            _list(payload.get("quests")), npc_refs=proposed_npc, place_refs=proposed_loc
        )
    )
    return gaps


def expansion_density(existing: ContentBundle, new_bundle: ContentBundle) -> DensitySignal:
    """Deterministic read on whether this expansion is over-loading the world with side content.

    Two signals a planner actually cares about: (1) the side/main balance — a single expansion that
    adds more quests than the whole world had is a strong dilution flag; (2) regional concentration
    — quests pile onto a region via their location's ``region_id``, so one region quietly becoming
    quest-dense is worth surfacing. ``note`` stays empty when the balance looks healthy."""
    existing_quests = len(existing.quests)
    new_quests = len(new_bundle.quests)

    loc_region: dict[str, str] = {
        loc_id: (poi.region_id or "")
        for poi_map in (existing.pois, new_bundle.pois)
        for loc_id, poi in poi_map.items()
    }
    region_load: dict[str, int] = {}
    for quest_map in (existing.quests, new_bundle.quests):
        for quest in quest_map.values():
            region = loc_region.get(quest.location or "", "")
            if region:
                region_load[region] = region_load.get(region, 0) + 1
    busiest, busiest_n = ("", 0)
    if region_load:
        busiest, busiest_n = max(region_load.items(), key=lambda kv: kv[1])

    notes: list[str] = []
    if new_quests and new_quests > max(existing_quests, 3):
        notes.append(
            f"本次新增 {new_quests} 条支线，已超过既有 {existing_quests} 条任务——"
            "主线密度可能被稀释，建议先补主线或放慢扩写节奏。"
        )
    if busiest_n >= 6:
        notes.append(f"区域「{busiest}」已聚集 {busiest_n} 条任务，单区任务偏密，注意分布。")
    return DensitySignal(
        existing_quests=existing_quests,
        new_quests=new_quests,
        busiest_region=busiest,
        busiest_region_quests=busiest_n,
        note=" ".join(notes),
    )


__all__ = ["WorldExpandService", "expand_grounding_gaps", "expansion_density"]
