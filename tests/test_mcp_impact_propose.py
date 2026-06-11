"""MCP tool handler tests for impact_of and propose_fix (offline, $0)."""

from __future__ import annotations

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.content.store import ContentStore
from owcopilot.mcp_server import audit_project, impact_of, list_issues, propose_fix


@pytest.fixture()
def content_root(tmp_path) -> str:
    root = tmp_path / "content"
    ContentStore(root).save(
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
                    giver_npc="npc_ghost",
                    location="loc_fort",
                    objective="Walk the border line.",
                    localization_keys=["quest.quest_patrol.objective"],
                )
            },
        )
    )
    return str(root)


def test_impact_of_returns_grouped_targets(content_root: str) -> None:
    result = impact_of(
        content_root=content_root,
        changes=[{"change_type": "entity_delete", "target_ref": "entity:loc_fort"}],
    )
    assert result["total"] >= 1
    assert result["cost_budget"]["used_usd"] == 0.0
    refs = {item["target_ref"] for item in result["must_change"]}
    assert refs


def test_propose_fix_persists_proposed_patches(content_root: str) -> None:
    audit_project(content_root=content_root, persist=True)
    issues = list_issues(content_root=content_root, rule_code="UNKNOWN_ENTITY_REF")["issues"]
    assert issues
    result = propose_fix(content_root=content_root, issue_id=issues[0]["id"])
    assert result["candidates"]
    assert result["candidates"][0]["target_resolved"] is True
    assert "owcopilot apply" in result["apply_hint"]

    # the proposal is persisted in the runtime DB for the CLI apply step
    from owcopilot.storage import SQLiteStore

    store = SQLiteStore(f"{content_root}/.owcopilot/runtime.sqlite")
    try:
        assert store.list_patches(status="proposed")
    finally:
        store.close()


def test_propose_fix_unknown_issue_raises(content_root: str) -> None:
    with pytest.raises(FileNotFoundError):
        propose_fix(content_root=content_root, issue_id="nope")
