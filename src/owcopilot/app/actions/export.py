# 导出：引擎文件导出 / 设定集渲染 / 文风检查。
# Export actions: engine files, lorebook render, prose check.

"""export actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...assist.prose_check import check_prose
from ...audit.context import AuditContext
from ...exporters import AuditFindingInput, EngineTarget, export_content_bundle
from ...exporters.lorebook import write_lorebook
from ...pipeline.export_gate import assert_export_ready
from ...storage.world_lock import serialized_world_write
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project


@serialized_world_write
def run_project_export_action(
    content_root: str | Path,
    *,
    output_dir: str | Path,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
    row_struct_map: dict[str, dict[str, str]] | None = None,
    unity_localization: bool = False,
    planning_workbook: bool = False,
    planning_notes: bool = False,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    # ``row_struct_map`` (UNREAL only) renames DataTable columns to the target project's own USTRUCT
    # fields; None keeps the default names so the export stays byte-for-byte identical.
    engine = EngineTarget(target_engine)
    actual_output = Path(output_dir) / engine.value
    with _project(content_root, sqlite_path) as project:
        assert_export_ready(project)
        review_findings = (
            tuple(
                AuditFindingInput(
                    rule_code=issue.rule_code,
                    severity=issue.severity.value,
                    target_ref=issue.target_ref,
                    message=issue.message,
                    evidence_paths=tuple(
                        evidence.path for evidence in issue.evidence if evidence.path
                    ),
                )
                for issue in project.audit_runner.run(
                    AuditContext.from_bundle(project.bundle, content_root=project.content_root)
                ).issues
            )
            if planning_notes
            else ()
        )
        manifest = export_content_bundle(
            project.bundle,
            actual_output,
            target_engine=engine,
            unity_localization=unity_localization,
            row_struct_map=row_struct_map,
            planning_workbook=planning_workbook,
            planning_notes=planning_notes,
            review_findings=review_findings,
        )
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


@serialized_world_write
def run_lorebook_export_action(
    content_root: str | Path,
    *,
    output_dir: str | Path,
    formats: tuple[str, ...] = ("md", "docx"),
    title: str = "世界设定集",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Export the readable lore book (creator-facing deliverable, deterministic)."""
    with _project(content_root, sqlite_path) as project:
        files = write_lorebook(project.bundle, output_dir, title=title, formats=formats)
        return {
            "output_dir": str(Path(output_dir)),
            "files": files,
            "cost_budget": _deterministic_cost_budget("lorebook_export"),
        }


def run_prose_check_action(
    content_root: str | Path,
    *,
    text: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Check a pasted chapter against the archive: mentions, forbidden terms, unknowns."""
    with _project(content_root, sqlite_path) as project:
        report = check_prose(text, project.bundle)
        return {
            "resolved_mentions": [m.model_dump(mode="json") for m in report.resolved_mentions],
            "issues": [issue.model_dump(mode="json") for issue in report.issues],
            "stats": report.stats,
            "cost_budget": _deterministic_cost_budget("prose_check"),
        }
