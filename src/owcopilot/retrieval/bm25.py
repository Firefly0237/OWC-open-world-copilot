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
        # Scale-P0 G2-C C2: scope-aware read. content_fts carries world_id/version as UNINDEXED
        # columns, so the MATCH is constrained to the store's current (world_id, version). In the
        # single default scope this returns the same rows in the same bm25 order as pre-C2 (every
        # row is in that scope); a multi-scope DB no longer ranks foreign rows into the result.
        rows = self.store.conn.execute(
            """
            SELECT ref, object_type, title, body,
                   bm25(content_fts, 0.0, 0.0, 4.0, 1.0) AS rank
            FROM content_fts
            WHERE content_fts MATCH ? AND world_id = ? AND version = ?
            ORDER BY rank
            LIMIT ?
            """,
            (match_query, self.store.world_id, self.store.version, limit),
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
                hit for hit in self._fallback_search(query, limit=limit) if hit.ref not in seen
            )
        return hits[:limit]

    def _fallback_search(self, query: str, *, limit: int) -> list[RetrievalHit]:
        # Scale-P0 G2-C C2: scope-aware read. The lexical fallback is a full scan of content_index,
        # so the (world_id, version) filter both isolates the scope and is a concrete reduce-N win:
        # the scan touches only this scope's rows, not the whole multi-world table. Single default
        # scope: identical scan/result to pre-C2.
        rows = self.store.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index "
            "WHERE world_id = ? AND version = ? ORDER BY ref",
            (self.store.world_id, self.store.version),
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
