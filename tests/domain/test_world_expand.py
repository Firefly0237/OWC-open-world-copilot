"""World EXPANSION tests (offline, $0): grow grounded content on an existing world, every new
reference landing on a real canon id, into the same review-queue write path a seed uses."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    add_reference_action,
    decide_review_action,
    list_review_items_action,
    run_world_expand_action,
)
from owcopilot.content.models import (
    POI,
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    RegionBrief,
    Relation,
    StyleGuide,
)
from owcopilot.content.store import ContentStore
from owcopilot.retrieval.models import ContextPack
from owcopilot.worldgen import WorldExpandBrief
from owcopilot.worldgen.expand import (
    _canon_id_sets,
    _expand_bundle_from_payload,
    _resolve_focus,
    expand_grounding_gaps,
    expansion_density,
)


def _seed_world(root) -> ContentBundle:
    """A small but coherent existing world: 2 factions, 2 regions, a hub location, a quest-giver and
    one main quest — enough canon for an expansion batch to reference by id."""
    bundle = ContentBundle()
    bundle.style_guides["style_guide"] = StyleGuide(
        body="戏剧主轴\n核心冲突：炉心公约垄断核心能源以维持城市秩序，旧林盟誓誓要夺回被开采的圣地。"
    )
    bundle.entities["fac_charter"] = Entity(
        id="fac_charter",
        name="炉心公约",
        type=EntityType.FACTION,
        description="控制核心能源的工程共同体。",
    )
    bundle.entities["fac_wilds"] = Entity(
        id="fac_wilds",
        name="旧林盟誓",
        type=EntityType.FACTION,
        description="守护旧生态的边境联盟。",
    )
    bundle.regions["region_crown"] = RegionBrief(
        id="region_crown", name="冠城环带", themes=["工业中心", "阶层矛盾"]
    )
    bundle.regions["region_wilds"] = RegionBrief(
        id="region_wilds", name="灰烬旧林", themes=["荒野探索", "古老信仰"]
    )
    bundle.pois["loc_gate"] = POI(
        id="loc_gate",
        name="冠门车站",
        region_id="region_crown",
        controlling_faction="fac_charter",
        purpose="新手枢纽，展示能源垄断与难民流动。",
    )
    bundle.entities["loc_gate"] = Entity(
        id="loc_gate", name="冠门车站", type=EntityType.LOCATION, description="新手枢纽。"
    )
    bundle.entities["npc_sera"] = Entity(
        id="npc_sera", name="瑟拉", type=EntityType.NPC, description="炉心公约的年轻工程官。"
    )
    bundle.quests["quest_intro"] = Quest(
        id="quest_intro",
        title="第一枚火花",
        giver_npc="npc_sera",
        location="loc_gate",
        objective="调查冠门车站的能源中断。",
    )
    bundle.relations.append(Relation(source="npc_sera", target="fac_charter", kind="member_of"))
    bundle.relations.append(Relation(source="npc_sera", target="loc_gate", kind="located_in"))
    bundle.relations.append(Relation(source="loc_gate", target="fac_charter", kind="controlled_by"))
    ContentStore(root).save(bundle)
    return bundle


def test_expand_grows_grounded_content_into_review_then_accept_merges(tmp_path) -> None:
    root = tmp_path / "content"
    _seed_world(root)
    result = run_world_expand_action(
        root,
        brief={
            "focus_ref": "region:region_crown",
            "poi_count": 2,
            "npc_count": 3,
            "quest_count": 2,
        },
    )

    assert result["counts"]["pois"] == 2
    assert result["counts"]["quests"] == 2
    npcs = [e for e in result["bundle"]["entities"].values() if e["type"] == "npc"]
    assert len(npcs) == 3

    # The grounding ledger is the honest gate: every reference landed on a real id, none dangled.
    grounding = result["grounding"]
    assert grounding["dangling_refs"] == []
    assert grounding["grounded_refs"] > 0
    assert grounding["canon_anchor"] == "region:region_crown"
    # New content references EXISTING canon ids, never inventing a region/faction.
    region_ids = {"region_crown", "region_wilds"}
    faction_ids = {"fac_charter", "fac_wilds"}
    for poi in result["bundle"]["pois"].values():
        assert poi["region_id"] in region_ids
        assert poi["controlling_faction"] in faction_ids

    # Queued through the world-seed write path; nothing written to canon yet.
    queue = list_review_items_action(root)
    assert queue["count"] == 1
    assert queue["items"][0]["item_type"] == "world_seed"

    accepted = decide_review_action(
        root, item_id=result["review_item_id"], decision="accepted", operator="lead"
    )
    assert accepted["written_ref"].startswith("world_seed:")
    saved = ContentStore(root).load()
    # Existing canon is untouched; the batch's new content is merged in.
    assert "npc_sera" in saved.entities and "fac_charter" in saved.entities
    assert "quest_intro" in saved.quests and len(saved.quests) == 3  # 1 existing + 2 new
    assert len(saved.pois) == 3  # 1 existing + 2 new
    new_quests = [q for q in saved.quests.values() if q.id != "quest_intro"]
    assert all(q.review_status.value == "approved" for q in new_quests)
    assert all(q.origin.value == "ai_draft" for q in new_quests)


def test_use_references_flag_toggles_inspiration_grounding_on_expand(tmp_path) -> None:
    """Mirrors WorldSeedBrief.use_references (N2.4 / B7): on by default, and turning it off keeps
    the expansion batch clear of the inspiration library entirely."""
    root = tmp_path / "content"
    _seed_world(root)
    add_reference_action(
        root,
        title="边境结构手记",
        text="三股势力围绕一处能源枢纽明争暗斗，谁掌握枢纽谁就掌握全局。",
        allowed_uses=["inspiration"],
    )
    brief = {
        "focus_ref": "region:region_crown",
        "reference_query": "三股势力 能源枢纽",
        "poi_count": 1,
        "npc_count": 1,
        "quest_count": 1,
    }
    grounded = run_world_expand_action(root, brief=brief)
    assert grounded["inspiration_context_refs"]

    clean_room = run_world_expand_action(root, brief={**brief, "use_references": False})
    assert clean_room["inspiration_context_refs"] == []


def test_expand_action_surfaces_conversion_report(tmp_path) -> None:
    # Parity with run_world_seed_action: the honest normalisation ledger rides on the action's
    # return dict (serialized ConversionReport -> {"notes": [...]}), so a partial expand is visible
    # to CLI/REST/web instead of silently completing.
    root = tmp_path / "content"
    _seed_world(root)
    brief = {"focus_ref": "region:region_crown", "poi_count": 1, "npc_count": 1, "quest_count": 1}
    result = run_world_expand_action(root, brief=brief)
    assert "conversion_report" in result
    assert isinstance(result["conversion_report"], dict)
    assert "notes" in result["conversion_report"]


def test_expand_emits_real_stages_and_quests_ground_on_canon_or_new(tmp_path) -> None:
    root = tmp_path / "content"
    existing = _seed_world(root)
    seen: list[str] = []
    result = run_world_expand_action(
        root,
        brief={
            "focus_ref": "region:region_crown",
            "poi_count": 2,
            "npc_count": 2,
            "quest_count": 2,
        },
        refine_rounds=0,  # plain staged chain, no refine loop
        progress=lambda _type, data: seen.append(str(data.get("name"))),
    )
    for stage in (
        "retrieving",
        "expand_focus",
        "expand_pois",
        "expand_cast",
        "expand_quests",
        "assembling",
    ):
        assert stage in seen, f"missing real expand stage: {stage}"

    bundle = result["bundle"]
    canon = _canon_id_sets(existing)
    new_npc_ids = {eid for eid, e in bundle["entities"].items() if e["type"] == "npc"}
    new_loc_ids = set(bundle["pois"])
    npc_universe = canon["npc"] | new_npc_ids
    loc_universe = canon["location"] | new_loc_ids
    for quest in bundle["quests"].values():
        assert quest["giver_npc"] in npc_universe
        assert quest["location"] in loc_universe
    # cross-batch relations wiring new content to canon and to each other
    kinds = {rel["kind"] for rel in bundle["relations"]}
    assert {"member_of", "located_in", "controlled_by"} <= kinds
    assert result["refine_trail"] == []


def test_expand_refine_loop_converges_and_deepens(tmp_path) -> None:
    root = tmp_path / "content"
    _seed_world(root)
    result = run_world_expand_action(
        root,
        brief={
            "focus_ref": "region:region_crown",
            "poi_count": 1,
            "npc_count": 2,
            "quest_count": 2,
        },
        refine_rounds=2,
    )
    trail = result["refine_trail"]
    assert [r["verdict"] for r in trail] == ["revise", "pass"]
    assert trail[0]["gap_count"] > 0
    assert trail[1]["gap_count"] == 0
    for quest in result["bundle"]["quests"].values():
        assert len(quest["stages"]) >= 2


def test_expand_focus_can_be_faction_or_quest(tmp_path) -> None:
    root = tmp_path / "content"
    _seed_world(root)
    by_faction = run_world_expand_action(
        root,
        brief={"focus_ref": "faction:fac_wilds", "poi_count": 1, "npc_count": 1, "quest_count": 1},
    )
    assert by_faction["focus_label"] == "旧林盟誓"
    assert by_faction["grounding"]["canon_anchor"] == "faction:fac_wilds"
    assert by_faction["grounding"]["dangling_refs"] == []

    by_quest = run_world_expand_action(
        root,
        brief={"focus_ref": "quest:quest_intro", "poi_count": 1, "npc_count": 1, "quest_count": 1},
    )
    assert by_quest["focus_label"] == "第一枚火花"
    assert by_quest["grounding"]["dangling_refs"] == []


def test_expand_unknown_focus_is_a_clear_error(tmp_path) -> None:
    root = tmp_path / "content"
    _seed_world(root)
    with pytest.raises(ValueError, match="focus_ref"):
        run_world_expand_action(root, brief={"focus_ref": "region:region_ghost"})


def test_expand_grounding_ledger_flags_dangling_and_preserves_raw_value_for_audit(tmp_path) -> None:
    """A reference the model invents (no such canon id) is recorded as dangling by the
    deterministic ledger AND kept VERBATIM in the assembled bundle — never round-robined onto a
    real canon sibling. This is the scale-p0 batch-2 alignment with genesis's ``_grounded_ref``
    (worldgen/service.py): the deterministic reference audit is the arbiter for a wrong reference,
    not a silent id-rescue here. The honest measure is on the raw payload AND on the assembled
    bundle now — they agree."""
    root = tmp_path / "content"
    existing = _seed_world(root)
    canon = _canon_id_sets(existing)
    payload = {
        "pois": [
            {
                "id": "loc_new",
                "name": "新据点",
                "region_id": "region_crown",
                "controlling_faction": "fac_ghost",
            }
        ],
        "npcs": [
            {"id": "npc_new", "name": "新角色", "faction_id": "fac_ghost", "location_id": "loc_new"}
        ],
        "quests": [
            {
                "title": "新支线",
                "objective": "测试悬空引用。",
                "giver_npc": "npc_ghost",
                "location": "loc_new",
                "stages": ["a", "b"],
            }
        ],
        "relations": [],
    }
    gaps = expand_grounding_gaps(payload, canon=canon)
    assert any("controlling_faction" in gap for gap in gaps)
    assert any("faction_id" in gap for gap in gaps)

    focus = _resolve_focus(existing, "region:region_crown")
    empty_pack = ContextPack(query="x", budget_tokens=10)
    bundle, grounding, _conversion_report = _expand_bundle_from_payload(
        payload,
        draft_id="test",
        brief=WorldExpandBrief(focus_ref="region:region_crown"),
        focus=focus,
        existing=existing,
        inspiration_pack=empty_pack,
        project_pack=empty_pack,
    )
    # the invented faction/npc references are recorded as dangling…
    assert any("fac_ghost" in ref for ref in grounding.dangling_refs)
    assert any("npc_ghost" in ref for ref in grounding.dangling_refs)
    # …and kept verbatim in the assembled bundle: the VALID region ref still lands on real canon,
    # but the INVALID faction/npc refs are preserved rather than silently replaced.
    assert len(bundle.pois) == 1
    poi = next(iter(bundle.pois.values()))
    assert poi.region_id == "region_crown"
    assert poi.controlling_faction == "fac_ghost"  # never falls back to a real canon faction
    quest = next(iter(bundle.quests.values()))
    assert quest.giver_npc == "npc_ghost"  # never falls back to a real canon npc

    # …which is the whole point: the deterministic audit (existing rules, unmodified) now actually
    # catches what the ledger flagged, instead of it vanishing into an anchored substitute.
    from owcopilot.app.actions import _merge_preview
    from owcopilot.audit.context import AuditContext
    from owcopilot.audit.rules.graph_rules import MissingRelationEndpointRule
    from owcopilot.audit.rules.reference_rules import MissingEntityReferenceRule

    merged = _merge_preview(existing, bundle)
    ctx = AuditContext.from_bundle(merged)
    entity_issues = {issue.target_ref for issue in MissingEntityReferenceRule().check(ctx)}
    assert f"poi:{poi.id}" in entity_issues  # controlling_faction -> fac_ghost
    assert f"quest:{quest.id}" in entity_issues  # giver_npc -> npc_ghost
    relation_issues = list(MissingRelationEndpointRule().check(ctx))
    assert any("fac_ghost" in issue.target_ref for issue in relation_issues)  # npc member_of


def test_expand_grounding_ledger_counts_omitted_refs_as_unspecified(tmp_path) -> None:
    """A reference the model leaves BLANK must not be silently passed off as grounded: the assembly
    auto-anchors it to the focus (so the bundle builds), but the ledger records it under
    ``unspecified_refs`` and the batch is no longer trustworthy. Closes the honesty hole where an
    omitted region_id read as 'zero dangling'."""
    root = tmp_path / "content"
    existing = _seed_world(root)
    focus = _resolve_focus(existing, "region:region_crown")
    empty_pack = ContextPack(query="x", budget_tokens=10)
    payload = {
        # region_id left blank — the model omitted it entirely
        "pois": [{"id": "loc_new", "name": "新据点", "region_id": "", "controlling_faction": ""}],
        "npcs": [],
        "quests": [],
        "relations": [],
    }
    _bundle, grounding, _conversion_report = _expand_bundle_from_payload(
        payload,
        draft_id="test",
        brief=WorldExpandBrief(focus_ref="region:region_crown"),
        focus=focus,
        existing=existing,
        inspiration_pack=empty_pack,
        project_pack=empty_pack,
    )
    assert grounding.dangling_refs == []  # nothing invented…
    assert grounding.unspecified_refs  # …but the blank refs are surfaced, not hidden
    assert grounding.is_trustworthy is False


def test_expand_bundle_records_derived_stage_on_conversion_report(tmp_path) -> None:
    """A quest that arrives with no ``stages`` gets one derived from its own ``objective`` (never a
    preset placeholder beat) — same as genesis's identical derivation in ``_bundle_from_payload``.
    This must be recorded on the returned ``ConversionReport``, not silently absorbed: expand used
    to derive the stage without any bookkeeping at all (the scale-p0 batch-2 gap this closes)."""
    root = tmp_path / "content"
    existing = _seed_world(root)
    focus = _resolve_focus(existing, "region:region_crown")
    empty_pack = ContextPack(query="x", budget_tokens=10)
    payload = {
        "pois": [],
        "npcs": [],
        "quests": [
            {
                "id": "quest_new",
                "title": "无阶段支线",
                "objective": "调查冠门车站的能源中断复发。",
                "giver_npc": "npc_sera",
                "location": "loc_gate",
                # no "stages" key at all — must be derived from objective, and recorded
            }
        ],
        "relations": [],
    }
    bundle, _grounding, conversion_report = _expand_bundle_from_payload(
        payload,
        draft_id="test",
        brief=WorldExpandBrief(focus_ref="region:region_crown"),
        focus=focus,
        existing=existing,
        inspiration_pack=empty_pack,
        project_pack=empty_pack,
    )
    quest = bundle.quests["quest_new"]
    assert len(quest.stages) == 1
    assert quest.stages[0].summary == quest.objective

    assert conversion_report.is_clean is False
    derived = conversion_report.by_kind("derived_stage")
    assert len(derived) == 1
    assert derived[0].ref == "quest:quest_new"
    assert derived[0].detail == f"objective={quest.objective}"


def test_expand_bundle_conversion_report_is_clean_when_quest_has_stages(tmp_path) -> None:
    """The counterpart to the derived-stage test above: a quest that DOES carry stages must not be
    flagged — the ledger only records what was actually completed, nothing else."""
    root = tmp_path / "content"
    existing = _seed_world(root)
    focus = _resolve_focus(existing, "region:region_crown")
    empty_pack = ContextPack(query="x", budget_tokens=10)
    payload = {
        "pois": [],
        "npcs": [],
        "quests": [
            {
                "id": "quest_new",
                "title": "有阶段支线",
                "objective": "调查冠门车站的能源中断复发。",
                "giver_npc": "npc_sera",
                "location": "loc_gate",
                "stages": ["勘察现场", "对峙抉择"],
            }
        ],
        "relations": [],
    }
    bundle, _grounding, conversion_report = _expand_bundle_from_payload(
        payload,
        draft_id="test",
        brief=WorldExpandBrief(focus_ref="region:region_crown"),
        focus=focus,
        existing=existing,
        inspiration_pack=empty_pack,
        project_pack=empty_pack,
    )
    assert len(bundle.quests["quest_new"].stages) == 2
    assert conversion_report.is_clean is True


def test_expansion_density_flags_dilution_and_stays_quiet_when_healthy() -> None:
    from owcopilot.content.models import POI, Quest, RegionBrief

    existing = ContentBundle(
        regions={"region_crown": RegionBrief(id="region_crown", name="冠城")},
        pois={"loc_a": POI(id="loc_a", name="甲", region_id="region_crown")},
        quests={"q_main": Quest(id="q_main", title="主线", location="loc_a")},
    )
    # a big batch of side quests, all landing in the same region → both signals fire
    new_bundle = ContentBundle(
        quests={
            f"q_side_{i}": Quest(id=f"q_side_{i}", title=f"支线{i}", location="loc_a")
            for i in range(8)
        }
    )
    busy = expansion_density(existing, new_bundle)
    assert busy.existing_quests == 1 and busy.new_quests == 8
    assert busy.busiest_region == "region_crown" and busy.busiest_region_quests == 9
    assert "稀释" in busy.note and "偏密" in busy.note

    # a modest, balanced expansion → no warning
    calm = expansion_density(
        existing, ContentBundle(quests={"q_one": Quest(id="q_one", title="一条支线")})
    )
    assert calm.note == ""
