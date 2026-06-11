"""Audit runner."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .baseline import AuditBaseline, apply_baseline
from .context import AuditContext
from .models import AuditRun, Issue, IssueStatus, Severity
from .registry import RuleConfig, RuleRegistry


@dataclass
class AuditResult:
    run: AuditRun
    issues: list[Issue]

    @property
    def open_errors(self) -> list[Issue]:
        return [
            issue
            for issue in self.issues
            if issue.status is IssueStatus.OPEN and issue.severity is Severity.ERROR
        ]


class AuditRunner:
    def __init__(
        self,
        registry: RuleRegistry,
        *,
        baseline: AuditBaseline | None = None,
        rule_set_version: str = "v2.0",
    ) -> None:
        self.registry = registry
        self.baseline = baseline
        self.rule_set_version = rule_set_version

    def run(self, ctx: AuditContext, *, config: RuleConfig | None = None) -> AuditResult:
        run_id = str(uuid.uuid4())
        issues: list[Issue] = []
        rule_config = config or RuleConfig()
        for rule in self.registry.enabled(rule_config):
            for issue in rule.check(ctx):
                severity = rule_config.severity_overrides.get(issue.rule_code, issue.severity)
                prepared = issue.model_copy(
                    update={
                        "severity": severity,
                        "audit_run_id": run_id,
                    }
                )
                issues.append(apply_baseline(prepared, self.baseline))

        run = AuditRun(
            id=run_id,
            content_hash=ctx.content_hash,
            rule_set_version=self.rule_set_version,
            totals=_totals(issues),
            baseline_delta=_baseline_delta(issues),
        )
        return AuditResult(run=run, issues=issues)


def _totals(issues: list[Issue]) -> dict[str, int]:
    totals = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        if issue.status is IssueStatus.OPEN:
            totals[issue.severity.value] += 1
    return totals


def _baseline_delta(issues: list[Issue]) -> dict[str, int]:
    suppressed = sum(1 for issue in issues if issue.status is IssueStatus.SUPPRESSED)
    open_count = sum(1 for issue in issues if issue.status is IssueStatus.OPEN)
    return {"open": open_count, "suppressed": suppressed}
