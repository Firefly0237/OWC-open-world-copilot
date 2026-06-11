"""Audit baseline support.

The baseline is a set of accepted issue fingerprints. Runner output keeps those issues but marks
them as suppressed, so CI can fail only on new open errors while still showing full context.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.hash import content_hash
from .models import Issue, IssueStatus


class AuditBaseline(BaseModel):
    fingerprints: set[str] = Field(default_factory=set)

    def contains(self, issue: Issue) -> bool:
        return issue_fingerprint(issue) in self.fingerprints

    def add(self, issue: Issue) -> None:
        self.fingerprints.add(issue_fingerprint(issue))


def issue_fingerprint(issue: Issue) -> str:
    if issue.fingerprint:
        return issue.fingerprint
    evidence = [item.model_dump(mode="json", exclude_none=True) for item in issue.evidence]
    return content_hash(
        {
            "rule_code": issue.rule_code,
            "target_ref": issue.target_ref,
            "evidence": evidence,
        }
    )


def apply_baseline(issue: Issue, baseline: AuditBaseline | None) -> Issue:
    fingerprint = issue_fingerprint(issue)
    status = (
        IssueStatus.SUPPRESSED
        if baseline is not None and fingerprint in baseline.fingerprints
        else issue.status
    )
    return issue.model_copy(update={"fingerprint": fingerprint, "status": status})
