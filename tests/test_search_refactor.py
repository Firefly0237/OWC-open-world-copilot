"""WS-C · global search + safe canon-wide rename/refactor (dry-run, atomic, re-audit, undo)."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    apply_rename_action,
    restore_snapshot_action,
    run_project_audit_action,
    search_all_action,
)
from owcopilot.content.models import (
    POI,
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    Quest,
    QuestLogic,
    QuestStage,
    Relation,
)
from owcopilot.content.refactor import apply_rename, plan_rename
from owcopilot.content.store import ContentStore
from owcopilot.search import search_all


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(id="npc_mara", name="玛拉", type=EntityType.NPC, description="斥候"),
            "loc_keep": Entity(id="loc_keep", name="北望要塞", type=EntityType.LOCATION),
        },
        relations=[Relation(source="npc_mara", target="loc_keep", kind="located_in")],
        quests={
            "q_relief": Quest(
                id="q_relief",
                title="盐风驰援",
                giver_npc="npc_mara",
                location="loc_keep",
                objective="护送盐队",
                stages=[QuestStage(id="s1", summary="出发", required_entities=["npc_mara"])],
                logic=QuestLogic(unlocks=[], branches=[]),
            )
        },
        pois={"poi_well": POI(id="poi_well", name="古井", controlling_faction="npc_mara")},
        dialogues={
            "dlg_hi": DialogueRef(id="dlg_hi", text_key="d.hi", speaker_id="npc_mara", text="站住")
        },
    )


# --------------------------------------------------------------- search
def test_search_all_ranks_exact_over_contains() -> None:
    hits = search_all(_bundle(), "玛拉")
    assert hits[0].ref == "entity:npc_mara"
    assert hits[0].score == 100
    assert search_all(_bundle(), "") == []  # empty query


def test_search_matches_id_and_body() -> None:
    refs = {h.ref for h in search_all(_bundle(), "npc_mara")}
    assert "entity:npc_mara" in refs  # id match
    assert any(h.ref == "quest:q_relief" for h in search_all(_bundle(), "盐队"))  # body match


# --------------------------------------------------------------- rename dry-run
def test_plan_rename_finds_all_references_without_mutating() -> None:
    bundle = _bundle()
    plan = plan_rename(bundle, ref="npc_mara", new_id="npc_mira")
    fields = {(e.owner_ref, e.field) for e in plan.edits}
    assert ("relation:0", "source") in fields
    assert ("quest:q_relief", "giver_npc") in fields
    assert ("quest:q_relief", "stages.0.required_entities") in fields
    assert ("poi:poi_well", "controlling_faction") in fields
    assert ("dialogue:dlg_hi", "speaker_id") in fields
    assert plan.conflicts == []
    assert "npc_mara" in bundle.entities  # dry-run mutated nothing


def test_plan_rename_accepts_public_typed_entity_ref() -> None:
    plan = plan_rename(_bundle(), ref="entity:npc_mara", new_id="npc_mira")

    assert plan.target == "entity:npc_mara"
    assert ("quest:q_relief", "giver_npc") in {(e.owner_ref, e.field) for e in plan.edits}


def test_plan_rename_reports_id_conflict() -> None:
    plan = plan_rename(_bundle(), ref="npc_mara", new_id="loc_keep")
    assert plan.conflicts and "已存在" in plan.conflicts[0]


# --------------------------------------------------------------- rename apply
def test_apply_rename_updates_every_reference() -> None:
    out = apply_rename(_bundle(), plan_rename(_bundle(), ref="npc_mara", new_id="npc_mira"))
    assert "npc_mira" in out.entities and "npc_mara" not in out.entities
    assert out.relations[0].source == "npc_mira"
    assert out.quests["q_relief"].giver_npc == "npc_mira"
    assert out.quests["q_relief"].stages[0].required_entities == ["npc_mira"]
    assert out.pois["poi_well"].controlling_faction == "npc_mira"
    assert out.dialogues["dlg_hi"].speaker_id == "npc_mira"


def test_apply_rename_display_name_only() -> None:
    out = apply_rename(_bundle(), plan_rename(_bundle(), ref="npc_mara", new_name="玛拉·改"))
    assert out.entities["npc_mara"].name == "玛拉·改"
    assert out.relations[0].source == "npc_mara"  # id references untouched


# --------------------------------------------------------------- action level (snapshot + undo)
def _write(root) -> None:
    ContentStore(root).save(_bundle())


def test_apply_rename_action_is_atomic_audited_and_undoable(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    before = run_project_audit_action(root)["open_errors"]

    result = apply_rename_action(root, ref="npc_mara", new_id="npc_mira", operator="editor")
    assert result["post_audit_open_errors"] <= before  # no NEW dangling introduced
    reloaded = ContentStore(root).load()
    assert "npc_mira" in reloaded.entities and reloaded.quests["q_relief"].giver_npc == "npc_mira"

    restore_snapshot_action(root, snapshot_id=result["undo_snapshot_id"])
    undone = ContentStore(root).load()
    assert "npc_mara" in undone.entities and "npc_mira" not in undone.entities


def test_apply_rename_action_requires_signature(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    with pytest.raises(ValueError, match="署名"):
        apply_rename_action(root, ref="npc_mara", new_id="npc_x", operator="  ")


def test_search_action_returns_ranked_hits(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    hits = search_all_action(root, query="玛拉")["hits"]
    assert hits and hits[0]["ref"] == "entity:npc_mara"
