from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.graph.query import edges_by_kind, edges_by_type, neighbors


def test_neighbors_returns_k_hop_refs() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
            "faction_iron_guard": Entity(
                id="faction_iron_guard",
                name="Iron Guard",
                type=EntityType.FACTION,
            ),
        },
        quests={
            "quest_missing_caravan": Quest(
                id="quest_missing_caravan",
                title="Missing Caravan",
                giver_npc="npc_aldric",
            )
        },
        relations=[
            Relation(source="npc_aldric", target="faction_iron_guard", kind="member_of")
        ],
    )
    graph = build_content_graph(bundle)

    refs = neighbors(graph, "quest:quest_missing_caravan", radius=2)

    assert refs == [
        "entity:faction_iron_guard",
        "entity:npc_aldric",
        "quest:quest_missing_caravan",
    ]


def test_query_filters_edges_by_kind_and_type() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
            "faction_iron_guard": Entity(
                id="faction_iron_guard",
                name="Iron Guard",
                type=EntityType.FACTION,
            ),
        },
        quests={
            "quest_missing_caravan": Quest(
                id="quest_missing_caravan",
                title="Missing Caravan",
                giver_npc="npc_aldric",
            )
        },
        relations=[
            Relation(source="npc_aldric", target="faction_iron_guard", kind="member_of")
        ],
    )
    graph = build_content_graph(bundle)

    assert [edge.kind for edge in edges_by_kind(graph, "member_of")] == ["member_of"]
    assert {edge.kind for edge in edges_by_type(graph, "reference")} == {"giver_npc"}
    assert neighbors(
        graph,
        "quest:quest_missing_caravan",
        radius=2,
        kinds={"giver_npc"},
    ) == ["entity:npc_aldric", "quest:quest_missing_caravan"]
