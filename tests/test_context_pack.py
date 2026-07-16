from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    Relation,
)
from owcopilot.graph.index import build_content_graph
from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.retrieval.graph_expand import GraphExpansionRetriever
from owcopilot.retrieval.models import RetrievalHit
from owcopilot.retrieval.neural_rerank import NeuralReranker
from owcopilot.retrieval.rerank import rerank_hits
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


class _CountingSemanticEmbedder(HashingEmbedder):
    """A deterministic embedder that advertises a semantic id (``st:*`` → ``is_semantic`` True, so
    the rerank similarity leg runs) and counts every text it encodes through either entry point."""

    def __init__(self) -> None:
        super().__init__(dim=64)
        self.model_id = "st:counting-64"
        self.encoded = 0

    def embed(self, text: str) -> list[float]:
        self.encoded += 1
        return super().embed(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.encoded += len(texts)
        return super().embed_many(texts)


def test_build_encodes_the_query_exactly_once_on_the_semantic_path() -> None:
    # On the semantic path both the dense recall leg and the rerank similarity leg use the query
    # embedding. Before the QueryBundle single-encode they each embedded the query (2×); now the
    # query is encoded once and reused. bm25/graph/relation-completion never embed, so build()'s
    # only encode is the query itself.
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "fac_iron": Entity(id="fac_iron", name="Iron Legion", type=EntityType.FACTION),
                "fac_sand": Entity(id="fac_sand", name="Sand Walkers", type=EntityType.FACTION),
            },
            relations=[Relation(source="fac_iron", target="fac_sand", kind="enemy_of")],
        )
        store.replace_content_index(bundle)
        embedder = _CountingSemanticEmbedder()
        vector = VectorRetriever(store, embedder=embedder)
        assert vector.is_semantic  # the similarities rerank leg will run
        builder = ContextPackBuilder(bm25=BM25Retriever(store), vector=vector)

        embedder.encoded = 0  # ignore the corpus embed done while indexing
        builder.build("Iron Legion enemies", budget_tokens=2000)

        assert embedder.encoded == 1  # was 2 before single-encode (search + similarities)
    finally:
        store.close()


def test_build_many_is_bit_identical_to_per_query_build() -> None:
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
        queries = ["Mistspine Pass beacon patrol", "watch member sentinel", "high mountain pass"]

        batched = builder.build_many(queries, budget_tokens=2000, limit=10)
        per_query = [builder.build(q, budget_tokens=2000, limit=10) for q in queries]

        # Batching only collapses the query encodes; every pack must match its per-query build
        # field-for-field (query text, hit refs/scores/order).
        assert [p.model_dump() for p in batched] == [p.model_dump() for p in per_query]
    finally:
        store.close()


def test_build_many_without_vector_leg_matches_per_query_build() -> None:
    # No vector leg → nothing to batch; build_many must still equal the per-query builds.
    store = SQLiteStore()
    try:
        store.replace_content_index(_flooded_bundle())
        builder = ContextPackBuilder(bm25=BM25Retriever(store))
        queries = ["beacon patrol", "sentinel watch"]

        batched = builder.build_many(queries, budget_tokens=2000)
        per_query = [builder.build(q, budget_tokens=2000) for q in queries]

        assert [p.model_dump() for p in batched] == [p.model_dump() for p in per_query]
    finally:
        store.close()


def _fused_hit(ref: str, *, score: float) -> RetrievalHit:
    return RetrievalHit(
        ref=ref,
        object_type=ref.split(":", 1)[0],
        title=ref,
        body="",
        score=score,
        source="rrf",
    )


def _neural_reranker_returning(scores: list[float]) -> NeuralReranker:
    """A real NeuralReranker whose cross-encoder is mocked (no ML load), reusing the production
    rerank() -- the test only supplies the joint scores the model would emit."""
    reranker = NeuralReranker("mock-model")
    model = MagicMock()
    model.predict.return_value = scores
    reranker._model = model
    return reranker


def test_neural_rerank_bounds_window_reorders_and_never_drops() -> None:
    """When a neural reranker is wired in, only the top-K candidate window is cross-encoded; the
    tail keeps its fused order and nothing is dropped (budget trim is the only stage that drops)."""
    store = SQLiteStore()
    try:
        builder = ContextPackBuilder(
            bm25=BM25Retriever(store),
            # Window of 2: only the first two fused hits reach the cross-encoder.
            neural_reranker=_neural_reranker_returning([0.1, 0.9]),
            rerank_candidate_window=2,
        )
        fused = [
            _fused_hit("entity:a", score=0.9),
            _fused_hit("entity:b", score=0.8),
            _fused_hit("entity:c", score=0.7),
            _fused_hit("entity:d", score=0.6),
        ]

        ranked = builder._rerank("anchor query", fused)

        # Window [a, b] scored [0.1, 0.9] → reordered to [b, a] and marked neural.
        assert [h.ref for h in ranked[:2]] == ["entity:b", "entity:a"]
        assert all(h.source == "reranked_neural" for h in ranked[:2])
        # Tail [c, d] keeps fused order and its fused source untouched.
        assert [h.ref for h in ranked[2:]] == ["entity:c", "entity:d"]
        assert all(h.source == "rrf" for h in ranked[2:])
        # Reorder never drops: same set of refs, same count.
        assert {h.ref for h in ranked} == {h.ref for h in fused}
        assert len(ranked) == len(fused)
    finally:
        store.close()


def test_default_rerank_is_byte_identical_to_lexical_rescorer() -> None:
    """Default construction (no neural reranker, both gates off) leaves the precision stage exactly
    the lexical re-scorer -- proving the change keeps the default path unchanged."""
    store = SQLiteStore()
    try:
        # vector=None → the semantic leg is off, matching the historical default on the stub.
        builder = ContextPackBuilder(bm25=BM25Retriever(store))
        assert builder.neural_reranker is None

        fused = [
            _fused_hit("quest:q_patrol", score=0.4),
            _fused_hit("entity:npc_a", score=0.9),
            _fused_hit("entity:npc_b", score=0.5),
        ]

        got = builder._rerank("patrol beacon", fused)
        expected = rerank_hits("patrol beacon", fused, semantic_scores=None)

        # Element-wise equality of the pydantic hits: same order, score, and source.
        assert got == expected
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
