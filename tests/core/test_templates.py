"""WS-G · template/archetype library: deterministic instantiation routed through review."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import instantiate_template_action, list_templates_action
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.templates import instantiate, list_templates


def test_library_lists_quest_and_faction_templates() -> None:
    ids = {t.id for t in list_templates()}
    assert {"quest_escort", "quest_investigate", "quest_subdue", "faction"} <= ids


def test_instantiate_quest_is_deterministic_and_structured() -> None:
    bundle = instantiate(
        "quest_escort",
        {
            "title": "盐队驰援",
            "giver": "npc_mara",
            "from": "盐港",
            "to": "北望",
            "reward": "150 金",
        },
        existing_ids=set(),
    )
    quest = next(iter(bundle.quests.values()))
    assert quest.title == "盐队驰援" and quest.giver_npc == "npc_mara"
    assert len(quest.stages) == 3 and "北望" in quest.objective
    assert quest.rewards and quest.rewards[0].value == "150 金"


def test_missing_required_param_is_rejected() -> None:
    with pytest.raises(ValueError, match="缺少必填参数"):
        instantiate("quest_escort", {"giver": "x"}, existing_ids=set())  # no title/from/to


def test_unique_id_avoids_collision() -> None:
    bundle = instantiate("faction", {"name": "铁卫"}, existing_ids={"fac_铁卫"})
    assert "fac_铁卫_2" in bundle.entities


def test_instantiate_action_routes_to_review_queue(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={"npc_mara": Entity(id="npc_mara", name="玛拉", type=EntityType.NPC)}
        )
    )
    result = instantiate_template_action(
        root,
        template_id="quest_investigate",
        params={"title": "枯井疑云", "giver": "npc_mara", "location": "古井", "subject": "失踪"},
    )
    new_id = result["created"]["quests"][0]  # a quest was created
    assert result["review_item_id"]
    # HITL: it is queued for review, NOT yet written to canon
    assert new_id not in ContentStore(root).load().quests


def test_list_templates_action() -> None:
    out = list_templates_action()
    assert any(t["kind"] == "faction" for t in out["templates"])
