"""Parity + correctness tests for the P0 #2a incremental replace_* sync.

The hard requirement: the incremental path (diff -> upsert changed + prune removed, in a single
transaction) must leave content_index / content_fts / graph_edges / reference_* tables in a state
*identical* to the old drop-and-reinsert for the same bundle. These tests pin that by snapshotting
the table contents and FTS reachability after a from-empty build (== full rebuild) and after an
incremental update, and asserting they match row-for-row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from owcopilot.content.models import (
    POI,
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    RegionBrief,
    Relation,
    Term,
)
from owcopilot.content.store import ContentStore
from owcopilot.graph.index import build_content_graph
from owcopilot.inspiration.models import ReferenceChunk, ReferenceSource
from owcopilot.storage import SQLiteStore

# --- bundle fixtures ---------------------------------------------------------------------------


def _empty_bundle() -> ContentBundle:
    return ContentBundle()


def _single_entity_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"
            )
        }
    )


def _multi_object_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"
            ),
            "faction_iron_guard": Entity(
                id="faction_iron_guard", name="Iron Guard", type=EntityType.FACTION
            ),
        },
        relations=[
            Relation(source="npc_aldric", target="faction_iron_guard", kind="member_of"),
            Relation(
                source="npc_aldric",
                target="faction_iron_guard",
                kind="ally_of",
                valid_from=10,
                valid_until=20,
            ),
        ],
        quests={
            "quest_missing_caravan": Quest(
                id="quest_missing_caravan",
                title="Missing Caravan",
                objective="Find the lost supply caravan",
                giver_npc="npc_aldric",
            )
        },
        regions={"region_north": RegionBrief(id="region_north", name="Northlands")},
        pois={"poi_keep": POI(id="poi_keep", name="Iron Keep", region_id="region_north")},
        dialogues={
            "dlg_intro": DialogueRef(id="dlg_intro", text_key="DLG_INTRO", text="Hello there")
        },
        localized_texts={
            "loc_en": LocalizedText(
                id="loc_en", text_key="DLG_INTRO", text="Hello there", locale="en"
            )
        },
        terms={"term_guild": Term(id="term_guild", canonical="Guild", description="A trade body")},
    )


# --- snapshot helpers --------------------------------------------------------------------------


def _content_index_rows(store: SQLiteStore) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in store.conn.execute(
            "SELECT ref, object_type, object_id, title, body, row_hash "
            "FROM content_index ORDER BY ref"
        )
    ]


def _content_fts_rows(store: SQLiteStore) -> list[tuple[Any, ...]]:
    return sorted(
        tuple(row)
        for row in store.conn.execute(
            "SELECT ref, object_type, title, body FROM content_fts"
        )
    )


def _graph_edge_rows(store: SQLiteStore) -> list[tuple[Any, ...]]:
    # id is AUTOINCREMENT and intentionally NOT compared (nothing reads it); the row *set*
    # including multiplicity is what must match.
    return sorted(
        tuple(row)
        for row in store.conn.execute(
            "SELECT source, target, kind, edge_type, valid_from, valid_until FROM graph_edges"
        )
    )


def _reference_rows(store: SQLiteStore) -> dict[str, list[tuple[Any, ...]]]:
    sources = [
        tuple(row)
        for row in store.conn.execute(
            "SELECT id, title, source_type, original_filename, allowed_uses_json, "
            "text_hash, metadata_json, created_at FROM reference_sources ORDER BY id"
        )
    ]
    chunks = [
        tuple(row)
        for row in store.conn.execute(
            "SELECT ref, source_id, chunk_index, title, body, metadata_json "
            "FROM reference_chunks ORDER BY ref"
        )
    ]
    fts = sorted(
        tuple(row)
        for row in store.conn.execute(
            "SELECT ref, source_id, source_title, title, body FROM reference_fts"
        )
    )
    return {"sources": sources, "chunks": chunks, "fts": fts}


def _content_snapshot(store: SQLiteStore) -> tuple[Any, ...]:
    return (_content_index_rows(store), _content_fts_rows(store))


# --- content_index / content_fts parity --------------------------------------------------------


@pytest.mark.parametrize(
    "from_bundle, to_bundle",
    [
        (_empty_bundle, _single_entity_bundle),
        (_empty_bundle, _multi_object_bundle),
        (_single_entity_bundle, _multi_object_bundle),
        (_multi_object_bundle, _single_entity_bundle),
        (_multi_object_bundle, _empty_bundle),
        (_multi_object_bundle, _multi_object_bundle),
    ],
)
def test_content_index_incremental_matches_full_rebuild(from_bundle, to_bundle) -> None:
    target = to_bundle()

    # Full-rebuild reference: a fresh store built straight to the target bundle.
    full = SQLiteStore()
    # Incremental: a store first built to a *different* bundle, then synced to target.
    incremental = SQLiteStore()
    try:
        full.replace_content_index(target)

        incremental.replace_content_index(from_bundle())
        incremental.replace_content_index(target)

        assert _content_snapshot(incremental) == _content_snapshot(full)
    finally:
        full.close()
        incremental.close()


def test_content_index_change_upserts_single_row() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_multi_object_bundle())
        before = {
            str(row["ref"]): str(row["row_hash"])
            for row in store.conn.execute("SELECT ref, row_hash FROM content_index")
        }

        changed = _multi_object_bundle()
        changed.entities["npc_aldric"] = Entity(
            id="npc_aldric",
            name="Aldric the Bold",
            type=EntityType.NPC,
            description="Caravan master",
        )
        store.replace_content_index(changed)

        after = {
            str(row["ref"]): str(row["row_hash"])
            for row in store.conn.execute("SELECT ref, row_hash FROM content_index")
        }
        # Only the edited entity's row_hash moved; every other row is byte-identical.
        moved = {ref for ref in after if before.get(ref) != after[ref]}
        assert moved == {"entity:npc_aldric"}
        # The new title is searchable; the old one is not (fts row was replaced).
        assert {r["ref"] for r in store.search_content("Bold")} == {"entity:npc_aldric"}
        assert store.search_content("Aldric")  # still finds it under the new text
    finally:
        store.close()


def test_content_index_delete_prunes_row_and_fts() -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_multi_object_bundle())
        assert store.search_content("caravan")  # quest present

        reduced = _multi_object_bundle()
        del reduced.quests["quest_missing_caravan"]
        store.replace_content_index(reduced)

        refs = {
            str(row["ref"]) for row in store.conn.execute("SELECT ref FROM content_index")
        }
        assert "quest:quest_missing_caravan" not in refs
        # FTS5 manual delete: the pruned ref is no longer reachable by search.
        fts_refs = {
            str(row["ref"]) for row in store.conn.execute("SELECT ref FROM content_fts")
        }
        assert "quest:quest_missing_caravan" not in fts_refs
        assert all(
            r["ref"] != "quest:quest_missing_caravan"
            for r in store.search_content("caravan")
        )
    finally:
        store.close()


def test_content_index_first_build_equals_full() -> None:
    # First incremental sync into an empty DB is, by construction, a full insert.
    bundle = _multi_object_bundle()
    store = SQLiteStore()
    try:
        store.replace_content_index(bundle)
        rows = _content_index_rows(store)
        # every row carries a real (non-empty) row_hash
        assert rows and all(row[5] for row in rows)
    finally:
        store.close()


# --- graph_edges parity -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_bundle, to_bundle",
    [
        (_empty_bundle, _multi_object_bundle),
        (_single_entity_bundle, _multi_object_bundle),
        (_multi_object_bundle, _single_entity_bundle),
        (_multi_object_bundle, _empty_bundle),
        (_multi_object_bundle, _multi_object_bundle),
    ],
)
def test_graph_edges_incremental_matches_full_rebuild(from_bundle, to_bundle) -> None:
    target_graph = build_content_graph(to_bundle())

    full = SQLiteStore()
    incremental = SQLiteStore()
    try:
        full.replace_graph_edges(target_graph)

        incremental.replace_graph_edges(build_content_graph(from_bundle()))
        incremental.replace_graph_edges(build_content_graph(to_bundle()))

        assert _graph_edge_rows(incremental) == _graph_edge_rows(full)
        # Row count (with multiplicity) must match too -- the fingerprint's occurrence ordinal
        # keeps parallel edges distinct rather than collapsing them.
        full_count = full.conn.execute("SELECT COUNT(*) AS c FROM graph_edges").fetchone()["c"]
        inc_count = incremental.conn.execute(
            "SELECT COUNT(*) AS c FROM graph_edges"
        ).fetchone()["c"]
        assert full_count == inc_count
    finally:
        full.close()
        incremental.close()


def test_graph_edges_preserves_duplicate_multiplicity() -> None:
    # Two relations identical in (source, target, kind, valid window) -> two parallel "relation"
    # edges plus their relation_ref pairs. The relation edges are byte-identical in edge_refs(),
    # so the fingerprint must keep both as separate rows.
    bundle = ContentBundle(
        entities={
            "a": Entity(id="a", name="A", type=EntityType.NPC),
            "b": Entity(id="b", name="B", type=EntityType.NPC),
        },
        relations=[
            Relation(source="a", target="b", kind="knows"),
            Relation(source="a", target="b", kind="knows"),
        ],
    )
    graph = build_content_graph(bundle)
    store = SQLiteStore()
    try:
        store.replace_graph_edges(graph)
        relation_edges = store.conn.execute(
            "SELECT source, target, kind FROM graph_edges "
            "WHERE edge_type = 'relation' AND kind = 'knows'"
        ).fetchall()
        assert len(relation_edges) == 2  # multiplicity preserved
        # fingerprints are unique even for the byte-identical pair
        fps = [
            str(row["edge_fingerprint"])
            for row in store.conn.execute("SELECT edge_fingerprint FROM graph_edges")
        ]
        assert len(fps) == len(set(fps))
    finally:
        store.close()


# --- reference index parity ---------------------------------------------------------------------


def _ref_source(source_id: str, text_hash: str, *, title: str | None = None) -> ReferenceSource:
    return ReferenceSource(
        id=source_id,
        title=title or source_id,
        text_hash=text_hash,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _ref_chunks(source_id: str, bodies: list[str]) -> list[ReferenceChunk]:
    return [
        ReferenceChunk(
            id=f"{source_id}_chunk_{i + 1:05d}",
            source_id=source_id,
            chunk_index=i,
            title=f"{source_id} #{i + 1}",
            body=body,
        )
        for i, body in enumerate(bodies)
    ]


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        # (sources, chunks) builders described inline below by index
        ("empty", "two_books"),
        ("two_books", "edit_one"),
        ("two_books", "drop_one"),
        ("two_books", "two_books"),  # no-op resync
        ("two_books", "empty"),
    ],
)
def test_reference_index_incremental_matches_full_rebuild(from_state, to_state) -> None:
    def build(state: str) -> tuple[list[ReferenceSource], list[ReferenceChunk]]:
        if state == "empty":
            return [], []
        if state == "two_books":
            sources = [_ref_source("ref_a", "hash_a1"), _ref_source("ref_b", "hash_b1")]
            chunks = _ref_chunks("ref_a", ["alpha one", "alpha two"]) + _ref_chunks(
                "ref_b", ["beta one"]
            )
            return sources, chunks
        if state == "edit_one":
            sources = [_ref_source("ref_a", "hash_a2"), _ref_source("ref_b", "hash_b1")]
            chunks = _ref_chunks("ref_a", ["alpha CHANGED", "alpha two extra"]) + _ref_chunks(
                "ref_b", ["beta one"]
            )
            return sources, chunks
        if state == "drop_one":
            sources = [_ref_source("ref_a", "hash_a1")]
            chunks = _ref_chunks("ref_a", ["alpha one", "alpha two"])
            return sources, chunks
        raise AssertionError(state)

    target_sources, target_chunks = build(to_state)

    full = SQLiteStore()
    incremental = SQLiteStore()
    try:
        full.replace_reference_index(target_sources, target_chunks)

        from_sources, from_chunks = build(from_state)
        incremental.replace_reference_index(from_sources, from_chunks)
        incremental.replace_reference_index(target_sources, target_chunks)

        assert _reference_rows(incremental) == _reference_rows(full)
    finally:
        full.close()
        incremental.close()


def test_reference_index_unchanged_book_zero_writes() -> None:
    sources = [_ref_source("ref_a", "hash_a1"), _ref_source("ref_b", "hash_b1")]
    chunks = _ref_chunks("ref_a", ["alpha one", "alpha two"]) + _ref_chunks("ref_b", ["beta one"])
    store = SQLiteStore()
    try:
        store.replace_reference_index(sources, chunks)
        before = _reference_rows(store)

        # Count write statements during the resync to prove the unchanged books touch nothing.
        # set_trace_callback fires for every statement run on the connection (execute is read-only
        # and cannot be monkeypatched on sqlite3.Connection).
        writes = {"n": 0}

        def trace(sql: str) -> None:
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes["n"] += 1

        store.conn.set_trace_callback(trace)
        try:
            store.replace_reference_index(sources, chunks)
        finally:
            store.conn.set_trace_callback(None)

        assert writes["n"] == 0  # every book unchanged -> no insert/update/delete
        assert _reference_rows(store) == before
    finally:
        store.close()


def test_reference_index_edited_book_rechunks_only_that_source() -> None:
    sources = [_ref_source("ref_a", "hash_a1"), _ref_source("ref_b", "hash_b1")]
    chunks = _ref_chunks("ref_a", ["alpha one", "alpha two"]) + _ref_chunks("ref_b", ["beta one"])
    store = SQLiteStore()
    try:
        store.replace_reference_index(sources, chunks)
        b_chunks_before = store.conn.execute(
            "SELECT ref, body FROM reference_chunks WHERE source_id = 'ref_b' ORDER BY ref"
        ).fetchall()

        # Edit ref_a only (new hash, fewer chunks); ref_b untouched.
        new_sources = [_ref_source("ref_a", "hash_a2"), _ref_source("ref_b", "hash_b1")]
        new_chunks = _ref_chunks("ref_a", ["alpha rewritten"]) + _ref_chunks("ref_b", ["beta one"])
        store.replace_reference_index(new_sources, new_chunks)

        # ref_a re-chunked: old second chunk gone, body changed.
        a_chunks = store.conn.execute(
            "SELECT ref, body FROM reference_chunks WHERE source_id = 'ref_a' ORDER BY ref"
        ).fetchall()
        assert [r["body"] for r in a_chunks] == ["alpha rewritten"]
        # ref_b chunk rows are byte-identical (not re-touched).
        b_chunks_after = store.conn.execute(
            "SELECT ref, body FROM reference_chunks WHERE source_id = 'ref_b' ORDER BY ref"
        ).fetchall()
        assert [tuple(r) for r in b_chunks_after] == [tuple(r) for r in b_chunks_before]
        # The stale chunk row (ref_a's old second chunk) is gone from both the table and FTS.
        stale_ref = "reference_chunk:ref_a_chunk_00002"
        assert not store.conn.execute(
            "SELECT 1 FROM reference_chunks WHERE ref = ?", (stale_ref,)
        ).fetchone()
        assert not store.conn.execute(
            "SELECT 1 FROM reference_fts WHERE ref = ?", (stale_ref,)
        ).fetchone()
        # FTS no longer surfaces the removed chunk's ref for its old body text.
        assert all(
            hit["ref"] != stale_ref for hit in store.search_reference_chunks("alpha two")
        )
    finally:
        store.close()


# --- ContentStore.load mtime fast path ----------------------------------------------------------


def _seed_world(root: Path) -> None:
    store = ContentStore(root)
    store.save(_multi_object_bundle())


def test_content_store_load_mtime_fast_path_skips_unchanged(tmp_path: Path) -> None:
    _seed_world(tmp_path)
    store = ContentStore(tmp_path)

    first = store.load()
    misses_after_first = store._cache_misses
    assert misses_after_first > 0  # cold load read files
    hits_before = store._cache_hits

    # Second load with nothing changed on disk: every file served from cache, zero new disk reads.
    second = store.load()
    assert store._cache_misses == misses_after_first  # no new reads
    assert store._cache_hits > hits_before  # served from cache

    # Same parsed content either way.
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_content_store_load_reparses_changed_file(tmp_path: Path) -> None:
    _seed_world(tmp_path)
    store = ContentStore(tmp_path)
    store.load()
    misses_after_first = store._cache_misses

    # Mutate one entity file on disk.
    entity_file = tmp_path / "world" / "entities" / "npc_aldric.json"
    text = entity_file.read_text(encoding="utf-8")
    entity_file.write_text(text.replace("Caravan master", "Caravan captain"), encoding="utf-8")

    reloaded = store.load()
    # The changed file missed (re-read); the others stayed cache hits.
    assert store._cache_misses == misses_after_first + 1
    assert reloaded.entities["npc_aldric"].description == "Caravan captain"


def test_content_store_save_invalidates_cache(tmp_path: Path) -> None:
    _seed_world(tmp_path)
    store = ContentStore(tmp_path)
    store.load()

    # A save through the same store must drop the cache so the next load re-reads.
    bundle = store.load()
    bundle.entities["npc_aldric"] = Entity(
        id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Rewritten"
    )
    store.save(bundle)
    assert store._parse_cache == {}
    reloaded = store.load()
    assert reloaded.entities["npc_aldric"].description == "Rewritten"
