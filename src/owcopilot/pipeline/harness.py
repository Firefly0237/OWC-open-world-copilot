"""Project quality harness for agent/tool-calling workflows.

The individual tools answer narrow questions. This harness answers the production question:
"can this world move forward, and which safe tool call should run next?" It is deterministic by
default and never writes canon content; optional proposals only persist patch candidates.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..audit.models import Issue, IssueStatus, Severity
from ..content.hash import content_hash
from ..readiness import assess_readiness
from .audit import run_full_audit
from .export_gate import ExportBlocker, export_blockers
from .patches import suggest_for_issue
from .project import ProjectContext


class HarnessToolCall(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    purpose: str
    writes_canon: bool = False


class HarnessIssueSummary(BaseModel):
    id: str | None = None
    rule_code: str
    severity: str
    target_ref: str
    message: str


class HarnessPatchProposal(BaseModel):
    issue_id: str
    issue_ref: str
    rule_code: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    rejected_count: int = 0


class HarnessReadinessSummary(BaseModel):
    overall_score: float
    ready_rate: float
    total_items: int
    ready_items: int
    weakest_items: list[dict[str, Any]] = Field(default_factory=list)


class QualityHarnessReport(BaseModel):
    content_hash: str
    phase: str
    stop_reason: str
    export_ready: bool
    audit_totals: dict[str, int]
    top_issues: list[HarnessIssueSummary] = Field(default_factory=list)
    export_blockers: list[dict[str, str]] = Field(default_factory=list)
    readiness: HarnessReadinessSummary
    patch_proposals: list[HarnessPatchProposal] = Field(default_factory=list)
    next_tool_calls: list[HarnessToolCall] = Field(default_factory=list)
    tool_trace: list[str] = Field(default_factory=list)


def run_quality_harness(
    project: ProjectContext,
    *,
    persist_audit: bool = True,
    propose_fixes: bool = True,
    max_issues: int = 5,
    max_candidates_per_issue: int = 1,
) -> QualityHarnessReport:
    """Run the deterministic quality loop used by MCP/tool-calling agents.

    The loop is intentionally conservative:
    audit -> export gate -> readiness -> optional shadow-validated proposals -> next tools.
    It stops before any canon write path. Applying a patch or accepting review stays in CLI/UI.
    """
    max_issues = max(0, max_issues)
    max_candidates_per_issue = max(0, max_candidates_per_issue)
    trace = ["audit_project"]
    audit = run_full_audit(project, persist=persist_audit)
    open_issues = [issue for issue in audit.issues if issue.status is IssueStatus.OPEN]
    top_issues = sorted(
        open_issues,
        key=lambda issue: (
            issue.severity is not Severity.ERROR,
            issue.rule_code,
            issue.target_ref,
        ),
    )[:max_issues]

    trace.append("export_gate")
    blockers = export_blockers(project)

    trace.append("assess_readiness")
    readiness = assess_readiness(project.bundle)

    proposals: list[HarnessPatchProposal] = []
    if propose_fixes and max_candidates_per_issue > 0:
        for issue in top_issues:
            if issue.severity is not Severity.ERROR or not issue.id:
                continue
            trace.append(f"propose_fix:{issue.id}")
            suggestion = suggest_for_issue(
                project,
                issue,
                max_candidates=max_candidates_per_issue,
            )
            proposals.append(
                HarnessPatchProposal(
                    issue_id=issue.id,
                    issue_ref=issue.target_ref,
                    rule_code=issue.rule_code,
                    candidates=[
                        {
                            "patch_id": ranked.candidate.id,
                            "source": ranked.source,
                            "target_resolved": ranked.target_resolved,
                            "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                            "rationale": ranked.candidate.rationale,
                        }
                        for ranked in suggestion.candidates
                    ],
                    rejected_count=suggestion.rejected_count,
                )
            )

    phase, stop_reason = _phase(
        open_errors=audit.open_errors,
        blockers=blockers,
        readiness_ready=readiness.ready_items == readiness.total_items,
    )
    next_calls = _next_tool_calls(
        project=project,
        open_errors=audit.open_errors,
        blockers=blockers,
        proposals=proposals,
        export_ready=not blockers,
    )
    return QualityHarnessReport(
        content_hash=content_hash(project.bundle),
        phase=phase,
        stop_reason=stop_reason,
        export_ready=not blockers,
        audit_totals=audit.run.totals,
        top_issues=[_issue_summary(issue) for issue in top_issues],
        export_blockers=[_blocker_dump(blocker) for blocker in blockers],
        readiness=HarnessReadinessSummary(
            overall_score=readiness.overall_score,
            ready_rate=readiness.ready_rate,
            total_items=readiness.total_items,
            ready_items=readiness.ready_items,
            weakest_items=[
                {
                    "ref": item.ref,
                    "kind": item.kind,
                    "score": item.score,
                    "missing": item.missing,
                }
                for item in sorted(readiness.items, key=lambda item: (item.ready, item.score))[:5]
                if not item.ready
            ],
        ),
        patch_proposals=proposals,
        next_tool_calls=next_calls,
        tool_trace=trace,
    )


def _phase(
    *,
    open_errors: list[Issue],
    blockers: list[ExportBlocker],
    readiness_ready: bool,
) -> tuple[str, str]:
    if open_errors:
        return "repair", "open audit errors must be resolved before export"
    if blockers:
        return "review", "unreviewed AI content must be accepted or rejected before export"
    if not readiness_ready:
        return "complete_design", "canon is correct but not production-complete"
    return "ready_to_export", "audit, review and readiness gates are clear"


def _next_tool_calls(
    *,
    project: ProjectContext,
    open_errors: list[Issue],
    blockers: list[ExportBlocker],
    proposals: list[HarnessPatchProposal],
    export_ready: bool,
) -> list[HarnessToolCall]:
    content_root = str(project.content_root)
    if open_errors:
        calls: list[HarnessToolCall] = [
            HarnessToolCall(
                tool="list_issues",
                args={"content_root": content_root, "status": "open"},
                purpose="show the current blocking audit issues",
            )
        ]
        proposed_issue_ids = {proposal.issue_id for proposal in proposals if proposal.candidates}
        for issue in open_errors[:3]:
            if issue.id and issue.id not in proposed_issue_ids:
                calls.append(
                    HarnessToolCall(
                        tool="propose_fix",
                        args={"content_root": content_root, "issue_id": issue.id},
                        purpose="generate shadow-validated fix candidates for this issue",
                    )
                )
        return calls
    if blockers:
        return [
            HarnessToolCall(
                tool="audit_project",
                args={"content_root": content_root, "persist": True},
                purpose="refresh provenance/audit state after review decisions in the UI",
            )
        ]
    if export_ready:
        return [
            HarnessToolCall(
                tool="export_project",
                args={"content_root": content_root, "target_engine": "generic"},
                purpose="write a release bundle after all gates are clear",
            )
        ]
    return []


def _issue_summary(issue: Issue) -> HarnessIssueSummary:
    return HarnessIssueSummary(
        id=issue.id,
        rule_code=issue.rule_code,
        severity=issue.severity.value,
        target_ref=issue.target_ref,
        message=issue.message,
    )


def _blocker_dump(blocker: ExportBlocker) -> dict[str, str]:
    return {
        "code": blocker.code,
        "target_ref": blocker.target_ref,
        "message": blocker.message,
    }
