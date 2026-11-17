"""Revision evidence exercises the production stepper and formal checkers directly."""

from owcopilot.content.models import (
    Branch,
    Effect,
    LogicVar,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.workflow.models import WorkflowNode, WorkflowSpec
from owcopilot.workflow.revision import affected_operations, build_intent_report, compare_quests


def test_missing_old_choice_does_not_fall_back_to_another_valid_ending() -> None:
    before = Quest(
        id="gate",
        title="入城",
        stages=[QuestStage(id="gate", summary="选择入口")],
        logic=QuestLogic(
            branches=[
                Branch(id="bribe", from_stage="gate", outcome="入城"),
                Branch(id="sea", from_stage="gate", outcome="入城"),
            ]
        ),
    )
    after = before.model_copy(deep=True)
    after.logic.branches = [after.logic.branches[1]]
    report = compare_quests(before, after)
    assert report["replay"]["status"] == "invalid_choice"
    assert report["replay"]["consumed_choices"] == 0
    assert report["replay"]["steps"] == []
    assert any(
        row["branch_id"] == "sea" and row["reachable"] for row in report["after"]["outcomes"]
    )


def test_disabled_old_choice_is_reported_without_taking_default_branch() -> None:
    before = Quest(
        id="gate",
        title="入城",
        stages=[QuestStage(id="gate", summary="选择")],
        logic=QuestLogic(
            branches=[
                Branch(id="bribe", from_stage="gate", outcome="入城"),
                Branch(id="sea", from_stage="gate", outcome="海路"),
            ]
        ),
    )
    after = before.model_copy(deep=True)
    after.logic.branches[0].condition = "false"
    report = compare_quests(before, after)
    assert report["replay"]["status"] == "invalid_choice"
    assert report["after"]["outcomes"][1]["reachable"]


def test_replayed_branch_and_changed_ending_are_separate_facts() -> None:
    before = Quest(
        id="gate",
        title="入城",
        stages=[QuestStage(id="gate", summary="进入")],
        logic=QuestLogic(branches=[Branch(id="enter", from_stage="gate", outcome="平安")]),
    )
    after = before.model_copy(deep=True)
    after.logic.branches[0].outcome = "被捕"
    report = compare_quests(before, after)
    assert report["replay"]["status"] == "replayed"
    assert report["replay"]["outcome"] == "被捕"
    assert report["replay"]["outcome_preserved"] is False


def test_explicit_initial_state_replays_but_does_not_relabel_default_exploration() -> None:
    quest = Quest(
        id="gate",
        title="入城",
        stages=[QuestStage(id="gate", summary="通行")],
        logic=QuestLogic(
            variables=[LogicVar(id="permit", default=False)],
            branches=[Branch(id="enter", from_stage="gate", condition="permit", outcome="通过")],
        ),
    )
    report = compare_quests(quest, quest, initial_state={"permit": True})
    assert report["replay"]["status"] == "replayed"
    assert report["before"]["status"] == "withheld"
    assert report["soft_lock"]["after"]["status"] == "withheld"
    assert report["scope"]["after_initial_state"] == {"permit": True}


def test_text_intent_is_withheld_while_declared_property_can_regress() -> None:
    before = Quest(
        id="medicine",
        title="一剂药",
        stages=[QuestStage(id="choose", summary="选择救谁")],
        logic=QuestLogic(variables=[LogicVar(id="dose", type="int", default=1)]),
    )
    after = before.model_copy(deep=True)
    after.logic.stage_logic = [
        StageLogic(stage_id="choose", effects_on_complete=[Effect(var="dose", op="set", value=0)])
    ]
    report = build_intent_report(
        before,
        after,
        protected=["选择仍要有代价"],
        formal_specs=[{"id": "keep_dose", "pattern": "AG", "expression": "dose >= 1"}],
    )
    assert report["protected"][0]["status"] == "withheld"
    assert report["formal"][0]["before"]["status"] == "proven"
    assert report["formal"][0]["after"]["status"] == "violated"
    assert report["formal"][0]["change"] == "regressed"


def test_different_default_initial_states_are_not_claimed_as_same_context() -> None:
    before = Quest(
        id="gate",
        title="入城",
        stages=[QuestStage(id="gate", summary="通行")],
        logic=QuestLogic(variables=[LogicVar(id="permit", default=False)]),
    )
    after = before.model_copy(deep=True)
    after.logic.variables[0].default = True
    report = compare_quests(before, after)
    assert report["scope"]["same_initial_state"] is False
    assert report["outcome_changes"]["same_initial_state"] is False


def test_missing_logic_is_not_reported_as_a_proven_property() -> None:
    quest = Quest(id="plain", title="只有正文", stages=[QuestStage(id="a", summary="正文")])
    report = compare_quests(quest, quest, protected=["保留动机"])
    assert report["status"] == "unsupported"
    assert report["intent"]["protected"][0]["status"] == "withheld"
    intent = build_intent_report(
        quest,
        quest,
        formal_specs=[{"pattern": "AG", "expression": "true"}],
    )
    assert intent["formal"][0]["after"]["status"] == "unsupported"


def test_optional_report_dependencies_are_invalidated_too() -> None:
    spec = WorkflowSpec(
        name="pipeline",
        nodes=[
            WorkflowNode(id="events", operation="plan_events"),
            WorkflowNode(id="quest", operation="draft_quest", depends_on=["events"]),
            WorkflowNode(id="logic", operation="draft_logic", depends_on=["quest"]),
            WorkflowNode(id="audit", operation="audit", optional_dependencies=["logic"]),
            WorkflowNode(id="summary", operation="package", optional_dependencies=["audit"]),
        ],
    )
    assert affected_operations(spec, "draft_quest") == [
        "draft_quest",
        "draft_logic",
        "audit",
        "package",
    ]


def test_truncated_exploration_is_bounded_and_not_a_strong_proof() -> None:
    quest = Quest(
        id="path",
        title="旅程",
        stages=[QuestStage(id="a", summary="出发"), QuestStage(id="b", summary="抵达")],
        logic=QuestLogic(branches=[Branch(id="next", from_stage="a", to_stage="b")]),
    )
    report = compare_quests(quest, quest, max_states=1)
    assert report["truncated"] is True
    assert report["soft_lock"]["after"]["strong"] is False
    assert report["soft_lock"]["after"]["status"] != "proven"


def test_bad_or_scoped_specs_are_visible_instead_of_silently_ignored() -> None:
    quest = Quest(id="q", title="任务", logic=QuestLogic())
    report = build_intent_report(
        quest,
        quest,
        formal_specs=[
            {"pattern": "not-supported"},
            {"pattern": "AG", "expression": "true", "scope": "another-quest"},
        ],
    )
    assert [row["status"] for row in report["formal"]] == ["error", "error"]
