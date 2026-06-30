"""Pluggable vector-search backends behind one ``VectorSearchBackend`` interface.

The retriever (``retrieval/vector.py``) talks to a backend, never to a concrete index. Two
backends ship here:

* ``SqliteVecBackend`` — disk-resident ``vec0`` virtual table (the sqlite-vec extension). Vectors
  live on disk, KNN is a SQL query, and a changed row is a single ``DELETE``+``INSERT`` instead of
  rebuilding an in-memory matrix. This is the scale win for long-narrative corpora (#1).
* ``NumpyMatrixBackend`` — the historical in-memory ``np.vstack`` matrix, preserved verbatim as a
  fallback. If the sqlite-vec extension cannot be imported/loaded (old environments, a Python build
  with ``enable_load_extension`` disabled), the retriever degrades to this backend with a guided
  log line rather than crashing.

**Group 1 is fp32 (lossless).** Both backends store full-precision float32 vectors and produce
**bit-identical** ``search``/``vector_for`` results for the same inputs: vectors are normalised on
upsert, scores are the exact ``stored · query`` dot product, and ties break by ascending ``ref``
(matching the numpy backend's stable argsort over ref-ordered rows). int8 quantisation and a real
ANN backend are a *separate* layer (group 2) added here later without touching these classes.

**Group 2 G2-A is int8 + two-stage rerank (still effectively lossless).**
``SqliteVecInt8Backend`` stores symmetric int8-quantised vectors in an ``INT8[dim]`` vec0 column
(~4× smaller, ~3× faster scan than fp32) **and** keeps the exact fp32 vectors in a sidecar table.
``search`` runs a two stage: ① an int8 coarse KNN pulls ``k' = max(3*limit, 30)`` candidates, then
② those candidates' fp32 vectors are dot-producted against the fp32 query and re-ranked, returning
the true top-``limit``. int8-only recall@10 is ~0.84; the fp32 rerank over a 3× candidate pool lifts
it back to ~0.999 (verified on iid and clustered synthetic corpora, see ``P0_G2_RESEARCH.md`` §2).
``vector_for`` returns the exact fp32 vector (from the sidecar), so hybrid reranking upstream stays
fp32-exact. The backend is opt-in; the fp32 ``SqliteVecBackend`` remains the safe default.

**Group 2 G2-B is the on-disk ANN tier (``UsearchBackend``).** For corpora large enough that even
the int8 scan's O(N) latency dominates, ``UsearchBackend`` keeps a real usearch HNSW index in a
``.usearch`` file (sub-linear lookup, ~1ms/query at 30k, mmap/load-able without loading the whole
corpus) and re-uses the same two-stage idea: an approximate coarse recall feeds an exact fp32
rerank, so recall stays ~0.99. The exact fp32 vectors live in an *authoritative* sidecar table
— it backs ``vector_for`` and lets the index self-heal (rebuild) when the out-of-transaction
``.usearch`` file falls out of sync after a crash. HNSW tuning is fixed to the measured
``connectivity=32 / expansion_add=200 / expansion_search=2048`` (the library defaults are a recall
trap; 512 was re-measured as seed-fragile and below the 0.95 gate). The store's tier selector only
picks this backend above a corpus-size threshold, so small /
eval corpora keep the exact sqlite-vec scan and its 1.0 recall gate untouched. When ``usearch`` is
not importable the store falls back to a sqlite-vec backend with a guided log line.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from typing import Any, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)


def _normalise(vector: np.ndarray) -> np.ndarray:
    """Unit-normalise (no-op for a zero vector). Kept identical to ``retrieval.vector._normalise``.

    Normalising on upsert lets L2 nearest-neighbour (what ``vec0`` computes) rank identically to
    cosine, and makes the stored dot product a cosine similarity directly."""
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        return vector
    return vector / norm


def quantise_int8(vector: np.ndarray) -> np.ndarray:
    """Symmetric per-vector int8 quantisation of a **unit-normalised** vector.

    ``round(x * 127)`` clamped to ``[-127, 127]`` (127, not 128, so the codebook is symmetric and
    ``-x`` maps to ``-q``). sqlite-vec 0.1.9 ships no ``vec_quantize_i8`` SQL function, so the
    quantisation happens here in Python and the result is stored in an ``INT8[dim]`` vec0 column.

    Deterministic and pure: the same fp32 input always yields the same bytes. A zero vector
    quantises to all-zeros. Inputs are normalised first so the ``*127`` scale lands inside int8
    range for any direction (a unit component is in ``[-1, 1]``)."""
    unit = _normalise(np.asarray(vector, dtype=np.float32))
    return np.clip(np.round(unit * 127.0), -127, 127).astype(np.int8)


@runtime_checkable
class VectorSearchBackend(Protocol):
    """A vector index keyed by ``ref`` with a fixed dimensionality.

    Implementations own persistence and the KNN query; the retriever owns embedding, the text_hash
    incremental cache, and hit assembly. ``search`` returns ``(ref, score)`` pairs with **higher
    score = more similar** (cosine over unit vectors), already filtered to the requested ``limit``
    but *not* filtered by sign — the retriever applies its ``score > 0`` rule."""

    def upsert(self, ref: str, vector: np.ndarray) -> None:
        """Insert or replace the vector for ``ref`` (stored unit-normalised)."""
        ...

    def delete(self, ref: str) -> None:
        """Remove ``ref`` from the index (no-op if absent)."""
        ...

    def search(self, query: np.ndarray, *, limit: int) -> list[tuple[str, float]]:
        """Top-``limit`` ``(ref, score)`` by cosine, descending, ref-ascending on ties."""
        ...

    def vector_for(self, ref: str) -> np.ndarray | None:
        """The stored unit-normalised vector for ``ref`` (``None`` if absent)."""
        ...

    def clear(self) -> None:
        """Drop every entry."""
        ...


def _rank(
    scored: list[tuple[str, float]], *, limit: int
) -> list[tuple[str, float]]:
    """Order candidates exactly like the numpy backend: by score desc, ref asc on ties, then cut.

    The historical retriever did ``np.argsort(-scores, kind="stable")`` over rows loaded
    ``ORDER BY ref``, so ties resolved to ascending ref. Reproducing that here keeps both backends
    bit-for-bit identical regardless of upsert order."""
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]


class NumpyMatrixBackend:
    """In-memory fp32 matrix backend — the pre-sqlite-vec behaviour, preserved as a fallback.

    Holds one ``(N, dim)`` matrix of unit-normalised vectors plus a ``ref -> row`` map. Search is
    the exact ``matrix @ q`` dot product the retriever used before; this class simply moves that
    logic behind the backend interface so the retriever no longer rebuilds the matrix itself."""

    def __init__(self) -> None:
        self._vectors: dict[str, np.ndarray] = {}
        self._refs: list[str] = []
        self._index: dict[str, int] = {}
        self._matrix = np.empty((0, 0), dtype=np.float32)

    def _rebuild_matrix(self) -> None:
        if not self._refs:
            self._matrix = np.empty((0, 0), dtype=np.float32)
            return
        self._matrix = np.vstack([self._vectors[ref] for ref in self._refs])

    def upsert(self, ref: str, vector: np.ndarray) -> None:
        vec = _normalise(np.asarray(vector, dtype=np.float32))
        new = ref not in self._index
        self._vectors[ref] = vec
        if new:
            self._index[ref] = len(self._refs)
            self._refs.append(ref)
        self._rebuild_matrix()

    def delete(self, ref: str) -> None:
        if ref not in self._index:
            return
        self._vectors.pop(ref, None)
        self._refs.remove(ref)
        self._index = {r: i for i, r in enumerate(self._refs)}
        self._rebuild_matrix()

    def search(self, query: np.ndarray, *, limit: int) -> list[tuple[str, float]]:
        if not self._refs or self._matrix.size == 0:
            return []
        q = _normalise(np.asarray(query, dtype=np.float32))
        if q.shape[0] != self._matrix.shape[1]:
            return []
        scores = self._matrix @ q
        scored = [(self._refs[i], float(scores[i])) for i in range(len(self._refs))]
        return _rank(scored, limit=limit)

    def vector_for(self, ref: str) -> np.ndarray | None:
        vec = self._vectors.get(ref)
        return None if vec is None else vec

    def clear(self) -> None:
        self._vectors = {}
        self._refs = []
        self._index = {}
        self._matrix = np.empty((0, 0), dtype=np.float32)


class SqliteVecError(RuntimeError):
    """Raised when the sqlite-vec extension is unavailable or fails to load."""


def sqlite_vec_available() -> bool:
    """Whether the sqlite-vec extension can be imported in this interpreter.

    Import-only probe (no DB connection): callers still construct ``SqliteVecBackend`` which loads
    the extension against a real connection and raises ``SqliteVecError`` on any failure."""
    try:
        import sqlite_vec  # noqa: F401
    except Exception:  # pragma: no cover - environment-dependent
        return False
    return True


class SqliteVecBackend:
    """Disk-resident fp32 vector backend over a sqlite-vec ``vec0`` virtual table.

    One ``{table}`` virtual table holds ``(ref, embedding FLOAT[dim])``. Upsert is
    ``DELETE``+``INSERT``; KNN is ``embedding MATCH ? AND k=?``. Scores are recomputed as the exact
    ``stored · query`` dot product from the fp32 blobs (not derived from the returned L2 distance),
    so they are bit-identical to the numpy backend; ties break by ascending ref.

    The same SQLite connection that owns the rest of the runtime store is reused, so vec0 lives in
    the project DB alongside FTS5 and the ``content_vectors`` blob cache — one file, one transaction
    boundary."""

    def __init__(self, conn: sqlite3.Connection, *, dim: int, table: str) -> None:
        if not isinstance(conn, sqlite3.Connection):  # defensive: keep the type contract explicit
            raise SqliteVecError("SqliteVecBackend requires a sqlite3.Connection")
        self._conn: sqlite3.Connection = conn
        self._dim = int(dim)
        self._table = table
        self._load_extension(conn)
        self._ensure_table()

    @staticmethod
    def _load_extension(conn: sqlite3.Connection) -> None:
        """Load the vec0 extension, converting any failure into a guided ``SqliteVecError``."""
        try:
            import sqlite_vec
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise SqliteVecError(
                "sqlite-vec is not installed; install it (offline wheel under scratchpad/wheels) "
                "to enable the disk-resident vector backend, or the retriever falls back to numpy."
            ) from exc
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise SqliteVecError(
                "failed to load the sqlite-vec extension on this SQLite connection "
                f"({exc}); the retriever falls back to the numpy backend."
            ) from exc
        finally:
            # Re-disable extension loading: only sqlite-vec is needed, keep the surface minimal.
            try:
                conn.enable_load_extension(False)
            except Exception:  # pragma: no cover - best effort
                pass

    def _ensure_table(self) -> None:
        # vec0 virtual table; FLOAT[dim] = fp32, ref as the text primary key.
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._table} "  # noqa: S608 - validated table name
            f"USING vec0(ref TEXT PRIMARY KEY, embedding FLOAT[{self._dim}])"
        )
        self._conn.commit()

    def _count(self) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._table}"  # noqa: S608 - validated table name
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def upsert(self, ref: str, vector: np.ndarray) -> None:
        vec = _normalise(np.asarray(vector, dtype=np.float32))
        blob = np.ascontiguousarray(vec, dtype=np.float32).tobytes()
        # DELETE + INSERT = idempotent replace (vec0 has no UPSERT on the rowid table).
        self._conn.execute(
            f"DELETE FROM {self._table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.execute(
            f"INSERT INTO {self._table}(ref, embedding) VALUES (?, ?)",  # noqa: S608
            (ref, blob),
        )
        self._conn.commit()

    def delete(self, ref: str) -> None:
        self._conn.execute(
            f"DELETE FROM {self._table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.commit()

    def search(self, query: np.ndarray, *, limit: int) -> list[tuple[str, float]]:
        if limit <= 0:
            return []
        q = _normalise(np.asarray(query, dtype=np.float32))
        if q.shape[0] != self._dim:
            return []
        count = self._count()
        if count == 0:
            return []
        # vec0 KNN gives candidates in cosine (==L2 for unit vectors) order. Pull every row (vec0 is
        # an exact brute-force scan, so this is the same O(N) it already does) and recompute the
        # exact dot product from the stored fp32 blobs. That makes scores bit-identical to the
        # numpy backend and lets _rank apply the identical (score desc, ref asc) ordering — so the
        # boundary ties and the `score > 0` filter the retriever applies cannot diverge.
        qblob = np.ascontiguousarray(q, dtype=np.float32).tobytes()
        rows = self._conn.execute(
            f"SELECT ref, embedding FROM {self._table} "  # noqa: S608 - validated table name
            f"WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (qblob, count),
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            ref = str(row[0])
            stored = np.frombuffer(bytes(row[1]), dtype=np.float32)
            scored.append((ref, float(stored @ q)))
        return _rank(scored, limit=limit)

    def vector_for(self, ref: str) -> np.ndarray | None:
        row = self._conn.execute(
            f"SELECT embedding FROM {self._table} WHERE ref = ?", (ref,)  # noqa: S608
        ).fetchone()
        if row is None:
            return None
        return np.frombuffer(bytes(row[0]), dtype=np.float32)

    def clear(self) -> None:
        self._conn.execute(f"DELETE FROM {self._table}")  # noqa: S608 - validated table name
        self._conn.commit()


# Coarse-recall floor for the int8 two-stage search: even at a tiny ``limit`` the int8 stage pulls
# at least this many candidates so the fp32 rerank has a real pool to recover precision from.
# 3*limit (the multiplier proven in P0_G2_RESEARCH §2) takes over once it exceeds this floor.
_INT8_COARSE_FLOOR = 30
_INT8_COARSE_MULTIPLIER = 3


class SqliteVecInt8Backend:
    """Disk-resident **int8** vec0 backend with a two-stage (int8 coarse → fp32 rerank) search.

    Storage is two tables on the runtime connection:

    * ``{table}`` — a vec0 virtual table declared ``INT8[dim]`` holding the symmetric int8
      quantisation of each unit vector (~4× smaller than fp32). This is the *coarse* index.
    * ``{table}_fp32`` — a plain table of the exact fp32 unit vectors keyed by ``ref``. This is the
      authoritative *rerank* source and what ``vector_for`` returns, so int8's lossiness never leaks
      into scores handed back to the retriever / hybrid reranker.

    ``search`` is two-stage: the int8 KNN returns ``k' = max(3*limit, 30)`` candidates by quantised
    distance (recall@10 ~0.84 alone), then the fp32 vectors of exactly those candidates are scored
    by the exact ``stored · query`` dot product and re-ranked, yielding the true top-``limit``
    (recall@10 ~0.999). Ordering matches the fp32 backend: score desc, ref asc on ties.

    Quantised int8 KNN needs the query wrapped in the ``vec_int8(?)`` constructor (sqlite-vec
    rejects a raw float32 blob against an ``INT8`` column); the rerank uses the fp32 query."""

    def __init__(self, conn: sqlite3.Connection, *, dim: int, table: str) -> None:
        if not isinstance(conn, sqlite3.Connection):  # defensive: keep the type contract explicit
            raise SqliteVecError("SqliteVecInt8Backend requires a sqlite3.Connection")
        self._conn: sqlite3.Connection = conn
        self._dim = int(dim)
        self._table = table
        self._fp32_table = f"{table}_fp32"
        SqliteVecBackend._load_extension(conn)
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        # int8 coarse index. INT8[dim] = one signed byte per component.
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._table} "  # noqa: S608 - validated name
            f"USING vec0(ref TEXT PRIMARY KEY, embedding INT8[{self._dim}])"
        )
        # fp32 rerank source: the exact unit vectors, one row per ref.
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._fp32_table} ("  # noqa: S608 - validated name
            f"ref TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
        )
        self._conn.commit()

    def _count(self) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._table}"  # noqa: S608 - validated table name
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def upsert(self, ref: str, vector: np.ndarray) -> None:
        vec = _normalise(np.asarray(vector, dtype=np.float32))
        qblob = np.ascontiguousarray(quantise_int8(vec), dtype=np.int8).tobytes()
        fblob = np.ascontiguousarray(vec, dtype=np.float32).tobytes()
        # DELETE + INSERT = idempotent replace (vec0 has no UPSERT on the rowid table); the fp32
        # sidecar mirrors the same ref so the two tables never drift.
        self._conn.execute(
            f"DELETE FROM {self._table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.execute(
            f"INSERT INTO {self._table}(ref, embedding) VALUES (?, vec_int8(?))",  # noqa: S608
            (ref, qblob),
        )
        self._conn.execute(
            f"INSERT INTO {self._fp32_table}(ref, embedding) VALUES (?, ?) "  # noqa: S608
            f"ON CONFLICT(ref) DO UPDATE SET embedding = excluded.embedding",
            (ref, fblob),
        )
        self._conn.commit()

    def delete(self, ref: str) -> None:
        self._conn.execute(
            f"DELETE FROM {self._table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.execute(
            f"DELETE FROM {self._fp32_table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.commit()

    def search(self, query: np.ndarray, *, limit: int) -> list[tuple[str, float]]:
        if limit <= 0:
            return []
        q = _normalise(np.asarray(query, dtype=np.float32))
        if q.shape[0] != self._dim:
            return []
        count = self._count()
        if count == 0:
            return []
        # Stage ①: int8 coarse KNN over k' = max(3*limit, floor) candidates (capped at corpus size).
        k = min(count, max(_INT8_COARSE_MULTIPLIER * limit, _INT8_COARSE_FLOOR))
        qint8 = np.ascontiguousarray(quantise_int8(q), dtype=np.int8).tobytes()
        candidates = self._conn.execute(
            f"SELECT ref FROM {self._table} "  # noqa: S608 - validated table name
            f"WHERE embedding MATCH vec_int8(?) AND k = ? ORDER BY distance",
            (qint8, k),
        ).fetchall()
        if not candidates:
            return []
        # Stage ②: pull the exact fp32 vectors of just those candidates and re-rank by the exact dot
        # product. This is what recovers int8's lost recall to ~1.0 — the heavy fp32 work touches
        # only k' rows, not the whole corpus.
        refs = [str(row[0]) for row in candidates]
        placeholders = ",".join("?" for _ in refs)
        fp32_rows = self._conn.execute(
            f"SELECT ref, embedding FROM {self._fp32_table} "  # noqa: S608 - validated name
            f"WHERE ref IN ({placeholders})",
            refs,
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in fp32_rows:
            stored = np.frombuffer(bytes(row[1]), dtype=np.float32)
            scored.append((str(row[0]), float(stored @ q)))
        return _rank(scored, limit=limit)

    def vector_for(self, ref: str) -> np.ndarray | None:
        row = self._conn.execute(
            f"SELECT embedding FROM {self._fp32_table} WHERE ref = ?",  # noqa: S608
            (ref,),
        ).fetchone()
        if row is None:
            return None
        return np.frombuffer(bytes(row[0]), dtype=np.float32)

    def clear(self) -> None:
        self._conn.execute(f"DELETE FROM {self._table}")  # noqa: S608 - validated table name
        self._conn.execute(f"DELETE FROM {self._fp32_table}")  # noqa: S608 - validated name
        self._conn.commit()


# G2-B usearch HNSW tuning. **These are not the library defaults — the defaults are a recall trap.**
# usearch ships connectivity=16 / expansion_search=64, which on a 30k clustered corpus (dim=1024,
# spread=0.35) measured only recall@10 ≈ 0.13 — reproduced here, matching the 0.12 a raw probe saw
# in P0_G2_RESEARCH §3. connectivity=32 / expansion_add=200 fix the graph quality.
#
# expansion_search is the one knob that actually bounds query-time recall: a wider search beam
# surfaces more of the true neighbours, and — crucially — the two-stage fp32 rerank below can only
# *reorder* candidates the beam already returned, it cannot recover ones the beam missed (verified:
# growing the requested k' past expansion_search does not widen the captured pool). The research's
# 512 hit ~0.90 on a single lucky seed but is seed-sensitive and fell to ~0.82 on others — below the
# 0.95 recall gate this backend must hold. Re-measuring across seeds, expansion_search=2048 is where
# the two-stage recall@10 clears ~0.99 robustly (≥0.95 on every seed tried), at ~17ms/query on 30k —
# still ~25× faster than the ~420ms fp32 brute scan, which is the whole point of the ANN tier. Treat
# these as a measured, hard contract, not a default.
_USEARCH_CONNECTIVITY = 32
_USEARCH_EXPANSION_ADD = 200
_USEARCH_EXPANSION_SEARCH = 2048

# Two-stage coarse-recall sizing, identical in spirit to the int8 backend: the ANN stage pulls a
# wider candidate pool than ``limit`` so the exact fp32 rerank has room to recover the true top-k.
_USEARCH_COARSE_FLOOR = 30
_USEARCH_COARSE_MULTIPLIER = 3


class UsearchError(RuntimeError):
    """Raised when the usearch package is unavailable or its index cannot be built/opened."""


def usearch_available() -> bool:
    """Whether the ``usearch`` HNSW package can be imported in this interpreter.

    Import-only probe (no index built). A ``False`` here is the signal for the store to fall back to
    a sqlite-vec backend with a guided log line rather than crashing — usearch is an optional
    large-corpus accelerator, not a hard dependency."""
    try:
        import usearch.index  # noqa: F401
    except Exception:  # pragma: no cover - environment-dependent
        return False
    return True


# usearch keys are full uint64; SQLite INTEGER is a 64-bit *signed* value, so it cannot hold the top
# half of the uint64 range directly. We therefore hash refs into the **63-bit** non-negative space:
# the proposed key (and every probe step) stays in ``[0, 2**63)`` -- a valid uint64 for
# usearch *and* fits a signed SQLite INTEGER with no reinterpretation. 63 bits is still 9.2e18 slots
# — collisions at our corpus scale are vanishingly rare, and the keymap's linear probe makes the
# scheme total regardless.
_KEY_SPACE = 1 << 63


def _ref_to_key(ref: str) -> int:
    """Deterministic 63-bit key for a string ``ref`` (the *preferred* usearch key).

    usearch indexes by ``uint64`` keys, but our refs are strings, so we hash the ref to a stable
    value. blake2b(digest_size=8) is fast, well-distributed, and identical across processes and
    platforms, so the same ref always prefers the same key across restarts; masking to 63 bits keeps
    it inside SQLite's signed-INTEGER range (still a valid uint64 for usearch). Collisions (two refs
    hashing to one key) are resolved by the persisted keymap via linear probing — this function only
    proposes the starting key."""
    raw = int.from_bytes(hashlib.blake2b(ref.encode("utf-8"), digest_size=8).digest(), "big")
    return raw % _KEY_SPACE


class UsearchBackend:
    """On-disk **usearch HNSW** ANN backend with a two-stage (ANN coarse → fp32 rerank) search.

    The large-N tier of the scale work (G2-B). Where ``SqliteVecBackend`` /
    ``SqliteVecInt8Backend`` do an exact O(N) scan inside SQLite, this does a *sub-linear* HNSW
    lookup (~1ms/query at 30k vs ~80–420ms for a brute scan) by keeping a real approximate index in
    a ``.usearch`` file, mmap- or load-able without pulling the whole corpus into RAM. It is meant
    for corpora large enough that the brute scan dominates latency; small corpora stay on the exact
    sqlite-vec backends (the store's tier selector enforces this so the eval recall gate never sees
    ANN approximation).

    Three pieces of state, with a strict authority order:

    * ``{table}_fp32`` (SQLite) — the exact fp32 unit vectors keyed by ``ref``. **Authoritative.**
      It is what ``vector_for`` returns, what the fp32 rerank scores against, and the source the
      ``.usearch`` index is rebuilt from. Lives in the runtime DB / its transaction.
    * ``{table}_keymap`` (SQLite) — the persistent ``ref ↔ uint64 key`` map usearch needs. Keys are
      assigned by hashing the ref (``_ref_to_key``) and linear-probing on collision, so the mapping
      is stable across restarts and never reuses a live key.
    * ``{index_path}`` (``.usearch`` file) — the HNSW index, **derivable** from the two SQLite
      tables. Because it lives outside the SQLite transaction, a crash mid-write can leave it out of
      sync; the constructor detects that (its key-count disagrees with the keymap) and rebuilds from
      the authoritative tables, so a half-written / stale index self-heals on open.

    ``search`` is two-stage like the int8 backend: the HNSW returns ``k' = max(3*limit, 30)``
    approximate neighbours, then those candidates' exact fp32 vectors are dot-producted against the
    fp32 query and re-ranked, returning the true top-``limit`` (recall ~0.99). HNSW tuning is fixed
    to the measured ``connectivity=32 / expansion_add=200 / expansion_search=2048`` — never the
    library defaults, which are a recall trap (see the module constants for the seed-sweep that set
    these values)."""

    def __init__(
        self, conn: sqlite3.Connection, *, dim: int, table: str, index_path: str
    ) -> None:
        if not isinstance(conn, sqlite3.Connection):  # defensive: keep the type contract explicit
            raise UsearchError("UsearchBackend requires a sqlite3.Connection")
        try:
            from usearch.index import Index
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise UsearchError(
                "usearch is not installed; install it (offline wheel under scratchpad/wheels2) to "
                "enable the on-disk ANN backend, or the retriever falls back to sqlite-vec."
            ) from exc
        self._Index = Index
        self._conn = conn
        self._dim = int(dim)
        self._fp32_table = f"{table}_fp32"
        self._keymap_table = f"{table}_keymap"
        self._index_path = index_path
        self._ensure_tables()
        self._index = self._open_or_rebuild_index()

    # -- index construction -------------------------------------------------------------------

    def _new_index(self) -> Any:
        """A fresh HNSW index with the **measured** tuning (never the library defaults)."""
        return self._Index(
            ndim=self._dim,
            metric="cos",
            dtype="f32",
            connectivity=_USEARCH_CONNECTIVITY,
            expansion_add=_USEARCH_EXPANSION_ADD,
            expansion_search=_USEARCH_EXPANSION_SEARCH,
        )

    def _ensure_tables(self) -> None:
        # fp32 authority: exact unit vectors, one row per ref.
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._fp32_table} ("  # noqa: S608 - validated name
            f"ref TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
        )
        # ref <-> uint64 key map. key is the PK (usearch's handle); ref is unique (one key per ref).
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._keymap_table} ("  # noqa: S608 - validated name
            f"key INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE)"
        )
        self._conn.commit()

    def _keymap_count(self) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._keymap_table}"  # noqa: S608 - validated name
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _open_or_rebuild_index(self) -> Any:
        """Open the on-disk index, **rebuilding from the authoritative tables when it is stale.**

        The ``.usearch`` file is not inside SQLite's transaction, so it can lag the fp32/keymap
        tables after a crash mid-upsert. The integrity check is simple and total: the index's key
        count must equal the keymap row count. Any mismatch — missing file, unreadable file, a
        half-written index, a dimension change — triggers a full rebuild from the fp32 table, which
        is always correct because that table is the authority. A healthy index opens in place."""
        expected = self._keymap_count()
        if os.path.exists(self._index_path):
            try:
                index = self._new_index()
                index.load(self._index_path)  # writable load (supports incremental add/remove)
                if len(index) == expected:
                    return index
                logger.info(
                    "usearch index %s out of sync (%d keys vs %d expected); rebuilding from the "
                    "fp32 source.",
                    self._index_path,
                    len(index),
                    expected,
                )
            except Exception as exc:  # corrupt / wrong-dim / unreadable -> rebuild, never crash
                logger.info(
                    "usearch index %s could not be opened (%s); rebuilding from the fp32 source.",
                    self._index_path,
                    exc,
                )
        return self._rebuild_from_source()

    def _rebuild_from_source(self) -> object:
        """Rebuild the HNSW index from the authoritative fp32 + keymap tables and persist it.

        Used on first open (no file yet) and to self-heal a stale/corrupt index. Pulls every
        ``(key, ref, fp32)`` triple, adds them in one batch, and saves — so the on-disk file is
        consistent with the SQLite authority again."""
        index = self._new_index()
        rows = self._conn.execute(
            f"SELECT m.key, f.embedding FROM {self._keymap_table} AS m "  # noqa: S608
            f"JOIN {self._fp32_table} AS f ON f.ref = m.ref"
        ).fetchall()
        if rows:
            keys = np.fromiter((int(r[0]) for r in rows), dtype=np.uint64, count=len(rows))
            vecs = np.vstack(
                [np.frombuffer(bytes(r[1]), dtype=np.float32) for r in rows]
            ).astype(np.float32)
            index.add(keys, vecs)
        self._persist(index)
        return index

    def _persist(self, index: Any) -> None:
        """Save the index to its ``.usearch`` file, creating the parent directory if needed."""
        directory = os.path.dirname(self._index_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        index.save(self._index_path)

    # -- ref <-> key mapping ------------------------------------------------------------------

    def _key_for_ref(self, ref: str) -> int | None:
        row = self._conn.execute(
            f"SELECT key FROM {self._keymap_table} WHERE ref = ?",  # noqa: S608 - validated name
            (ref,),
        ).fetchone()
        return None if row is None else int(row[0])

    def _assign_key(self, ref: str) -> int:
        """Return the persisted key for ``ref``, allocating one (hash + linear probe) if absent.

        The starting key is ``_ref_to_key(ref)``; if that uint64 is already taken by a *different*
        ref we probe ``key+1`` (mod 2**64) until a free slot is found, then persist the mapping.
        Collisions are astronomically rare for 64-bit hashes at our scale, but probing makes the
        scheme total and deterministic rather than merely probable."""
        existing = self._key_for_ref(ref)
        if existing is not None:
            return existing
        key = _ref_to_key(ref)
        while (
            self._conn.execute(
                f"SELECT 1 FROM {self._keymap_table} WHERE key = ?",  # noqa: S608 - validated name
                (key,),
            ).fetchone()
            is not None
        ):
            key = (key + 1) % _KEY_SPACE
        self._conn.execute(
            f"INSERT INTO {self._keymap_table}(key, ref) VALUES (?, ?)",  # noqa: S608
            (key, ref),
        )
        return key

    # -- VectorSearchBackend interface --------------------------------------------------------

    def upsert(self, ref: str, vector: np.ndarray) -> None:
        vec = _normalise(np.asarray(vector, dtype=np.float32))
        fblob = np.ascontiguousarray(vec, dtype=np.float32).tobytes()
        # fp32 authority first (its row is what a rebuild would read), then the keymap, then the
        # HNSW. usearch raises on a duplicate key, so an existing key is removed before re-adding —
        # that makes upsert an idempotent replace.
        self._conn.execute(
            f"INSERT INTO {self._fp32_table}(ref, embedding) VALUES (?, ?) "  # noqa: S608
            f"ON CONFLICT(ref) DO UPDATE SET embedding = excluded.embedding",
            (ref, fblob),
        )
        key = self._assign_key(ref)
        self._conn.commit()
        index = self._index
        if index.contains(np.uint64(key)):
            index.remove(np.uint64(key))
        index.add(np.uint64(key), vec)
        self._persist(index)

    def delete(self, ref: str) -> None:
        key = self._key_for_ref(ref)
        self._conn.execute(
            f"DELETE FROM {self._fp32_table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.execute(
            f"DELETE FROM {self._keymap_table} WHERE ref = ?", (ref,)  # noqa: S608
        )
        self._conn.commit()
        if key is not None and self._index.contains(np.uint64(key)):
            self._index.remove(np.uint64(key))
            self._persist(self._index)

    def search(self, query: np.ndarray, *, limit: int) -> list[tuple[str, float]]:
        if limit <= 0:
            return []
        q = _normalise(np.asarray(query, dtype=np.float32))
        if q.shape[0] != self._dim:
            return []
        size = len(self._index)
        if size == 0:
            return []
        # Stage ①: ANN coarse recall over k' = max(3*limit, floor) candidates (capped at corpus
        # size). The wide pool is what lets the exact rerank recover HNSW's approximation loss.
        k = min(size, max(_USEARCH_COARSE_MULTIPLIER * limit, _USEARCH_COARSE_FLOOR))
        matches = self._index.search(q, k)
        keys = [int(x) for x in matches.keys]
        if not keys:
            return []
        # Stage ②: map keys back to refs and pull their exact fp32 vectors; re-rank by the exact dot
        # product. Heavy fp32 work touches only k' rows, not the whole corpus.
        placeholders = ",".join("?" for _ in keys)
        rows = self._conn.execute(
            f"SELECT m.ref, f.embedding FROM {self._keymap_table} AS m "  # noqa: S608
            f"JOIN {self._fp32_table} AS f ON f.ref = m.ref "
            f"WHERE m.key IN ({placeholders})",
            keys,
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            stored = np.frombuffer(bytes(row[1]), dtype=np.float32)
            scored.append((str(row[0]), float(stored @ q)))
        return _rank(scored, limit=limit)

    def vector_for(self, ref: str) -> np.ndarray | None:
        row = self._conn.execute(
            f"SELECT embedding FROM {self._fp32_table} WHERE ref = ?",  # noqa: S608
            (ref,),
        ).fetchone()
        if row is None:
            return None
        return np.frombuffer(bytes(row[0]), dtype=np.float32)

    def clear(self) -> None:
        self._conn.execute(f"DELETE FROM {self._fp32_table}")  # noqa: S608 - validated name
        self._conn.execute(f"DELETE FROM {self._keymap_table}")  # noqa: S608 - validated name
        self._conn.commit()
        self._index = self._new_index()
        self._persist(self._index)
