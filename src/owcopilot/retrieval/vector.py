"""Offline vector retriever using the existing HashingEmbedder."""

from __future__ import annotations

from dataclasses import dataclass

from ..llm.cache import Embedder, HashingEmbedder, cosine
from ..storage import SQLiteStore
from .models import RetrievalHit


@dataclass
class _VectorRow:
    ref: str
    object_type: str
    title: str
    body: str
    vector: list[float]


class VectorRetriever:
    def __init__(self, store: SQLiteStore, *, embedder: Embedder | None = None) -> None:
        self.store = store
        self.embedder = embedder or HashingEmbedder()
        self.rows = self._load_rows()

    def search(self, query: str, *, limit: int = 10) -> list[RetrievalHit]:
        q = self.embedder.embed(query)
        hits: list[RetrievalHit] = []
        for row in self.rows:
            score = cosine(q, row.vector)
            if score <= 0:
                continue
            hits.append(
                RetrievalHit(
                    ref=row.ref,
                    object_type=row.object_type,
                    title=row.title,
                    body=row.body,
                    score=score,
                    source="vector",
                )
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.ref))[:limit]

    def _load_rows(self) -> list[_VectorRow]:
        rows = self.store.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index ORDER BY ref"
        ).fetchall()
        return [
            _VectorRow(
                ref=str(row["ref"]),
                object_type=str(row["object_type"]),
                title=str(row["title"]),
                body=str(row["body"]),
                vector=self.embedder.embed(f"{row['title']} {row['body']}"),
            )
            for row in rows
        ]
