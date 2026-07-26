"""Scale-P0 G2-C C4a/C4b: cross-version diff + version-aware snapshots (INV-4)."""

from __future__ import annotations

import json

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.snapshot import (
    list_snapshots,
    load_snapshot,
    version_diff,
    write_snapshot,
)
from owcopilot.content.store import ContentStore


def _npc(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC)


def _seed(root) -> ContentStore:
    store = ContentStore(root)
    bundle = ContentBundle()
    bundle.entities["e1"] = _npc("e1", "Alice")
    bundle.entities["e2"] = _npc("e2", "Bob")
    store.save(bundle)
    return store


def _make_v2(store: ContentStore) -> None:
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["e1"] = _npc("e1", "Alice-v2")  # override
    b2.entities["e3"] = _npc("e3", "Carol")  # add
    del b2.entities["e2"]  # drop
    store.save_scoped(b2, version="v2")


def test_version_diff_reports_override_add_remove(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    _make_v2(store)
    diff = version_diff(store, from_version="v1", to_version="v2")
    assert {c.id for c in diff.changed} == {"e1"}
    assert {c.id for c in diff.added} == {"e3"}
    assert {c.id for c in diff.removed} == {"e2"}


def test_snapshot_freezes_version_and_records_scope(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    _make_v2(store)
    meta = write_snapshot(store, label="v2 freeze", version="v2")
    assert meta.version == "v2"
    assert meta.world_id == "default"
    frozen = load_snapshot(store, meta.id)
    assert frozen is not None
    assert frozen.entities["e1"].name == "Alice-v2"  # v2's effective content is frozen
    assert "e3" in frozen.entities
    assert "e2" not in frozen.entities


def test_snapshot_default_scope_freezes_baseline(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    _make_v2(store)  # a derived version exists, but the default snapshot must freeze the baseline
    meta = write_snapshot(store)  # default scope
    assert meta.version == "v1"
    frozen = load_snapshot(store, meta.id)
    assert frozen is not None
    assert set(frozen.entities) == {"e1", "e2"}  # baseline, not v2


def test_list_snapshots_backward_compat_pre_c4(tmp_path) -> None:
    root = tmp_path / "c"
    store = _seed(root)
    snapdir = root / ".snapshots"
    snapdir.mkdir(parents=True, exist_ok=True)
    (snapdir / "20200101_000000_000000.json").write_text(
        json.dumps(
            {
                "id": "20200101_000000_000000",
                "label": "old",
                "created_at": "2020-01-01T00:00:00+00:00",
                "content_hash": "abc",
                "bundle": {},
            }
        ),
        encoding="utf-8",
    )
    metas = list_snapshots(store)
    old = next(m for m in metas if m.id == "20200101_000000_000000")
    assert old.world_id == "default"  # pre-C4 snapshot falls back to the default scope
    assert old.version == "v1"
