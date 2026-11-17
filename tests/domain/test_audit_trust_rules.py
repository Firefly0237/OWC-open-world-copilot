from __future__ import annotations

import re
from pathlib import Path

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.models import Category, Severity
from owcopilot.audit.rules.trust_rules import UnreviewedAIContentRule
from owcopilot.audit.source_location import ContentSourceLocator
from owcopilot.content.models import ContentBundle, Entity, EntityType, Origin, Quest, ReviewStatus
from owcopilot.content.store import ContentStore

# 中文字符探测：六要素改造要求"原因"用策划语言（中文），不是英文技术描述。
_HAS_CJK = re.compile(r"[一-鿿]")


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


# -- 六要素改造：消息中文化 + 实例化建议动作（policy §15.9） -----------------------------------


def test_unreviewed_ai_content_rule_pending_review_message_and_action() -> None:
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

    issue = list(UnreviewedAIContentRule().check(AuditContext.from_bundle(bundle)))[0]

    assert _HAS_CJK.search(issue.message)
    assert "npc_ai" in issue.message
    assert "review_status" not in issue.message
    assert "review_status" not in issue.suggested_action
    # 具体的目标状态值——不是泛泛的"送去评审"，而是评审通过后要改成的确切状态。
    assert "已通过" in issue.suggested_action


def test_unreviewed_ai_content_rule_rejected_gets_a_different_action() -> None:
    bundle = ContentBundle(
        quests={
            "q_ai": Quest(
                id="q_ai",
                title="AI 生成的任务",
                objective="ok",
                localization_keys=["quest.q_ai.objective"],
                origin=Origin.AI_DRAFT,
                review_status=ReviewStatus.REJECTED,
            )
        }
    )

    issue = list(UnreviewedAIContentRule().check(AuditContext.from_bundle(bundle)))[0]

    assert _HAS_CJK.search(issue.message)
    assert "驳回" in issue.message
    assert "重新提交" in issue.suggested_action
    assert "已通过" not in issue.suggested_action  # 驳回和待评审的处理方式不能混用


def test_unreviewed_ai_content_rule_locates_to_the_entity_file(tmp_path: Path) -> None:
    """顺带核对：这条问题能被既有的定位器落到文件 + 行号（六要素里的"文件/行号"）。"""
    store = ContentStore(tmp_path)
    store.save(
        ContentBundle(
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
    )
    saved = store.load()
    issue = list(UnreviewedAIContentRule().check(AuditContext.from_bundle(saved)))[0]

    annotated = ContentSourceLocator(tmp_path).annotate(issue)

    assert annotated.source_path == "world/entities/npc_ai.json"
    assert annotated.source_line is not None
