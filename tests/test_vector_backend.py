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
    sqlite_vec_available,
)
from owcopilot.storage import SQLiteStore

requires_sqlite_vec = pytest.mark.skipif(
    not sqlite_vec_available(), reason="sqlite-vec extension not installed"
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
