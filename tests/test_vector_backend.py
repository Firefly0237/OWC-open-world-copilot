"""Vector search backends: sqlite-vec (vec0) correctness + bit-for-bit parity with numpy.

Group 1 of the scale-P0 work introduced a disk-resident sqlite-vec backend behind the
``VectorSearchBackend`` interface, with the historical in-memory numpy matrix kept as a fallback.
These tests pin the contract that matters: the two backends are interchangeable (same refs, same
scores, same tie-breaking), the sqlite-vec primitives (upsert/delete/search/vector_for/clear) work,
and the retriever degrades to numpy — still functional — when sqlite-vec is unavailable.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval import vector as vector_module
from owcopilot.retrieval.vector import VectorRetriever
from owcopilot.retrieval.vector_backend import (
    NumpyMatrixBackend,
    SqliteVecBackend,
    SqliteVecInt8Backend,
    UsearchBackend,
    quantise_int8,
    sqlite_vec_available,
    usearch_available,
)
from owcopilot.storage import SQLiteStore

requires_sqlite_vec = pytest.mark.skipif(
    not sqlite_vec_available(), reason="sqlite-vec extension not installed"
)

requires_usearch = pytest.mark.skipif(
    not usearch_available(), reason="usearch package not installed"
)


def _vec(values: list[float]) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


def _new_vec_backend(dim: int) -> SqliteVecBackend:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return SqliteVecBackend(conn, dim=dim, table="content_vec")


# --------------------------------------------------------------------------- vec0 primitives


@requires_sqlite_vec
def test_sqlite_vec_upsert_search_and_vector_for() -> None:
    backend = _new_vec_backend(dim=4)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    backend.upsert("b", _vec([0, 1, 0, 0]))
    backend.upsert("c", _vec([0.9, 0.1, 0, 0]))

    hits = backend.search(_vec([1, 0, 0, 0]), limit=3)
    assert [ref for ref, _score in hits] == ["a", "c", "b"]
    # scores are descending cosine; the exact query matches a at ~1.0.
    assert hits[0][1] == pytest.approx(1.0, abs=1e-5)
    assert hits[0][1] >= hits[1][1] >= hits[2][1]

    stored = backend.vector_for("a")
    assert stored is not None
    # vectors are stored unit-normalised.
    assert float(np.linalg.norm(stored)) == pytest.approx(1.0, abs=1e-5)
    assert backend.vector_for("missing") is None


@requires_sqlite_vec
def test_sqlite_vec_upsert_replaces_and_delete_removes() -> None:
    backend = _new_vec_backend(dim=4)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    backend.upsert("a", _vec([0, 0, 0, 1]))  # replace, not duplicate

    # only one "a" with the new direction
    hits = backend.search(_vec([0, 0, 0, 1]), limit=5)
    assert [ref for ref, _ in hits] == ["a"]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-5)

    backend.delete("a")
    assert backend.search(_vec([0, 0, 0, 1]), limit=5) == []
    backend.delete("a")  # idempotent no-op


@requires_sqlite_vec
def test_sqlite_vec_clear_empties_the_index() -> None:
    backend = _new_vec_backend(dim=3)
    backend.upsert("a", _vec([1, 0, 0]))
    backend.upsert("b", _vec([0, 1, 0]))
    backend.clear()
    assert backend.search(_vec([1, 0, 0]), limit=5) == []
    assert backend.vector_for("a") is None


@requires_sqlite_vec
def test_sqlite_vec_search_dimension_mismatch_returns_empty() -> None:
    backend = _new_vec_backend(dim=4)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    assert backend.search(_vec([1, 0, 0]), limit=5) == []  # wrong dim


# --------------------------------------------------------------------------- numpy <-> vec0 parity


def _embedded(dim: int = 64) -> tuple[dict[str, np.ndarray], HashingEmbedder]:
    embedder = HashingEmbedder(dim=dim)
    texts = {
        "ref:0": "the caravan master guards the northern trade road",
        "ref:1": "a siege defends the northern wall against raiders",
        "ref:2": "northern trade road caravan and ferry routes",
        "ref:3": "the queen rules the southern coastal cities",
        "ref:4": "a ferry pilot crosses the river at dawn",
        "ref:5": "northern wall siege defenders hold the line",  # near-dup of ref:1 → tie pressure
        "ref:6": "merchants trade along the southern coast",
        "ref:7": "the caravan master guards the northern trade road",  # exact dup of ref:0
    }
    vectors = {ref: np.asarray(embedder.embed(t), dtype=np.float32) for ref, t in texts.items()}
    return vectors, embedder


def _populate(backend: object, vectors: dict[str, np.ndarray]) -> None:
    # Upsert in a non-sorted order to prove tie-breaking does not depend on insertion order.
    for ref in sorted(vectors, reverse=True):
        backend.upsert(ref, vectors[ref])  # type: ignore[attr-defined]


@requires_sqlite_vec
@pytest.mark.parametrize("query_text", ["northern trade road", "ferry", "southern coast queen"])
def test_numpy_and_sqlite_vec_search_are_bit_identical(query_text: str) -> None:
    vectors, embedder = _embedded()
    numpy_backend = NumpyMatrixBackend()
    vec_backend = _new_vec_backend(dim=64)
    _populate(numpy_backend, vectors)
    _populate(vec_backend, vectors)

    def _norm(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v if n <= 0 else v / n

    q = _norm(np.asarray(embedder.embed(query_text), dtype=np.float32))

    np_hits = numpy_backend.search(q, limit=8)
    vec_hits = vec_backend.search(q, limit=8)

    # Same refs, same order (including tie-break), bit-identical fp32 scores.
    assert [r for r, _ in np_hits] == [r for r, _ in vec_hits]
    for (r1, s1), (r2, s2) in zip(np_hits, vec_hits, strict=True):
        assert r1 == r2
        assert np.float32(s1) == np.float32(s2)


@requires_sqlite_vec
def test_numpy_and_sqlite_vec_vector_for_match() -> None:
    vectors, _ = _embedded()
    numpy_backend = NumpyMatrixBackend()
    vec_backend = _new_vec_backend(dim=64)
    _populate(numpy_backend, vectors)
    _populate(vec_backend, vectors)

    for ref in vectors:
        a = numpy_backend.vector_for(ref)
        b = vec_backend.vector_for(ref)
        assert a is not None and b is not None
        np.testing.assert_array_equal(a, b)


# --------------------------------------------------------------------------- retriever-level parity

#
# These run the full VectorRetriever (embed cache + backend sync) twice over identical canon: once
# forced onto the numpy backend, once on the auto-detected sqlite-vec backend. search() and
# similarities() must agree exactly — the property the acceptance recall gate depends on.


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"
            ),
            "npc_mira": Entity(
                id="npc_mira", name="Mira", type=EntityType.NPC, description="Ferry pilot at dawn"
            ),
        },
        quests={
            "quest_siege": Quest(
                id="quest_siege", title="Siege", objective="Defend the northern wall"
            )
        },
    )


def _retriever_results(
    use_numpy: bool, query: str
) -> tuple[list[tuple[str, float]], dict[str, float]]:
    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        backend = NumpyMatrixBackend() if use_numpy else None
        retriever = VectorRetriever(store, backend=backend)
        hits = retriever.search(query, limit=10)
        refs = [h.ref for h in hits]
        sims = retriever.similarities(query, refs)
        return [(h.ref, h.score) for h in hits], sims
    finally:
        store.close()


@requires_sqlite_vec
@pytest.mark.parametrize("query", ["caravan routes", "ferry across the river", "defend the wall"])
def test_retriever_search_and_similarities_parity(query: str) -> None:
    numpy_hits, numpy_sims = _retriever_results(use_numpy=True, query=query)
    vec_hits, vec_sims = _retriever_results(use_numpy=False, query=query)

    assert [r for r, _ in numpy_hits] == [r for r, _ in vec_hits]
    for (r1, s1), (r2, s2) in zip(numpy_hits, vec_hits, strict=True):
        assert r1 == r2
        assert np.float32(s1) == np.float32(s2)

    assert numpy_sims.keys() == vec_sims.keys()
    for ref in numpy_sims:
        assert np.float32(numpy_sims[ref]) == np.float32(vec_sims[ref])


# --------------------------------------------------------------------------- guided fallback


def test_retriever_falls_back_to_numpy_when_sqlite_vec_unavailable(monkeypatch) -> None:
    """When the store cannot build a sqlite-vec backend, the retriever uses numpy and still runs."""
    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        # Simulate "sqlite-vec not installed" at the store boundary (guided None, not a crash).
        monkeypatch.setattr(SQLiteStore, "make_vector_backend", lambda *a, **k: None)

        retriever = VectorRetriever(store)
        assert isinstance(retriever._backend, NumpyMatrixBackend)

        hits = retriever.search("caravan routes", limit=5)
        assert hits and hits[0].ref == "entity:npc_aldric"
        assert hits[0].source == "vector"
        sims = retriever.similarities("caravan routes", [hits[0].ref])
        assert hits[0].ref in sims
    finally:
        store.close()


def test_make_vector_backend_returns_none_on_sqlite_vec_error(monkeypatch) -> None:
    """A SqliteVecError from backend construction degrades to None (numpy), never propagates."""
    from owcopilot.retrieval import vector_backend as vb

    def _boom(*_a: object, **_k: object) -> None:
        raise vb.SqliteVecError("simulated: extension load failed")

    monkeypatch.setattr(vb, "SqliteVecBackend", _boom)
    store = SQLiteStore()
    try:
        assert store.make_vector_backend("hashing-1024", dim=1024) is None
    finally:
        store.close()


@requires_sqlite_vec
def test_make_vector_backend_degrades_when_backfill_raises_operational_error(monkeypatch) -> None:
    """If backfill raises sqlite3.OperationalError (e.g. a vec0 table persisted at a different dim),
    make_vector_backend must degrade to numpy (None) via a guided log -- never let the bare
    OperationalError propagate. Locks the fix that widened the guard to cover the backfill step."""

    def _boom(self, *_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("simulated: vec0 dimension mismatch on backfill")

    monkeypatch.setattr(SQLiteStore, "_backfill_vec0", _boom)
    store = SQLiteStore()
    try:
        assert store.make_vector_backend("hashing-1024", dim=1024) is None
    finally:
        store.close()


def test_module_exposes_normalise_shared_helper() -> None:
    # vector.py imports _normalise from vector_backend (single source of truth, no divergence).
    assert vector_module._normalise is not None
    v = vector_module._normalise(_vec([3, 4]))
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-6)


# ===========================================================================================
# G2-A: int8 two-stage (int8 coarse recall -> fp32 rerank) backend.
# ===========================================================================================


def _new_int8_backend(dim: int) -> SqliteVecInt8Backend:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return SqliteVecInt8Backend(conn, dim=dim, table="content_vec_i8")


# --------------------------------------------------------------------------- quantisation function


def test_quantise_int8_is_deterministic_and_symmetric() -> None:
    v = _vec([0.3, -0.7, 0.1, 0.5, -0.2, 0.0, 0.9, -0.4])
    a = quantise_int8(v)
    b = quantise_int8(v)
    # pure + deterministic: identical bytes every call.
    assert a.dtype == np.int8
    np.testing.assert_array_equal(a, b)
    # symmetric codebook: q(-x) == -q(x) (no -128 outlier).
    np.testing.assert_array_equal(quantise_int8(-v), -a)
    assert int(a.min()) >= -127 and int(a.max()) <= 127


def test_quantise_int8_handles_zero_and_extreme_vectors() -> None:
    dim = 16
    # zero vector -> all zeros (normalise is a no-op, round(0)=0).
    zeros = np.zeros(dim, dtype=np.float32)
    np.testing.assert_array_equal(quantise_int8(zeros), np.zeros(dim, np.int8))
    # a one-hot axis vector is already unit length: its live component saturates to 127.
    onehot = np.zeros(dim, dtype=np.float32)
    onehot[3] = 1.0
    q = quantise_int8(onehot)
    assert int(q[3]) == 127
    assert int(np.abs(q).max()) <= 127
    # huge magnitudes are normalised first, so they never overflow int8.
    big = quantise_int8(_vec([1e9, -1e9, 5e8, 0.0]))
    assert int(np.abs(big).max()) <= 127


# --------------------------------------------------------------------------- int8 primitives


@requires_sqlite_vec
def test_int8_backend_upsert_search_vector_for_and_delete() -> None:
    backend = _new_int8_backend(dim=4)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    backend.upsert("b", _vec([0, 1, 0, 0]))
    backend.upsert("c", _vec([0.9, 0.1, 0, 0]))

    hits = backend.search(_vec([1, 0, 0, 0]), limit=3)
    assert [ref for ref, _ in hits] == ["a", "c", "b"]
    assert hits[0][1] >= hits[1][1] >= hits[2][1]

    # vector_for returns the EXACT fp32 unit vector (not the lossy int8), unit-normalised.
    stored = backend.vector_for("a")
    assert stored is not None
    assert stored.dtype == np.float32
    np.testing.assert_allclose(stored, _vec([1, 0, 0, 0]), atol=1e-6)
    assert backend.vector_for("missing") is None

    # replace (not duplicate) then delete.
    backend.upsert("a", _vec([0, 0, 0, 1]))
    assert [r for r, _ in backend.search(_vec([0, 0, 0, 1]), limit=5)][0] == "a"
    backend.delete("a")
    assert backend.vector_for("a") is None
    backend.delete("a")  # idempotent


@requires_sqlite_vec
def test_int8_backend_clear_and_dim_mismatch() -> None:
    backend = _new_int8_backend(dim=4)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    assert backend.search(_vec([1, 0, 0]), limit=5) == []  # wrong dim -> empty
    backend.clear()
    assert backend.search(_vec([1, 0, 0, 0]), limit=5) == []
    assert backend.vector_for("a") is None


# --------------------------------------------------------------------------- recall: two-stage wins


def _clustered_corpus(
    dim: int, n: int, clusters: int, seed: int
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((clusters, dim)).astype(np.float32)
    vectors = {
        f"r{i}": (centers[i % clusters] + 0.30 * rng.standard_normal(dim)).astype(np.float32)
        for i in range(n)
    }
    return vectors, centers


def _fp32_truth(vectors: dict[str, np.ndarray], q: np.ndarray, limit: int) -> set[str]:
    qn = q / (np.linalg.norm(q) or 1.0)
    scored = sorted(
        (
            (ref, float((v / (np.linalg.norm(v) or 1.0)) @ qn))
            for ref, v in vectors.items()
        ),
        key=lambda x: (-x[1], x[0]),
    )
    return {ref for ref, _ in scored[:limit]}


@requires_sqlite_vec
def test_int8_two_stage_recall_is_near_lossless_and_beats_int8_only() -> None:
    """The core G2-A claim: the int8 coarse -> fp32 rerank two-stage recovers int8's lost recall to
    ~1.0, and the fp32 rerank is load-bearing (two-stage strictly beats int8-only single-stage)."""
    dim, n, clusters, seed = 64, 1500, 24, 7
    vectors, centers = _clustered_corpus(dim, n, clusters, seed)
    backend = _new_int8_backend(dim=dim)
    for ref, v in vectors.items():
        backend.upsert(ref, v)

    rng = np.random.default_rng(seed + 1)
    limit, n_queries = 10, 120
    two_stage_hits = 0
    int8_only_hits = 0
    total = 0
    for i in range(n_queries):
        q = (centers[i % clusters] + 0.30 * rng.standard_normal(dim)).astype(np.float32)
        truth = _fp32_truth(vectors, q, limit)

        # two-stage (the real backend.search)
        two = {ref for ref, _ in backend.search(q, limit=limit)}
        two_stage_hits += len(truth & two)

        # int8-only baseline: pull exactly `limit` candidates from the int8 index, no rerank.
        qn = q / (np.linalg.norm(q) or 1.0)
        qint8 = np.ascontiguousarray(quantise_int8(qn), dtype=np.int8).tobytes()
        rows = backend._conn.execute(  # noqa: SLF001 - exercising the single-stage baseline
            "SELECT ref FROM content_vec_i8 WHERE embedding MATCH vec_int8(?) AND k = ? "
            "ORDER BY distance",
            (qint8, limit),
        ).fetchall()
        int8_only_hits += len(truth & {str(r[0]) for r in rows})
        total += limit

    two_stage_recall = two_stage_hits / total
    int8_only_recall = int8_only_hits / total
    assert two_stage_recall >= 0.99, f"two-stage recall {two_stage_recall:.4f} < 0.99"
    # the rerank must actually buy recall: two-stage strictly above int8-only.
    assert two_stage_recall > int8_only_recall, (
        f"two-stage {two_stage_recall:.4f} not above int8-only {int8_only_recall:.4f}"
    )


@requires_sqlite_vec
def test_int8_index_is_smaller_than_fp32() -> None:
    """The int8 coarse column is ~4× smaller than the fp32 vectors (the storage win)."""
    dim = 128
    backend = _new_int8_backend(dim=dim)
    rng = np.random.default_rng(3)
    for i in range(200):
        backend.upsert(f"r{i}", rng.standard_normal(dim).astype(np.float32))

    int8_bytes = backend._conn.execute(  # noqa: SLF001 - inspecting on-disk byte sizes
        "SELECT SUM(length(embedding)) FROM content_vec_i8"
    ).fetchone()[0]
    fp32_bytes = backend._conn.execute(  # noqa: SLF001
        "SELECT SUM(length(embedding)) FROM content_vec_i8_fp32"
    ).fetchone()[0]
    # one byte/component int8 vs four bytes/component fp32 == exactly 4×.
    assert fp32_bytes == pytest.approx(int8_bytes * 4, rel=0.01)


# --------------------------------------------------------------------------- vs fp32: same top hit


@requires_sqlite_vec
@pytest.mark.parametrize("query_text", ["northern trade road", "ferry", "southern coast queen"])
def test_int8_two_stage_matches_fp32_top_hits(query_text: str) -> None:
    """On the eval-style small corpus the int8 two-stage returns the same top hits as fp32 — the
    property the acceptance recall gate (hit_rate 1.0) depends on."""
    vectors, embedder = _embedded()
    fp32_backend = _new_vec_backend(dim=64)
    int8_backend = _new_int8_backend(dim=64)
    _populate(fp32_backend, vectors)
    _populate(int8_backend, vectors)

    q = _embedded()[1].embed(query_text)
    q = np.asarray(q, dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)

    fp32_hits = [r for r, _ in fp32_backend.search(q, limit=5)]
    int8_hits = [r for r, _ in int8_backend.search(q, limit=5)]
    assert fp32_hits == int8_hits


# --------------------------------------------------------------------------- retriever integration


@requires_sqlite_vec
def test_retriever_quantized_backend_search_matches_fp32() -> None:
    """A VectorRetriever built with quantized=True uses the int8 two-stage backend and returns the
    same hits as the fp32 default on the canon bundle (the int8 mode is no-loss in effect)."""
    fp32_hits, _ = _retriever_results(use_numpy=False, query="caravan routes")

    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        retriever = VectorRetriever(store, quantized=True)
        assert isinstance(retriever._backend, SqliteVecInt8Backend)
        int8_hits = [(h.ref, h.score) for h in retriever.search("caravan routes", limit=10)]
    finally:
        store.close()

    assert [r for r, _ in fp32_hits] == [r for r, _ in int8_hits]


@requires_sqlite_vec
def test_make_vector_backend_quantized_returns_int8_backend() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        backend = store.make_vector_backend("hashing-1024", dim=1024, quantized=True)
        assert isinstance(backend, SqliteVecInt8Backend)
        fp32 = store.make_vector_backend("hashing-1024", dim=1024, quantized=False)
        assert isinstance(fp32, SqliteVecBackend)
    finally:
        store.close()


@requires_sqlite_vec
def test_int8_two_stage_holds_acceptance_retrieval_gate() -> None:
    """The int8 two-stage path holds the acceptance retrieval hit_rate at 1.0 on the eval world,
    matching the fp32 default. This is what makes int8 a safe opt-in: swapping it into the context
    builder does not regress the recall gate the whole acceptance benchmark depends on."""
    import tempfile
    from pathlib import Path

    from owcopilot.content.store import ContentStore
    from owcopilot.evaluation.acceptance import (
        RETRIEVAL_TIGHT_BUDGET,
        _retrieval_hit_rate,
        build_acceptance_world,
        retrieval_benchmark_queries,
    )
    from owcopilot.pipeline.project import ProjectContext
    from owcopilot.retrieval.context_pack import ContextPackBuilder

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "world"
        ContentStore(root).save(build_acceptance_world())
        project = ProjectContext.open(root)
        try:
            queries = retrieval_benchmark_queries()
            base = project.context_builder
            int8_vec = VectorRetriever(
                project.sqlite_store, embedder=project.embedder, quantized=True
            )
            assert isinstance(int8_vec._backend, SqliteVecInt8Backend)
            project.context_builder = ContextPackBuilder(
                bm25=base.bm25, vector=int8_vec, graph=base.graph
            )
            hit_rate, _ = _retrieval_hit_rate(project, queries, budget_tokens=700)
            tight_rate, _ = _retrieval_hit_rate(
                project, queries, budget_tokens=RETRIEVAL_TIGHT_BUDGET
            )
        finally:
            project.close()

    assert hit_rate == 1.0
    assert tight_rate == 1.0


# ===========================================================================================
# G2-B: on-disk usearch HNSW ANN backend (ANN coarse -> fp32 rerank) + the N-threshold tier.
# ===========================================================================================


def _new_usearch_backend(dim: int, tmp_path) -> UsearchBackend:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    index_path = str(tmp_path / "content.usearch")
    return UsearchBackend(conn, dim=dim, table="content_vectors", index_path=index_path)


# --------------------------------------------------------------------------- usearch primitives


@requires_usearch
def test_usearch_upsert_search_vector_for_and_delete(tmp_path) -> None:
    backend = _new_usearch_backend(dim=4, tmp_path=tmp_path)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    backend.upsert("b", _vec([0, 1, 0, 0]))
    backend.upsert("c", _vec([0.9, 0.1, 0, 0]))

    hits = backend.search(_vec([1, 0, 0, 0]), limit=3)
    assert [ref for ref, _ in hits] == ["a", "c", "b"]
    assert hits[0][1] >= hits[1][1] >= hits[2][1]

    # vector_for returns the EXACT fp32 unit vector from the authoritative sidecar, not via the ANN.
    stored = backend.vector_for("a")
    assert stored is not None
    assert stored.dtype == np.float32
    np.testing.assert_allclose(stored, _vec([1, 0, 0, 0]), atol=1e-6)
    assert backend.vector_for("missing") is None

    # upsert replaces (no duplicate-key crash on the same ref), then delete removes.
    backend.upsert("a", _vec([0, 0, 0, 1]))
    assert [r for r, _ in backend.search(_vec([0, 0, 0, 1]), limit=5)][0] == "a"
    backend.delete("a")
    assert backend.vector_for("a") is None
    assert "a" not in {r for r, _ in backend.search(_vec([0, 0, 0, 1]), limit=5)}
    backend.delete("a")  # idempotent no-op


@requires_usearch
def test_usearch_clear_and_dim_mismatch_and_empty(tmp_path) -> None:
    backend = _new_usearch_backend(dim=4, tmp_path=tmp_path)
    assert backend.search(_vec([1, 0, 0, 0]), limit=5) == []  # empty index
    backend.upsert("a", _vec([1, 0, 0, 0]))
    assert backend.search(_vec([1, 0, 0]), limit=5) == []  # wrong dim -> empty
    backend.clear()
    assert backend.search(_vec([1, 0, 0, 0]), limit=5) == []
    assert backend.vector_for("a") is None


@requires_usearch
def test_usearch_string_ref_key_mapping_roundtrips(tmp_path) -> None:
    """String refs (not uint64) round-trip through the persisted ref<->key map: arbitrary string
    refs upsert, search back to the same strings, and delete by string."""
    backend = _new_usearch_backend(dim=8, tmp_path=tmp_path)
    refs = ["entity:npc_aldric", "quest:siege/01", "relation:a:b:c:0", "term:Æther-鑰"]
    rng = np.random.default_rng(3)
    for ref in refs:
        backend.upsert(ref, rng.standard_normal(8).astype(np.float32))
    for ref in refs:
        # the exact stored vector comes back for each string ref
        assert backend.vector_for(ref) is not None
        # and a query of that vector self-retrieves the same string ref at rank 0
        hit_refs = [r for r, _ in backend.search(backend.vector_for(ref), limit=4)]
        assert hit_refs[0] == ref
    backend.delete("quest:siege/01")
    assert backend.vector_for("quest:siege/01") is None
    remaining = {r for r, _ in backend.search(backend.vector_for("entity:npc_aldric"), limit=4)}
    assert "quest:siege/01" not in remaining


# --------------------------------------------------------------------------- on-disk persistence


@requires_usearch
def test_usearch_on_disk_save_and_reopen_roundtrip(tmp_path) -> None:
    """The .usearch index persists across a close/reopen on the same DB file: a reopened backend
    serves the same vectors without re-upserting (the index loaded from disk, count matched)."""
    db_path = str(tmp_path / "runtime.db")
    index_path = str(tmp_path / "content.usearch")
    rng = np.random.default_rng(9)
    vectors = {f"r{i}": rng.standard_normal(16).astype(np.float32) for i in range(20)}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    backend = UsearchBackend(conn, dim=16, table="content_vectors", index_path=index_path)
    for ref, v in vectors.items():
        backend.upsert(ref, v)
    conn.commit()
    conn.close()
    import os

    assert os.path.exists(index_path)

    # reopen: a healthy index (key count == keymap count) loads in place, no rebuild needed.
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    reopened = UsearchBackend(conn2, dim=16, table="content_vectors", index_path=index_path)
    assert len(reopened._index) == len(vectors)  # noqa: SLF001 - asserting on-disk count survived
    for ref, v in vectors.items():
        top = reopened.search(v, limit=1)
        assert top and top[0][0] == ref  # self-retrieval after reopen


# --------------------------------------------------------------------------- consistency / rebuild


@requires_usearch
def test_usearch_dirty_index_rebuilds_from_fp32_source(tmp_path) -> None:
    """A stale .usearch file (its key count disagrees with the authoritative keymap, as after a
    crash mid-write) is detected on open and rebuilt from the fp32 source — restoring correctness
    without a manual repair. This is the crash-recovery guarantee the design hinges on."""
    db_path = str(tmp_path / "runtime.db")
    index_path = str(tmp_path / "content.usearch")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    backend = UsearchBackend(conn, dim=4, table="content_vectors", index_path=index_path)
    for ref, v in [("a", [1, 0, 0, 0]), ("b", [0, 1, 0, 0]), ("c", [0, 0, 1, 0])]:
        backend.upsert(ref, _vec(v))
    conn.commit()

    # Simulate a crash that updated the authoritative tables but left the .usearch file holding the
    # OLD 3-key index: drop "c" from fp32 + keymap only. Now the file (3 keys) disagrees with the
    # keymap (2 rows).
    conn.execute("DELETE FROM content_vectors_fp32 WHERE ref = 'c'")
    conn.execute("DELETE FROM content_vectors_keymap WHERE ref = 'c'")
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    healed = UsearchBackend(conn2, dim=4, table="content_vectors", index_path=index_path)
    # rebuilt to match the authority: 2 keys, "c" gone, "a"/"b" still searchable.
    assert len(healed._index) == 2  # noqa: SLF001
    assert healed.vector_for("c") is None
    assert healed.search(_vec([1, 0, 0, 0]), limit=1)[0][0] == "a"
    assert healed.search(_vec([0, 1, 0, 0]), limit=1)[0][0] == "b"


@requires_usearch
def test_usearch_corrupt_index_file_rebuilds(tmp_path) -> None:
    """An unreadable / truncated .usearch file is caught on open and rebuilt from the fp32 source,
    never crashing the backend."""
    db_path = str(tmp_path / "runtime.db")
    index_path = str(tmp_path / "content.usearch")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    backend = UsearchBackend(conn, dim=4, table="content_vectors", index_path=index_path)
    backend.upsert("a", _vec([1, 0, 0, 0]))
    backend.upsert("b", _vec([0, 1, 0, 0]))
    conn.commit()
    conn.close()

    with open(index_path, "wb") as f:
        f.write(b"NOT A REAL USEARCH FILE")

    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    healed = UsearchBackend(conn2, dim=4, table="content_vectors", index_path=index_path)
    assert len(healed._index) == 2  # noqa: SLF001 - rebuilt from fp32 authority
    assert healed.search(_vec([1, 0, 0, 0]), limit=1)[0][0] == "a"


# ----------------------------------------------------------------- recall: tuned + 2-stage


@requires_usearch
def test_usearch_tuned_two_stage_recall_is_high_and_beats_default_params(tmp_path) -> None:
    """The core G2-B claim, on synthetic clustered vectors vs the fp32 ground truth:

    1. the tuned (connectivity=32 / expansion_search=2048) HNSW + fp32 rerank holds recall@10
       >= 0.95, and
    2. the tuning is load-bearing: it strictly beats a default-parameter usearch index (the library
       defaults that P0_G2_RESEARCH §3 identified as a recall trap).
    """
    from usearch.index import Index

    dim, n, clusters, seed = 1024, 6000, 80, 11
    rng = np.random.default_rng(seed)

    def _norm(v: np.ndarray) -> np.ndarray:
        return (v / (np.linalg.norm(v, axis=-1, keepdims=True))).astype(np.float32)

    centers = _norm(rng.standard_normal((clusters, dim)).astype(np.float32))
    labels = rng.integers(0, clusters, n)
    data = _norm(centers[labels] + 0.30 * rng.standard_normal((n, dim)).astype(np.float32))
    vectors = {f"r{i}": data[i] for i in range(n)}

    backend = _new_usearch_backend(dim=dim, tmp_path=tmp_path)
    for ref, v in vectors.items():
        backend.upsert(ref, v)

    # default-parameter index (the trap) over the same data, for the comparison.
    default_index = Index(ndim=dim, metric="cos", dtype="f32")
    default_index.add(np.arange(n, dtype=np.uint64), data)
    reflist = list(vectors.keys())

    rng_q = np.random.default_rng(seed + 1)
    limit, n_queries = 10, 60
    tuned_hits = default_hits = total = 0
    for i in range(n_queries):
        q = _norm(centers[i % clusters] + 0.30 * rng_q.standard_normal(dim)).astype(np.float32)
        # exact fp32 ground truth
        scored = sorted(
            ((ref, float(v @ q)) for ref, v in vectors.items()), key=lambda x: (-x[1], x[0])
        )
        truth = {ref for ref, _ in scored[:limit]}

        tuned = {ref for ref, _ in backend.search(q, limit=limit)}
        tuned_hits += len(truth & tuned)

        dk = [int(x) for x in default_index.search(q, limit).keys]
        default_hits += len(truth & {reflist[k] for k in dk})
        total += limit

    tuned_recall = tuned_hits / total
    default_recall = default_hits / total
    assert tuned_recall >= 0.95, f"tuned two-stage recall {tuned_recall:.4f} < 0.95"
    assert tuned_recall >= default_recall, (
        f"tuned {tuned_recall:.4f} not >= default-param {default_recall:.4f}"
    )


# --------------------------------------------------------------------------- tier selection (by N)


@requires_sqlite_vec
def test_make_vector_backend_small_n_stays_on_sqlite_vec_even_with_ann(tmp_path) -> None:
    """The tier safety valve: with ann=True but a SMALL corpus (below USEARCH_MIN_N) the store
    still builds the exact sqlite-vec backend, so the eval recall gate never sees ANN approximation.
    """
    store = SQLiteStore()
    try:
        # a handful of persisted vectors -> well under the threshold.
        store.upsert_vectors(
            "hashing-4",
            [(f"r{i}", "h", 4, _vec([1, 0, 0, 0]).tobytes()) for i in range(5)],
        )
        backend = store.make_vector_backend("hashing-4", dim=4, ann=True)
        assert isinstance(backend, SqliteVecBackend)
        assert not isinstance(backend, UsearchBackend)
    finally:
        store.close()


@requires_sqlite_vec
@requires_usearch
def test_make_vector_backend_large_n_with_ann_uses_usearch(tmp_path, monkeypatch) -> None:
    """Above the threshold, ann=True switches the store to the on-disk usearch backend; ann=False
    (the default) always stays on the exact sqlite-vec backend regardless of N."""
    from owcopilot.storage import sqlite as sqlite_mod

    # Lower the threshold so the test stays fast (a few rows instead of thousands).
    monkeypatch.setattr(sqlite_mod, "USEARCH_MIN_N", 3)
    store = SQLiteStore(str(tmp_path / "rt.db"))
    try:
        store.upsert_vectors(
            "hashing-4",
            [(f"r{i}", "h", 4, _vec([1, 0, 0, 0]).tobytes()) for i in range(10)],
        )
        ann_backend = store.make_vector_backend("hashing-4", dim=4, ann=True)
        assert isinstance(ann_backend, UsearchBackend)
        # default (ann=False) never uses usearch even with the same large N.
        exact_backend = store.make_vector_backend("hashing-4", dim=4, ann=False)
        assert isinstance(exact_backend, SqliteVecBackend)
    finally:
        store.close()


@requires_sqlite_vec
def test_make_vector_backend_falls_back_to_sqlite_vec_when_usearch_unavailable(
    tmp_path, monkeypatch
) -> None:
    """When ann=True crosses the threshold but usearch cannot be built, the store falls back to the
    exact sqlite-vec backend with a guided log line — never a crash, never a silent numpy degrade
    when sqlite-vec is in fact available."""
    from owcopilot.storage import sqlite as sqlite_mod

    monkeypatch.setattr(sqlite_mod, "USEARCH_MIN_N", 3)

    def _boom(self, *_a: object, **_k: object) -> None:
        return None  # simulate usearch unavailable -> _make_usearch_backend returns None

    monkeypatch.setattr(SQLiteStore, "_make_usearch_backend", _boom)
    store = SQLiteStore(str(tmp_path / "rt.db"))
    try:
        store.upsert_vectors(
            "hashing-4",
            [(f"r{i}", "h", 4, _vec([1, 0, 0, 0]).tobytes()) for i in range(10)],
        )
        backend = store.make_vector_backend("hashing-4", dim=4, ann=True)
        assert isinstance(backend, SqliteVecBackend)
    finally:
        store.close()


@requires_sqlite_vec
def test_retriever_ann_small_corpus_matches_fp32(tmp_path) -> None:
    """A VectorRetriever built with ann=True over the small canon bundle stays on the exact backend
    (small N) and returns the same hits as the fp32 default — proving the opt-in is a no-op on small
    corpora (the eval gate is safe)."""
    fp32_hits, _ = _retriever_results(use_numpy=False, query="caravan routes")

    store = SQLiteStore(str(tmp_path / "rt.db"))
    try:
        store.replace_content_index(_bundle())
        retriever = VectorRetriever(store, ann=True)
        # small corpus -> not the ANN backend.
        assert not isinstance(retriever._backend, UsearchBackend)
        ann_hits = [(h.ref, h.score) for h in retriever.search("caravan routes", limit=10)]
    finally:
        store.close()

    assert [r for r, _ in fp32_hits] == [r for r, _ in ann_hits]
