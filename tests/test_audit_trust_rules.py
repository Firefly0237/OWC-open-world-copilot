from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.models import Category, Severity
from owcopilot.audit.rules.trust_rules import UnreviewedAIContentRule
from owcopilot.content.models import ContentBundle, Entity, EntityType, Origin, ReviewStatus


def test_unreviewed_ai_content_rule_flags_pending_ai_draft() -> None:
    bundle = ContentBundle(
        entities={
            "npc_ai": Entity(
                id="npc_ai",
                name="AI",
                type=EntityType.NPC,
                origin=Origin.AI_DRAFT,
                review_status=ReviewStatus.PENDING_REVIEW,
            )
        }
    )

    issues = list(UnreviewedAIContentRule().check(AuditContext.from_bundle(bundle)))

    assert len(issues) == 1
    assert issues[0].rule_code == "UNREVIEWED_AI_CONTENT"
    assert issues[0].severity is Severity.WARNING
    assert issues[0].category is Category.TRUST
    assert issues[0].target_ref == "entity:npc_ai"


def test_unreviewed_ai_content_rule_ignores_human_content() -> None:
    bundle = ContentBundle(
        entities={"npc_human": Entity(id="npc_human", name="Human", type=EntityType.NPC)}
    )

    assert list(UnreviewedAIContentRule().check(AuditContext.from_bundle(bundle))) == []


def test_default_rule_registry_includes_trust_rule() -> None:
    assert "UNREVIEWED_AI_CONTENT" in build_default_rule_registry().codes()
