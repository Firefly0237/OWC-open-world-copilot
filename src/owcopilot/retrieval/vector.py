"""Dense vector retriever: persisted embeddings + exact cosine search.

The embedder is injected (``Embedder`` protocol). With the deterministic ``HashingEmbedder``
this stays an offline $0 lexical-ish leg; with ``SemanticEmbedder`` (bge-m3) it becomes the
real semantic leg of the hybrid retriever.

Embeddings are **persisted in SQLite** keyed by ``(ref, model_id, text_hash)`` so a neural
model only ever embeds new or changed rows -- re-opening a project reads vectors back instead
of re-running the model over unchanged canon. Search is an **exact** cosine top-k over an
in-memory numpy matrix: deterministic ordering, no ANN approximation, fast at lore scale.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..llm.cache import Embedder, HashingEmbedder
from ..storage import SQLiteStore
from .models import RetrievalHit


@dataclass
class _Row:
    ref: str
    object_type: str
    title: str
    body: str


def load_content_rows(store: SQLiteStore) -> list[_Row]:
    """Rows for the content-graph vector index (the default corpus)."""
    return [
        _Row(str(r["ref"]), str(r["object_type"]), str(r["title"]), str(r["body"]))
        for r in store.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index ORDER BY ref"
        ).fetchall()
    ]


def load_reference_rows(store: SQLiteStore) -> list[_Row]:
    """Rows for the inspiration-library vector index (reference chunks)."""
    return [
        _Row(str(r["ref"]), "reference_chunk", str(r["title"]), str(r["body"]))
        for r in store.conn.execute(
            "SELECT ref, title, body FROM reference_chunks ORDER BY ref"
        ).fetchall()
    ]


class VectorRetriever:
    """Dense retriever over a corpus (default: the content graph; or reference chunks).

    The corpus is selected by ``rows_loader`` (which table to read) and ``vectors_table`` (where
    to persist its embeddings), so the content graph and the inspiration library share one
    implementation instead of duplicating the embed-cache-search logic."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        embedder: Embedder | None = None,
        rows_loader: Callable[[SQLiteStore], list[_Row]] = load_content_rows,
        vectors_table: str = "content_vectors",
    ) -> None:
        self.store = store
        self.embedder = embedder or HashingEmbedder()
        self.model_id = self.embedder.model_id
        self._rows_loader = rows_loader
        self._vectors_table = vectors_table
        self._rows: list[_Row] = []
        self._row_index: dict[str, int] = {}
        self._matrix = np.empty((0, 0), dtype=np.float32)
        self._reindex()

    @property
    def is_semantic(self) -> bool:
        """True when the active embedder is a real semantic model (not the hashing stub)."""
        return self.model_id.startswith("st:")

    def similarities(self, query: str, refs: list[str]) -> dict[str, float]:
        """Cosine of ``query`` to each requested ref's stored vector, for hybrid reranking."""
        if not refs or self._matrix.size == 0:
            return {}
        q = _normalise(np.asarray(self.embedder.embed(query), dtype=np.float32))
        if q.shape[0] != self._matrix.shape[1]:
            return {}
        scores: dict[str, float] = {}
        for ref in refs:
            index = self._row_index.get(ref)
            if index is not None:
                scores[ref] = float(self._matrix[index] @ q)
        return scores

    def search(self, query: str, *, limit: int = 10) -> list[RetrievalHit]:
        if not self._rows or self._matrix.size == 0:
            return []
        q = _normalise(np.asarray(self.embedder.embed(query), dtype=np.float32))
        if q.shape[0] != self._matrix.shape[1]:
            return []
        scores = self._matrix @ q
        order = np.argsort(-scores, kind="stable")
        hits: list[RetrievalHit] = []
        for index in order:
            score = float(scores[index])
            if score <= 0:
                break
            row = self._rows[index]
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
            if len(hits) >= limit:
                break
        return hits

    def _reindex(self) -> None:
        """Load rows, (re)embed only what changed, persist, build the search matrix."""
        self._rows = self._rows_loader(self.store)
        if not self._rows:
            self._matrix = np.empty((0, 0), dtype=np.float32)
            self._row_index = {}
            return

        table = self._vectors_table
        cached = self.store.get_vectors(self.model_id, table=table)
        vectors: dict[str, np.ndarray] = {}
        to_embed: list[tuple[str, str, str]] = []  # (ref, text, text_hash)
        for row in self._rows:
            text = f"{row.title} {row.body}".strip()
            text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
            hit = cached.get(row.ref)
            if hit is not None and hit[0] == text_hash:
                vectors[row.ref] = np.frombuffer(hit[2], dtype=np.float32)
            else:
                to_embed.append((row.ref, text, text_hash))

        if to_embed:
            embedded = self.embedder.embed_many([text for _ref, text, _h in to_embed])
            upserts: list[tuple[str, str, int, bytes]] = []
            for (ref, _text, text_hash), vector in zip(to_embed, embedded, strict=True):
                arr = np.asarray(vector, dtype=np.float32)
                vectors[ref] = arr
                upserts.append((ref, text_hash, int(arr.shape[0]), arr.tobytes()))
            self.store.upsert_vectors(self.model_id, upserts, table=table)

        self.store.prune_vectors(self.model_id, {row.ref for row in self._rows}, table=table)
        self._matrix = np.vstack([_normalise(vectors[row.ref]) for row in self._rows])
        self._row_index = {row.ref: index for index, row in enumerate(self._rows)}


def _normalise(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        return vector
    return vector / norm
