from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
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
