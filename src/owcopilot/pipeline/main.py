"""Fixed workflow stage entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field

from ..audit.runner import AuditResult
from .audit import run_full_audit
from .project import ProjectContext


class PipelineStage(str, Enum):
    INGEST = "INGEST"
    NORMALIZE = "NORMALIZE"
    INDEX = "INDEX"
    AUDIT = "AUDIT"
    SUGGEST = "SUGGEST"
    REVIEW = "REVIEW"
    EXPORT = "EXPORT"


class PipelineRunSummary(BaseModel):
    completed: list[PipelineStage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@dataclass
class AuditWorkflowResult:
    summary: PipelineRunSummary
    audit: AuditResult


def run_audit_workflow(project: ProjectContext, *, persist: bool = True) -> AuditWorkflowResult:
    project.reload()
    audit = run_full_audit(project, persist=persist)
    return AuditWorkflowResult(
        summary=PipelineRunSummary(
            completed=[PipelineStage.INDEX, PipelineStage.AUDIT],
            notes=[f"open_errors={len(audit.open_errors)}"],
        ),
        audit=audit,
    )
