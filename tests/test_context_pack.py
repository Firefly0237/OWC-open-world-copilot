from __future__ import annotations

import pytest

from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    Relation,
)
from owcopilot.graph.index import build_content_graph
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.retrieval.graph_expand import GraphExpansionRetriever
from owcopilot.retrieval.vector import VectorRetriever
from owcopilot.storage import SQLiteStore


def test_context_pack_builder_combines_retrievers_and_budget() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Caravan Quest", giver_npc="npc_aldric")},
        )
        store.replace_content_index(bundle)
        graph = build_content_graph(bundle)
        builder = ContextPackBuilder(
            bm25=BM25Retriever(store),
            vector=VectorRetriever(store),
            graph=GraphExpansionRetriever(graph),
        )

        pack = builder.build("Aldric caravan", budget_tokens=20)

        assert pack.query == "Aldric caravan"
        assert "entity:npc_aldric" in pack.refs
        assert pack.budget_tokens == 20
    finally:
        store.close()


def _flooded_bundle() -> ContentBundle:
    """A location with many graph neighbours plus one quest that fully matches a query."""
    entities = {
        "loc_mistspine": Entity(
            id="loc_mistspine",
            name="Mistspine Pass",
            type=EntityType.LOCATION,
            description="A high mountain pass",
        )
    }
    relations = []
    for index in range(1, 7):
        npc_id = f"npc_sentinel_{index}"
        entities[npc_id] = Entity(
            id=npc_id,
            name=f"Sentinel {index}",
            type=EntityType.NPC,
            description="A watch member",
        )
        relations.append(
            Relation(source=f"entity:{npc_id}", target="entity:loc_mistspine", kind="stationed_at")
        )
    quests = {
        "q_patrol": Quest(
            id="q_patrol",
            title="Mistspine Pass Beacon Patrol",
            objective="Patrol the beacon towers of the pass",
            giver_npc="npc_sentinel_1",
        )
    }
    return ContentBundle(entities=entities, relations=relations, quests=quests)


def test_rerank_lifts_on_topic_quest_above_graph_flood() -> None:
    store = SQLiteStore()
    try:
        bundle = _flooded_bundle()
        store.replace_content_index(bundle)
        graph = build_content_graph(bundle)

        def _make(rerank: bool) -> ContextPackBuilder:
            return ContextPackBuilder(
                bm25=BM25Retriever(store),
                vector=VectorRetriever(store),
                graph=GraphExpansionRetriever(graph),
                rerank=rerank,
            )

        query = "Mistspine Pass beacon patrol"
        fused = _make(rerank=False).build(query, budget_tokens=10_000)
        reranked = _make(rerank=True).build(query, budget_tokens=10_000)

        # Reranking surfaces the document that covers the whole query, never below where
        # plain fusion left it, and marks that the precision stage ran.
        assert reranked.refs[0] == "quest:q_patrol"
        assert reranked.refs.index("quest:q_patrol") <= fused.refs.index("quest:q_patrol")
        assert reranked.hits[0].source == "reranked"
        assert fused.hits[0].source == "rrf"
    finally:
        store.close()


def test_build_expanded_widens_recall_with_variants() -> None:
    # A variant phrasing retrieves a document the original query alone would not, so query
    # expansion widens recall; the original-only build does not see it.
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_alpha": Entity(
                    id="npc_alpha", name="Alpha", type=EntityType.NPC, description="alpha unique"
                ),
                "npc_beta": Entity(
                    id="npc_beta", name="Beta", type=EntityType.NPC, description="beta distinct"
                ),
            }
        )
        store.replace_content_index(bundle)
        builder = ContextPackBuilder(bm25=BM25Retriever(store), vector=VectorRetriever(store))

        original = builder.build("alpha", budget_tokens=2000).refs
        expanded = builder.build_expanded("alpha", ["beta"], budget_tokens=2000).refs

        assert "entity:npc_beta" not in original  # the original phrasing misses it
        assert "entity:npc_beta" in expanded  # the variant widens recall to include it
    finally:
        store.close()


def test_relation_completion_surfaces_relations_of_recalled_entities() -> None:
    # A relationship question that retrieves the entities must also retrieve their relations,
    # so the model can answer the structure instead of falsely refusing for lack of them.
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "fac_iron": Entity(id="fac_iron", name="Iron Legion", type=EntityType.FACTION),
                "fac_sand": Entity(id="fac_sand", name="Sand Walkers", type=EntityType.FACTION),
            },
            relations=[
                Relation(source="fac_iron", target="fac_sand", kind="enemy_of"),
            ],
        )
        store.replace_content_index(bundle)
        graph = build_content_graph(bundle)
        builder = ContextPackBuilder(
            bm25=BM25Retriever(store),
            vector=VectorRetriever(store),
            graph=GraphExpansionRetriever(graph),
        )

        # The query names the factions but not the relation; recall finds the factions, and
        # relation completion pulls their enemy_of relation in.
        refs = builder.build("Iron Legion Sand Walkers", budget_tokens=2000).refs

        assert any(r.startswith("relation:") and "enemy_of" in r for r in refs)
    finally:
        store.close()


@pytest.mark.parametrize(
    "query",
    [
        "",
        "     ",
        "!!! ??? ...",
        "🙂🔥🌊",
        'NEAR( AND OR "unterminated',
        "iron* OR (col:val)",
        "'; DROP TABLE content_index;--",
        "iron " * 2000,
        "铁卫军团 Iron Watch 🛡 NEAR()",
    ],
)
def test_build_never_raises_on_adversarial_queries(query: str) -> None:
    store = SQLiteStore()
    try:
        bundle = _flooded_bundle()
        store.replace_content_index(bundle)
        graph = build_content_graph(bundle)
        builder = ContextPackBuilder(
            bm25=BM25Retriever(store),
            vector=VectorRetriever(store),
            graph=GraphExpansionRetriever(graph),
        )

        # Untrusted, possibly hostile query text must degrade gracefully (FTS operators,
        # SQL punctuation, emoji, oversized input) -- never crash the retrieval chain.
        pack = builder.build(query, budget_tokens=200)

        assert pack.query == query
        assert isinstance(pack.hits, list)
    finally:
        store.close()
