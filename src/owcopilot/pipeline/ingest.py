"""Ingest pipeline entrypoints."""

from __future__ import annotations

from pathlib import Path

from ..content.ingest import IngestResult, ingest_paths
from ..content.mapping import FieldMapping
from .project import ProjectContext


def run_ingest(
    project: ProjectContext,
    paths: list[str | Path],
    *,
    dry_run: bool = True,
    field_mapping: FieldMapping | None = None,
    write_non_conflicting: bool = False,
) -> IngestResult:
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
