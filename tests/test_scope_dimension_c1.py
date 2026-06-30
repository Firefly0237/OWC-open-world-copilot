"""Scale-P0 G2-C C1: the (world_id, version) scope dimension — schema, migration, write path.

C1 only *adds* the dimension; the hard contract is that the canonical default scope ("default",
"v1") behaves exactly as before (no scope-aware filtering yet — that is C2). These tests pin:

* migration — a legacy runtime DB with no scope columns upgrades in place, existing rows land in the
  default scope, and nothing is lost;
* default-scope parity — replace_*/vector retrieval over the default scope match a pre-scope run;
* the scope columns/PARTITION KEY actually carry a non-default scope to its own rows/partition.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.retrieval.vector import VectorRetriever
from owcopilot.retrieval.vector_backend import (
    SqliteVecBackend,
    SqliteVecInt8Backend,
    sqlite_vec_available,
)
from owcopilot.storage import SQLiteStore

requires_sqlite_vec = pytest.mark.skipif(
    not sqlite_vec_available(), reason="sqlite-vec extension not installed"
)

_SCOPE_TABLES = (
    "content_index",
    "content_vectors",
    "reference_vectors",
    "graph_edges",
    "reference_sources",
    "reference_chunks",
)


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
        relations=[Relation(source="npc_aldric", target="npc_mira", kind="knows")],
        quests={
            "quest_siege": Quest(
                id="quest_siege", title="Siege", objective="Defend the northern wall"
            )
        },
    )


# --------------------------------------------------------------------------- INV-1: scope is NOT
# a content field (byte-for-byte parity with the pre-scope baseline 4013691)


def test_scope_is_absent_from_content_serialization() -> None:
    """INV-1: scope is a storage dimension, never a content field. No content object's model_dump
    carries world_id/version — so content_hash / snapshot payloads / ContentStore.save are
    byte-identical to the pre-scope baseline."""
    for obj in (
        Entity(id="x", name="X", type=EntityType.NPC),
        Quest(id="q", title="Q"),
        Relation(source="a", target="b", kind="knows"),
    ):
        dumped = obj.model_dump(mode="json")  # full dump (not exclude_none) — must still be clean
        assert "world_id" not in dumped, f"{type(obj).__name__} leaks world_id into content"
        assert "version" not in dumped or type(obj) is Entity, (
            f"{type(obj).__name__} leaks scope version into content"
        )
    # Entity keeps its own free-text content ``version`` (None default, omitted on exclude_none).
    e = Entity(id="x", name="X", type=EntityType.NPC)
    assert e.version is None
    assert "version" not in e.model_dump(mode="json", exclude_none=True)
    assert Entity(id="y", name="Y", type=EntityType.NPC, version="1.3").version == "1.3"


def test_content_hash_matches_pre_scope_baseline_4013691() -> None:
    """INV-1 hard lock: content_hash of entity/quest/relation equals the literal values computed on
    baseline commit 4013691 (G2-B, the last commit before this unit). Captured by running
    content_hash on that worktree; pinning them here fails loudly if scope ever re-leaks into the
    content serialization and silently shifts every hash."""
    e = Entity(id="x", name="X", type=EntityType.NPC, description="d")
    q = Quest(id="q", title="Q", objective="o")
    r = Relation(source="a", target="b", kind="knows")
    assert content_hash(e) == (
        "fd212c3eb828986069b169b7eda47218a387a4d3189ceb565ee0e003bc700032"
    )
    assert content_hash(q) == (
        "2b0b47ec093199872e5e3adfdef0a70a25411c4fde889d953cc0aef7e0e56dfe"
    )
    assert content_hash(r) == (
        "66ae7df11e65921c18719874fdd7aa8f47e1ac85d5acd0cac1099fa3add16a57"
    )


# --------------------------------------------------------------------------- schema


def test_scope_columns_exist_on_all_indexed_tables() -> None:
    store = SQLiteStore()
    try:
        for table in _SCOPE_TABLES:
            cols = {row["name"] for row in store.conn.execute(f"PRAGMA table_info({table})")}
            assert "world_id" in cols, f"{table} missing world_id"
            assert "version" in cols, f"{table} missing version"
        # the composite scope indexes exist too.
        idx = {
            row["name"]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        for table in _SCOPE_TABLES:
            assert f"idx_{table}_scope" in idx
    finally:
        store.close()


# --------------------------------------------------------------------------- migration


def _build_legacy_db(path: str) -> None:
    """A pre-C1 runtime DB: the indexed tables exist *without* the scope columns, with one row each.

    Mirrors the schema before this unit so the in-place upgrade path is exercised against real
    legacy rows (the migration must back-fill them to the default scope, not drop them)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE content_index (
            ref TEXT PRIMARY KEY, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, row_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE content_vectors (
            ref TEXT NOT NULL, model_id TEXT NOT NULL, text_hash TEXT NOT NULL,
            dim INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (ref, model_id)
        );
        CREATE TABLE reference_vectors (
            ref TEXT NOT NULL, model_id TEXT NOT NULL, text_hash TEXT NOT NULL,
            dim INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (ref, model_id)
        );
        CREATE TABLE graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL,
            kind TEXT NOT NULL, edge_type TEXT NOT NULL, valid_from INTEGER, valid_until INTEGER,
            edge_fingerprint TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE reference_sources (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, source_type TEXT NOT NULL,
            original_filename TEXT, allowed_uses_json TEXT NOT NULL, text_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE reference_chunks (
            ref TEXT PRIMARY KEY, source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, metadata_json TEXT NOT NULL
        );
        INSERT INTO content_index (ref, object_type, object_id, title, body, row_hash)
            VALUES ('entity:legacy', 'entity', 'legacy', 'Legacy', 'a legacy body', 'h0');
        INSERT INTO content_vectors (ref, model_id, text_hash, dim, vector)
            VALUES ('entity:legacy', 'hashing-4', 'th', 4, X'00000000');
        INSERT INTO graph_edges (source, target, kind, edge_type, edge_fingerprint)
            VALUES ('a', 'b', 'knows', 'relation', 'fp0');
        INSERT INTO reference_sources (id, title, source_type, original_filename,
            allowed_uses_json, text_hash, metadata_json, created_at)
            VALUES ('src0', 'Src', 'book', NULL, '[]', 'th', '{}', '2026-01-01');
        INSERT INTO reference_chunks (ref, source_id, chunk_index, title, body, metadata_json)
            VALUES ('reference_chunk:src0:0', 'src0', 0, 'Chunk', 'chunk body', '{}');
        """
    )
    conn.commit()
    conn.close()


def test_legacy_db_upgrades_in_place_and_backfills_default_scope(tmp_path) -> None:
    db = str(tmp_path / "legacy.db")
    _build_legacy_db(db)

    store = SQLiteStore(db)  # opening runs initialize() -> migration
    try:
        for table in _SCOPE_TABLES:
            cols = {row["name"] for row in store.conn.execute(f"PRAGMA table_info({table})")}
            assert {"world_id", "version"} <= cols, f"{table} not migrated"

        # the legacy rows survived and were back-filled to the canonical scope (no data loss).
        row = store.conn.execute(
            "SELECT world_id, version, title FROM content_index WHERE ref = 'entity:legacy'"
        ).fetchone()
        assert row is not None
        assert (row["world_id"], row["version"], row["title"]) == ("default", "v1", "Legacy")

        for table, where in (
            ("content_vectors", "ref = 'entity:legacy'"),
            ("graph_edges", "edge_fingerprint = 'fp0'"),
            ("reference_sources", "id = 'src0'"),
            ("reference_chunks", "ref = 'reference_chunk:src0:0'"),
        ):
            r = store.conn.execute(
                f"SELECT world_id, version FROM {table} WHERE {where}"  # noqa: S608 - test fixture
            ).fetchone()
            assert r is not None and (r["world_id"], r["version"]) == ("default", "v1")
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path) -> None:
    """Re-opening an already-migrated DB does not error or duplicate rows."""
    db = str(tmp_path / "legacy.db")
    _build_legacy_db(db)
    SQLiteStore(db).close()  # first migration
    store = SQLiteStore(db)  # second open: columns already present
    try:
        count = store.conn.execute("SELECT COUNT(*) FROM content_index").fetchone()[0]
        assert count == 1  # not duplicated
    finally:
        store.close()


# --------------------------------------------------------------------------- write-path stamping


def test_replace_writes_stamp_the_store_scope() -> None:
    """A store opened on a non-default scope stamps its rows with that scope; the default-scope
    store stamps ('default','v1'). Same DB, two scopes, no cross-contamination."""
    store = SQLiteStore(world_id="w2", version="v3")
    try:
        bundle = _bundle()
        store.replace_content_index(bundle)
        store.replace_graph_edges(build_content_graph(bundle))
        rows = store.conn.execute(
            "SELECT DISTINCT world_id, version FROM content_index"
        ).fetchall()
        assert {(r["world_id"], r["version"]) for r in rows} == {("w2", "v3")}
        edges = store.conn.execute(
            "SELECT DISTINCT world_id, version FROM graph_edges"
        ).fetchall()
        assert {(r["world_id"], r["version"]) for r in edges} == {("w2", "v3")}
    finally:
        store.close()


def test_default_scope_write_is_canonical() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        rows = store.conn.execute(
            "SELECT DISTINCT world_id, version FROM content_index"
        ).fetchall()
        assert {(r["world_id"], r["version"]) for r in rows} == {("default", "v1")}
    finally:
        store.close()


def test_upsert_vectors_stamps_scope() -> None:
    store = SQLiteStore(world_id="w2", version="v1")
    try:
        store.upsert_vectors(
            "hashing-4", [("r0", "h", 4, np.zeros(4, dtype=np.float32).tobytes())]
        )
        r = store.conn.execute(
            "SELECT world_id, version FROM content_vectors WHERE ref = 'r0'"
        ).fetchone()
        assert (r["world_id"], r["version"]) == ("w2", "v1")
        # get_vectors is scope-filtered: the default-scope view does not see the w2 row.
        default_store = SQLiteStore(store.path, world_id="default", version="v1")
        try:
            assert default_store.get_vectors("hashing-4") == {}
            assert "r0" in store.get_vectors("hashing-4")
        finally:
            default_store.close()
    finally:
        store.close()


# --------------------------------------------------------------------------- INV-2: cross-scope
# write isolation (a write to one scope must not read/update/delete another scope's rows)


def test_cross_scope_write_does_not_delete_other_scope_content(tmp_path) -> None:
    """INV-2 hard lock — the case the original test could not catch (it only wrote a single scope to
    a fresh DB). Write the default scope, then open a *different* scope on the SAME db and write
    different content: the default scope's rows must SURVIVE (replace_* must not prune across
    scopes)."""
    db = str(tmp_path / "rt.db")
    s1 = SQLiteStore(db, world_id="default", version="v1")
    try:
        s1.replace_content_index(
            ContentBundle(entities={"a": Entity(id="a", name="A", type=EntityType.NPC)})
        )
    finally:
        s1.close()

    s2 = SQLiteStore(db, world_id="w2", version="v1")
    try:
        s2.replace_content_index(
            ContentBundle(entities={"b": Entity(id="b", name="B", type=EntityType.NPC)})
        )
        rows = {
            (r["world_id"], r["ref"])
            for r in s2.conn.execute("SELECT world_id, ref FROM content_index")
        }
    finally:
        s2.close()
    # both scopes' rows coexist; the default scope's entity:a was NOT deleted by the w2 write.
    assert ("default", "entity:a") in rows
    assert ("w2", "entity:b") in rows


def test_cross_scope_graph_edges_isolation(tmp_path) -> None:
    """INV-2 for graph_edges: a write to scope w2 must not delete default scope's edges."""
    db = str(tmp_path / "rt.db")
    b1 = ContentBundle(
        entities={
            "a": Entity(id="a", name="A", type=EntityType.NPC),
            "b": Entity(id="b", name="B", type=EntityType.NPC),
        },
        relations=[Relation(source="a", target="b", kind="knows")],
    )
    s1 = SQLiteStore(db, world_id="default", version="v1")
    try:
        s1.replace_graph_edges(build_content_graph(b1))
    finally:
        s1.close()
    s2 = SQLiteStore(db, world_id="w2", version="v1")
    try:
        s2.replace_graph_edges(build_content_graph(b1))  # same edges, different scope
        counts = {
            (r["world_id"], r["c"])
            for r in s2.conn.execute(
                "SELECT world_id, COUNT(*) AS c FROM graph_edges GROUP BY world_id"
            )
        }
    finally:
        s2.close()
    # the default scope's edge survived (same fingerprint exists once per scope, not globally).
    assert ("default", 1) in counts
    assert ("w2", 1) in counts


def test_same_ref_coexists_across_scopes_in_authoritative_pk(tmp_path) -> None:
    """The authoritative PK is (world_id, version, ref): the SAME ref persists independently in two
    scopes (proving the PK is composite, not single-column)."""
    db = str(tmp_path / "rt.db")
    bundle = ContentBundle(entities={"a": Entity(id="a", name="A", type=EntityType.NPC)})
    for wid in ("default", "w2"):
        s = SQLiteStore(db, world_id=wid, version="v1")
        try:
            s.replace_content_index(bundle)
        finally:
            s.close()
    s = SQLiteStore(db)
    try:
        rows = [
            (r["world_id"], r["ref"])
            for r in s.conn.execute(
                "SELECT world_id, ref FROM content_index WHERE ref = 'entity:a' ORDER BY world_id"
            )
        ]
    finally:
        s.close()
    assert rows == [("default", "entity:a"), ("w2", "entity:a")]  # one row per scope, same ref


# --------------------------------------------------------------------------- version_registry CRUD


def test_version_registry_crud() -> None:
    store = SQLiteStore()
    try:
        assert store.get_version("default", "v1") is None
        store.register_version("default", "v1")
        store.register_version("default", "v2", base_version="v1")
        store.register_version("w2", "v1")

        row = store.get_version("default", "v2")
        assert row is not None
        assert row["base_version"] == "v1"
        assert row["world_id"] == "default" and row["version"] == "v2"
        assert len(row["created_at"]) > 0

        assert {(v["world_id"], v["version"]) for v in store.list_versions()} == {
            ("default", "v1"),
            ("default", "v2"),
            ("w2", "v1"),
        }
        assert {v["version"] for v in store.list_versions("default")} == {"v1", "v2"}

        # upsert: re-register updates base_version, does not duplicate.
        store.register_version("default", "v2", base_version="v1b")
        assert store.get_version("default", "v2")["base_version"] == "v1b"
        assert len(store.list_versions("default")) == 2

        store.delete_version("default", "v2")
        assert store.get_version("default", "v2") is None
        assert len(store.list_versions("default")) == 1
    finally:
        store.close()


def test_version_registry_table_exists() -> None:
    store = SQLiteStore()
    try:
        cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(version_registry)")}
        assert {"world_id", "version", "base_version", "created_at"} <= cols
    finally:
        store.close()


# --------------------------------------------------------------------------- default-scope parity


@requires_sqlite_vec
@pytest.mark.parametrize("query", ["caravan routes", "ferry across the river", "defend the wall"])
def test_default_scope_vector_retrieval_unchanged(query: str) -> None:
    """Vector retrieval over the default scope returns the same hits/scores as a store with no
    explicit scope — the property the acceptance recall gate depends on (C1 a no-op by default)."""

    def _run(store: SQLiteStore) -> list[tuple[str, float]]:
        store.replace_content_index(_bundle())
        retriever = VectorRetriever(store)
        return [(h.ref, h.score) for h in retriever.search(query, limit=10)]

    a = SQLiteStore()  # implicit default scope
    b = SQLiteStore(world_id="default", version="v1")  # explicit default scope
    try:
        ra, rb = _run(a), _run(b)
    finally:
        a.close()
        b.close()
    assert [r for r, _ in ra] == [r for r, _ in rb]
    for (r1, s1), (r2, s2) in zip(ra, rb, strict=True):
        assert r1 == r2 and np.float32(s1) == np.float32(s2)


# --------------------------------------------------------------------------- vec0 PARTITION KEY


@requires_sqlite_vec
def test_vec0_partition_key_isolates_scopes() -> None:
    """Two scopes write through the *same* DB connection into one vec0 table; each scope's search
    only sees its own partition's vectors (the PARTITION KEY is real, not cosmetic)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    a = SqliteVecBackend(conn, dim=4, table="content_vec", world_id="default", version="v1")
    b = SqliteVecBackend(conn, dim=4, table="content_vec", world_id="w2", version="v1")
    qa = np.asarray([1, 0, 0, 0], dtype=np.float32)
    a.upsert("only_in_default", qa)
    b.upsert("only_in_w2", qa)

    da = a.search(qa, limit=5)
    db = b.search(qa, limit=5)
    assert [r for r, _ in da] == ["only_in_default"]
    assert [r for r, _ in db] == ["only_in_w2"]
    # vector_for is scope-scoped too: a default backend cannot see the w2 ref and vice versa.
    assert a.vector_for("only_in_w2") is None
    assert b.vector_for("only_in_default") is None
    conn.close()


@requires_sqlite_vec
def test_int8_vec0_partition_key_isolates_scopes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    a = SqliteVecInt8Backend(
        conn, dim=4, table="content_vec_i8", world_id="default", version="v1"
    )
    b = SqliteVecInt8Backend(conn, dim=4, table="content_vec_i8", world_id="w2", version="v1")
    q = np.asarray([1, 0, 0, 0], dtype=np.float32)
    a.upsert("only_in_default", q)
    b.upsert("only_in_w2", q)
    assert [r for r, _ in a.search(q, limit=5)] == ["only_in_default"]
    assert [r for r, _ in b.search(q, limit=5)] == ["only_in_w2"]
    assert a.vector_for("only_in_w2") is None
    conn.close()
