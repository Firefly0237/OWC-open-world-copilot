from __future__ import annotations

import re

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.lore_rules import (
    CharacterStateContradictionRule,
    EventResultReferencedTooEarlyRule,
    TimelineViolationRule,
)
from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    QuestEventReference,
    QuestEventRefKind,
)

# -- 可执行报告改造：中文消息 + 实例化建议动作的机械核验 -------------------------------------------
# 三条机械检查（计划 §三）：消息是中文、建议含这一条实例的具体字段名或值、消息与建议都不含代码
# 标识符。对象 id（quest/entity 的 id）与内容数据本身（标签值等）不算代码标识符，允许出现。

_HAS_CJK = re.compile(r"[一-鿿]")

# 这几条规则背后的字段名——一旦泄露到消息或建议里，就是策划看不懂的黑话。
_FORBIDDEN_IDENTIFIERS = (
    "timeline_order",
    "prerequisites",
    "quest_order",
    "prereq_order",
    "event_order",
    "quest_event_refs",
    "references_event_results",
    "ref_kind",
    "status",
    "entity.tags",
    "target_ref",
)


def _assert_planner_readable(text: str) -> None:
    assert _HAS_CJK.search(text), f"不是中文: {text!r}"
    for identifier in _FORBIDDEN_IDENTIFIERS:
        assert identifier not in text, f"消息/建议里混进了代码标识符 {identifier!r}: {text!r}"


def test_timeline_violation_rule_flags_late_prerequisite() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", timeline_order=1, prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", timeline_order=2),
            }
        )
    )

    issues = list(TimelineViolationRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "TIMELINE_VIOLATION"


def test_timeline_violation_rule_skips_prerequisite_cycles() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", timeline_order=1, prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", timeline_order=2, prerequisites=["q1"]),
            }
        )
    )

    issues = list(TimelineViolationRule().check(ctx))

    assert issues == []


def test_event_result_referenced_too_early_rule_flags_future_event_result() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "event_siege": Entity(
                    id="event_siege",
                    name="Siege",
                    type=EntityType.EVENT,
                    metadata={"timeline_order": 5},
                )
            },
            quests={
                "q1": Quest(
                    id="q1",
                    title="Q1",
                    timeline_order=3,
                    metadata={"references_event_results": ["event_siege"]},
                )
            },
        )
    )

    issues = list(EventResultReferencedTooEarlyRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "EVENT_RESULT_REFERENCED_TOO_EARLY"


def test_event_result_rule_uses_ref_kind_not_any_event_mention() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "event_siege": Entity(
                    id="event_siege",
                    name="Siege",
                    type=EntityType.EVENT,
                    metadata={"timeline_order": 5},
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", timeline_order=3)},
            quest_event_refs={
                "mention": QuestEventReference(
                    id="mention",
                    quest_id="q1",
                    event_id="event_siege",
                    ref_kind=QuestEventRefKind.MENTIONS_EVENT,
                ),
                "result": QuestEventReference(
                    id="result",
                    quest_id="q1",
                    event_id="event_siege",
                    ref_kind=QuestEventRefKind.REFERENCES_RESULT,
                ),
            },
        )
    )

    issues = list(EventResultReferencedTooEarlyRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].evidence[0].path == "quest_event_refs.result"


def test_character_state_contradiction_rule_flags_dead_active_entity() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_dead": Entity(
                    id="npc_dead",
                    name="Dead",
                    type=EntityType.NPC,
                    status="dead",
                    tags=["active"],
                )
            }
        )
    )

    issues = list(CharacterStateContradictionRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "CHARACTER_STATE_CONTRADICTION"


# -- 中文消息 + 实例化建议动作 ----------------------------------------------------------------


def test_timeline_violation_rule_message_and_suggestion_are_planner_readable() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", timeline_order=1, prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", timeline_order=2),
            }
        )
    )

    (issue,) = list(TimelineViolationRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    # 任务 id 允许出现——策划就是靠它定位。
    assert "q1" in issue.message and "q2" in issue.message
    # 建议必须带这一条实例真实的时间线数字（1 和 2），不能只是泛泛的规则描述。
    assert "1" in issue.suggested_action
    assert "2" in issue.suggested_action


def test_event_result_rule_suggestion_via_formal_reference_offers_ref_kind_change() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "event_siege": Entity(
                    id="event_siege",
                    name="Siege",
                    type=EntityType.EVENT,
                    metadata={"timeline_order": 5},
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", timeline_order=3)},
            quest_event_refs={
                "result": QuestEventReference(
                    id="result",
                    quest_id="q1",
                    event_id="event_siege",
                    ref_kind=QuestEventRefKind.REFERENCES_RESULT,
                ),
            },
        )
    )

    (issue,) = list(EventResultReferencedTooEarlyRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "3" in issue.suggested_action  # 任务的时间线顺序号
    assert "5" in issue.suggested_action  # 事件的时间线顺序号
    # 走正式引用这条路径时，第三个选项是改引用类型，不是删列表项。
    assert "依赖事件结果" in issue.suggested_action
    assert "只是提到这个事件" in issue.suggested_action


def test_event_result_rule_suggestion_via_metadata_list_offers_removal() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "event_siege": Entity(
                    id="event_siege",
                    name="Siege",
                    type=EntityType.EVENT,
                    metadata={"timeline_order": 5},
                )
            },
            quests={
                "q1": Quest(
                    id="q1",
                    title="Q1",
                    timeline_order=3,
                    metadata={"references_event_results": ["event_siege"]},
                )
            },
        )
    )

    (issue,) = list(EventResultReferencedTooEarlyRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    # 走 metadata 列表这条路径时，第三个选项是把这个事件 id 从列表里删掉。
    assert "event_siege" in issue.suggested_action
    assert "删掉" in issue.suggested_action


def test_character_state_contradiction_rule_message_and_suggestion_keep_original_tag_case() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_dead": Entity(
                    id="npc_dead",
                    name="Dead",
                    type=EntityType.NPC,
                    status="dead",
                    tags=["Active"],  # 大写写法：消息/建议必须照抄原文，不能悄悄换成全小写
                )
            }
        )
    )

    (issue,) = list(CharacterStateContradictionRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "npc_dead" in issue.message
    assert "Active" in issue.message  # 保留策划写的原始大小写
    assert "Active" in issue.suggested_action
    assert "dead" in issue.message  # 具体的状态值
