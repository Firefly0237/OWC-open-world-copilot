from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Term
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.storage import SQLiteStore


def test_bm25_retriever_searches_content_fts() -> None:
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
                    "quest_missing_caravan": Quest(
                        id="quest_missing_caravan",
                        title="Missing Caravan",
                        objective="Find the lost supplies",
                    )
                },
            )
        )

        hits = BM25Retriever(store).search("caravan")

        assert [hit.ref for hit in hits] == [
            "quest:quest_missing_caravan",
            "entity:npc_aldric",
        ]
        assert all(hit.source == "bm25" for hit in hits)
    finally:
        store.close()


def test_bm25_retriever_respects_limit() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(
            ContentBundle(
                entities={
                    "npc_a": Entity(id="npc_a", name="Caravan A", type=EntityType.NPC),
                    "npc_b": Entity(id="npc_b", name="Caravan B", type=EntityType.NPC),
                }
            )
        )

        hits = BM25Retriever(store).search("caravan", limit=1)

        assert len(hits) == 1
    finally:
        store.close()


def test_bm25_retriever_accepts_natural_language_punctuation() -> None:
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
                }
            )
        )

        hits = BM25Retriever(store).search("Who is Aldric?")

        assert [hit.ref for hit in hits] == ["entity:npc_aldric"]
    finally:
        store.close()


def test_bm25_retriever_falls_back_to_cjk_phrase_matching() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(
            ContentBundle(
                terms={
                    "term_zhenqi": Term(
                        id="term_zhenqi",
                        canonical="真气",
                        description="战斗资源统一叫真气",
                    )
                }
            )
        )

        hits = BM25Retriever(store).search("战斗资源在剧情文本里应该叫什么?")

        assert [hit.ref for hit in hits] == ["term:term_zhenqi"]
    finally:
        store.close()
