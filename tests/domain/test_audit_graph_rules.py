from __future__ import annotations

import re

from owcopilot.audit.context import AuditContext
from owcopilot.audit.models import Issue
from owcopilot.audit.rules.graph_rules import (
    DuplicateRelationRule,
    FactionConflictRule,
    MissingRelationEndpointRule,
    PrerequisiteCycleRule,
    RelationshipConflictRule,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation, SourceRef

# ---------------------------------------------------------------------------
# 可执行报告改造：消息中文化 + 建议动作实例化的验收（§15.9 六要素里的"原因"与"建议动作"）。
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[一-鿿]")
# 规则实现里真实用到的蛇形字段名/类名，策划面对的文本里不该出现这些代码标识符。
_BANNED_IDENTIFIERS = ["target_ref", "field_path", "source_ref", "giver_npc"]


def _assert_actionable(issue: Issue, *expected_values: str) -> None:
    """消息是中文、建议含具体字段名或值、两处都不含代码标识符 —— 三条机械检查。"""
    assert _CJK.search(issue.message), issue.message
    assert issue.suggested_action, "建议动作不能为空"
    assert _CJK.search(issue.suggested_action), issue.suggested_action
    for banned in _BANNED_IDENTIFIERS:
        assert banned not in issue.message, f"消息里混进了代码标识符 {banned!r}"
        assert banned not in issue.suggested_action, f"建议动作里混进了代码标识符 {banned!r}"
    for value in expected_values:
        assert value in issue.suggested_action, f"建议动作里没有具体值 {value!r}"


def test_missing_relation_endpoint_rule_flags_unknown_relation_entities() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[Relation(source="npc_missing", target="faction_a", kind="member_of")]
        )
    )

    issues = list(MissingRelationEndpointRule().check(ctx))

    assert [issue.rule_code for issue in issues] == [
        "MISSING_RELATION_ENDPOINT",
        "MISSING_RELATION_ENDPOINT",
    ]


def test_duplicate_relation_rule_flags_duplicate_triples() -> None:
    relation = Relation(source="npc_a", target="faction_a", kind="member_of")
    ctx = AuditContext.from_bundle(ContentBundle(relations=[relation, relation]))

    issues = list(DuplicateRelationRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "DUPLICATE_RELATION"


def test_duplicate_relation_rule_allows_cross_source_repeated_facts() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[
                Relation(
                    source="faction_a",
                    target="faction_b",
                    kind="allied_with",
                    source_ref=SourceRef(path="style.md"),
                ),
                Relation(
                    source="faction_a",
                    target="faction_b",
                    kind="allied_with",
                    source_ref=SourceRef(path="relations.xlsx"),
                ),
            ]
        )
    )

    issues = list(DuplicateRelationRule().check(ctx))

    assert issues == []


def test_relationship_conflict_rule_flags_enemy_and_ally_pair() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[
                Relation(source="faction_a", target="faction_b", kind="allied_with"),
                Relation(source="faction_b", target="faction_a", kind="enemy_of"),
            ]
        )
    )

    issues = list(RelationshipConflictRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "RELATION_CONFLICT"


def test_prerequisite_cycle_rule_flags_quest_cycles() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", prerequisites=["q1"]),
            }
        )
    )

    issues = list(PrerequisiteCycleRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "PREREQ_CYCLE"


def test_faction_conflict_rule_flags_enemy_controlled_location() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC),
                "loc_b": Entity(id="loc_b", name="B", type=EntityType.LOCATION),
                "faction_a": Entity(id="faction_a", name="A", type=EntityType.FACTION),
                "faction_b": Entity(id="faction_b", name="B", type=EntityType.FACTION),
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_a", location="loc_b")},
            relations=[
                Relation(source="npc_a", target="faction_a", kind="member_of"),
                Relation(source="loc_b", target="faction_b", kind="controlled_by"),
                Relation(source="faction_a", target="faction_b", kind="enemy_of"),
            ],
        )
    )

    issues = list(FactionConflictRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "FACTION_CONFLICT"


def test_faction_conflict_rule_does_not_flag_main_quests() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC),
                "loc_b": Entity(id="loc_b", name="B", type=EntityType.LOCATION),
                "faction_a": Entity(id="faction_a", name="A", type=EntityType.FACTION),
                "faction_b": Entity(id="faction_b", name="B", type=EntityType.FACTION),
            },
            quests={
                "q_main": Quest(
                    id="q_main",
                    title="Main",
                    giver_npc="npc_a",
                    location="loc_b",
                    tags=["main"],
                ),
                "q_side": Quest(
                    id="q_side",
                    title="Side",
                    giver_npc="npc_a",
                    location="loc_b",
                    tags=["side"],
                ),
            },
            relations=[
                Relation(source="npc_a", target="faction_a", kind="member_of"),
                Relation(source="loc_b", target="faction_b", kind="controlled_by"),
                Relation(source="faction_a", target="faction_b", kind="enemy_of"),
            ],
        )
    )

    issues = list(FactionConflictRule().check(ctx))

    assert [issue.target_ref for issue in issues] == ["quest:q_side"]


# ---------------------------------------------------------------------------
# 消息中文化 + 建议动作实例化
# ---------------------------------------------------------------------------


def test_missing_relation_endpoint_rule_message_and_suggestion_are_actionable() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[Relation(source="npc_missing", target="faction_a", kind="member_of")]
        )
    )

    issues = list(MissingRelationEndpointRule().check(ctx))

    assert len(issues) == 2
    source_issue = next(i for i in issues if i.evidence[0].path == "source")
    _assert_actionable(source_issue, "npc_missing", "faction_a", "member_of")
    assert "来源" in source_issue.message


def test_duplicate_relation_rule_message_and_suggestion_are_actionable() -> None:
    relation = Relation(source="npc_a", target="faction_a", kind="member_of")
    ctx = AuditContext.from_bundle(ContentBundle(relations=[relation, relation, relation]))

    issues = list(DuplicateRelationRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "npc_a", "faction_a", "member_of")
    assert "3" in issue.message
    assert "2" in issue.suggested_action  # 3 份里删掉 2 份重复的，保留第一条


def test_relationship_conflict_rule_message_and_suggestion_needs_human_judgement() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[
                Relation(source="faction_a", target="faction_b", kind="allied_with"),
                Relation(source="faction_b", target="faction_a", kind="enemy_of"),
            ]
        )
    )

    issues = list(RelationshipConflictRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "faction_a", "faction_b")
    # 留哪条关系是设定判断，报告如实说明需要人来确认，不代为拍板。
    assert "策划确认" in issue.suggested_action


def test_prerequisite_cycle_rule_message_and_suggestion_are_actionable() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", prerequisites=["q1"]),
            }
        )
    )

    issues = list(PrerequisiteCycleRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "q1", "q2")


def test_faction_conflict_rule_message_and_suggestion_needs_human_judgement() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC),
                "loc_b": Entity(id="loc_b", name="B", type=EntityType.LOCATION),
                "faction_a": Entity(id="faction_a", name="A", type=EntityType.FACTION),
                "faction_b": Entity(id="faction_b", name="B", type=EntityType.FACTION),
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_a", location="loc_b")},
            relations=[
                Relation(source="npc_a", target="faction_a", kind="member_of"),
                Relation(source="loc_b", target="faction_b", kind="controlled_by"),
                Relation(source="faction_a", target="faction_b", kind="enemy_of"),
            ],
        )
    )

    issues = list(FactionConflictRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "q1", "faction_a")
    # 换地点/换发布人/标记为主线 三选一是设计判断，报告如实说明需要人来确认。
    assert "策划确认" in issue.suggested_action
