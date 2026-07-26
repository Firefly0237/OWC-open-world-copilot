# 审计结果的 Markdown 人类可读渲染。
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
            lines.extend(render_issue_markdown(issue))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_issue_markdown(issue: Issue) -> list[str]:
    """One issue as the report's own bullet block — the exact text a reader gets for that issue.

    Factored out of :func:`render_audit_markdown` so anything that has to reason about "what does
    the report actually tell someone about THIS issue" (the report-actionability measurement)
    reads the shipping renderer instead of re-implementing it, and therefore tracks any future
    change to the issue presentation automatically.
    """
    lines = [f"- `{issue.rule_code}` **{issue.target_ref}** — {issue.message}"]
    # 行业报告 §15.9 的六要素：文件、行号、任务 id、节点 id、原因、建议动作。任务 id 与原因已经在
    # 上面那行里（``target_ref`` 与 ``message``），其余四项按"读的人照着就能改"的顺序排：先说去哪
    # 儿改（文件第几行、哪个节点），再说改成什么（建议动作）。定位不出来的条目不写空行，报告只声称
    # 真定位到的那部分。
    location = _location_line(issue)
    if location:
        lines.append(f"  - 位置:{location}")
    if issue.suggested_action:
        lines.append(f"  - 建议动作:{issue.suggested_action}")
    evidence_line = _evidence_summary(issue)
    if evidence_line:
        lines.append(f"  - evidence: {evidence_line}")
    return lines


def _location_line(issue: Issue) -> str:
    """文件 / 行号 / 节点 id 拼成的一行，三项都没有时返回空串。"""
    parts: list[str] = []
    if issue.source_path:
        where = f"`{issue.source_path}`"
        if issue.source_line is not None:
            where += f" 第 {issue.source_line} 行"
        parts.append(where)
    if issue.node_ref:
        parts.append(f"对话节点 `{issue.node_ref}`")
    return ",".join(parts)


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
