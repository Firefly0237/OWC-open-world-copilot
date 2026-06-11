"""Workbench action-layer tests: the full UI loop without Streamlit, offline, $0."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    decide_review_action,
    list_patches_action,
    list_project_issues_action,
    list_review_items_action,
    run_apply_action,
    run_ask_action,
    run_barks_action,
    run_draft_action,
    run_impact_action,
    run_project_audit_action,
    run_rollback_action,
    run_suggest_action,
)
from owcopilot.app.view_models import build_project_overview
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.content.store import ContentStore


@pytest.fixture()
def root(tmp_path) -> str:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="Mara", type=EntityType.NPC, description="Scout."
                ),
                "loc_fort": Entity(
                    id="loc_fort",
                    name="Border Fort",
                    type=EntityType.LOCATION,
                    description="A fort.",
                ),
            },
            relations=[Relation(source="npc_mara", target="loc_fort", kind="located_in")],
            quests={
                "quest_patrol": Quest(
                    id="quest_patrol",
                    title="Patrol the Border",
                    giver_npc="npc_ghost",  # seeded error for the forge loop
                    location="loc_fort",
                    objective="Walk the border line.",
                    localization_keys=["quest.quest_patrol.objective"],
                )
            },
        )
    )
    return str(content_root)


def test_overview_includes_provenance(root: str) -> None:
    overview = build_project_overview(root)
    assert overview["provenance"]["total"] >= 3
    assert "by_origin" in overview["provenance"]


def test_audit_action_returns_markdown_report(root: str) -> None:
    result = run_project_audit_action(root)
    assert result["open_errors"] == 1
    assert "UNKNOWN_ENTITY_REF" in result["markdown_report"]


def test_forge_loop_suggest_apply_rollback(root: str) -> None:
    run_project_audit_action(root)
    issues = list_project_issues_action(root, rule_code="UNKNOWN_ENTITY_REF")
    issue_id = issues["issues"][0]["id"]

    suggested = run_suggest_action(root, issue_id=issue_id)
    assert suggested["candidates"]
    patch_id = suggested["candidates"][0]["patch_id"]
    assert list_patches_action(root, status="proposed")["count"] >= 1

    applied = run_apply_action(root, patch_id=patch_id, operator="lead")
    assert applied["applied"] is True
    assert applied["post_audit_open_errors"] == 0
    assert list_patches_action(root, status="applied")["count"] == 1

    rolled = run_rollback_action(root, patch_id=patch_id, operator="lead")
    assert rolled["rolled_back"] is True
    assert rolled["post_audit_open_errors"] == 1


def test_ask_action_grounded_and_refusal(root: str) -> None:
    grounded = run_ask_action(root, query="Who is Mara?")
    assert not grounded["answer"]["refused"]
    assert grounded["answer"]["citations"]
    refused = run_ask_action(root, query="Who is the dragon king of the moon?")
    assert refused["answer"]["refused"]


def test_impact_action(root: str) -> None:
    result = run_impact_action(
        root, changes=[{"change_type": "entity_delete", "target_ref": "entity:loc_fort"}]
    )
    assert result["total"] >= 1


def test_create_and_review_loop(root: str) -> None:
    draft = run_draft_action(root, brief="Escort the salt caravan to the fort")
    assert draft["quest"]["review_status"] == "pending_review"
    barks = run_barks_action(
        root, speaker_ids=["npc_mara"], topic="intruder spotted", variants_per_speaker=2
    )
    assert len(barks["accepted"]) == 2

    queue = list_review_items_action(root)
    assert queue["count"] == 3  # one draft + two barks

    decided = decide_review_action(
        root, item_id=draft["review_item_id"], decision="accepted", operator="lead"
    )
    assert decided["written_ref"] and decided["written_ref"].startswith("quest:")
    assert list_review_items_action(root)["count"] == 2


def test_barks_action_rejects_unknown_speaker(root: str) -> None:
    with pytest.raises(ValueError, match="unknown speaker"):
        run_barks_action(root, speaker_ids=["npc_nobody"], topic="hello")
