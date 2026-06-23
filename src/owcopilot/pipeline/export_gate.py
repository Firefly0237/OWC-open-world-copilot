"""Release gate for creator-facing exports.

The low-level exporter only writes files. Product entrypoints call this gate first so a world with
new deterministic errors or unreviewed AI content cannot be handed to an engine as if it were
release-ready.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..audit.context import AuditContext
from ..audit.models import Issue, IssueStatus, Severity
from .project import ProjectContext


@dataclass(frozen=True)
class ExportBlocker:
    code: str
    target_ref: str
    message: str


def export_blockers(project: ProjectContext) -> list[ExportBlocker]:
    result = project.audit_runner.run(AuditContext.from_bundle(project.bundle))
    blockers: list[ExportBlocker] = []
    for issue in result.issues:
        if issue.status is not IssueStatus.OPEN:
            continue
        if issue.severity is Severity.ERROR or issue.rule_code == "UNREVIEWED_AI_CONTENT":
            blockers.append(_blocker(issue))
    return blockers


def assert_export_ready(project: ProjectContext) -> None:
    blockers = export_blockers(project)
    if not blockers:
        return
    preview = "；".join(
        f"{blocker.code} @ {blocker.target_ref}: {blocker.message}" for blocker in blockers[:8]
    )
    raise ValueError(
        f"导出被发布门阻断：需要先清零 open error，并处理所有未审 AI 内容。阻断项：{preview}"
    )


def _blocker(issue: Issue) -> ExportBlocker:
    return ExportBlocker(
        code=issue.rule_code,
        target_ref=issue.target_ref,
        message=issue.message,
    )
