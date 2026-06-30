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
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Protocol, runtime_checkable

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
