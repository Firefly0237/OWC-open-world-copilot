"""Feedback-driven revision: a reviewer asks for changes and the draft is revised in place."""

from __future__ import annotations

from owcopilot.app.actions import (
    add_reference_action,
    revise_draft_action,
    run_character_action,
    run_dialogue_tree_action,
    run_draft_action,
    run_world_expand_action,
    run_world_seed_action,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, RegionBrief
from owcopilot.content.store import ContentStore


def _world(tmp_path):
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_lin": Entity(
                    id="npc_lin", name="林九", type=EntityType.NPC, description="古物商"
                ),
                "npc_shen": Entity(
                    id="npc_shen", name="沈砚", type=EntityType.NPC, description="藏书人"
                ),
            }
        )
    )
    return root


def test_revise_quest_draft_updates_item_in_place(tmp_path) -> None:
    root = _world(tmp_path)
    gen = run_draft_action(root, brief="让林九护送一卷古书给沈砚")
    item_id = gen["review_item_id"]

    revised = revise_draft_action(
        root, item_id=item_id, feedback="把任务目标写得更具体，并加入失败后果"
    )

    assert revised["item"]["status"] == "pending_review"  # still in review, not auto-landed
    assert revised["item"]["id"] == item_id  # same item, revised in place
    assert revised["revised_payload"]["metadata"].get("revised_from_feedback") is True


def test_revise_character_profile(tmp_path) -> None:
    root = _world(tmp_path)
    gen = run_character_action(
        root, brief={"name": "苏璃", "concept": "隐居山林的草药师"}, refine_rounds=0
    )
    item_id = gen["review_item_id"]

    revised = revise_draft_action(root, item_id=item_id, feedback="让她的动机更具体")

    assert revised["item"]["status"] == "pending_review"
    assert revised["revised_payload"]["entity"]["metadata"].get("revised_from_feedback") is True


def test_revise_dialogue_tree(tmp_path) -> None:
    root = _world(tmp_path)
    gen = run_dialogue_tree_action(
        root, participant_ids=["npc_lin", "npc_shen"], brief="林九把古书卖给沈砚", refine_rounds=0
    )
    item_id = gen["review_item_id"]

    revised = revise_draft_action(root, item_id=item_id, feedback="让两人的对话更有张力")

    assert revised["item"]["status"] == "pending_review"
    assert revised["revised_payload"]["metadata"].get("revised_from_feedback") == "true"


def test_revise_world_seed_reruns_only_the_targeted_stage(tmp_path) -> None:
    # world_seed revision re-runs only the stage the feedback targets (here: factions), grounded
    # in the rest of the world — keeping the staged quality rather than a single-pass rewrite.
    root = _world(tmp_path)
    add_reference_action(root, title="灵感", text="一个关于古籍与背叛的故事。")
    gen = run_world_seed_action(
        root, brief={"idea": "古籍与背叛", "faction_count": 2, "npc_count": 2, "quest_count": 1}
    )
    item_id = gen["review_item_id"]

    revised = revise_draft_action(root, item_id=item_id, feedback="让两个阵营之间更对立")

    assert revised["item"]["status"] == "pending_review"
    assert revised["revised_payload"]["revised_stage"] == "factions"  # classified from "阵营"
    assert revised["revised_payload"]["bundle"]["entities"]  # still a full, assembled world


def test_revise_world_expand_regrows_the_batch_in_place(tmp_path) -> None:
    # Regression: world_expand drafts carry item_type "world_seed" but no seed brief, so revising
    # one used to crash validating an empty WorldSeedBrief (idea required). It must instead re-grow
    # expansion at the same focus, steered by the note, and stay in review.
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            regions={
                "region_crown": RegionBrief(id="region_crown", name="冠城环带", themes=["工业中心"])
            },
            entities={
                "fac_charter": Entity(
                    id="fac_charter",
                    name="炉心公约",
                    type=EntityType.FACTION,
                    description="控制核心能源的工程共同体。",
                ),
            },
        )
    )
    gen = run_world_expand_action(
        root,
        brief={
            "focus_ref": "region:region_crown",
            "poi_count": 1,
            "npc_count": 1,
            "quest_count": 1,
        },
    )
    item_id = gen["review_item_id"]

    revised = revise_draft_action(root, item_id=item_id, feedback="让这一批扩写的冲突更尖锐")

    assert revised["item"]["status"] == "pending_review"  # still in review, not auto-landed
    assert revised["item"]["id"] == item_id  # same item, re-grown in place
    assert revised["revised_payload"]["kind"] == "world_expand"  # routed to the expand path
    assert revised["revised_payload"]["bundle"]["quests"]  # a full re-grown batch
    assert revised["revised_payload"]["grounding"]["dangling_refs"] == []  # still grounded on canon
