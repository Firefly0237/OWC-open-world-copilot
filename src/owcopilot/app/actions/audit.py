# 审计与影响：确定性审计 / 问题清单 / 变更影响面。
# Deterministic audit, issue listing and change-impact actions.

"""audit actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...audit.report import render_audit_markdown
from ...content.hash import content_hash
from ...impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ...pipeline.audit import run_full_audit
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project


def run_project_audit_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        result = run_full_audit(project, persist=persist)
        return {
            "audit_run": result.run.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "open_errors": len(result.open_errors),
            "markdown_report": render_audit_markdown(
                result, content_hash=content_hash(project.bundle)
            ),
            "cost_budget": _deterministic_cost_budget("audit_project"),
        }


def list_project_issues_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    severity: str | None = None,
    rule_code: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        issues = project.sqlite_store.list_issues(
            severity=severity, rule_code=rule_code, status=status
        )
        return {
            "count": len(issues),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "cost_budget": _deterministic_cost_budget("list_issues"),
        }


def run_impact_action(
    content_root: str | Path,
    *,
    changes: list[dict[str, str]],
    sqlite_path: str | None = None,
    max_depth: int = 2,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        parsed = [
            Change(
                change_type=ChangeType(str(spec["change_type"])),
                target_ref=str(spec["target_ref"]),
            )
            for spec in changes
        ]
        result = ImpactAnalyzer(project.graph).analyze(
            ChangeSet(changes=parsed), max_depth=max_depth
        )
        return {
            "must_change": [
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.MUST_CHANGE)
            ],
            "suggest_check": [
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.SUGGEST_CHECK)
            ],
            "total": len(result.items),
            "cost_budget": _deterministic_cost_budget("impact_of"),
        }
