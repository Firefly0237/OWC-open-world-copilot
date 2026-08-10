from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.retrieval.graph_expand import GraphExpansionRetriever


def test_graph_expansion_retriever_returns_seed_and_neighbors() -> None:
    graph = build_content_graph(
        ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "faction_guard": Entity(
                    id="faction_guard",
                    name="Iron Guard",
                    type=EntityType.FACTION,
                ),
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_aldric")},
            relations=[Relation(source="npc_aldric", target="faction_guard", kind="member_of")],
        )
    )

    hits = GraphExpansionRetriever(graph).search("Aldric", radius=1)

    assert {hit.ref for hit in hits} == {
        "entity:npc_aldric",
        "entity:faction_guard",
        "quest:q1",
    }


def test_graph_expansion_retriever_matches_cjk_alias_substrings() -> None:
    graph = build_content_graph(
        ContentBundle(
            entities={
                "npc_shen": Entity(
                    id="npc_shen",
                    name="沈青鹤",
                    type=EntityType.NPC,
                    aliases=["青鹤"],
                )
            }
        )
    )

    hits = GraphExpansionRetriever(graph).search("青鹤是谁?")

    assert [hit.ref for hit in hits] == ["entity:npc_shen"]


def test_graph_expansion_retriever_exposes_relation_conflicts() -> None:
    graph = build_content_graph(
        ContentBundle(
            entities={
                "fac_a": Entity(id="fac_a", name="漕帮", type=EntityType.FACTION),
                "fac_b": Entity(id="fac_b", name="沧浪阁", type=EntityType.FACTION),
            },
            relations=[
                Relation(source="fac_a", target="fac_b", kind="allied_with"),
                Relation(source="fac_a", target="fac_b", kind="enemy_of"),
            ],
        )
    )

    hits = GraphExpansionRetriever(graph).search("漕帮和沧浪阁是什么关系?")
    body = " ".join(hit.body for hit in hits)

    assert "relation_conflict entity:fac_a entity:fac_b both allied_with and enemy_of" in body
    assert all(hit.source == "graph" for hit in hits)
