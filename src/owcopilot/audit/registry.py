"""Rule registry and rule configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import Severity
from .rule import Rule


class RuleConfig(BaseModel):
    enabled_rules: set[str] | None = None
    disabled_rules: set[str] = Field(default_factory=set)
    severity_overrides: dict[str, Severity] = Field(default_factory=dict)

    def is_enabled(self, code: str) -> bool:
        if code in self.disabled_rules:
            return False
        return self.enabled_rules is None or code in self.enabled_rules


class RuleRegistry:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: dict[str, Rule] = {}
        for rule in rules or []:
            self.register(rule)

    def register(self, rule: Rule) -> None:
        if rule.code in self._rules:
            raise ValueError(f"duplicate audit rule: {rule.code}")
        self._rules[rule.code] = rule

    def get(self, code: str) -> Rule:
        return self._rules[code]

    def enabled(self, config: RuleConfig | None = None) -> list[Rule]:
        rule_config = config or RuleConfig()
        return [rule for rule in self._rules.values() if rule_config.is_enabled(rule.code)]

    def codes(self) -> list[str]:
        return sorted(self._rules)
