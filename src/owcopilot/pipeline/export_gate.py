# 面向创作者导出的发布门（不合格不放行）。
"""Release gate for creator-facing exports.

The low-level exporter only writes files. Product entrypoints call this gate first so a world with
new deterministic errors or unreviewed AI content cannot be handed to an engine as if it were
release-ready.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..audit.context import AuditContext
from ..audit.models import Issue, IssueStatus, Severity
from ..exporters.unreal_datatable import AuditFindingInput
from .project import ProjectContext


@dataclass(frozen=True)
class ExportBlocker:
    code: str
    target_ref: str
    message: str
    #: 六要素里的定位与建议动作，直接从审计问题带过来：发布门是策划最常撞上的一堵墙，只说"哪个
    #: 对象不行"会把人卡死在原地。定位不出来时为空串。
    source_path: str = ""
    source_line: int | None = None
    suggested_action: str = ""


def export_blockers(project: ProjectContext) -> list[ExportBlocker]:
    result = project.audit_runner.run(
        AuditContext.from_bundle(project.bundle, content_root=project.content_root)
    )
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
    preview = "；".join(_blocker_preview(blocker) for blocker in blockers[:8])
    raise ValueError(
        f"导出被发布门阻断：需要先清零 open error，并处理所有未审 AI 内容。阻断项：{preview}"
    )


def _blocker(issue: Issue) -> ExportBlocker:
    return ExportBlocker(
        code=issue.rule_code,
        target_ref=issue.target_ref,
        message=issue.message,
        source_path=issue.source_path,
        source_line=issue.source_line,
        suggested_action=issue.suggested_action,
    )


def _blocker_preview(blocker: ExportBlocker) -> str:
    """一条阻断项在报错文案里的样子：对象 + 原因 + 去哪儿改 + 改成什么。"""
    text = f"{blocker.code} @ {blocker.target_ref}: {blocker.message}"
    if blocker.source_path:
        where = blocker.source_path
        if blocker.source_line is not None:
            where += f" 第 {blocker.source_line} 行"
        text += f"(位置:{where})"
    if blocker.suggested_action:
        text += f"(建议:{blocker.suggested_action})"
    return text


def audit_findings_for_export(project: ProjectContext) -> list[AuditFindingInput]:
    """Run the SAME audit ``export_blockers`` runs, translated into the exporter's own vocabulary
    (:class:`AuditFindingInput`) so ``exporters.unreal_datatable`` never has to import the audit
    package itself — this pipeline layer, which already depends on both, does the one translation.

    Every OPEN issue is included, not just the ERROR/``UNREVIEWED_AI_CONTENT`` ones
    ``export_blockers`` treats as release-blocking: a WARNING like
    ``DIALOGUE_UNGATED_SIBLING_CHOICE`` is exactly the kind of finding the ``blueprint_friendly``
    ``AuditFindings`` table (see ``export_unreal_datatables``) exists to surface at runtime."""
    result = project.audit_runner.run(AuditContext.from_bundle(project.bundle))
    return [
        AuditFindingInput(
            rule_code=issue.rule_code,
            severity=issue.severity.value,
            target_ref=issue.target_ref,
            message=issue.message,
            evidence_paths=tuple(e.path for e in issue.evidence if e.path),
        )
        for issue in result.issues
        if issue.status is IssueStatus.OPEN
    ]
