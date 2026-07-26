from __future__ import annotations

import re

from owcopilot.audit.context import AuditContext
from owcopilot.audit.models import Issue
from owcopilot.audit.rules.reference_rules import (
    DeprecatedEntityReferenceRule,
    MissingDialogueReferenceRule,
    MissingEntityReferenceRule,
    MissingPrerequisiteRule,
)
from owcopilot.content.models import (
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    Quest,
    QuestStage,
)

# ---------------------------------------------------------------------------
# 可执行报告改造：消息中文化 + 建议动作实例化的验收（§15.9 六要素里的"原因"与"建议动作"）。
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[一-鿿]")
# 规则实现里真实用到的蛇形字段名/类名，策划面对的文本里不该出现这些代码标识符。
_BANNED_IDENTIFIERS = [
    "giver_npc",
    "controlling_faction",
    "speaker_id",
    "required_entities",
    "dialogue_refs",
    "prerequisites.",
    "target_ref",
    "field_path",
    "quest_event_ref",
]


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


def test_missing_entity_reference_rule_flags_unknown_refs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "quest_missing": Quest(
                    id="quest_missing",
                    title="Missing",
                    giver_npc="npc_missing",
                    location="location_missing",
                )
            }
        )
    )

    issues = list(MissingEntityReferenceRule().check(ctx))

    assert [issue.rule_code for issue in issues] == ["UNKNOWN_ENTITY_REF", "UNKNOWN_ENTITY_REF"]
    assert {issue.evidence[0].path for issue in issues} == {"giver_npc", "location"}


def test_deprecated_entity_reference_rule_flags_deprecated_refs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_old": Entity(
                    id="npc_old",
                    name="Old NPC",
                    type=EntityType.NPC,
                    status="deprecated",
                )
            },
            quests={"quest_old": Quest(id="quest_old", title="Old", giver_npc="npc_old")},
        )
    )

    issues = list(DeprecatedEntityReferenceRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "DEPRECATED_ENTITY_REF"
    assert issues[0].target_ref == "quest:quest_old"


def test_missing_dialogue_reference_rule_flags_dangling_dialogue_refs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={"dlg_existing": DialogueRef(id="dlg_existing", text_key="existing")},
            quests={
                "quest_dialogue": Quest(
                    id="quest_dialogue",
                    title="Dialogue",
                    dialogue_refs=["dlg_missing"],
                )
            },
        )
    )

    issues = list(MissingDialogueReferenceRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "MISSING_DIALOGUE_REF"
    assert issues[0].evidence[0].path == "dialogue_refs.dlg_missing"


def test_missing_prerequisite_rule_flags_unknown_prereqs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", prerequisites=["q_missing"])})
    )

    issues = list(MissingPrerequisiteRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "PREREQ_MISSING"
    assert issues[0].target_ref == "quest:q1"


# ---------------------------------------------------------------------------
# 消息中文化 + 建议动作实例化
# ---------------------------------------------------------------------------


def test_missing_entity_reference_rule_message_and_suggestion_are_actionable() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "quest_missing": Quest(
                    id="quest_missing",
                    title="Missing",
                    giver_npc="npc_missing",
                    location="location_missing",
                    stages=[
                        QuestStage(
                            id="s1",
                            summary="",
                            location="stage_loc_missing",
                            required_entities=["item_missing"],
                        )
                    ],
                )
            }
        )
    )

    issues = list(MissingEntityReferenceRule().check(ctx))
    by_path = {issue.evidence[0].path: issue for issue in issues}

    # 可整字段删除的字段（giver_npc）：建议要和修复器实际会做的事一致——删掉这个字段。
    giver_issue = by_path["giver_npc"]
    _assert_actionable(giver_issue, "npc_missing")
    assert "任务发布人" in giver_issue.message
    assert "任务发布人" in giver_issue.suggested_action
    assert "quest_missing" in giver_issue.message

    # 阶段必要对象列表：修复器只去掉那一个 id，其它保留。
    stage_entities_issue = by_path["stages.0.required_entities"]
    _assert_actionable(stage_entities_issue, "item_missing")
    assert "第 1 阶段" in stage_entities_issue.message
    assert "其它对象保留" in stage_entities_issue.suggested_action

    # 阶段地点：修复器清空该字段。
    stage_location_issue = by_path["stages.0.location"]
    _assert_actionable(stage_location_issue, "stage_loc_missing")
    assert "第 1 阶段" in stage_location_issue.message


def test_deprecated_entity_reference_rule_message_and_suggestion_needs_human_judgement() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_old": Entity(
                    id="npc_old",
                    name="Old NPC",
                    type=EntityType.NPC,
                    status="deprecated",
                )
            },
            quests={"quest_old": Quest(id="quest_old", title="Old", giver_npc="npc_old")},
        )
    )

    issues = list(DeprecatedEntityReferenceRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "npc_old", "quest_old")
    assert "停用" in issue.message
    # 内容里没有记录该换成哪个对象，只能如实说明需要策划判断，不能编一个假的替代 id。
    assert "策划确认" in issue.suggested_action


def test_missing_dialogue_reference_rule_message_and_suggestion_are_actionable() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={"dlg_existing": DialogueRef(id="dlg_existing", text_key="existing")},
            quests={
                "quest_dialogue": Quest(
                    id="quest_dialogue",
                    title="Dialogue",
                    dialogue_refs=["dlg_missing"],
                )
            },
        )
    )

    issues = list(MissingDialogueReferenceRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "dlg_missing", "quest_dialogue")


def test_missing_prerequisite_rule_message_and_suggestion_are_actionable() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", prerequisites=["q_missing"])})
    )

    issues = list(MissingPrerequisiteRule().check(ctx))

    assert len(issues) == 1
    issue = issues[0]
    _assert_actionable(issue, "q_missing", "q1")
