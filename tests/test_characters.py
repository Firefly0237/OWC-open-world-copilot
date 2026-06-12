"""Round-18 surface: detailed character-sheet generation + review materialization +
profile maintenance, all offline ($0)."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    decide_review_action,
    run_character_action,
    update_entity_action,
)
from owcopilot.assist.characters import CharacterBrief, _brief_user_message
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore


@pytest.fixture()
def world(tmp_path) -> str:
    root = tmp_path / "world"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "fac_council": Entity(
                    id="fac_council",
                    name="灯塔议会",
                    type=EntityType.FACTION,
                    description="控制航道的权威机构。",
                ),
                "npc_mara": Entity(
                    id="npc_mara", name="玛拉", type=EntityType.NPC, description="斥候。"
                ),
            }
        )
    )
    return str(root)


def test_character_brief_message_omits_empty_dimensions() -> None:
    brief = CharacterBrief(name="白盐", concept="走私船上长大的年轻巡查官")
    message = _brief_user_message(brief)
    assert "名字：白盐" in message and "一句话概念：走私船上长大" in message
    for label in ("年龄/性别：", "戏剧定位：", "说话方式：", "人际关系提示"):
        assert label not in message
    rich = CharacterBrief(
        name="白盐",
        concept="巡查官",
        role_function="同伴",
        relationship_hints=["与 npc_mara 是旧识"],
    )
    rich_message = _brief_user_message(rich)
    assert "戏剧定位：同伴" in rich_message
    assert "- 与 npc_mara 是旧识" in rich_message


def test_character_generation_grounds_relations_and_queues_review(world: str) -> None:
    result = run_character_action(
        world,
        brief={
            "name": "白盐",
            "concept": "走私船上长大的年轻巡查官",
            "faction_id": "fac_council",
            "relationship_hints": ["与 npc_mara 是旧识，互相欠过人情"],
        },
    )
    entity = result["entity"]
    assert entity["id"].startswith("npc_")
    assert entity["review_status"] == "pending_review"
    assert result["profile"]  # sheet sections present
    targets = {(r["target"], r["kind"]) for r in result["relations"]}
    assert ("fac_council", "member_of") in targets  # brief faction wired
    assert any(t == "npc_mara" for t, _k in targets)  # hint landed on a known entity
    assert result["review_item_id"]


def test_character_accept_materializes_entity_and_relations(world: str) -> None:
    result = run_character_action(
        world,
        brief={"name": "白盐", "concept": "巡查官", "faction_id": "fac_council"},
    )
    decided = decide_review_action(
        world, item_id=result["review_item_id"], decision="accepted", operator="lead"
    )
    assert decided["written_ref"] == f"entity:{result['entity']['id']}"
    bundle = ContentStore(world).load()
    assert result["entity"]["id"] in bundle.entities
    assert bundle.entities[result["entity"]["id"]].review_status.value == "approved"
    assert any(
        r.source == result["entity"]["id"] and r.target == "fac_council" for r in bundle.relations
    )


def test_profile_maintenance_via_metadata_updates(world: str) -> None:
    result = run_character_action(world, brief={"name": "白盐", "concept": "巡查官"})
    decide_review_action(
        world, item_id=result["review_item_id"], decision="accepted", operator="lead"
    )
    entity_id = result["entity"]["id"]
    updated = update_entity_action(
        world,
        entity_id=entity_id,
        metadata_updates={"profile": {"voice": "短句，带海腔。"}},
    )
    assert updated["entity"]["metadata"]["profile"]["voice"] == "短句，带海腔。"
    # deleting a key by None
    cleared = update_entity_action(
        world, entity_id=entity_id, metadata_updates={"suggested_relations": None}
    )
    assert "suggested_relations" not in cleared["entity"]["metadata"]


def test_long_idea_within_limit_is_accepted_offline(tmp_path) -> None:
    """长输入承接：3500 字的核心想法在上限内应正常工作（更长的成稿走文稿提炼）。"""
    from owcopilot.app.actions import run_world_seed_action

    root = tmp_path / "w"
    ContentStore(root).save(ContentBundle())
    long_idea = ("雾海之上漂着十二座灯塔岛。" * 250)[:3500]
    result = run_world_seed_action(
        str(root),
        brief={"idea": long_idea, "npc_count": 0, "quest_count": 0},
    )
    assert result["bundle"]["entities"]
