"""Human-readable Markdown rendering of an audit result.

JSON stays the machine interface; this report is what gets pasted into a chat channel or
attached to a CI run so non-engineers can read the outcome without tooling.
"""

from __future__ import annotations

from collections import Counter

from .models import Issue, IssueStatus, Severity
from .runner import AuditResult

_SEVERITY_ORDER = [Severity.ERROR, Severity.WARNING, Severity.INFO]
_SEVERITY_LABEL = {
    Severity.ERROR: "Errors",
    Severity.WARNING: "Warnings",
    Severity.INFO: "Info",
}


def render_audit_markdown(
    result: AuditResult, *, content_hash: str, title: str = "Content audit report"
) -> str:
    run = result.run
    open_issues = [issue for issue in result.issues if issue.status is IssueStatus.OPEN]
    suppressed = sum(1 for issue in result.issues if issue.status is IssueStatus.SUPPRESSED)

    lines: list[str] = [
        f"# {title}",
        "",
        f"- Audit run: `{run.id}`",
        f"- Content hash: `{content_hash}`",
        f"- Started at: {run.started_at.isoformat()}",
        f"- Rule set: {run.rule_set_version}",
        (
            f"- Open: **{run.totals.get('error', 0)} error / "
            f"{run.totals.get('warning', 0)} warning / {run.totals.get('info', 0)} info**"
            + (f" (+{suppressed} baseline-suppressed)" if suppressed else "")
        ),
        "",
    ]

    if not open_issues:
        lines.append("No open issues. ✅")
        return "\n".join(lines) + "\n"

    rule_counts = Counter(issue.rule_code for issue in open_issues)
    lines.append("## Issues by rule")
    lines.append("")
    lines.append("| Rule | Open count |")
    lines.append("|---|---:|")
    for rule_code, count in rule_counts.most_common():
        lines.append(f"| `{rule_code}` | {count} |")
    lines.append("")

    for severity in _SEVERITY_ORDER:
        block = [issue for issue in open_issues if issue.severity is severity]
        if not block:
            continue
        lines.append(f"## {_SEVERITY_LABEL[severity]} ({len(block)})")
        lines.append("")
        for issue in sorted(block, key=lambda item: (item.rule_code, item.target_ref)):
            lines.append(f"- `{issue.rule_code}` **{issue.target_ref}** — {issue.message}")
            evidence_line = _evidence_summary(issue)
            if evidence_line:
                lines.append(f"  - evidence: {evidence_line}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _evidence_summary(issue: Issue) -> str:
    parts: list[str] = []
    for evidence in issue.evidence[:3]:
        if evidence.relation is not None:
            source, kind, target = evidence.relation
            parts.append(f"({source} -{kind}-> {target})")
        elif evidence.path:
            parts.append(f"`{evidence.path}`")
    if len(issue.evidence) > 3:
        parts.append(f"… +{len(issue.evidence) - 3} more")
    return ", ".join(parts)
