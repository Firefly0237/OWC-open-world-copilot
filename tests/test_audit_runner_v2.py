from __future__ import annotations

from owcopilot.audit.baseline import AuditBaseline, issue_fingerprint
from owcopilot.audit.context import AuditContext
from owcopilot.audit.models import Category, Evidence, Issue, IssueStatus, Severity
from owcopilot.audit.registry import RuleConfig, RuleRegistry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle


class AlwaysIssueRule:
    code = "ALWAYS_ISSUE"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> list[Issue]:
        return [
            Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref="bundle",
                message="Demo issue",
                evidence=[Evidence(kind="field_path", path="entities")],
            )
        ]


def test_audit_runner_sets_run_id_fingerprint_and_totals() -> None:
    ctx = AuditContext.from_bundle(ContentBundle())
    runner = AuditRunner(RuleRegistry([AlwaysIssueRule()]))

    result = runner.run(ctx)

    assert result.run.content_hash == ctx.content_hash
    assert result.run.totals == {"error": 1, "warning": 0, "info": 0}
    assert result.run.baseline_delta == {"open": 1, "suppressed": 0}
    assert result.issues[0].audit_run_id == result.run.id
    assert result.issues[0].fingerprint
    assert len(result.open_errors) == 1


def test_audit_runner_applies_baseline_and_severity_override() -> None:
    issue = AlwaysIssueRule().check(AuditContext.from_bundle(ContentBundle()))[0]
    baseline = AuditBaseline(fingerprints={issue_fingerprint(issue)})
    runner = AuditRunner(RuleRegistry([AlwaysIssueRule()]), baseline=baseline)

    result = runner.run(
        AuditContext.from_bundle(ContentBundle()),
        config=RuleConfig(severity_overrides={"ALWAYS_ISSUE": Severity.WARNING}),
    )

    assert result.issues[0].status is IssueStatus.SUPPRESSED
    assert result.issues[0].severity is Severity.WARNING
    assert result.run.totals == {"error": 0, "warning": 0, "info": 0}
    assert result.run.baseline_delta == {"open": 0, "suppressed": 1}
    assert result.open_errors == []


def test_audit_runner_respects_disabled_rules() -> None:
    runner = AuditRunner(RuleRegistry([AlwaysIssueRule()]))

    result = runner.run(
        AuditContext.from_bundle(ContentBundle()),
        config=RuleConfig(disabled_rules={"ALWAYS_ISSUE"}),
    )

    assert result.issues == []
    assert result.run.totals == {"error": 0, "warning": 0, "info": 0}
