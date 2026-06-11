from __future__ import annotations

from owcopilot.content.models import (
    POI,
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    Quest,
    RegionBrief,
    Relation,
)
from owcopilot.graph.index import build_content_graph


def test_content_graph_indexes_entities_content_and_derived_refs() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
            "location_northwatch": Entity(
                id="location_northwatch",
                name="Northwatch",
                type=EntityType.LOCATION,
            ),
            "faction_iron_guard": Entity(
                id="faction_iron_guard",
                name="Iron Guard",
                type=EntityType.FACTION,
            ),
        },
        regions={"region_north": RegionBrief(id="region_north", name="North")},
        pois={
            "poi_gate": POI(
                id="poi_gate",
                name="North Gate",
                region_id="region_north",
                controlling_faction="faction_iron_guard",
            )
        },
        dialogues={
            "dlg_intro": DialogueRef(
                id="dlg_intro",
                text_key="dlg_intro",
                speaker_id="npc_aldric",
                quest_id="quest_missing_caravan",
            )
        },
        quests={
            "quest_missing_caravan": Quest(
                id="quest_missing_caravan",
                title="The Missing Caravan",
                giver_npc="npc_aldric",
                location="location_northwatch",
                dialogue_refs=["dlg_intro"],
                localization_keys=["quest.missing_caravan.title"],
            )
        },
        relations=[
            Relation(
                source="npc_aldric",
                target="faction_iron_guard",
                kind="member_of",
            )
        ],
    )

    graph = build_content_graph(bundle)

    assert graph.has_node("entity:npc_aldric")
    assert graph.has_node("quest:quest_missing_caravan")
    assert graph.has_node("localization:quest.missing_caravan.title")
    assert any(edge.kind == "giver_npc" for edge in graph.edge_refs(edge_type="reference"))
    assert any(edge.kind == "member_of" for edge in graph.edge_refs(edge_type="relation"))


def test_content_graph_uses_stable_node_refs() -> None:
    bundle = ContentBundle(
        entities={"npc_mara": Entity(id="npc_mara", name="Mara", type=EntityType.NPC)}
    )

    graph = build_content_graph(bundle)

    assert graph.node_refs() == ["entity:npc_mara"]
