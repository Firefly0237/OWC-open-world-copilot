from __future__ import annotations

from owcopilot.audit.baseline import AuditBaseline, apply_baseline, issue_fingerprint
from owcopilot.audit.models import Category, Evidence, Issue, IssueStatus, Severity


def _issue() -> Issue:
    return Issue(
        rule_code="UNKNOWN_ENTITY",
        severity=Severity.ERROR,
        category=Category.REFERENCE,
        target_ref="quest:q1",
        message="Unknown entity",
        evidence=[Evidence(kind="field_path", path="giver_npc")],
    )


def test_issue_fingerprint_is_stable_for_same_evidence() -> None:
    assert issue_fingerprint(_issue()) == issue_fingerprint(_issue())


def test_apply_baseline_marks_existing_issue_suppressed() -> None:
    issue = _issue()
    baseline = AuditBaseline(fingerprints={issue_fingerprint(issue)})

    suppressed = apply_baseline(issue, baseline)

    assert suppressed.status is IssueStatus.SUPPRESSED
    assert suppressed.fingerprint == issue_fingerprint(issue)


def test_apply_baseline_keeps_new_issue_open() -> None:
    issue = apply_baseline(_issue(), AuditBaseline())

    assert issue.status is IssueStatus.OPEN
    assert issue.fingerprint
