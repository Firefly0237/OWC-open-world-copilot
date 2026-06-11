"""SQLite FTS5/BM25 retriever."""

from __future__ import annotations

from ..storage import SQLiteStore
from ..storage.sqlite import build_fts_match_query
from .models import RetrievalHit
from .text_match import lexical_score


class BM25Retriever:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def search(self, query: str, *, limit: int = 10) -> list[RetrievalHit]:
        match_query = build_fts_match_query(query)
        hits: list[RetrievalHit] = []
        if match_query is None:
            return self._fallback_search(query, limit=limit)
        rows = self.store.conn.execute(
            """
            SELECT ref, object_type, title, body,
                   bm25(content_fts, 0.0, 0.0, 4.0, 1.0) AS rank
            FROM content_fts
            WHERE content_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        hits.extend(
            RetrievalHit(
                ref=str(row["ref"]),
                object_type=str(row["object_type"]),
                title=str(row["title"]),
                body=str(row["body"]),
                score=-float(row["rank"]),
                source="bm25",
            )
            for row in rows
        )
        if len(hits) < limit:
            seen = {hit.ref for hit in hits}
            hits.extend(
                hit
                for hit in self._fallback_search(query, limit=limit)
                if hit.ref not in seen
            )
        return hits[:limit]

    def _fallback_search(self, query: str, *, limit: int) -> list[RetrievalHit]:
        rows = self.store.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index ORDER BY ref"
        ).fetchall()
        hits: list[RetrievalHit] = []
        for row in rows:
            score = lexical_score(query, [str(row["ref"]), str(row["title"]), str(row["body"])])
            if score <= 0:
                continue
            hits.append(
                RetrievalHit(
                    ref=str(row["ref"]),
                    object_type=str(row["object_type"]),
                    title=str(row["title"]),
                    body=str(row["body"]),
                    score=score,
                    source="bm25",
                )
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.ref))[:limit]
