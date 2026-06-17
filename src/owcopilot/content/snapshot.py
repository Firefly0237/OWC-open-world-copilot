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

from .hash import content_hash
from .models import ContentBundle
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
]


class SnapshotMeta(BaseModel):
    id: str
    label: str = ""
    created_at: str
    content_hash: str


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


def write_snapshot(store: ContentStore, *, label: str = "") -> SnapshotMeta:
    """Dump the current world to ``<root>/.snapshots/<id>.json`` and return its metadata."""
    bundle = store.load()
    snap_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    meta = SnapshotMeta(
        id=snap_id,
        label=label.strip(),
        created_at=datetime.now(UTC).isoformat(),
        content_hash=content_hash(bundle),
    )
    payload = {**meta.model_dump(mode="json"), "bundle": bundle.model_dump(mode="json")}
    path = store.root / _SNAP_DIR / f"{snap_id}.json"
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
        fields = {key: data.get(key) for key in SnapshotMeta.model_fields}
        metas.append(SnapshotMeta.model_validate(fields))
    metas.sort(key=lambda m: m.id, reverse=True)
    return metas


def load_snapshot(store: ContentStore, snapshot_id: str) -> ContentBundle | None:
    path = store.root / _SNAP_DIR / f"{snapshot_id}.json"
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
