"""Scale-P0 G2-C C3a: file-backed copy-on-write version overlay in ``ContentStore.load_scoped``.

A derived version is ``root/versions/<version>/`` (only its added/changed object files) + an
optional ``tombstones.json``; ``version.json`` records its ``base_version``. Reading walks the base
chain [baseline … target] and overlays each layer (nearest definition wins; tombstone removes).
Default ``v1`` with no ``versions/`` dir == ``load()`` (INV-1 byte-identical).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.content.store import ContentStore


def _entity(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC)


def _baseline(store: ContentStore, entities: dict[str, str]) -> None:
    bundle = ContentBundle()
    for eid, name in entities.items():
        bundle.entities[eid] = _entity(eid, name)
    store.save(bundle)


def _write_version(
    root: Path,
    version: str,
    *,
    base: str | None,
    entities: dict[str, str] | None = None,
    tombstones: list[str] | None = None,
) -> None:
    vroot = root / "versions" / version
    vroot.mkdir(parents=True, exist_ok=True)
    (vroot / "version.json").write_text(
        json.dumps({"version": version, "base_version": base}), encoding="utf-8"
    )
    if entities:
        edir = vroot / "world" / "entities"
        edir.mkdir(parents=True, exist_ok=True)
        for eid, name in entities.items():
            (edir / f"{eid}.json").write_text(
                _entity(eid, name).model_dump_json(), encoding="utf-8"
            )
    if tombstones:
        (vroot / "tombstones.json").write_text(json.dumps(tombstones), encoding="utf-8")


def test_load_scoped_default_v1_equals_load(tmp_path) -> None:
    """INV-1: default (v1, no versions dir) is byte-identical to load()."""
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob"})
    assert store.load_scoped(version="v1").model_dump() == store.load().model_dump()


def test_version_override_wins_over_baseline(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob"})
    _write_version(tmp_path, "v2", base="v1", entities={"e1": "Alice-v2"})
    scoped = store.load_scoped(version="v2")
    assert scoped.entities["e1"].name == "Alice-v2"  # override wins
    assert scoped.entities["e2"].name == "Bob"  # inherited from baseline


def test_version_adds_new_object(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice"})
    _write_version(tmp_path, "v2", base="v1", entities={"e9": "New"})
    assert set(store.load_scoped(version="v2").entities) == {"e1", "e9"}


def test_version_tombstone_removes_baseline_object(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob"})
    _write_version(tmp_path, "v2", base="v1", tombstones=["entity:e2"])
    assert set(store.load_scoped(version="v2").entities) == {"e1"}


def test_multi_level_base_chain_nearest_wins(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "v1-name"})
    _write_version(tmp_path, "v2", base="v1", entities={"e1": "v2-name"})
    _write_version(tmp_path, "v3", base="v2", entities={"e1": "v3-name"})
    assert store.load_scoped(version="v3").entities["e1"].name == "v3-name"
    assert store.load_scoped(version="v2").entities["e1"].name == "v2-name"
    assert store.load_scoped(version="v1").entities["e1"].name == "v1-name"


def test_tombstone_then_readd_in_later_version(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob"})
    _write_version(tmp_path, "v2", base="v1", tombstones=["entity:e2"])
    _write_version(tmp_path, "v3", base="v2", entities={"e2": "Bob-again"})
    v3 = store.load_scoped(version="v3")
    assert v3.entities["e2"].name == "Bob-again"  # re-added in v3 after v2 tombstoned it


def test_relations_union_across_overlay(tmp_path) -> None:
    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.entities["e1"] = _entity("e1", "A")
    bundle.entities["e2"] = _entity("e2", "B")
    bundle.relations = [Relation(source="e1", target="e2", kind="knows")]
    store.save(bundle)
    vroot = tmp_path / "versions" / "v2"
    (vroot / "world").mkdir(parents=True, exist_ok=True)
    (vroot / "version.json").write_text(
        json.dumps({"version": "v2", "base_version": "v1"}), encoding="utf-8"
    )
    (vroot / "world" / "relations.jsonl").write_text(
        Relation(source="e1", target="e2", kind="allies").model_dump_json(), encoding="utf-8"
    )
    kinds = {r.kind for r in store.load_scoped(version="v2").relations}
    assert kinds == {"knows", "allies"}  # union across the overlay


def test_relation_tombstone_removes_baseline_relation(tmp_path) -> None:
    # A relation present in a base version can be whited out by a derived version's tombstone
    # (relation:<source>:<kind>:<target>), closing the union-only gap.
    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.entities["e1"] = _entity("e1", "A")
    bundle.entities["e2"] = _entity("e2", "B")
    bundle.relations = [
        Relation(source="e1", target="e2", kind="knows"),
        Relation(source="e1", target="e2", kind="allies"),
    ]
    store.save(bundle)
    _write_version(tmp_path, "v2", base="v1", tombstones=["relation:e1:knows:e2"])
    kinds = {r.kind for r in store.load_scoped(version="v2").relations}
    assert kinds == {"allies"}  # the tombstoned 'knows' edge is gone, 'allies' survives


def test_relation_tombstone_only_matches_the_exact_triple(tmp_path) -> None:
    # Identity is the (source, kind, target) triple, exact match — a tombstone for one edge must
    # not remove a same-endpoints edge of another kind, nor the reverse-direction edge.
    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.entities["e1"] = _entity("e1", "A")
    bundle.entities["e2"] = _entity("e2", "B")
    bundle.relations = [
        Relation(source="e1", target="e2", kind="knows"),
        Relation(source="e2", target="e1", kind="knows"),  # reverse direction
        Relation(source="e1", target="e2", kind="allies"),  # different kind
    ]
    store.save(bundle)
    _write_version(tmp_path, "v2", base="v1", tombstones=["relation:e1:knows:e2"])
    triples = {(r.source, r.kind, r.target) for r in store.load_scoped(version="v2").relations}
    assert triples == {("e2", "knows", "e1"), ("e1", "allies", "e2")}


def test_save_scoped_roundtrips_relation_drop_via_tombstone(tmp_path) -> None:
    # End-to-end write side: dropping a base relation in a derived bundle emits a relation tombstone
    # on save, and load_scoped honours it — the relation is gone after the round-trip.
    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.entities["e1"] = _entity("e1", "A")
    bundle.entities["e2"] = _entity("e2", "B")
    bundle.relations = [
        Relation(source="e1", target="e2", kind="knows"),
        Relation(source="e1", target="e2", kind="allies"),
    ]
    store.save(bundle)
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.relations = [r for r in b2.relations if r.kind != "knows"]  # drop the 'knows' edge
    store.save_scoped(b2, version="v2")

    # The tombstone was materialised...
    tombstones = json.loads(
        (tmp_path / "versions" / "v2" / "tombstones.json").read_text(encoding="utf-8")
    )
    assert "relation:e1:knows:e2" in tombstones
    # ...and the reload honours it while keeping the untouched edge, and leaves the baseline intact.
    assert {r.kind for r in store.load_scoped(version="v2").relations} == {"allies"}
    assert {r.kind for r in store.load_scoped(version="v1").relations} == {"knows", "allies"}


def test_relation_union_unchanged_when_no_tombstone(tmp_path) -> None:
    # Behaviour fidelity: with no relation tombstone, the overlay is byte-for-byte the old union —
    # both the reloaded relations and the on-disk tombstones.json (empty) are unchanged.
    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.entities["e1"] = _entity("e1", "A")
    bundle.entities["e2"] = _entity("e2", "B")
    bundle.relations = [Relation(source="e1", target="e2", kind="knows")]
    store.save(bundle)
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    # Reassign (not in-place mutate) the relations list, mirroring how the existing save_scoped
    # tests edit a loaded bundle; add a relation, drop nothing.
    b2.relations = [*b2.relations, Relation(source="e2", target="e1", kind="allies")]
    store.save_scoped(b2, version="v2")

    triples = {(r.source, r.kind, r.target) for r in store.load_scoped(version="v2").relations}
    assert triples == {("e1", "knows", "e2"), ("e2", "allies", "e1")}  # union preserved
    tombstones = json.loads(
        (tmp_path / "versions" / "v2" / "tombstones.json").read_text(encoding="utf-8")
    )
    assert tombstones == []  # no drop → no tombstone emitted


def test_version_chain_cycle_is_defensive(tmp_path) -> None:
    """A cycle in version.json must not infinite-loop (create_version in C3b prevents cycles)."""
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice"})
    _write_version(tmp_path, "v2", base="v3")
    _write_version(tmp_path, "v3", base="v2")
    assert "e1" in store.load_scoped(version="v2").entities  # terminates, baseline present


# --------------------------------------------------------------------------- C3b: create + save


def test_create_version_starts_equal_to_base(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob"})
    store.create_version("v2", base_version="v1")
    assert (tmp_path / "versions" / "v2" / "version.json").exists()
    # copy-on-write: a fresh branch equals its base until content is saved into it
    assert (
        store.load_scoped(version="v2").model_dump() == store.load_scoped(version="v1").model_dump()
    )


def test_save_scoped_roundtrips_override_add_and_drop(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob"})
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["e1"] = _entity("e1", "Alice-v2")  # override
    b2.entities["e3"] = _entity("e3", "New")  # add
    del b2.entities["e2"]  # drop -> tombstone
    store.save_scoped(b2, version="v2")
    reloaded = store.load_scoped(version="v2")
    assert {k: v.name for k, v in reloaded.entities.items()} == {"e1": "Alice-v2", "e3": "New"}
    # the baseline is untouched by a version save
    assert set(store.load_scoped(version="v1").entities) == {"e1", "e2"}


def test_save_scoped_writes_only_the_diff(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice", "e2": "Bob", "e3": "Carol"})
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["e2"] = _entity("e2", "Bob-v2")  # change only e2
    store.save_scoped(b2, version="v2")
    override_dir = tmp_path / "versions" / "v2" / "world" / "entities"
    assert {p.stem for p in override_dir.glob("*.json")} == {"e2"}  # only the diff materialised


def test_create_version_rejects_bad_inputs(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice"})
    with pytest.raises(ValueError):
        store.create_version("v1")  # baseline name
    store.create_version("v2", base_version="v1")
    with pytest.raises(ValueError):
        store.create_version("v2", base_version="v1")  # already exists
    with pytest.raises(ValueError):
        store.create_version("v9", base_version="nope")  # unknown base


def test_save_scoped_baseline_writes_root(tmp_path) -> None:
    store = ContentStore(tmp_path)
    _baseline(store, {"e1": "Alice"})
    bundle = store.load()
    bundle.entities["e1"] = _entity("e1", "Alice-edited")
    store.save_scoped(bundle, version="v1")  # baseline -> writes the root tree (== save)
    assert store.load().entities["e1"].name == "Alice-edited"
    assert not (tmp_path / "versions").exists()  # no version dir created


def test_in_place_relation_append_does_not_pollute_the_parse_cache(tmp_path) -> None:
    """The cached relations list must never be handed out by reference: an in-place
    ``bundle.relations.append`` used to mutate the cache itself, so ``save_scoped``'s fresh
    ``base`` load already contained the new relation and the diff (and tombstones) came out
    empty. Loaders now return copies; the cache stays byte-true to disk."""
    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.entities["e1"] = _entity("e1", "Alice")
    bundle.entities["e2"] = _entity("e2", "Bob")
    bundle.relations = [Relation(source="e1", kind="knows", target="e2")]
    store.save(bundle)

    loaded = store.load()
    loaded.relations.append(Relation(source="e2", kind="knows", target="e1"))  # in-place edit
    fresh = store.load()  # cache hit — must still reflect disk, not the caller's mutation
    assert [(r.source, r.kind, r.target) for r in fresh.relations] == [("e1", "knows", "e2")]


def test_in_place_quest_event_ref_edit_does_not_pollute_the_parse_cache(tmp_path) -> None:
    """Same aliasing guarantee for the jsonl-backed ``quest_event_refs`` dict."""
    from owcopilot.content.models import QuestEventReference

    store = ContentStore(tmp_path)
    bundle = ContentBundle()
    bundle.quest_event_refs["ev1"] = QuestEventReference(id="ev1", quest_id="q1", event_id="evt1")
    store.save(bundle)

    loaded = store.load()
    loaded.quest_event_refs.pop("ev1")  # in-place, on purpose
    fresh = store.load()
    assert set(fresh.quest_event_refs) == {"ev1"}
