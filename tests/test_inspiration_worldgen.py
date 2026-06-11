from __future__ import annotations

from owcopilot.app.actions import (
    add_reference_action,
    decide_review_action,
    list_references_action,
    list_review_items_action,
    run_ask_action,
    run_world_seed_action,
    search_references_action,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore


def test_reference_library_is_separate_from_project_lore_rag(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara",
                    name="Mara",
                    type=EntityType.NPC,
                    description="Border scout loyal to the guard.",
                )
            }
        )
    )

    added = add_reference_action(
        root,
        title="Mara variant note",
        text="Mara is a rival queen in the inspiration sample, not project lore.",
        allowed_uses=["inspiration", "structure"],
    )
    assert added["indexed_count"] >= 1
    assert list_references_action(root)["count"] == 1

    reference_hits = search_references_action(root, query="rival queen Mara")
    assert reference_hits["hits"]
    assert all(hit["ref"].startswith("reference_chunk:") for hit in reference_hits["hits"])

    answer = run_ask_action(root, query="Who is Mara?")
    assert not answer["answer"]["refused"]
    assert answer["answer"]["citations"]
    assert all(
        not citation["ref"].startswith("reference_chunk:")
        for citation in answer["answer"]["citations"]
    )
    assert "rival queen" not in answer["answer"]["answer"]


def test_world_seed_enters_review_queue_then_accept_writes_bundle(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    add_reference_action(
        root,
        title="Three-way frontier conflict",
        text=(
            "A useful structure: three factions compete for a failing resource. "
            "One controls infrastructure, one protects old rites, one sells information."
        ),
        allowed_uses=["inspiration", "structure"],
    )

    result = run_world_seed_action(
        root,
        brief={
            "idea": "A steam-powered border world where forests remember old wars.",
            "world_styles": ["蒸汽朋克", "魔幻"],
            "reference_query": "three factions failing resource",
            "reference_mode": "参考剧情结构",
            "faction_count": 3,
            "region_count": 2,
            "npc_count": 4,
            "quest_count": 3,
            "term_count": 3,
        },
    )

    assert result["counts"]["quests"] == 3
    assert result["counts"]["regions"] == 2
    assert result["inspiration_context_refs"]
    assert result["reference_report"]
    assert result["bundle"]["quests"]
    assert all(
        quest["review_status"] == "pending_review" for quest in result["bundle"]["quests"].values()
    )
    queue = list_review_items_action(root)
    assert queue["count"] == 1
    assert queue["items"][0]["item_type"] == "world_seed"

    accepted = decide_review_action(
        root,
        item_id=result["review_item_id"],
        decision="accepted",
        operator="lead",
    )
    assert accepted["written_ref"].startswith("world_seed:")
    saved = ContentStore(root).load()
    assert len(saved.quests) == 3
    assert len(saved.regions) == 2
    assert all(quest.review_status == "approved" for quest in saved.quests.values())
    assert all(entity.origin == "ai_draft" for entity in saved.entities.values())
