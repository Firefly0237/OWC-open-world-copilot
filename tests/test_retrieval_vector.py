from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.llm.cache import HashingEmbedder
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
