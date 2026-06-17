"""Retrieval over inspiration references — the same two-stage hybrid pipeline as project lore.

A whole book lives in the inspiration library as reference chunks, and it is retrieved to ground
generation (genesis / expansion). Lexical BM25 alone misses paraphrase and cross-lingual matches
("海上的权力斗争" vs a chunk about "舰队与商会的角力"), which is exactly what grounding needs.
So this builder runs BM25 + semantic recall, fuses them, reranks, and only then spends the token
budget — identical to the content-graph retriever, reusing its vector + fusion + rerank parts.
"""

from __future__ import annotations

import sqlite3

from ..llm.cache import Embedder
from ..retrieval.budget import trim_hits_to_budget
from ..retrieval.fusion import reciprocal_rank_fusion
from ..retrieval.models import ContextPack, RetrievalHit
from ..retrieval.rerank import rerank_hits
from ..retrieval.vector import VectorRetriever, load_reference_rows
from ..storage import SQLiteStore


class ReferenceContextBuilder:
    def __init__(
        self, store: SQLiteStore, *, embedder: Embedder | None = None, rerank: bool = True
    ) -> None:
        self.store = store
        self.rerank = rerank
        self.vector = (
            VectorRetriever(
                store,
                embedder=embedder,
                rows_loader=load_reference_rows,
                vectors_table="reference_vectors",
            )
            if embedder is not None
            else None
        )

    def build(self, query: str, *, budget_tokens: int = 1000, limit: int = 8) -> ContextPack:
        bm25 = [_ranking_hit(row) for row in self.store.search_reference_chunks(query, limit=limit)]
        result_lists = [bm25]
        if self.vector is not None:
            result_lists.append(self.vector.search(query, limit=limit))
        fused = reciprocal_rank_fusion(result_lists)
        if self.rerank:
            semantic_scores = None
            if self.vector is not None and self.vector.is_semantic:
                semantic_scores = self.vector.similarities(query, [hit.ref for hit in fused])
            ranked = rerank_hits(query, fused, semantic_scores=semantic_scores)
        else:
            ranked = fused
        # Cap the final count by `limit`. Fusing a BM25 + vector leg (each up to `limit`) grows the
        # candidate pool on purpose — for better ranking — but the caller still asked for at most
        # `limit` hits, so honour that here rather than leaning on budget trimming alone.
        ranked = ranked[:limit]
        # Rank by ref, then materialise display hits with correct source metadata regardless of
        # which leg surfaced each ref (a semantic-only hit still gets its source title/index).
        rows = self.store.reference_chunks_by_refs([hit.ref for hit in ranked])
        hits = [_display_hit(rows[hit.ref]) for hit in ranked if hit.ref in rows]
        return ContextPack(
            query=query,
            budget_tokens=budget_tokens,
            hits=trim_hits_to_budget(hits, budget_tokens=budget_tokens),
        )


def _ranking_hit(row: dict[str, object]) -> RetrievalHit:
    """A lightweight hit from a BM25 row, used only for fusion/rerank scoring."""
    return RetrievalHit(
        ref=str(row["ref"]),
        object_type="reference_chunk",
        title=_title(row),
        body=str(row["body"]),
        score=float(row["score"]),  # type: ignore[arg-type]
        source="reference_bm25",
        metadata={"source_title": str(row.get("source_title") or "")},
    )


def _display_hit(row: sqlite3.Row) -> RetrievalHit:
    """The final, display-ready hit with full source metadata from the canonical table."""
    data = dict(row)
    source_title = str(data.get("source_title") or "")
    chunk_title = str(data.get("title") or "")
    return RetrievalHit(
        ref=str(data["ref"]),
        object_type="reference_chunk",
        title=f"{source_title} / {chunk_title}".strip(" /"),
        body=str(data["body"]),
        score=0.0,
        source="reference",
        metadata={
            "source_id": str(data.get("source_id") or ""),
            "source_title": source_title,
            "chunk_index": str(data.get("chunk_index") or ""),
        },
    )


def _title(row: dict[str, object]) -> str:
    source_title = str(row.get("source_title") or "")
    chunk_title = str(row.get("title") or "")
    return f"{source_title} / {chunk_title}".strip(" /")
