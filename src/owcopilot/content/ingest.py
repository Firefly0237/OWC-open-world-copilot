"""v2 content ingestion service.

Ingestion defaults to dry-run: parse, normalize, compare and report. Writing is explicit and
conflicting IDs are never silently overwritten.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..audit.models import Issue
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
        issue.target_ref
        for issue in detect_import_conflicts(existing, incoming)
        if skip_conflicts
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
