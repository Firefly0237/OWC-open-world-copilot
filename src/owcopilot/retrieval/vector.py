"""Dense vector retriever: persisted embeddings + exact cosine search.

The embedder is injected (``Embedder`` protocol). With the deterministic ``HashingEmbedder``
this stays an offline $0 lexical-ish leg; with ``SemanticEmbedder`` (bge-m3) it becomes the
real semantic leg of the hybrid retriever.

Embeddings are **persisted in SQLite** keyed by ``(ref, model_id, text_hash)`` so a neural
model only ever embeds new or changed rows -- re-opening a project reads vectors back instead
of re-running the model over unchanged canon.

Search runs through a pluggable :class:`~owcopilot.retrieval.vector_backend.VectorSearchBackend`.
The default is the disk-resident sqlite-vec ``vec0`` index (``SqliteVecBackend``): vectors live on
disk and a changed row is an incremental upsert, no full in-memory matrix rebuild. When the
sqlite-vec extension is unavailable the retriever degrades to the in-memory numpy-matrix backend
(``NumpyMatrixBackend``) with a guided log line — old environments stay functional. Either way the
search is **exact** (fp32, brute-force over the corpus): deterministic ordering, no ANN
approximation, and bit-identical results across the two backends.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..llm.cache import Embedder, HashingEmbedder
from ..storage import SQLiteStore
from .models import RetrievalHit
from .vector_backend import (
    NumpyMatrixBackend,
    VectorSearchBackend,
    _normalise,
)

logger = logging.getLogger(__name__)


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
        backend: VectorSearchBackend | None = None,
        quantized: bool = False,
        ann: bool = False,
    ) -> None:
        self.store = store
        self.embedder = embedder or HashingEmbedder()
        self._rows_loader = rows_loader
        self._vectors_table = vectors_table
        # Default fp32 (lossless, the safe default). ``quantized=True`` opts into the int8 two-stage
        # backend (G2-A): ~4× smaller / ~3× faster scan with an fp32 rerank that keeps recall
        # ~0.999. Ignored when an explicit ``backend`` is injected.
        self._quantized = quantized
        # ``ann=True`` (G2-B) opts the *large-N* corpus into the on-disk usearch HNSW backend: the
        # store only actually switches once the corpus crosses ``USEARCH_MIN_N``, so a small corpus
        # stays on the exact scan (recall 1.0) even with this set. Ignored when ``backend`` is
        # injected. Default ``False`` keeps every existing caller on the exact backend.
        self._ann = ann
        self._rows: list[_Row] = []
        # The search index. Built lazily once the embedding dim is known (a sqlite-vec ``vec0``
        # table is declared ``FLOAT[dim]``, and a lazy ``SemanticEmbedder`` only reveals its dim
        # after the first embed). ``backend`` may be injected (tests / a shared connection);
        # otherwise ``_reindex`` builds the sqlite-vec backend, falling back to numpy when absent.
        self._backend: VectorSearchBackend | None = backend
        self._reindex()

    @property
    def model_id(self) -> str:
        """The embedder's *current* id — read live, never snapshotted at construction.

        A lazy ``SemanticEmbedder`` reports ``st:bge-m3`` until its first embed; if that load
        fails it degrades to the hashing stub and flips its ``model_id`` to ``hashing-*``. Reading
        it live (rather than caching it in ``__init__``, before ``_reindex`` triggers the first
        embed) keeps ``is_semantic`` and the persisted cache key honest about the backend actually
        in use — so a degraded process never claims to be semantic, and its hashing vectors are
        never persisted under an ``st:`` key (which would poison the cache for a later run where
        the real model loads)."""
        return self.embedder.model_id

    @property
    def is_semantic(self) -> bool:
        """True when the active embedder is a real semantic model (not the hashing stub).

        Reads the embedder's live ``model_id``, so a runtime degrade (semantic model failed to
        load → hashing fallback) flips this to ``False`` instead of lying ``True``."""
        return self.model_id.startswith("st:")

    def similarities(self, query: str, refs: list[str]) -> dict[str, float]:
        """Cosine of ``query`` to each requested ref's stored vector, for hybrid reranking."""
        if not refs or self._backend is None:
            return {}
        q = _normalise(np.asarray(self.embedder.embed(query), dtype=np.float32))
        scores: dict[str, float] = {}
        for ref in refs:
            stored = self._backend.vector_for(ref)
            if stored is None or stored.shape[0] != q.shape[0]:
                continue
            scores[ref] = float(stored @ q)
        return scores

    def search(self, query: str, *, limit: int = 10) -> list[RetrievalHit]:
        if not self._rows or self._backend is None:
            return []
        q = _normalise(np.asarray(self.embedder.embed(query), dtype=np.float32))
        # The backend returns up to ``limit`` (ref, score) pairs already ordered by score desc,
        # ref asc on ties. Apply the retriever's ``score > 0`` rule and materialise hits; because
        # the backend order matches the historical stable argsort, the kept set is identical to the
        # previous "argsort, break on first score <= 0, cap at limit" walk.
        scored = self._backend.search(q, limit=limit)
        rows_by_ref = {row.ref: row for row in self._rows}
        hits: list[RetrievalHit] = []
        for ref, score in scored:
            if score <= 0:
                continue
            row = rows_by_ref.get(ref)
            if row is None:
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
        return hits

    def _reindex(self) -> None:
        """Load rows, (re)embed only what changed, persist, and sync the search backend.

        The persisted cache is keyed by the embedder's id. A lazy semantic embedder can *degrade*
        mid-reindex (its first ``embed_many`` here fails to load the model and falls back to
        hashing, flipping ``model_id`` from ``st:*`` to ``hashing-*``). When that happens the
        vectors we just produced are hashing vectors, so they must be keyed/persisted under the
        post-degrade id — never under the original ``st:*`` key, which would poison the cache for a
        later run where the real model loads. We therefore re-read ``self.model_id`` *after*
        embedding and, if the backend changed, discard the lookup done under the stale key and
        re-key the whole index to the backend that actually produced the vectors.

        The search index is synced **incrementally**: only re-embedded rows are upserted and only
        vanished rows are deleted (no full matrix rebuild). The disk-resident sqlite-vec backend
        persists across re-opens, so an unchanged corpus touches the index zero times; the in-memory
        numpy fallback starts empty per instance, so it is fully populated once."""
        self._rows = self._rows_loader(self.store)
        if not self._rows:
            if self._backend is not None:
                self._backend.clear()
            return

        table = self._vectors_table
        # Refs persisted before this reindex — used to compute the set of vanished rows to prune
        # from both the blob cache and the search index, mirroring ``prune_vectors``.
        # Key used for the *lookup*, captured before embedding can trigger a degrade.
        lookup_model_id = self.model_id
        cached = self.store.get_vectors(lookup_model_id, table=table)
        vectors: dict[str, np.ndarray] = {}
        to_embed: list[tuple[str, str, str]] = []  # (ref, text, text_hash)
        text_hashes: dict[str, str] = {}
        for row in self._rows:
            text = f"{row.title} {row.body}".strip()
            text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
            text_hashes[row.ref] = text_hash
            hit = cached.get(row.ref)
            if hit is not None and hit[0] == text_hash:
                vectors[row.ref] = np.frombuffer(hit[2], dtype=np.float32)
            else:
                to_embed.append((row.ref, text, text_hash))

        if to_embed:
            embedded = self.embedder.embed_many([text for _ref, text, _h in to_embed])
            for (ref, _text, _h), vector in zip(to_embed, embedded, strict=True):
                vectors[ref] = np.asarray(vector, dtype=np.float32)

        # Re-read after embedding: if the embedder degraded mid-reindex, the backend (and the key)
        # changed under us. The vectors looked up under the stale key belong to a different
        # embedding space, so re-embed every row under the real backend rather than mixing spaces
        # or persisting hashing vectors under the original ``st:*`` key.
        persist_model_id = self.model_id
        if persist_model_id != lookup_model_id:
            cached = self.store.get_vectors(persist_model_id, table=table)
            vectors = {}
            to_embed = []
            for row in self._rows:
                hit = cached.get(row.ref)
                if hit is not None and hit[0] == text_hashes[row.ref]:
                    vectors[row.ref] = np.frombuffer(hit[2], dtype=np.float32)
                else:
                    text = f"{row.title} {row.body}".strip()
                    to_embed.append((row.ref, text, text_hashes[row.ref]))
            if to_embed:
                embedded = self.embedder.embed_many([text for _ref, text, _h in to_embed])
                for (ref, _text, _h), vector in zip(to_embed, embedded, strict=True):
                    vectors[ref] = np.asarray(vector, dtype=np.float32)

        if to_embed:
            upserts: list[tuple[str, str, int, bytes]] = []
            for ref, _text, text_hash in to_embed:
                arr = vectors[ref]
                upserts.append((ref, text_hash, int(arr.shape[0]), arr.tobytes()))
            self.store.upsert_vectors(persist_model_id, upserts, table=table)

        current_refs = {row.ref for row in self._rows}
        persisted_refs = set(self.store.get_vectors(persist_model_id, table=table))
        removed_refs = persisted_refs - current_refs
        self.store.prune_vectors(persist_model_id, current_refs, table=table)

        dim = int(next(iter(vectors.values())).shape[0])
        self._sync_backend(
            vectors=vectors,
            changed_refs={ref for ref, _text, _h in to_embed},
            removed_refs=removed_refs,
            dim=dim,
            model_id=persist_model_id,
        )

    def _sync_backend(
        self,
        *,
        vectors: dict[str, np.ndarray],
        changed_refs: set[str],
        removed_refs: set[str],
        dim: int,
        model_id: str,
    ) -> None:
        """Bring the search backend in step with ``vectors`` (the current normalised corpus).

        Constructs the backend on first call (now that ``dim`` is known): the disk-resident
        sqlite-vec backend when available — pre-populated from the blob cache by the store — else
        the in-memory numpy fallback. A fresh numpy backend is empty and must take every row; a
        re-opened sqlite-vec backend already holds the unchanged rows, so it only needs the changed
        ones upserted and the vanished ones deleted."""
        if self._backend is None:
            built: VectorSearchBackend | None = self.store.make_vector_backend(
                model_id,
                dim=dim,
                table=self._vectors_table,
                quantized=self._quantized,
                ann=self._ann,
            )
            self._backend = built if built is not None else NumpyMatrixBackend()

        backend = self._backend
        if isinstance(backend, NumpyMatrixBackend):
            # In-memory, per-instance: nothing persists, so (re)populate the whole corpus. clear()
            # first so a reload that dropped rows does not leave stale entries behind.
            backend.clear()
            for row in self._rows:
                backend.upsert(row.ref, vectors[row.ref])
            return

        # Persistent backend (sqlite-vec): incremental. Upsert only re-embedded rows, delete only
        # vanished ones — the store already backfilled the unchanged rows from the blob cache.
        for ref in changed_refs:
            backend.upsert(ref, vectors[ref])
        for ref in removed_refs:
            backend.delete(ref)
