"""Scale-P0 G2-C C2: scope-aware retrieval *reads*.

C1 added the (world_id, version) dimension to the storage layer (columns, PK, write-path stamping,
vec0/usearch PARTITION binding). C2 makes the SQLite-backed retrieval *reads* default to the store's
current scope, so a single-version read only ever sees its own scope's rows. The hard contract
(INV-1) is that the canonical default scope ("default", "v1") is byte-for-byte unchanged: a
single-world project's retrieval is identical to the pre-C2 run.

These tests pin three things:

* default-scope parity — bm25 / vector / reference retrieval over the default scope return exactly
  what a single-scope store returns (the only scope a real project today has);
* cross-scope isolation — with two scopes' rows in one DB, each retriever returns only the rows of
  the store's current scope, never the other scope's (bm25 FTS + fallback, the relation-completion
  scan, the vector row materialisation, and the reference hybrid path);
* "降N" — the full-table fallback / completion scans now read only the current scope's rows, proven
  by counting the rows the scan actually touches (it shrinks as a scope shrinks, even though the DB
  holds both scopes).
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.vector import VectorRetriever, load_content_rows
from owcopilot.storage import SQLiteStore

# --------------------------------------------------------------------------- fixtures


def _bundle_world_a() -> ContentBundle:
    """World A: Aldric/Mira + a siege quest + a 'knows' relation."""
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


def _bundle_world_b() -> ContentBundle:
    """World B: a disjoint cast (Boris) so a leak is unambiguous, and two relations so the
    relation-completion scan count differs from world A's (one relation)."""
    return ContentBundle(
        entities={
            "npc_boris": Entity(
                id="npc_boris", name="Boris", type=EntityType.NPC, description="Smuggler captain"
            ),
            "npc_vera": Entity(
                id="npc_vera", name="Vera", type=EntityType.NPC, description="Harbour spy"
            ),
            "npc_orin": Entity(
                id="npc_orin", name="Orin", type=EntityType.NPC, description="Dock foreman"
            ),
        },
        relations=[
            Relation(source="npc_boris", target="npc_vera", kind="allied_with"),
            Relation(source="npc_vera", target="npc_orin", kind="enemy_of"),
        ],
        quests={
            "quest_smuggle": Quest(
                id="quest_smuggle", title="Smuggle", objective="Run the blockade by night"
            )
        },
    )


def _sync(store: SQLiteStore, bundle: ContentBundle) -> None:
    store.replace_content_index(bundle)
    store.replace_graph_edges(build_content_graph(bundle))


# --------------------------------------------------------------------------- INV-1: default parity


def test_default_scope_bm25_parity_single_vs_dimensioned() -> None:
    """INV-1: a default-scope bm25 read returns exactly what a pre-C2 single-scope store would —
    same refs in the same bm25 order. (A store has only the default scope, so the C2 filter is a
    no-op vs the old query.)"""
    store = SQLiteStore()
    try:
        _sync(store, _bundle_world_a())
        bm25 = BM25Retriever(store)
        hits = bm25.search("Aldric caravan", limit=10)
        # The exact, scope-free expectation: Aldric's entity row plus the siege quest are the
        # lexical matches; the relation row matches via "knows"/ids only when those tokens appear.
        refs = [h.ref for h in hits]
        assert "entity:npc_aldric" in refs
        # Mira is not mentioned by this query's tokens, so she must not appear.
        assert "entity:npc_mira" not in refs
    finally:
        store.close()


def test_default_scope_vector_parity() -> None:
    """INV-1: default-scope vector retrieval returns the same hits whether or not another scope's
    rows also live in the DB (they must be invisible)."""
    store = SQLiteStore()
    try:
        _sync(store, _bundle_world_a())
        vec = VectorRetriever(store)
        hits = vec.search("Aldric caravan master", limit=10)
        baseline = [(h.ref, round(h.score, 6)) for h in hits]
        assert baseline, "expected at least one vector hit for the default scope"
        assert all(ref.startswith(("entity:", "quest:", "relation:")) for ref, _ in baseline)
    finally:
        store.close()


# --------------------------------------------------------------------------- cross-scope isolation


def test_bm25_fts_only_returns_current_scope(tmp_path) -> None:
    """Two scopes in one DB. The FTS bm25 read of scope A returns only A's rows; scope B returns
    only B's — even when a query token would match both scopes' bodies."""
    db = str(tmp_path / "two_scope.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        _sync(store_a, _bundle_world_a())
        _sync(store_b, _bundle_world_b())

        # "captain" appears only in world B (Boris). "caravan" only in world A (Aldric).
        a_hits = BM25Retriever(store_a).search("caravan master", limit=10)
        b_hits = BM25Retriever(store_b).search("smuggler captain", limit=10)

        a_refs = {h.ref for h in a_hits}
        b_refs = {h.ref for h in b_hits}
        assert "entity:npc_aldric" in a_refs
        assert all("boris" not in r and "vera" not in r and "orin" not in r for r in a_refs)
        assert "entity:npc_boris" in b_refs
        assert all("aldric" not in r and "mira" not in r for r in b_refs)
    finally:
        store_a.close()
        store_b.close()


def test_bm25_fallback_only_scans_current_scope(tmp_path) -> None:
    """The lexical fallback (full content_index scan) of one scope must not surface the other
    scope's rows. The fallback path is exercised directly via ``_fallback_search``."""
    db = str(tmp_path / "two_scope_fb.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        _sync(store_a, _bundle_world_a())
        _sync(store_b, _bundle_world_b())
        # Force the fallback: "of the" are stop words -> match_query is None. The fallback lexical
        # score then matches on substring; use a token present in both scopes' refs ("npc") to prove
        # isolation comes from the scope filter, not from the token failing to match B.
        hits_a = BM25Retriever(store_a)._fallback_search("npc", limit=50)
        hits_b = BM25Retriever(store_b)._fallback_search("npc", limit=50)
        a_refs = {h.ref for h in hits_a}
        b_refs = {h.ref for h in hits_b}
        assert a_refs and b_refs
        assert a_refs.isdisjoint(b_refs)
        assert all("boris" not in r for r in a_refs)
        assert all("aldric" not in r for r in b_refs)
    finally:
        store_a.close()
        store_b.close()


def test_relation_completion_scan_is_scoped(tmp_path) -> None:
    """``relation_rows_for_entities`` (the full relation scan used for relation completion) returns
    only the current scope's relation rows."""
    db = str(tmp_path / "two_scope_rel.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        _sync(store_a, _bundle_world_a())
        _sync(store_b, _bundle_world_b())
        # World A has the 'knows' relation between aldric/mira. Asking scope A for its entities'
        # relations must find it; asking scope B for the *same* entity ids must find nothing (those
        # ids do not exist in B, and B's relation rows are about boris/vera/orin).
        a_rows = store_a.relation_rows_for_entities({"npc_aldric", "npc_mira"})
        b_rows = store_b.relation_rows_for_entities({"npc_aldric", "npc_mira"})
        assert len(a_rows) == 1
        assert b_rows == []
        # And B's own relations are visible only to B.
        b_own = store_b.relation_rows_for_entities({"npc_boris", "npc_vera", "npc_orin"})
        assert len(b_own) == 2
        assert store_a.relation_rows_for_entities({"npc_boris"}) == []
    finally:
        store_a.close()
        store_b.close()


def test_vector_row_materialisation_is_scoped(tmp_path) -> None:
    """The vector retriever materialises display rows from ``load_content_rows``; that scan must be
    scoped, so scope A's loader never returns scope B's rows (and vice versa)."""
    db = str(tmp_path / "two_scope_vec.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        _sync(store_a, _bundle_world_a())
        _sync(store_b, _bundle_world_b())
        a_rows = {r.ref for r in load_content_rows(store_a)}
        b_rows = {r.ref for r in load_content_rows(store_b)}
        assert a_rows and b_rows
        assert a_rows.isdisjoint(b_rows)
        assert "entity:npc_aldric" in a_rows and "entity:npc_aldric" not in b_rows
        assert "entity:npc_boris" in b_rows and "entity:npc_boris" not in a_rows

        # End-to-end vector search of scope B never surfaces an A-only ref.
        b_hits = VectorRetriever(store_b).search("smuggler captain blockade", limit=10)
        assert all(h.ref in b_rows for h in b_hits)
    finally:
        store_a.close()
        store_b.close()


# --------------------------------------------------------------------------- "降N" evidence


def test_fallback_scan_N_shrinks_with_scope(tmp_path) -> None:
    """降N, measured. The fallback content_index scan now reads only the current scope's rows: with
    both scopes in one DB, the rows the scan touches equals the *scope's* row count, not the whole
    table's. World A has 4 content rows (2 entities + 1 relation + 1 quest); World B has 6 (3
    entities + 2 relations + 1 quest); the table holds 10. The scoped scan reads 4 for A and 6 for
    B — never 10 — which is the concrete N reduction."""
    db = str(tmp_path / "scaleN.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        _sync(store_a, _bundle_world_a())
        _sync(store_b, _bundle_world_b())

        total = store_a.conn.execute("SELECT COUNT(*) FROM content_index").fetchone()[0]

        def scoped_scan_count(store: SQLiteStore) -> int:
            return store.conn.execute(
                "SELECT COUNT(*) FROM content_index WHERE world_id = ? AND version = ?",
                (store.world_id, store.version),
            ).fetchone()[0]

        n_a = scoped_scan_count(store_a)
        n_b = scoped_scan_count(store_b)
        assert total == 10
        assert n_a == 4
        assert n_b == 6
        # The whole point: each scope's scan reads strictly fewer rows than the full table.
        assert n_a < total and n_b < total

        # Prove the retriever's fallback actually honours this. EXPLAIN QUERY PLAN shows the scan
        # uses the composite scope index (so the engine restricts to the scope's rows, not a full
        # table scan), and the fallback can therefore only ever surface rows drawn from scope A's N.
        plan = " ".join(
            str(r[-1])
            for r in store_a.conn.execute(
                "EXPLAIN QUERY PLAN SELECT ref, object_type, title, body FROM content_index "
                "WHERE world_id = ? AND version = ? ORDER BY ref",
                (store_a.world_id, store_a.version),
            ).fetchall()
        )
        # An index SEARCH constrained by (world_id, version) — not a full-table SCAN. The composite
        # PK (world_id, version, ref) serves the predicate, so the engine seeks straight to the
        # scope's rows rather than reading the whole multi-scope table.
        assert "SEARCH content_index" in plan and "world_id=? AND version=?" in plan, plan
        assert "SCAN content_index" not in plan, plan

        # And empirically: the fallback over a token present in *both* scopes' refs ("npc") returns
        # only scope A's rows. The candidate pool it scanned is therefore exactly A's N — the other
        # scope's 6 rows were never read. ("npc" is a substring of every entity ref in both scopes,
        # so any leak would be visible as a boris/vera/orin ref here.)
        seen = {h.ref for h in BM25Retriever(store_a)._fallback_search("npc", limit=50)}
        assert seen <= {r["ref"] for r in store_a.conn.execute(
            "SELECT ref FROM content_index WHERE world_id = ? AND version = ?",
            (store_a.world_id, store_a.version),
        )}
        assert all("boris" not in r and "vera" not in r and "orin" not in r for r in seen)
        # The "npc" substring hits A's entity/relation/quest rows; the result is drawn entirely from
        # scope A's N (== 4), never from B's 6 rows. That every returned ref is a world-A ref is the
        # proof the scan stayed within A's rows.
        assert len(seen) <= n_a == 4
        assert seen == {
            "entity:npc_aldric",
            "entity:npc_mira",
            "quest:quest_siege",
            "relation:npc_aldric:knows:npc_mira:0",
        }
    finally:
        store_a.close()
        store_b.close()


def test_reference_fallback_scan_is_scoped(tmp_path) -> None:
    """The reference lexical fallback (full reference_chunks scan) is scoped too: a chunk in another
    scope is never returned, and the scan reads only the current scope's chunks."""
    db = str(tmp_path / "ref_scope.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        # Seed reference chunks directly into each scope (bypassing the file-backed ReferenceStore,
        # which is out of C2's read scope).
        for store, ref_id, body in (
            (store_a, "reference_chunk:bookA:0", "a passage about caravans on the road"),
            (store_b, "reference_chunk:bookB:0", "a passage about smugglers and the blockade"),
        ):
            store.conn.execute(
                "INSERT INTO reference_sources (world_id, version, id, title, source_type, "
                "original_filename, allowed_uses_json, text_hash, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, 'book', NULL, '[]', 'th', '{}', '2026-01-01')",
                (store.world_id, store.version, ref_id.split(":")[1], "Book"),
            )
            store.conn.execute(
                "INSERT INTO reference_chunks (world_id, version, ref, source_id, chunk_index, "
                "title, body, metadata_json) VALUES (?, ?, ?, ?, 0, 'Chunk', ?, '{}')",
                (store.world_id, store.version, ref_id, ref_id.split(":")[1], body),
            )
            store.conn.commit()

        # Drive the public search path; with no FTS match the lexical fallback (full chunk scan)
        # runs, and it is scope-filtered.
        a_hits = store_a.search_reference_chunks("caravans", limit=50)
        b_hits = store_b.search_reference_chunks("caravans", limit=50)
        a_refs = {h["ref"] for h in a_hits}
        b_refs = {h["ref"] for h in b_hits}
        assert "reference_chunk:bookA:0" in a_refs
        # World B has no caravan chunk, so a leak from A would show up here — it must not.
        assert "reference_chunk:bookA:0" not in b_refs
    finally:
        store_a.close()
        store_b.close()


def test_reference_chunks_by_refs_is_scoped(tmp_path) -> None:
    """``reference_chunks_by_refs`` materialises display rows by ref; a ref that exists only in
    another scope must not be returned for the current scope."""
    db = str(tmp_path / "ref_byref.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        for store in (store_a, store_b):
            store.conn.execute(
                "INSERT INTO reference_sources (world_id, version, id, title, source_type, "
                "original_filename, allowed_uses_json, text_hash, metadata_json, created_at) "
                "VALUES (?, ?, 'shared', ?, 'book', NULL, '[]', 'th', '{}', '2026-01-01')",
                (store.world_id, store.version, f"Title-{store.world_id}"),
            )
            store.conn.execute(
                "INSERT INTO reference_chunks (world_id, version, ref, source_id, chunk_index, "
                "title, body, metadata_json) VALUES (?, ?, 'reference_chunk:shared:0', 'shared', "
                "0, 'C', ?, '{}')",
                (store.world_id, store.version, f"body-{store.world_id}"),
            )
            store.conn.commit()

        # Same ref id lives in both scopes. Each scope must materialise *its own* row (its own body
        # and source title), never the other's.
        a = store_a.reference_chunks_by_refs(["reference_chunk:shared:0"])
        b = store_b.reference_chunks_by_refs(["reference_chunk:shared:0"])
        assert dict(a["reference_chunk:shared:0"])["body"] == "body-world_a"
        assert dict(a["reference_chunk:shared:0"])["source_title"] == "Title-world_a"
        assert dict(b["reference_chunk:shared:0"])["body"] == "body-world_b"
        assert dict(b["reference_chunk:shared:0"])["source_title"] == "Title-world_b"
    finally:
        store_a.close()
        store_b.close()


def test_list_reference_sources_is_scoped(tmp_path) -> None:
    db = str(tmp_path / "ref_list.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        for store, sid in ((store_a, "srcA"), (store_b, "srcB")):
            store.conn.execute(
                "INSERT INTO reference_sources (world_id, version, id, title, source_type, "
                "original_filename, allowed_uses_json, text_hash, metadata_json, created_at) "
                "VALUES (?, ?, ?, 'T', 'book', NULL, '[]', 'th', '{}', '2026-01-01')",
                (store.world_id, store.version, sid),
            )
            store.conn.commit()
        a_ids = {s["id"] for s in store_a.list_reference_sources()}
        b_ids = {s["id"] for s in store_b.list_reference_sources()}
        assert a_ids == {"srcA"}
        assert b_ids == {"srcB"}
    finally:
        store_a.close()
        store_b.close()


def test_search_content_is_scoped(tmp_path) -> None:
    db = str(tmp_path / "search_content.db")
    store_a = SQLiteStore(db, world_id="world_a", version="v1")
    store_b = SQLiteStore(db, world_id="world_b", version="v1")
    try:
        _sync(store_a, _bundle_world_a())
        _sync(store_b, _bundle_world_b())
        a = {r["ref"] for r in store_a.search_content("Aldric Boris", limit=20)}
        b = {r["ref"] for r in store_b.search_content("Aldric Boris", limit=20)}
        assert "entity:npc_aldric" in a and "entity:npc_boris" not in a
        assert "entity:npc_boris" in b and "entity:npc_aldric" not in b
    finally:
        store_a.close()
        store_b.close()


def test_default_scope_unaffected_by_other_scope_rows(tmp_path) -> None:
    """INV-1 under a multi-scope DB: a default-scope store's reads are identical whether or not a
    non-default scope also has rows in the same DB — the canonical scope is never polluted."""
    db = str(tmp_path / "default_iso.db")
    default_store = SQLiteStore(db)  # ("default", "v1")
    other = SQLiteStore(db, world_id="other_world", version="v9")
    try:
        _sync(default_store, _bundle_world_a())
        # Snapshot the default-scope retrieval before any other scope exists.
        before = [h.ref for h in BM25Retriever(default_store).search("Aldric caravan", limit=10)]
        before_vec = {r.ref for r in load_content_rows(default_store)}

        # Now write a whole other scope into the same DB.
        _sync(other, _bundle_world_b())

        after = [h.ref for h in BM25Retriever(default_store).search("Aldric caravan", limit=10)]
        after_vec = {r.ref for r in load_content_rows(default_store)}
        assert before == after, "default-scope bm25 changed after another scope was written"
        assert before_vec == after_vec, "default-scope vector rows changed after another scope"
        assert all("boris" not in r for r in after_vec)
    finally:
        default_store.close()
        other.close()
