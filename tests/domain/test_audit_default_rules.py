from __future__ import annotations

from owcopilot.audit.default_rules import build_default_rule_registry


def test_default_rule_registry_contains_at_least_twenty_rules() -> None:
    registry = build_default_rule_registry()

    assert len(registry.codes()) >= 20
    assert "UNKNOWN_ENTITY_REF" in registry.codes()
    assert "REGION_BANNED_CONTENT_USED" in registry.codes()
    assert "UNREVIEWED_AI_CONTENT" in registry.codes()
