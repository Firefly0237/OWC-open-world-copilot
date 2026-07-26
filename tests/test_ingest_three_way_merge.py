# 版本 scope 迁移 + ingest 三方合并更新（防吃书"更新那一半"根因修复）的验收测试。
"""Acceptance tests for the transparent version-scope migration + three-way update-ingest.

These exercise the *production* code paths (``ContentStore.ensure_migrated`` /
``ingest_update`` / ``merge_bundles_three_way``) -- no merge logic is re-implemented here; the
tests only assemble inputs and assert outputs. They cover the target scenario (upload updated
material -> non-conflicts auto-merge, conflicts flag) and the hard data-integrity properties
(lossless migration, hash stability, no bare-save baseline back-contamination).
"""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.content.hash import content_hash
from owcopilot.content.importers.json import JSONImporter
from owcopilot.content.ingest import ChangeType, ingest_update
from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.content.store import ContentStore, is_internal_version


def _entity(eid: str, name: str, desc: str = "") -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC, description=desc)


def _seed_world(root: Path) -> ContentStore:
    store = ContentStore(root)
    bundle = ContentBundle()
    bundle.entities["npc_aldric"] = _entity("npc_aldric", "Aldric", "Old desc")
    bundle.entities["npc_mara"] = _entity("npc_mara", "Mara", "Guard")
    bundle.relations.append(Relation(source="npc_aldric", kind="allied_with", target="npc_mara"))
    store.save(bundle)
    return store


def _write_json(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _raw(path: Path) -> list:
    return JSONImporter().parse(path)


def _bundle_files(root: Path) -> dict[str, bytes]:
    """Every content file under the *baseline* root (versions/ excluded) -> its bytes."""
    files: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] == "versions":
            continue
        files[str(rel).replace("\\", "/")] = p.read_bytes()
    return files


# -- Migration: lossless + transparent ----------------------------------------------------------


def test_migration_is_lossless_byte_equivalent(tmp_path) -> None:
    store = _seed_world(tmp_path / "c")
    before_bundle = store.load()
    before_files = _bundle_files(tmp_path / "c")

    working = store.ensure_migrated()

    # The working line reads back byte-for-byte identical to the baseline (empty COW branch).
    after_bundle = store.load_scoped(version=working)
    assert after_bundle.model_dump(mode="json") == before_bundle.model_dump(mode="json")
    assert content_hash(after_bundle) == content_hash(before_bundle)
    # The baseline files on disk were not rewritten by migration.
    assert _bundle_files(tmp_path / "c") == before_files


def test_migration_is_idempotent(tmp_path) -> None:
    store = _seed_world(tmp_path / "c")
    first = store.ensure_migrated()
    second = store.ensure_migrated()
    assert first == second
    head = store.read_head()
    assert head is not None and head["working"] == first


def test_working_and_merge_point_and_mirror_are_internal(tmp_path) -> None:
    store = _seed_world(tmp_path / "c")
    working = store.ensure_migrated()
    assert is_internal_version("v1")
    assert is_internal_version(working)
    assert is_internal_version("m__1")
    assert is_internal_version("imp__1")
    assert not is_internal_version("my_named_branch")


# -- Target scenario A1: update half is reachable ------------------------------------------------


def test_external_update_auto_merges_into_library(tmp_path) -> None:
    """Re-upload a changed existing object; library untouched -> the update lands (not skipped)."""
    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "New desc"}],
    )

    result = ingest_update(_raw(src), store=store, dry_run=False)

    assert result.issues == []  # no conflict: one-sided external update
    change = next(c for c in result.changes if c.object_id == "npc_aldric")
    assert change.change_type is ChangeType.UPDATE
    # The update is really in the library now (the "update half", previously unreachable).
    new_working = store.read_head()["working"]
    reloaded = store.load_scoped(version=new_working)
    assert reloaded.entities["npc_aldric"].description == "New desc"
    # Untouched objects survive an incremental upload that did not re-send them.
    assert reloaded.entities["npc_mara"].description == "Guard"


def test_both_sides_diverged_is_a_conflict_kept_as_ours(tmp_path) -> None:
    """Library edits an object AND the external source changes it differently -> conflict warning,
    library value kept (never silently overwritten)."""
    store = _seed_world(tmp_path / "c")
    working = store.ensure_migrated()
    # Library-side edit on the working line.
    lib = store.load_scoped(version=working)
    lib.entities["npc_aldric"] = _entity("npc_aldric", "Aldric", "Library edit")
    store.save_scoped(lib, version=working)
    # External source changes the same object differently.
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "Vendor edit"}],
    )

    result = ingest_update(_raw(src), store=store, dry_run=False)

    assert [i.rule_code for i in result.issues] == ["IMPORT_CONFLICT"]
    assert result.issues[0].severity == "warning"  # non-blocking review item
    assert result.issues[0].target_ref == "entity:npc_aldric"
    change = next(c for c in result.changes if c.object_id == "npc_aldric")
    assert change.change_type is ChangeType.CONFLICT
    # Library value preserved; vendor value did NOT overwrite it.
    reloaded = store.load_scoped(version=store.read_head()["working"])
    assert reloaded.entities["npc_aldric"].description == "Library edit"


def test_new_object_is_added(tmp_path) -> None:
    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    src = _write_json(
        tmp_path / "new.json",
        [{"kind": "entity", "id": "npc_ivo", "name": "Ivo", "type": "npc"}],
    )

    result = ingest_update(_raw(src), store=store, dry_run=False)

    assert result.issues == []
    change = next(c for c in result.changes if c.object_id == "npc_ivo")
    assert change.change_type is ChangeType.ADD
    reloaded = store.load_scoped(version=store.read_head()["working"])
    assert "npc_ivo" in reloaded.entities


def test_second_update_does_not_falsely_conflict(tmp_path) -> None:
    """The merge point advances, so a second external update to the same object (library still
    untouched) is a clean update, not a false conflict against the original baseline."""
    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    src1 = _write_json(
        tmp_path / "u1.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "Rev 1"}],
    )
    ingest_update(_raw(src1), store=store, dry_run=False)
    src2 = _write_json(
        tmp_path / "u2.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "Rev 2"}],
    )

    result = ingest_update(_raw(src2), store=store, dry_run=False)

    assert result.issues == []  # NOT a false conflict
    reloaded = store.load_scoped(version=store.read_head()["working"])
    assert reloaded.entities["npc_aldric"].description == "Rev 2"


# -- Hard data-integrity properties -------------------------------------------------------------


def test_merge_result_hash_is_stable_across_persist_reload(tmp_path) -> None:
    """content_hash of the in-memory merged bundle == the hash read back after save_scoped."""
    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "New desc"}],
    )

    result = ingest_update(_raw(src), store=store, dry_run=False)

    reloaded = store.load_scoped(version=store.read_head()["working"])
    assert content_hash(reloaded) == result.content_hash_after


def test_update_ingest_does_not_touch_baseline(tmp_path) -> None:
    """No bare-save baseline back-contamination: the baseline files are byte-identical after a
    write-mode update-ingest."""
    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    baseline_before = _bundle_files(tmp_path / "c")
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "New desc"}],
    )

    ingest_update(_raw(src), store=store, dry_run=False)

    assert _bundle_files(tmp_path / "c") == baseline_before
    # And the baseline still holds the pre-update value.
    assert store.load().entities["npc_aldric"].description == "Old desc"


def test_dry_run_writes_nothing(tmp_path) -> None:
    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    head_before = store.read_head()
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "New desc"}],
    )

    result = ingest_update(_raw(src), store=store, dry_run=True)

    assert result.dry_run is True
    assert next(c for c in result.changes if c.object_id == "npc_aldric").change_type is (
        ChangeType.UPDATE
    )
    # No new merge point / working line; head unmoved.
    assert store.read_head() == head_before
    assert store.load_scoped(version=head_before["working"]).entities["npc_aldric"].description == (
        "Old desc"
    )


def test_frozen_merge_point_and_mirror_refuse_writes(tmp_path) -> None:
    import pytest

    store = _seed_world(tmp_path / "c")
    store.ensure_migrated()
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "New desc"}],
    )
    ingest_update(_raw(src), store=store, dry_run=False)

    # The write path persisted a vendor-drop mirror and a merge point, both frozen.
    assert (tmp_path / "c" / "versions" / "imp__1" / "version.json").exists()
    for internal in ("m__1", "imp__1"):
        bundle = store.load_scoped(version=internal)
        with pytest.raises(ValueError, match="frozen"):
            store.save_scoped(bundle, version=internal)


def test_external_relation_deletion_propagates(tmp_path) -> None:
    """A full re-upload of an entity that drops a relation propagates the deletion via three-way
    (the old pure-union merge could never remove a relation)."""
    from owcopilot.content.snapshot import merge_bundles_three_way

    base = ContentBundle()
    base.relations.append(Relation(source="a", kind="allied_with", target="b"))
    ours = base.model_copy(deep=True)  # library untouched
    theirs = ContentBundle()  # external source dropped the relation

    merged = merge_bundles_three_way(base=base, ours=ours, theirs=theirs)

    assert merged.merged.relations == []  # deletion propagated


# -- Pipeline wiring: migrated ProjectContext -> three-way ingest + scoped review ---------------


def test_run_ingest_three_way_through_migrated_project(tmp_path) -> None:
    """The pipeline entry routes a migrated world through the three-way update-ingest and resyncs
    the project scope so reads see the merge; the baseline is never touched."""
    from owcopilot.pipeline.ingest import run_ingest
    from owcopilot.pipeline.project import ProjectContext

    root = tmp_path / "c"
    _seed_world(root)
    src = _write_json(
        tmp_path / "upd.json",
        [{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "description": "New desc"}],
    )
    project = ProjectContext.open(root, migrate=True)
    try:
        baseline_before = _bundle_files(root)
        result = run_ingest(project, [src], dry_run=False)
        assert result.issues == []
        # project resynced to the advanced working line: the merge is visible in project.bundle.
        assert project.bundle.entities["npc_aldric"].description == "New desc"
        # baseline untouched (no bare-save back-contamination).
        assert _bundle_files(root) == baseline_before
        assert ContentStore(root).load().entities["npc_aldric"].description == "Old desc"
    finally:
        project.close()


def test_review_accept_on_migrated_world_writes_working_line_not_baseline(tmp_path) -> None:
    """A review accept on a migrated world lands on the working line via the scope-aware write, not
    the baseline root tree."""
    from owcopilot.assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
    from owcopilot.pipeline.project import ProjectContext
    from owcopilot.pipeline.review import decide_review_item

    root = tmp_path / "c"
    _seed_world(root)
    project = ProjectContext.open(root, migrate=True)
    try:
        queue = ReviewQueue(project.sqlite_store)
        item = queue.add(
            ReviewItem(
                item_type=ReviewItemType.CHARACTER_PROFILE,
                object_ref="entity:npc_new",
                payload={"entity": {"id": "npc_new", "name": "New", "type": "npc"}},
            )
        )
        outcome = decide_review_item(project, item.id, decision="accepted", operator="alice")
        assert outcome.decision == "accepted"
        # Written to the working line...
        assert "npc_new" in project.content_store.load_scoped(version=project.version).entities
        # ...and NOT into the baseline root tree.
        assert "npc_new" not in ContentStore(root).load().entities
    finally:
        project.close()
