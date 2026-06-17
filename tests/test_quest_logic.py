"""WS-A · native quest logic/state layer: contract, safe expression engine, deterministic audit."""

from __future__ import annotations

import pytest

from owcopilot.content.models import (
    Branch,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.logic import (
    LogicSyntaxError,
    WorldState,
    audit_quest_logic,
    evaluate,
    parse_expr,
    type_errors,
)


# --------------------------------------------------------------- S1 contract
def test_quest_logic_round_trips_and_legacy_is_unchanged() -> None:
    quest = Quest(
        id="q1",
        title="盐风驰援",
        stages=[QuestStage(id="s1", summary="出发"), QuestStage(id="s2", summary="抵达")],
        logic=QuestLogic(
            variables=[LogicVar(id="has_token", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s2",
                    precondition="has_token",
                    effects_on_complete=[Effect(var="has_token", op="set", value=True)],
                )
            ],
        ),
    )
    restored = Quest.model_validate(quest.model_dump(mode="json"))
    assert restored.logic is not None
    assert restored.logic.variables[0].id == "has_token"
    assert restored.logic.stage_logic[0].precondition == "has_token"
    # a legacy quest with no logic still validates and stays None
    assert Quest.model_validate({"id": "q0", "title": "老任务"}).logic is None


# --------------------------------------------------------------- S2 expression engine
def test_parse_and_evaluate_boolean_and_int() -> None:
    expr = parse_expr("has_token and (gold >= 50 or vip)")
    state = {"has_token": True, "gold": 40, "vip": True}
    assert evaluate(expr, state) is True
    assert evaluate(parse_expr("not has_token"), {"has_token": True}) is False
    assert evaluate(parse_expr("gold >= 50"), {"gold": 50}) is True


def test_quest_state_reference_evaluates() -> None:
    expr = parse_expr("quest:q_intro.done and chapter == 2")
    assert evaluate(expr, {"quest:q_intro.done": True, "chapter": 2}) is True


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "and or",
        "has_token ==",
        "(unclosed",
        "1 2 3",
        "__import__('os').system('rm -rf /')",  # not python — must not execute, just fail to parse
        "'; DROP TABLE quests; --",
        "a" * 5000,  # pathological length — rejected, not accepted as a giant identifier
        "(" * 200,  # deep nesting — capped, must not RecursionError
    ],
)
def test_adversarial_expressions_raise_not_execute(bad: str) -> None:
    with pytest.raises(LogicSyntaxError):
        parse_expr(bad)


def test_long_but_valid_identifier_parses_without_crashing() -> None:
    from owcopilot.logic.expr import Ref

    name = "flag_" + "x" * 100
    assert parse_expr(name) == Ref(name)


def test_type_errors_flag_undefined_and_mismatch() -> None:
    symbols = {"flag": "bool", "gold": "int"}
    assert type_errors(parse_expr("missing_var"), symbols) == ["undefined variable: missing_var"]
    assert any("compare" in e for e in type_errors(parse_expr("flag == gold"), symbols))
    assert any("integer" in e for e in type_errors(parse_expr("flag > gold"), symbols))
    assert type_errors(parse_expr("flag and gold > 3"), symbols) == []


def test_world_state_apply_set_and_increment() -> None:
    state = WorldState({"gold": 10})
    state.apply("gold", "inc", 5)
    state.apply("seen", "set", True)
    assert state.as_mapping() == {"gold": 15, "seen": True}


# --------------------------------------------------------------- S3 logic audit
def _linear_quest_with_logic(logic: QuestLogic) -> Quest:
    return Quest(
        id="q",
        title="t",
        stages=[
            QuestStage(id="s1", summary="a"),
            QuestStage(id="s2", summary="b"),
            QuestStage(id="s3", summary="c"),
        ],
        logic=logic,
    )


def test_clean_linear_quest_logic_has_no_issues() -> None:
    quest = _linear_quest_with_logic(
        QuestLogic(
            variables=[LogicVar(id="flag", type=LogicVarType.BOOL)],
            stage_logic=[StageLogic(stage_id="s2", precondition="flag")],
        )
    )
    assert audit_quest_logic(quest) == []
    assert audit_quest_logic(Quest(id="q0", title="t")) == []  # no logic = no issues


def test_audit_flags_undefined_variable() -> None:
    quest = _linear_quest_with_logic(
        QuestLogic(stage_logic=[StageLogic(stage_id="s2", precondition="ghost_flag")])
    )
    codes = {i.code for i in audit_quest_logic(quest)}
    assert "LOGIC_UNDEFINED_VAR" in codes


def test_audit_flags_type_mismatch() -> None:
    quest = _linear_quest_with_logic(
        QuestLogic(
            variables=[LogicVar(id="flag", type=LogicVarType.BOOL)],
            stage_logic=[StageLogic(stage_id="s2", precondition="flag > 3")],
        )
    )
    assert "LOGIC_TYPE_MISMATCH" in {i.code for i in audit_quest_logic(quest)}


def test_audit_flags_unreachable_stage_and_deadlock() -> None:
    # s1 -> s2 -> s1 (cycle); s3 is the only terminal but is never reached
    quest = _linear_quest_with_logic(
        QuestLogic(
            branches=[
                Branch(id="b1", from_stage="s1", to_stage="s2"),
                Branch(id="b2", from_stage="s2", to_stage="s1"),
            ]
        )
    )
    codes = {i.code for i in audit_quest_logic(quest)}
    assert "LOGIC_UNREACHABLE_STAGE" in codes
    assert "LOGIC_DEADLOCK" in codes


def test_audit_runner_surfaces_logic_and_dangling_refs() -> None:
    from owcopilot.audit.context import AuditContext
    from owcopilot.audit.rules.logic_rules import QuestLogicRule
    from owcopilot.content.models import ContentBundle

    quest = _linear_quest_with_logic(
        QuestLogic(
            stage_logic=[StageLogic(stage_id="ghost_stage", precondition="undef_var")],
            unlocks=["quest_does_not_exist"],
        )
    )
    ctx = AuditContext.from_bundle(ContentBundle(quests={quest.id: quest}))
    messages = [i.message for i in QuestLogicRule().check(ctx)]
    blob = " ".join(messages)
    assert "LOGIC_UNDEFINED_VAR" in blob  # bundle-free check ran
    assert "LOGIC_DANGLING_STATE_REF" in blob  # bundle-aware checks ran
    assert "ghost_stage" in blob and "quest_does_not_exist" in blob


def test_audit_branching_quest_that_completes_is_clean() -> None:
    # s1 branches to s2 or s3; both are terminal (last / outcome) -> reachable + completable
    quest = _linear_quest_with_logic(
        QuestLogic(
            variables=[LogicVar(id="flag", type=LogicVarType.BOOL)],
            branches=[
                Branch(id="b1", from_stage="s1", condition="flag", to_stage="s3"),
                Branch(id="b2", from_stage="s1", condition="not flag", to_stage="s2"),
            ],
        )
    )
    assert audit_quest_logic(quest) == []
