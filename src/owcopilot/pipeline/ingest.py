# 摄取流水线入口。
"""Ingest pipeline entrypoints."""

from __future__ import annotations

from pathlib import Path

from ..content.ingest import IngestResult, ingest_paths, ingest_update, parse_paths
from ..content.mapping import FieldMapping, apply_field_mapping
from ..storage.world_lock import world_write_lock
from .project import ProjectContext


def run_ingest(
    project: ProjectContext,
    paths: list[str | Path],
    *,
    dry_run: bool = True,
    field_mapping: FieldMapping | None = None,
    write_non_conflicting: bool = False,
) -> IngestResult:
    """Run previews lock-free; serialize only an ingest that may commit canon changes."""
    if dry_run:
        return _run_ingest_unlocked(
            project,
            paths,
            dry_run=True,
            field_mapping=field_mapping,
            write_non_conflicting=write_non_conflicting,
        )
    with world_write_lock(project.content_store.root):
        return _run_ingest_unlocked(
            project,
            paths,
            dry_run=dry_run,
            field_mapping=field_mapping,
            write_non_conflicting=write_non_conflicting,
        )


def _run_ingest_unlocked(
    project: ProjectContext,
    paths: list[str | Path],
    *,
    dry_run: bool = True,
    field_mapping: FieldMapping | None = None,
    write_non_conflicting: bool = False,
) -> IngestResult:
    # A migrated world (opened on a working line) takes the vendor-branch three-way update-ingest:
    # a re-uploaded change to an object the library did not touch auto-merges (the reachable
    # "update half"), and only a genuine both-sides divergence is flagged for review -- never the
    # old two-way "any changed id => IMPORT_CONFLICT => skipped". An un-migrated world keeps the
    # backward-compatible two-way path (a bare store has no merge base, so auto-update is unsafe).
    if project.content_store.read_head() is not None:
        raw = apply_field_mapping(parse_paths(paths), field_mapping)
        result = ingest_update(
            raw, store=project.content_store, dry_run=dry_run, world_id=project.world_id
        )
        if not dry_run:
            # advance_head moved the working line; re-point the project so reads see the merge.
            project.resync_scope()
        return result
    result = ingest_paths(
        paths,
        store=project.content_store,
        dry_run=dry_run,
        field_mapping=field_mapping,
        write_non_conflicting=write_non_conflicting,
    )
    if not dry_run and (not result.has_errors or write_non_conflicting):
        project.reload()
    return result
