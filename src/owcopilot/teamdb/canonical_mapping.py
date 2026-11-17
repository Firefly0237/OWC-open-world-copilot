"""Deterministic bridge from the legacy :class:`ContentStore` to a PG genesis plan.

The bridge deliberately imports only the *resolved current state*.  It does not
invent a lineage for the legacy copy-on-write directories: PostgreSQL history
starts at the one genesis revision produced here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ..content.models import ContentBundle, Relation
from ..content.store import ContentStore
from ..contracts import (
    ContentObjectVersion,
    ContentRevision,
    ReleasePointer,
    RevisionMembership,
)
from ..contracts.hashing import canonical_json
from ..contracts.revisions import canonical_manifest
from ..storage.atomic_file import read_json_file
from ..trust.security import resolve_under_root


@dataclass(frozen=True, slots=True)
class LegacyCanonSnapshot:
    """The one resolved legacy state eligible for a genesis import."""

    bundle: ContentBundle
    resolved_version: str
    active_tombstones: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GenesisPlan:
    """Fully validated immutable records for one parentless revision."""

    object_versions: tuple[ContentObjectVersion, ...]
    memberships: tuple[RevisionMembership, ...]
    revision: ContentRevision
    release_pointer: ReleasePointer
    legacy_tombstones: tuple[str, ...]
    resolved_legacy_version: str
    source_manifest_hash: str | None = None

    @property
    def manifest_json(self) -> str:
        return canonical_manifest(self.memberships)


# These are the eleven id-keyed collections in ContentBundle.  Keep this
# explicit: silently overlooking a newly added canon collection would be data
# loss, so ``map_genesis`` also checks the model field set below.
_ID_COLLECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("entity", "entities"),
    ("quest", "quests"),
    ("region", "regions"),
    ("poi", "pois"),
    ("dialogue", "dialogues"),
    ("dialogue_tree", "dialogue_trees"),
    ("localized_text", "localized_texts"),
    ("term", "terms"),
    ("style_guide", "style_guides"),
    ("style_rubric", "style_rubrics"),
    ("quest_event_ref", "quest_event_refs"),
)
_KIND_TO_ATTRIBUTE: Final = dict(_ID_COLLECTIONS)
_EXPECTED_BUNDLE_FIELDS: Final = {attribute for _, attribute in _ID_COLLECTIONS} | {"relations"}
_BASELINE_VERSION: Final = "v1"


def relation_legacy_tombstone_key(relation: Relation) -> str:
    """Return the complete key used by the existing ContentStore whiteout."""

    return f"relation:{relation.source}:{relation.kind}:{relation.target}"


def relation_set_logical_id(legacy_tombstone_key: str) -> str:
    """Map an opaque complete relation whiteout key to a stable logical id."""

    if not legacy_tombstone_key.startswith("relation:"):
        raise ValueError("relation tombstone key must start with 'relation:'")
    digest = hashlib.sha256(legacy_tombstone_key.encode("utf-8", errors="strict")).hexdigest()
    return f"relation_set:{digest}"


def resolve_legacy_canon(
    store: ContentStore,
    *,
    version: str | None = None,
) -> LegacyCanonSnapshot:
    """Resolve the current bundle and the tombstones still effective at its head.

    ``version=None`` follows the transparent legacy working head when present,
    otherwise it resolves the baseline.  Metadata and tombstone files are read
    strictly rather than through the legacy permissive helpers: malformed
    cutover input must stop instead of becoming an empty list.
    """

    selected_version = version
    if selected_version is None:
        head_path = resolve_under_root(store.root, store.root / "versions" / "_head.json")
        if head_path.exists():
            head = read_json_file(head_path)
            if not isinstance(head, dict):
                raise ValueError("legacy head must be a JSON object")
            working = head.get("working")
            if type(working) is not str or not working:
                raise ValueError("legacy head working version must be a non-empty string")
            selected_version = working
        else:
            selected_version = _BASELINE_VERSION

    chain = _strict_version_chain(store.root, selected_version)
    bundle = store.load_scoped(version=selected_version)
    observed_tombstones: set[str] = set()
    for chain_version in chain[1:]:
        path = _version_path(store.root, chain_version) / "tombstones.json"
        if not path.exists():
            continue
        raw = read_json_file(path)
        if not isinstance(raw, list) or any(type(item) is not str for item in raw):
            raise ValueError(f"legacy tombstone file must be a JSON string array: {path}")
        if len(raw) != len(set(raw)):
            raise ValueError(f"legacy tombstone file contains duplicate keys: {path}")
        observed_tombstones.update(raw)

    live_logical_ids = set(_iter_live_logical_ids(bundle))
    active: list[str] = []
    for key in sorted(observed_tombstones):
        logical_id = _tombstone_logical_id(key)
        if logical_id not in live_logical_ids:
            active.append(key)
    return LegacyCanonSnapshot(
        bundle=bundle,
        resolved_version=selected_version,
        active_tombstones=tuple(active),
    )


def map_genesis(
    snapshot: LegacyCanonSnapshot,
    *,
    created_at: datetime,
    created_by: str,
    source_manifest_hash: str | None = None,
) -> GenesisPlan:
    """Convert one resolved snapshot to immutable N1.1 records.

    Id-keyed objects use ``<kind>:<id>``.  Relations sharing the complete
    legacy tombstone key become one ``relation_set:<sha256>`` object whose
    canonically sorted payload retains every occurrence, including exact
    duplicates and ``source_ref`` fields.
    """

    if (
        source_manifest_hash is not None
        and re.fullmatch(r"[0-9a-f]{64}", source_manifest_hash) is None
    ):
        raise ValueError("source_manifest_hash must be a lowercase SHA-256 digest")
    actual_fields = set(type(snapshot.bundle).model_fields)
    if actual_fields != _EXPECTED_BUNDLE_FIELDS:
        missing = sorted(actual_fields - _EXPECTED_BUNDLE_FIELDS)
        stale = sorted(_EXPECTED_BUNDLE_FIELDS - actual_fields)
        raise RuntimeError(
            f"ContentBundle collection registry is stale; unmapped={missing!r}, removed={stale!r}"
        )

    versions: dict[str, ContentObjectVersion] = {}
    for kind, attribute in _ID_COLLECTIONS:
        collection = getattr(snapshot.bundle, attribute)
        for object_id, model in sorted(collection.items()):
            if object_id != model.id:
                raise ValueError(
                    f"legacy {attribute} key {object_id!r} does not match payload id {model.id!r}"
                )
            logical_id = f"{kind}:{object_id}"
            versions[logical_id] = ContentObjectVersion.from_payload(
                logical_id=logical_id,
                object_type=kind,
                payload=model.model_dump(mode="json", exclude_none=True),
            )

    relation_groups: dict[str, list[dict[str, object]]] = {}
    for relation in snapshot.bundle.relations:
        key = relation_legacy_tombstone_key(relation)
        relation_groups.setdefault(key, []).append(
            relation.model_dump(mode="json", exclude_none=True)
        )
    for key, payloads in sorted(relation_groups.items()):
        # Canonical JSON is both a total ordering and the exact identity
        # spelling used by ContentObjectVersion.  ``sorted`` never deduplicates.
        ordered = sorted(payloads, key=canonical_json)
        logical_id = relation_set_logical_id(key)
        versions[logical_id] = ContentObjectVersion.from_payload(
            logical_id=logical_id,
            object_type="relation_set",
            payload={"legacy_tombstone_key": key, "relations": ordered},
        )

    memberships: dict[str, RevisionMembership] = {
        logical_id: RevisionMembership.live(
            logical_id=logical_id,
            object_version_id=version.object_version_id,
        )
        for logical_id, version in versions.items()
    }
    tombstone_logical_ids: set[str] = set()
    for key in snapshot.active_tombstones:
        logical_id = _tombstone_logical_id(key)
        if logical_id in memberships:
            raise ValueError(f"active tombstone conflicts with live object: {key!r}")
        if logical_id in tombstone_logical_ids:
            raise ValueError(f"duplicate tombstone logical id: {logical_id!r}")
        tombstone_logical_ids.add(logical_id)
        memberships[logical_id] = RevisionMembership.deleted(logical_id=logical_id)

    ordered_versions = tuple(versions[key] for key in sorted(versions))
    ordered_memberships = tuple(memberships[key] for key in sorted(memberships))
    revision = ContentRevision.from_memberships(
        parent_revision=None,
        memberships=ordered_memberships,
        created_at=created_at,
        created_by=created_by,
    )
    return GenesisPlan(
        object_versions=ordered_versions,
        memberships=ordered_memberships,
        revision=revision,
        release_pointer=ReleasePointer(authoring_head=revision.revision_id, serving_head=None),
        legacy_tombstones=tuple(sorted(snapshot.active_tombstones)),
        resolved_legacy_version=snapshot.resolved_version,
        source_manifest_hash=source_manifest_hash,
    )


def _iter_live_logical_ids(bundle: ContentBundle) -> Iterator[str]:
    for kind, attribute in _ID_COLLECTIONS:
        for object_id in getattr(bundle, attribute):
            yield f"{kind}:{object_id}"
    for relation in bundle.relations:
        yield relation_set_logical_id(relation_legacy_tombstone_key(relation))


def _tombstone_logical_id(key: str) -> str:
    if key.startswith("relation:"):
        return relation_set_logical_id(key)
    kind, separator, object_id = key.partition(":")
    if not separator or not object_id or kind not in _KIND_TO_ATTRIBUTE:
        raise ValueError(f"unknown or malformed legacy tombstone key: {key!r}")
    return f"{kind}:{object_id}"


def _version_path(root: Path, version: str) -> Path:
    if version == _BASELINE_VERSION:
        return root
    candidate = root / "versions" / version
    return resolve_under_root(root, candidate)


def _strict_version_chain(root: Path, version: str) -> tuple[str, ...]:
    if not version:
        raise ValueError("legacy version must not be empty")
    if version == _BASELINE_VERSION:
        return (_BASELINE_VERSION,)

    descending: list[str] = []
    seen: set[str] = set()
    current = version
    while current != _BASELINE_VERSION:
        if current in seen:
            raise ValueError(f"legacy version graph contains a cycle at {current!r}")
        seen.add(current)
        descending.append(current)
        version_path = _version_path(root, current)
        if not version_path.is_dir():
            raise ValueError(f"legacy version directory does not exist: {current!r}")
        meta_path = version_path / "version.json"
        if not meta_path.exists():
            parent = _BASELINE_VERSION
        else:
            raw = read_json_file(meta_path)
            if not isinstance(raw, dict):
                raise ValueError(f"legacy version metadata must be a JSON object: {meta_path}")
            raw_parent = raw.get("base_version")
            if raw_parent is None:
                parent = _BASELINE_VERSION
            elif type(raw_parent) is str and raw_parent:
                parent = raw_parent
            else:
                raise ValueError(f"invalid base_version in {meta_path}")
        current = parent
    return tuple([_BASELINE_VERSION, *reversed(descending)])


__all__ = [
    "GenesisPlan",
    "LegacyCanonSnapshot",
    "map_genesis",
    "relation_legacy_tombstone_key",
    "relation_set_logical_id",
    "resolve_legacy_canon",
]
