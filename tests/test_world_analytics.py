"""WS-H · deterministic world analytics: counts, density, under-developed factions, content gaps."""

from __future__ import annotations

from owcopilot.app.analytics import build_world_analytics
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, QuestStage, Relation


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_a": Entity(id="npc_a", name="甲", type=EntityType.NPC, description="斥候"),
            "npc_b": Entity(id="npc_b", name="乙", type=EntityType.NPC, description=""),  # no desc
            "fac_x": Entity(id="fac_x", name="铁卫", type=EntityType.FACTION, description="军团"),
            "fac_y": Entity(id="fac_y", name="孤会", type=EntityType.FACTION, description="小会"),
        },
        relations=[Relation(source="npc_a", target="fac_x", kind="member_of")],
        quests={
            "q1": Quest(
                id="q1", title="A", objective="护送", stages=[QuestStage(id="s1", summary="x")]
            ),
            "q2": Quest(id="q2", title="B", objective=""),  # no objective, no stages
        },
    )


def test_counts_density_and_types() -> None:
    a = build_world_analytics(_bundle())
    assert a["counts"]["entities"] == 4 and a["counts"]["quests"] == 2
    assert a["entities_by_type"]["npc"] == 2 and a["entities_by_type"]["faction"] == 2
    assert a["relation_density"] == round(1 / 4, 2)


def test_underdeveloped_factions_and_gaps() -> None:
    a = build_world_analytics(_bundle())
    # fac_x has 1 member, fac_y has 0 -> fac_y is under-developed
    under = {f["id"] for f in a["underdeveloped_factions"]}
    assert under == {"fac_y"}
    members = {f["id"]: f["members"] for f in a["factions"]}
    assert members == {"fac_x": 1, "fac_y": 0}
    assert a["gaps"]["entities_without_description"] == ["npc_b"]
    assert a["gaps"]["quests_without_objective"] == ["q2"]
    assert a["gaps"]["quests_without_stages"] == ["q2"]


def test_empty_world_is_safe() -> None:
    a = build_world_analytics(ContentBundle())
    assert a["counts"]["entities"] == 0 and a["relation_density"] == 0.0
    assert a["underdeveloped_factions"] == []
