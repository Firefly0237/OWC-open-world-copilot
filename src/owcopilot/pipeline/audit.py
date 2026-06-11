"""Audit pipeline entrypoints."""

from __future__ import annotations

from ..audit.context import AuditContext
from ..audit.runner import AuditResult
from .project import ProjectContext


def run_full_audit(project: ProjectContext, *, persist: bool = True) -> AuditResult:
    project.reload()
    result = project.audit_runner.run(AuditContext.from_bundle(project.bundle))
    if persist:
        project.sqlite_store.save_audit_run(result.run)
        for issue in result.issues:
            project.sqlite_store.save_issue(issue)
    return result
