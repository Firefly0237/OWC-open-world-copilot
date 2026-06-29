from __future__ import annotations

from pathlib import Path

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval.embedding import SemanticEmbedder
from owcopilot.retrieval.vector import VectorRetriever
from owcopilot.storage import SQLiteStore


def _aldric_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Caravan master",
            )
        },
        quests={
            "quest_siege": Quest(
                id="quest_siege", title="Siege", objective="Defend the northern wall"
            )
        },
    )


def test_vector_retriever_searches_index_with_hashing_embedder() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_aldric_bundle())

        hits = VectorRetriever(store).search("caravan routes")

        assert hits[0].ref == "entity:npc_aldric"
        assert hits[0].source == "vector"
    finally:
        store.close()


class _CountingEmbedder(HashingEmbedder):
    """A deterministic embedder that records how many texts it actually embedded."""

    def __init__(self) -> None:
        super().__init__(dim=64)
        self.model_id = "counting-64"
        self.embedded = 0

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return super().embed_many(texts)


def test_vector_retriever_persists_and_reuses_embeddings() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_aldric_bundle())
        embedder = _CountingEmbedder()

        VectorRetriever(store, embedder=embedder)
        first = embedder.embedded
        assert first == 2  # both rows embedded once

        # Re-opening over unchanged canon must read vectors back, embedding nothing more.
        VectorRetriever(store, embedder=embedder)
        assert embedder.embedded == first
    finally:
        store.close()


def test_vector_retriever_reembeds_only_changed_rows() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_aldric_bundle())
        embedder = _CountingEmbedder()
        VectorRetriever(store, embedder=embedder)
        baseline = embedder.embedded

        # Change one row's text; only that row should be re-embedded on the next open.
        changed = _aldric_bundle()
        changed.entities["npc_aldric"].description = "Caravan master and ferry pilot"
        store.replace_content_index(changed)

        VectorRetriever(store, embedder=embedder)
        assert embedder.embedded == baseline + 1
    finally:
        store.close()


# --------------------------------------------------------------------------- degrade honesty
#
# A lazy ``SemanticEmbedder`` pointed at a model that cannot load degrades to the hashing stub on
# its first embed (which the VectorRetriever triggers inside ``__init__`` via ``_reindex``). These
# tests pin the contract that the retriever stays HONEST through that runtime degrade — no real
# ~2GB model needed, $0 — by forcing the fallback with a non-existent model name.


def _degraded_embedder() -> SemanticEmbedder:
    """A semantic embedder that will degrade to hashing on first embed (model can't load)."""
    return SemanticEmbedder("nonexistent/model-for-vector-degrade-test-xyz")


def test_runtime_degrade_makes_is_semantic_false_not_a_lie() -> None:
    # Before the fix, model_id was snapshotted in __init__ (still "st:*" because the load is lazy),
    # so is_semantic reported True even after _reindex's first embed degraded to hashing.
    store = SQLiteStore()
    try:
        store.replace_content_index(_aldric_bundle())
        embedder = _degraded_embedder()

        retriever = VectorRetriever(store, embedder=embedder)

        # _reindex embedded the corpus → the lazy load failed → the embedder degraded.
        assert embedder.degraded is True
        assert embedder.model_id == HashingEmbedder().model_id
        # The retriever must reflect the live backend, not the construction-time "st:*" snapshot.
        assert retriever.model_id == embedder.model_id
        assert retriever.is_semantic is False  # no longer lies "True" about being semantic
    finally:
        store.close()


def test_degraded_hashing_vectors_do_not_poison_the_semantic_cache_key() -> None:
    # The degraded run produced hashing vectors; they must be persisted under the hashing model_id,
    # never under the "st:*" key (which a later run with the real model would read as a cache hit).
    store = SQLiteStore()
    try:
        store.replace_content_index(_aldric_bundle())
        embedder = _degraded_embedder()

        VectorRetriever(store, embedder=embedder)

        hashing_id = HashingEmbedder().model_id
        # Hashing vectors are keyed correctly...
        assert store.get_vectors(hashing_id) != {}
        # ...and nothing is keyed under the never-loaded semantic model.
        assert store.get_vectors("st:nonexistent/model-for-vector-degrade-test-xyz") == {}
    finally:
        store.close()


def test_later_real_model_run_does_not_reuse_stale_hashing_vectors(tmp_path: Path) -> None:
    # Cross-process scenario over one SQLite file: run 1 degrades (model unavailable) and persists
    # hashing vectors; run 2 has the real model available. Run 2 must NOT find run 1's hashing
    # vectors under the "st:*" key (which would mean the real model never re-embeds = poison).
    db = tmp_path / "vectors.sqlite"

    store1 = SQLiteStore(db)
    try:
        store1.replace_content_index(_aldric_bundle())
        VectorRetriever(store1, embedder=_degraded_embedder())  # run 1 degrades to hashing
    finally:
        store1.close()

    semantic_id = "st:nonexistent/model-for-vector-degrade-test-xyz"
    store2 = SQLiteStore(db)
    try:
        # Run 2: the real (semantic) model "loads" — simulated by an embedder advertising the same
        # st: id that never degrades — would look up vectors under semantic_id. Run 1 must not have
        # left any hashing vectors under that key for it to (wrongly) reuse.
        assert store2.get_vectors(semantic_id) == {}
    finally:
        store2.close()
