# SQLite FTS5/BM25 词法检索器。
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
        # Scope-aware read. content_fts carries world_id/version as UNINDEXED columns, so the MATCH
        # is constrained to the store's current (world_id, version). In the single default scope
        # this returns the same rows in the same bm25 order as the un-scoped path (every row is in
        # that scope); a multi-scope DB no longer ranks foreign rows into the result.
        # Display text comes from the authoritative content_index join: the FTS columns hold the
        # CJK-bigram rewrite (index tokens), which must never leak into hits/context packs.
        # bm25 weights are positional per content_fts column (ref, object_type, world_id, version,
        # title, body) → title boost 4.0 / body 1.0 sit at positions 5/6. (The pre-scope weight
        # list was never realigned when the scope columns landed, which silently parked the title
        # boost on UNINDEXED world_id — restored here.)
        rows = self.store.conn.execute(
            """
            SELECT f.ref, f.object_type, c.title, c.body,
                   bm25(content_fts, 0.0, 0.0, 0.0, 0.0, 4.0, 1.0) AS rank
            FROM content_fts AS f
            JOIN content_index AS c
                ON c.ref = f.ref AND c.world_id = f.world_id AND c.version = f.version
            WHERE content_fts MATCH ? AND f.world_id = ? AND f.version = ?
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

    # The lexical fallback is a linear scan; on a target-scale corpus (10^5+ rows) an unbounded
    # scan per query is a latency cliff. With CJK bigram indexing, 2+ char Chinese queries are
    # served by the FTS index, so this path only carries single-char / stop-worded queries and
    # under-filled top-ups — bounding it trades tail-of-corpus recall on that rare path for a
    # hard latency ceiling. Deterministic (ORDER BY ref) so the trade is at least reproducible.
    _FALLBACK_SCAN_CAP = 20_000

    def _fallback_search(self, query: str, *, limit: int) -> list[RetrievalHit]:
        # Scope-aware read. The lexical fallback scans content_index rows (bounded by
        # _FALLBACK_SCAN_CAP), so the (world_id, version) filter both isolates the scope and is a
        # concrete reduce-N win: the scan touches only this scope's rows, not the whole
        # multi-world table. Single default scope: identical scan/result to the un-scoped path.
        rows = self.store.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index "
            "WHERE world_id = ? AND version = ? ORDER BY ref LIMIT ?",
            (self.store.world_id, self.store.version, self._FALLBACK_SCAN_CAP),
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
