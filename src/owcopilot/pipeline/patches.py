"""Patch workflows shared by the CLI, REST and MCP entry points.

The heavy lifting lives in `patches.*` services; this module owns the project-level glue —
look up the issue, persist proposals, re-validate against the *current* content before an apply,
record the actor and rollback ops, and re-run the audit afterwards — so all three interfaces
behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.models import Issue
from ..llm.gateway import LLMGateway
from ..patches import (
    PatchCandidate,
    PatchOperation,
    PatchSuggestService,
    SuggestResult,
    apply_patch_shadow,
    apply_patch_to_store,
    rollback_patch_in_store,
)
from .audit import run_full_audit
from .project import ProjectContext


def find_issue(project: ProjectContext, issue_id: str) -> Issue:
    for issue in project.sqlite_store.list_issues():
        if issue.id == issue_id:
            return issue
    raise FileNotFoundError(f"issue not found: {issue_id} (run an audit first, then list issues)")


def suggest_for_issue(
    project: ProjectContext,
    issue: Issue,
    *,
    gateway: LLMGateway | None = None,
    max_candidates: int = 3,
    budget_tokens: int = 600,
) -> SuggestResult:
    """Run the suggest service and persist surviving candidates as proposed patches."""
    service = PatchSuggestService(
        bundle=project.bundle,
        audit_runner=project.audit_runner,
        gateway=gateway,
        context_builder=project.context_builder,
    )
    result = service.suggest(issue, max_candidates=max_candidates, budget_tokens=budget_tokens)
    for ranked in result.candidates:
        project.sqlite_store.save_patch(
            {
                "id": ranked.candidate.id,
                "issue_id": ranked.candidate.issue_id,
                "status": "proposed",
                "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                "rationale": ranked.candidate.rationale,
                "evidence": [
                    *ranked.candidate.evidence,
                    {
                        "source": ranked.source,
                        "target_resolved": ranked.target_resolved,
                        "resolved_error_count": len(ranked.resolved_errors),
                    },
                ],
                "origin": ranked.candidate.origin.value,
            }
        )
    return result


@dataclass
class ApplyOutcome:
    applied: bool
    patch_id: str
    reason: str = ""
    introduced_errors: list[str] = field(default_factory=list)
    resolved_errors: list[str] = field(default_factory=list)
    rollback_ops_count: int = 0
    post_audit_open_errors: int = 0


def apply_patch_workflow(project: ProjectContext, patch_id: str, *, operator: str) -> ApplyOutcome:
    stored = project.sqlite_store.get_patch(patch_id)
    if stored is None:
        raise FileNotFoundError(f"patch not found: {patch_id}")
    if stored["status"] != "proposed":
        raise ValueError(f"patch {patch_id} has status '{stored['status']}', expected 'proposed'")
    candidate = candidate_from_stored(stored)

    before = project.audit_runner.run(AuditContext.from_bundle(project.bundle))
    before_errors = {issue_fingerprint(item) for item in before.open_errors}
    patched = apply_patch_shadow(project.bundle, candidate.ops)
    after = project.audit_runner.run(AuditContext.from_bundle(patched))
    after_errors = {issue_fingerprint(item) for item in after.open_errors}
    introduced = sorted(after_errors - before_errors)
    if introduced:
        return ApplyOutcome(
            applied=False,
            patch_id=patch_id,
            reason="patch would introduce new open errors on the current content",
            introduced_errors=introduced,
        )

    applied = apply_patch_to_store(project.content_store, candidate, applied_by=operator)
    project.sqlite_store.update_patch(
        patch_id,
        status="applied",
        applied_by=operator,
        applied_at=datetime.now(UTC).isoformat(),
        rollback_ops=[op.model_dump(mode="json") for op in applied.rollback_ops],
    )
    project.reload()
    audit = run_full_audit(project, persist=True)
    return ApplyOutcome(
        applied=True,
        patch_id=patch_id,
        resolved_errors=sorted(before_errors - after_errors),
        rollback_ops_count=len(applied.rollback_ops),
        post_audit_open_errors=len(audit.open_errors),
    )


@dataclass
class RollbackOutcome:
    rolled_back: bool
    patch_id: str
    post_audit_open_errors: int = 0


def rollback_patch_workflow(
    project: ProjectContext, patch_id: str, *, operator: str
) -> RollbackOutcome:
    stored = project.sqlite_store.get_patch(patch_id)
    if stored is None:
        raise FileNotFoundError(f"patch not found: {patch_id}")
    if stored["status"] != "applied":
        raise ValueError(f"patch {patch_id} has status '{stored['status']}', expected 'applied'")
    rollback_ops = stored.get("rollback_ops") or []
    if not rollback_ops:
        raise ValueError(f"patch {patch_id} has no stored rollback operations")
    ops = [PatchOperation.model_validate(op) for op in rollback_ops]
    rollback_patch_in_store(project.content_store, ops)
    project.sqlite_store.update_patch(
        patch_id,
        status="rolled_back",
        rolled_back_by=operator,
        rolled_back_at=datetime.now(UTC).isoformat(),
    )
    project.reload()
    audit = run_full_audit(project, persist=True)
    return RollbackOutcome(
        rolled_back=True,
        patch_id=patch_id,
        post_audit_open_errors=len(audit.open_errors),
    )


def candidate_from_stored(stored: dict[str, Any]) -> PatchCandidate:
    return PatchCandidate(
        id=stored["id"],
        issue_id=stored.get("issue_id"),
        ops=[PatchOperation.model_validate(op) for op in stored["ops"]],
        rationale=stored.get("rationale") or "",
        evidence=[item for item in stored.get("evidence") or [] if isinstance(item, dict)],
    )
