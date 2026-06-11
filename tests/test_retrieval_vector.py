from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.retrieval.vector import VectorRetriever
from owcopilot.storage import SQLiteStore


def test_vector_retriever_searches_index_with_hashing_embedder() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(
            ContentBundle(
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
                        id="quest_siege",
                        title="Siege",
                        objective="Defend the northern wall",
                    )
                },
            )
        )

        hits = VectorRetriever(store).search("caravan routes")

        assert hits[0].ref == "entity:npc_aldric"
        assert hits[0].source == "vector"
    finally:
        store.close()
