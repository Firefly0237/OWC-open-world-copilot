"""Scale-P0 G2-C C4c: three-way version merge -- auto-merge one-sided changes, flag conflicts
(never auto-write canon; conflicts go to human review)."""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.snapshot import merge_versions
from owcopilot.content.store import ContentStore


def _npc(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC)


def _seed(root) -> ContentStore:
    store = ContentStore(root)
    bundle = ContentBundle()
    for eid, name in {"e1": "Alice", "e2": "Bob", "e3": "Carol"}.items():
        bundle.entities[eid] = _npc(eid, name)
    store.save(bundle)
    return store


def _branch(store: ContentStore, name: str) -> ContentBundle:
    store.create_version(name, base_version="v1")
    return store.load_scoped(version=name)


def test_common_base_of_two_branches_is_v1(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    store.create_version("v2", base_version="v1")
    store.create_version("v3", base_version="v1")
    assert store.common_base_version("v2", "v3") == "v1"


def test_merge_auto_resolves_one_sided_changes(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    v2 = _branch(store, "v2")
    v2.entities["e1"] = _npc("e1", "Alice-v2")  # only v2 changes e1
    store.save_scoped(v2, version="v2")
    v3 = _branch(store, "v3")
    v3.entities["e2"] = _npc("e2", "Bob-v3")  # only v3 changes e2
    store.save_scoped(v3, version="v3")
    result = merge_versions(store, ours="v2", theirs="v3")
    assert not result.conflicts
    assert result.merged.entities["e1"].name == "Alice-v2"  # ours-only change kept
    assert result.merged.entities["e2"].name == "Bob-v3"  # theirs-only change taken
    assert result.merged.entities["e3"].name == "Carol"  # untouched by both


def test_merge_both_changed_is_a_conflict(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    v2 = _branch(store, "v2")
    v2.entities["e1"] = _npc("e1", "Alice-v2")
    store.save_scoped(v2, version="v2")
    v3 = _branch(store, "v3")
    v3.entities["e1"] = _npc("e1", "Alice-v3")  # both changed e1 differently
    store.save_scoped(v3, version="v3")
    result = merge_versions(store, ours="v2", theirs="v3")
    assert [(c.id, c.reason) for c in result.conflicts] == [("e1", "both-changed")]
    assert result.merged.entities["e1"].name == "Alice-v2"  # placeholder keeps ours


def test_merge_add_add_conflict(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    v2 = _branch(store, "v2")
    v2.entities["e9"] = _npc("e9", "New-v2")
    store.save_scoped(v2, version="v2")
    v3 = _branch(store, "v3")
    v3.entities["e9"] = _npc("e9", "New-v3")  # both add e9 differently
    store.save_scoped(v3, version="v3")
    result = merge_versions(store, ours="v2", theirs="v3")
    assert [(c.id, c.reason) for c in result.conflicts] == [("e9", "add-add")]


def test_merge_modify_delete_conflict(tmp_path) -> None:
    store = _seed(tmp_path / "c")
    v2 = _branch(store, "v2")
    v2.entities["e1"] = _npc("e1", "Alice-v2")  # v2 modifies e1
    store.save_scoped(v2, version="v2")
    v3 = _branch(store, "v3")
    del v3.entities["e1"]  # v3 deletes e1
    store.save_scoped(v3, version="v3")
    result = merge_versions(store, ours="v2", theirs="v3")
    assert [(c.id, c.reason) for c in result.conflicts] == [("e1", "modify-delete")]
