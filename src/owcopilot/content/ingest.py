# v2 内容摄取服务：外部素材→规范化→入内容模型。
"""v2 content ingestion service.

Ingestion defaults to dry-run: parse, normalize, compare and report. Writing is explicit and
conflicting IDs are never silently overwritten.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..audit.models import Category, Evidence, Issue, Severity
from ..audit.rules.import_rules import detect_import_conflicts
from .hash import content_hash
from .importers.base import RawObject
from .importers.csv import CSVImporter
from .importers.json import JSONImporter
from .importers.markdown import MarkdownImporter
from .importers.xlsx import XLSXImporter
from .mapping import FieldMapping, apply_field_mapping
from .models import ContentBundle
from .normalize import normalize_raw_objects
from .snapshot import MergeConflict, merge_bundles_three_way, merge_versions
from .store import ContentStore


class ChangeType(str, Enum):
    ADD = "add"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


class IngestChange(BaseModel):
    change_type: ChangeType
    object_type: str
    object_id: str
    before_hash: str | None = None
    after_hash: str | None = None


class IngestResult(BaseModel):
    dry_run: bool
    content_hash_before: str
    content_hash_after: str
    incoming_count: int
    changes: list[IngestChange] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


def ingest_paths(
    paths: list[str | Path],
    *,
    store: ContentStore,
    dry_run: bool = True,
    field_mapping: FieldMapping | None = None,
    write_non_conflicting: bool = False,
) -> IngestResult:
    return ingest_raw_objects(
        apply_field_mapping(parse_paths(paths), field_mapping),
        store=store,
        dry_run=dry_run,
        write_non_conflicting=write_non_conflicting,
    )


def ingest_raw_objects(
    raw_objects: list[RawObject],
    *,
    store: ContentStore,
    dry_run: bool = True,
    write_non_conflicting: bool = False,
) -> IngestResult:
    existing = store.load() if store.exists() else ContentBundle()
    incoming = normalize_raw_objects(raw_objects)
    issues = detect_import_conflicts(existing, incoming)
    changes = diff_bundles(existing, incoming, conflict_refs={issue.target_ref for issue in issues})
    merged = merge_bundles(existing, incoming, skip_conflicts=True)
    result = IngestResult(
        dry_run=dry_run,
        content_hash_before=content_hash(existing),
        content_hash_after=content_hash(merged),
        incoming_count=len(raw_objects),
        changes=changes,
        issues=issues,
    )
    if not dry_run and (not result.has_errors or write_non_conflicting):
        store.save(merged)
    return result


def ingest_update(
    raw_objects: list[RawObject],
    *,
    store: ContentStore,
    dry_run: bool = True,
    world_id: str = "default",
) -> IngestResult:
    """Update-ingest via the vendor-branch three-way merge -- the reachable "update half".

    This is the root-cause fix for "re-uploaded material can never update the knowledge base": the
    old two-way :func:`ingest_raw_objects` had only ``existing`` and ``incoming``, could not tell an
    external-source update apart from a library-side edit, and so flagged *every* changed object as
    an ``IMPORT_CONFLICT`` and skipped it. Here the world is migrated to the working-line model so a
    real merge base exists, and each upload is a vendor drop:

    * ``theirs`` = the current merge point (base) with this batch overlaid (incremental: objects the
      source did not re-send stay at their base value, so nothing is silently dropped),
    * ``ours``   = the working line (carries library-side edits),
    * ``base``   = the immutable merge point.

    A one-sided change (external source updated an object the library left alone) auto-merges into
    the library -- the update half, now reachable. Only a genuine both-sides-diverged change
    (both-changed / add-add / modify-delete) is an ``IMPORT_CONFLICT``; it is kept as the ``ours``
    placeholder and reported as a non-blocking review warning, never silently overwritten and never
    blocking the rest of the batch (msgmerge fuzzy semantics). On write the merge lands as a new
    frozen merge point + working line via :meth:`ContentStore.advance_head`; the baseline seed is
    never touched, so there is no bare-save baseline back-contamination."""
    working = store.ensure_migrated()
    head = store.read_head()
    assert head is not None  # ensure_migrated guarantees it
    merge_point = str(head["merge_point"])

    incoming = normalize_raw_objects(raw_objects)
    base_bundle = store.load_scoped(world_id=world_id, version=merge_point)
    ours = store.load_scoped(world_id=world_id, version=working)
    # theirs = base + this batch (incremental): re-sent objects override, unmentioned objects keep
    # their base value so an incomplete upload can never tombstone the rest of the world.
    theirs = base_bundle.model_copy(deep=True)
    for _object_type, theirs_map, incoming_map in _object_maps(theirs, incoming):
        theirs_map.update(incoming_map)
    _extend_relations(theirs, incoming.relations)

    if dry_run:
        # Merge in memory through the shared core -- a dry run must not persist a mirror version.
        result = merge_bundles_three_way(base=base_bundle, ours=ours, theirs=theirs)
    else:
        # Persist the vendor drop as a frozen import mirror hung off the immutable merge point, then
        # merge THROUGH the version store (merge_versions) -- so the persisted three-way merge and
        # the dry-run preview run the exact same logic. advance_head then lands the merged bundle as
        # a fresh frozen merge point + working line.
        mirror = store.create_import_mirror(seed=theirs)
        result = merge_versions(store, ours=working, theirs=mirror, world_id=world_id)
        store.advance_head(merged=result.merged)

    conflict_refs = {f"{c.kind}:{c.id}" for c in result.conflicts}
    changes = diff_bundles(ours, result.merged, conflict_refs=conflict_refs)
    issues = [_import_conflict_issue(c) for c in result.conflicts]

    return IngestResult(
        dry_run=dry_run,
        content_hash_before=content_hash(ours),
        content_hash_after=content_hash(result.merged),
        incoming_count=len(raw_objects),
        changes=changes,
        issues=issues,
    )


def _extend_relations(bundle: ContentBundle, incoming: list[Any]) -> None:
    """Add incoming relations to ``bundle``, deduped on full canonical payload (incremental)."""
    seen = {content_hash(rel) for rel in bundle.relations}
    for rel in incoming:
        key = content_hash(rel)
        if key not in seen:
            bundle.relations.append(rel)
            seen.add(key)


def _import_conflict_issue(conflict: MergeConflict) -> Issue:
    """A non-blocking review warning for a genuine three-way conflict (both sides diverged).

    Same ``rule_code`` / ``fingerprint`` shape as the two-way :func:`detect_import_conflicts` so
    downstream dedup is unaffected, but severity is ``WARNING``: the merge already kept ``ours`` as
    a safe placeholder, so the batch is not blocked -- the conflict is a review item, not a hard
    error. This is the narrowed ``IMPORT_CONFLICT`` (both-changed / add-add / modify-delete)."""
    ref = f"{conflict.kind}:{conflict.id}"
    return Issue(
        rule_code="IMPORT_CONFLICT",
        severity=Severity.WARNING,
        category=Category.IMPORT,
        target_ref=ref,
        message=(
            f"Imported {conflict.kind} '{conflict.id}' both-sides-diverged ({conflict.reason}); "
            "kept the library value and flagged it for review."
        ),
        evidence=[Evidence(kind="field_path", target_ref=ref, path=conflict.id)],
        fingerprint=content_hash(
            {
                "rule": "IMPORT_CONFLICT",
                "object_type": conflict.kind,
                "object_id": conflict.id,
            }
        ),
    )


def parse_paths(paths: list[str | Path]) -> list[RawObject]:
    raw_objects: list[RawObject] = []
    for path in paths:
        raw_objects.extend(_importer_for(Path(path)).parse(path))
    return raw_objects


def diff_bundles(
    existing: ContentBundle,
    incoming: ContentBundle,
    *,
    conflict_refs: set[str] | None = None,
) -> list[IngestChange]:
    conflicts = conflict_refs or set()
    changes: list[IngestChange] = []
    for object_type, old, new in _object_maps(existing, incoming):
        for object_id, incoming_obj in sorted(new.items()):
            before_obj = old.get(object_id)
            after_hash = content_hash(incoming_obj)
            before_hash = content_hash(before_obj) if before_obj is not None else None
            target_ref = f"{object_type}:{object_id}"
            if target_ref in conflicts:
                change_type = ChangeType.CONFLICT
            elif before_obj is None:
                change_type = ChangeType.ADD
            elif before_hash == after_hash:
                change_type = ChangeType.UNCHANGED
            else:
                change_type = ChangeType.UPDATE
            changes.append(
                IngestChange(
                    change_type=change_type,
                    object_type=object_type,
                    object_id=object_id,
                    before_hash=before_hash,
                    after_hash=after_hash,
                )
            )
    return changes


def merge_bundles(
    existing: ContentBundle, incoming: ContentBundle, *, skip_conflicts: bool
) -> ContentBundle:
    merged = existing.model_copy(deep=True)
    conflicts = {
        issue.target_ref for issue in detect_import_conflicts(existing, incoming) if skip_conflicts
    }
    for object_type, old, new in _object_maps(merged, incoming):
        for object_id, value in new.items():
            if f"{object_type}:{object_id}" in conflicts:
                continue
            old[object_id] = value
    merged.relations.extend(incoming.relations)
    return merged


def _importer_for(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        return JSONImporter()
    if suffix == ".csv":
        return CSVImporter()
    if suffix in {".md", ".markdown"}:
        return MarkdownImporter()
    if suffix == ".xlsx":
        return XLSXImporter()
    raise ValueError(f"unsupported content import format: {path.suffix!r}")


def _object_maps(
    bundle: ContentBundle,
    incoming: ContentBundle,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        ("entity", bundle.entities, incoming.entities),
        ("quest_event_ref", bundle.quest_event_refs, incoming.quest_event_refs),
        ("quest", bundle.quests, incoming.quests),
        ("region", bundle.regions, incoming.regions),
        ("poi", bundle.pois, incoming.pois),
        ("dialogue", bundle.dialogues, incoming.dialogues),
        ("localized_text", bundle.localized_texts, incoming.localized_texts),
        ("term", bundle.terms, incoming.terms),
        ("style_guide", bundle.style_guides, incoming.style_guides),
    ]
