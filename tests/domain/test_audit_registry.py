from __future__ import annotations

import pytest

from owcopilot.audit.context import AuditContext
from owcopilot.audit.models import Category, Issue, Severity
from owcopilot.audit.registry import RuleConfig, RuleRegistry


class DemoRule:
    code = "DEMO_RULE"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> list[Issue]:
        return []


class OtherRule:
    code = "OTHER_RULE"
    severity = Severity.WARNING
    category = Category.LORE

    def check(self, ctx: AuditContext) -> list[Issue]:
        return []


def test_rule_registry_registers_and_filters_rules() -> None:
    registry = RuleRegistry([DemoRule(), OtherRule()])

    enabled = registry.enabled(RuleConfig(disabled_rules={"OTHER_RULE"}))

    assert registry.codes() == ["DEMO_RULE", "OTHER_RULE"]
    assert [rule.code for rule in enabled] == ["DEMO_RULE"]


def test_rule_registry_rejects_duplicate_codes() -> None:
    registry = RuleRegistry([DemoRule()])

    with pytest.raises(ValueError, match="duplicate audit rule"):
        registry.register(DemoRule())
