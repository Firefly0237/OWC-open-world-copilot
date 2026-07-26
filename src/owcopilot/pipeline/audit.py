# 审计流水线入口。
"""Audit pipeline entrypoints."""

from __future__ import annotations

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.runner import AuditResult
from .project import ProjectContext


def run_full_audit(project: ProjectContext, *, persist: bool = True) -> AuditResult:
    project.reload()
    # 带上内容根目录，每条问题就带着"文件 + 行号 + 节点 id"出来（六要素）。这是 reload 之后的
    # 落盘内容，磁盘上的行号与 bundle 一致。
    result = project.audit_runner.run(
        AuditContext.from_bundle(project.bundle, content_root=project.content_root)
    )
    if persist:
        project.sqlite_store.save_audit_run(result.run)
        saved_issues = [project.sqlite_store.save_issue(issue) for issue in result.issues]
        project.sqlite_store.mark_resolved_issues(
            {issue_fingerprint(issue) for issue in saved_issues}
        )
        result = AuditResult(run=result.run, issues=saved_issues)
    return result
