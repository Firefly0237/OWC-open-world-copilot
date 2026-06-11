"""Retrieval over inspiration references, separate from project lore retrieval."""

from __future__ import annotations

from ..retrieval.budget import trim_hits_to_budget
from ..retrieval.models import ContextPack, RetrievalHit
from ..storage import SQLiteStore


class ReferenceContextBuilder:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def build(self, query: str, *, budget_tokens: int = 1000, limit: int = 8) -> ContextPack:
        rows = self.store.search_reference_chunks(query, limit=limit)
        hits = [
            RetrievalHit(
                ref=str(row["ref"]),
                object_type="reference_chunk",
                title=_title(row),
                body=str(row["body"]),
                score=float(row["score"]),
                source="reference_bm25",
                metadata={
                    "source_id": str(row["source_id"]),
                    "source_title": str(row["source_title"]),
                    "chunk_index": str(row["chunk_index"]),
                },
            )
            for row in rows
        ]
        return ContextPack(
            query=query,
            budget_tokens=budget_tokens,
            hits=trim_hits_to_budget(hits, budget_tokens=budget_tokens),
        )


def _title(row: dict[str, object]) -> str:
    source_title = str(row.get("source_title") or "")
    chunk_title = str(row.get("title") or "")
    return f"{source_title} / {chunk_title}".strip(" /")
