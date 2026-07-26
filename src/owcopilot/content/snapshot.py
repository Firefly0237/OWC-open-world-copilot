# canon 快照 + 两个世界的结构化 diff。
"""Canon snapshots and a structural diff between two worlds.

The content store is already canonical, file-backed JSON — so "what changed" is answerable without
any new database: take a labelled snapshot (a single canonical dump under ``<root>/.snapshots/``),
and diff a snapshot against the live world. The diff is a pure function over two bundles: per object
kind it reports added ids, removed ids, and, for survivors, the fields that changed (before→after).
This is the version history wiki-style tools usually hide behind a paywall; ours is just files.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ..trust.security import resolve_under_root
from .hash import content_hash
from .models import ContentBundle, Relation
from .normalize import _FORBIDDEN_ID_CHARS, _validate_id_chars
from .store import ContentStore

_SNAP_DIR = ".snapshots"

# (kind label, bundle attribute, field to use as a display name)
_COLLECTIONS: list[tuple[str, str, str]] = [
    ("entity", "entities", "name"),
    ("quest", "quests", "title"),
    ("region", "regions", "name"),
    ("poi", "pois", "name"),
    ("dialogue", "dialogues", "text_key"),
    ("dialogue_tree", "dialogue_trees", "title"),
    ("term", "terms", "canonical"),
    ("localized_text", "localized_texts", "text_key"),
    ("quest_event_ref", "quest_event_refs", "id"),
    # the world's writing voice/rules feed every generation stage, so an edit to one is a real
    # canon change the version history must show (it was silently missing from the diff).
    ("style_guide", "style_guides", "id"),
    # the 12th canon type (D5): a versioned element checklist. ``label`` is its human name (falls
    # back to id via ``_name`` when blank), matching the store's own dict_kinds slot for this kind
    # (content/store.py's COW diff/tombstone list) -- same registration shape, same position.
    ("style_rubric", "style_rubrics", "label"),
]


class SnapshotMeta(BaseModel):
    id: str
    label: str = ""
    created_at: str
    content_hash: str
    # The (world, version) scope this snapshot froze (INV-4: a snapshot is an
    # immutable point-in-time freeze OF A SCOPE, distinct from the mutable version line). Defaults
    # keep older snapshots (which have no scope fields) loading as the canonical default scope.
    world_id: str = "default"
    version: str = "v1"


class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class ObjectChange(BaseModel):
    kind: str
    id: str
    name: str = ""
    changes: list[FieldChange] = Field(default_factory=list)


class CanonDiff(BaseModel):
    added: list[ObjectChange] = Field(default_factory=list)
    removed: list[ObjectChange] = Field(default_factory=list)
    changed: list[ObjectChange] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


def write_snapshot(
    store: ContentStore, *, label: str = "", world_id: str = "default", version: str = "v1"
) -> SnapshotMeta:
    """Freeze a ``(world_id, version)`` scope's effective world to ``<root>/.snapshots/<id>.json``.

    The snapshot captures that version's copy-on-write effective bundle
    (``load_scoped``) and records the scope, so a snapshot of a derived version differs from the
    baseline. The default scope freezes ``load()`` exactly as before (INV-1)."""
    bundle = store.load_scoped(world_id=world_id, version=version)
    snap_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    meta = SnapshotMeta(
        id=snap_id,
        label=label.strip(),
        created_at=datetime.now(UTC).isoformat(),
        content_hash=content_hash(bundle),
        world_id=world_id,
        version=version,
    )
    payload = {**meta.model_dump(mode="json"), "bundle": bundle.model_dump(mode="json")}
    path = store.root / _SNAP_DIR / f"{snap_id}.json"
    # `snap_id` is an internal timestamp, but assert the final path stays under the store root
    # anyway (same shared canon helper as the store write boundary — defense-in-depth).
    resolve_under_root(store.root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def list_snapshots(store: ContentStore) -> list[SnapshotMeta]:
    """Snapshot metadata, newest first."""
    directory = store.root / _SNAP_DIR
    if not directory.exists():
        return []
    metas: list[SnapshotMeta] = []
    for path in directory.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        # Only carry keys actually present, so an older snapshot (no world_id/version) falls back to
        # the SnapshotMeta defaults instead of validating a missing key as None.
        fields = {key: data[key] for key in SnapshotMeta.model_fields if key in data}
        metas.append(SnapshotMeta.model_validate(fields))
    metas.sort(key=lambda m: m.id, reverse=True)
    return metas


def load_snapshot(store: ContentStore, snapshot_id: str) -> ContentBundle | None:
    # ``snapshot_id`` is externally controlled (GET /diff?from=, POST /snapshots:restore).
    # Validate it with the SAME id-invariant the store write boundary uses so a crafted id
    # like ``../../secret`` or ``C:/Windows/foo`` cannot escape ``<root>/.snapshots`` when
    # it is interpolated into ``{id}.json``. Internally generated ids (write_snapshot's
    # timestamp) always pass; only attacker-supplied ids are rejected.
    safe_id = _validate_id_chars(
        snapshot_id, context="snapshot_id (load_snapshot)", forbidden=_FORBIDDEN_ID_CHARS
    )
    path = store.root / _SNAP_DIR / f"{safe_id}.json"
    # Second layer: a container assertion over the final interpolated path, via the same shared
    # canon helper. `_validate_id_chars` above already rejects traversal ids with a guided error;
    # this is the defense-in-depth backstop (PathSecurityError is a ValueError subclass).
    resolve_under_root(store.root, path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return ContentBundle.model_validate(data.get("bundle", {}))


def bundle_diff(old: ContentBundle, new: ContentBundle) -> CanonDiff:
    """Structural diff between two worlds: per kind, what was added / removed / changed."""
    old_json = old.model_dump(mode="json")
    new_json = new.model_dump(mode="json")
    added: list[ObjectChange] = []
    removed: list[ObjectChange] = []
    changed: list[ObjectChange] = []

    for kind, attr, name_field in _COLLECTIONS:
        old_map: dict[str, dict[str, Any]] = old_json.get(attr) or {}
        new_map: dict[str, dict[str, Any]] = new_json.get(attr) or {}
        for obj_id in sorted(new_map.keys() - old_map.keys()):
            added.append(
                ObjectChange(kind=kind, id=obj_id, name=_name(new_map[obj_id], name_field))
            )
        for obj_id in sorted(old_map.keys() - new_map.keys()):
            removed.append(
                ObjectChange(kind=kind, id=obj_id, name=_name(old_map[obj_id], name_field))
            )
        for obj_id in sorted(old_map.keys() & new_map.keys()):
            before, after = old_map[obj_id], new_map[obj_id]
            if before == after:
                continue
            fields = [
                FieldChange(field=key, before=before.get(key), after=after.get(key))
                for key in sorted(set(before) | set(after))
                if before.get(key) != after.get(key)
            ]
            changed.append(
                ObjectChange(kind=kind, id=obj_id, name=_name(after, name_field), changes=fields)
            )

    _diff_relations(old_json, new_json, added, removed)

    added.sort(key=lambda c: (c.kind, c.id))
    removed.sort(key=lambda c: (c.kind, c.id))
    changed.sort(key=lambda c: (c.kind, c.id))
    return CanonDiff(
        added=added,
        removed=removed,
        changed=changed,
        summary={"added": len(added), "removed": len(removed), "changed": len(changed)},
    )


def version_diff(
    store: ContentStore,
    *,
    from_version: str,
    to_version: str,
    world_id: str = "default",
) -> CanonDiff:
    """Structural diff between two versions' effective (copy-on-write resolved) bundles.

    Reuses ``bundle_diff`` over ``ContentStore.load_scoped`` of each version, so
    it reports what ``to_version`` added / removed / changed relative to ``from_version`` (e.g. a
    version vs its base). ``world_id`` is threaded for the (world, version) scope."""
    old = store.load_scoped(world_id=world_id, version=from_version)
    new = store.load_scoped(world_id=world_id, version=to_version)
    return bundle_diff(old, new)


class MergeConflict(BaseModel):
    kind: str
    id: str
    reason: str  # "add-add" | "both-changed" | "modify-delete"


class MergeResult(BaseModel):
    merged: ContentBundle
    conflicts: list[MergeConflict] = Field(default_factory=list)


_MERGE_DICT_KINDS = (
    ("entity", "entities"),
    ("region", "regions"),
    ("quest", "quests"),
    ("poi", "pois"),
    ("dialogue", "dialogues"),
    ("dialogue_tree", "dialogue_trees"),
    ("localized_text", "localized_texts"),
    ("term", "terms"),
    ("style_guide", "style_guides"),
    # the 12th canon type (D5): without this entry ``merge_bundles_three_way`` silently dropped
    # every ``style_rubrics`` entry from ``merged`` (it iterates only the kinds listed here) --
    # real data loss on any three-way merge touching a world with rubrics.
    ("style_rubric", "style_rubrics"),
    ("quest_event_ref", "quest_event_refs"),
)


def merge_versions(
    store: ContentStore, *, ours: str, theirs: str, world_id: str = "default"
) -> MergeResult:
    """Three-way merge of version ``theirs`` into ``ours`` over their common base.

    For each object: if both sides agree, or only one side changed vs the merge base, that value is
    taken automatically; if both sides changed it differently (add/add, both-changed, or
    modify/delete) it is a CONFLICT -- collected, never auto-applied. ``merged`` carries every
    non-conflicting resolution (a conflict keeps ``ours`` as a placeholder); relations are merged
    three-way (deletions propagate). Nothing is written to canon: the caller reviews the conflicts
    and persists via ``save_scoped`` + the review queue, so human review stays the only canon-write
    path."""
    base = store.load_scoped(world_id=world_id, version=store.common_base_version(ours, theirs))
    ours_b = store.load_scoped(world_id=world_id, version=ours)
    theirs_b = store.load_scoped(world_id=world_id, version=theirs)
    return merge_bundles_three_way(base=base, ours=ours_b, theirs=theirs_b)


def merge_bundles_three_way(
    *, base: ContentBundle, ours: ContentBundle, theirs: ContentBundle
) -> MergeResult:
    """Pure three-way merge over three already-loaded bundles (the core of :func:`merge_versions`).

    Extracted so an in-memory caller (update-ingest's dry-run, which must not persist a mirror
    version just to compare) merges through the *same* logic the persisted path uses -- there is one
    merge implementation, exercised identically on dry-run and write. Semantics are the diff3 table:
    both-agree / one-sided-change auto-resolve; a divergent change on both sides is a conflict that
    keeps ``ours`` as a placeholder."""
    merged = ContentBundle()
    conflicts: list[MergeConflict] = []
    for kind, attr in _MERGE_DICT_KINDS:
        base_map = getattr(base, attr)
        ours_map = getattr(ours, attr)
        theirs_map = getattr(theirs, attr)
        out = getattr(merged, attr)
        for oid in set(base_map) | set(ours_map) | set(theirs_map):
            bo, oo, to = base_map.get(oid), ours_map.get(oid), theirs_map.get(oid)
            if oo == to:  # both sides agree (same value, or both absent)
                resolved = oo
            elif to == bo:  # theirs unchanged vs base -> take ours
                resolved = oo
            elif oo == bo:  # ours unchanged vs base -> take theirs
                resolved = to
            else:  # both changed the object differently -> conflict, keep ours as placeholder
                reason = (
                    "add-add"
                    if bo is None
                    else "modify-delete"
                    if oo is None or to is None
                    else "both-changed"
                )
                conflicts.append(MergeConflict(kind=kind, id=oid, reason=reason))
                resolved = oo
            if resolved is not None:
                out[oid] = resolved
    merged.relations = _merge_relations(
        base=base.relations, ours=ours.relations, theirs=theirs.relations
    )
    return MergeResult(merged=merged, conflicts=conflicts)


def _merge_relations(
    *, base: list[Relation], ours: list[Relation], theirs: list[Relation]
) -> list[Relation]:
    """Three-way set merge of relations, keyed on each relation's full canonical payload.

    ``relations`` is a list whose semantics are a set, so a merge must be a real three-way set
    operation, not the union it used to be (union could never propagate a deletion -- an external
    source dropping a relation was silently re-added from ``ours``). Full-payload identity means a
    metadata change is a delete + add rather than a silent overwrite. A relation survives iff both
    sides still carry it, or it is new versus the base on at least one side (an addition the other
    side never had the chance to delete); a base relation dropped by either side is removed."""
    base_keys = {content_hash(r) for r in base}
    ours_keys = {content_hash(r) for r in ours}
    theirs_keys = {content_hash(r) for r in theirs}
    kept: dict[str, Relation] = {}
    # ours first, then theirs-only, so an unchanged shared relation keeps ours' object identity and
    # the on-disk order stays deterministic (ContentBundle re-sorts by relation_sort_key anyway).
    for rel in [*ours, *theirs]:
        key = content_hash(rel)
        if key in kept:
            continue
        both_keep = key in ours_keys and key in theirs_keys
        if both_keep or key not in base_keys:
            kept[key] = rel
    return list(kept.values())


def _diff_relations(
    old_json: dict[str, Any],
    new_json: dict[str, Any],
    added: list[ObjectChange],
    removed: list[ObjectChange],
) -> None:
    old_rel = {_rel_sig(r) for r in old_json.get("relations") or []}
    new_rel = {_rel_sig(r) for r in new_json.get("relations") or []}
    for sig in sorted(new_rel - old_rel):
        added.append(ObjectChange(kind="relation", id=sig, name=sig))
    for sig in sorted(old_rel - new_rel):
        removed.append(ObjectChange(kind="relation", id=sig, name=sig))


def _rel_sig(relation: dict[str, Any]) -> str:
    return f"{relation.get('source')} --{relation.get('kind')}--> {relation.get('target')}"


def _name(obj: dict[str, Any], field: str) -> str:
    value = obj.get(field) or obj.get("id") or ""
    return str(value)
