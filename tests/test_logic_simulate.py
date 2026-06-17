"""WS-E · deterministic playtest of a quest's logic: completed / blocked / deadlock / cycle."""

from __future__ import annotations

from owcopilot.content.models import (
    Branch,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.logic import simulate_quest


def _quest(logic: QuestLogic, n: int = 3) -> Quest:
    return Quest(
        id="q",
        title="t",
        stages=[QuestStage(id=f"s{i}", summary=str(i)) for i in range(1, n + 1)],
        logic=logic,
    )


def test_linear_quest_completes_and_applies_effects() -> None:
    run = simulate_quest(
        _quest(
            QuestLogic(
                variables=[LogicVar(id="flag", type=LogicVarType.BOOL, default=False)],
                stage_logic=[
                    StageLogic(
                        stage_id="s1",
                        effects_on_complete=[{"var": "flag", "op": "set", "value": True}],  # type: ignore[list-item]
                    )
                ],
            )
        )
    )
    assert run.status == "completed"
    assert run.path == ["s1", "s2", "s3"]
    assert run.final_state["flag"] is True  # effect applied during the walk


def test_blocked_when_precondition_false() -> None:
    run = simulate_quest(
        _quest(
            QuestLogic(
                variables=[LogicVar(id="has_key", type=LogicVarType.BOOL, default=False)],
                stage_logic=[StageLogic(stage_id="s2", precondition="has_key")],
            )
        )
    )
    assert run.status == "blocked" and run.path[-1] == "s2"


def test_branch_path_and_choice() -> None:
    logic = QuestLogic(
        variables=[LogicVar(id="brave", type=LogicVarType.BOOL, default=True)],
        branches=[
            Branch(id="b_fight", from_stage="s1", condition="brave", to_stage="s3"),
            Branch(id="b_flee", from_stage="s1", condition="not brave", to_stage="s2"),
        ],
    )
    auto = simulate_quest(_quest(logic))
    assert auto.status == "completed" and "s3" in auto.path  # brave -> fight -> s3
    assert auto.steps[0].branch_taken == "b_fight"

    # an explicit choice is honoured when satisfiable
    chosen = simulate_quest(_quest(logic), initial_state={"brave": False}, choices=["b_flee"])
    assert "s2" in chosen.path and chosen.steps[0].branch_taken == "b_flee"


def test_deadlock_when_no_branch_satisfiable() -> None:
    run = simulate_quest(
        _quest(
            QuestLogic(
                variables=[LogicVar(id="flag", type=LogicVarType.BOOL, default=False)],
                # the only branch from s1 needs flag=true, which never holds -> stuck
                branches=[Branch(id="b", from_stage="s1", condition="flag", to_stage="s3")],
            )
        )
    )
    assert run.status == "deadlock"


def test_cycle_is_detected() -> None:
    run = simulate_quest(
        _quest(
            QuestLogic(
                branches=[
                    Branch(id="b1", from_stage="s1", to_stage="s2"),
                    Branch(id="b2", from_stage="s2", to_stage="s1"),
                ]
            ),
            n=2,
        )
    )
    assert run.status == "cycle"


def test_quest_without_logic_is_trivially_completed() -> None:
    run = simulate_quest(Quest(id="q", title="t", stages=[QuestStage(id="s1", summary="x")]))
    assert run.status == "completed"


def test_simulate_action_walks_real_quest(tmp_path) -> None:
    import pytest

    from owcopilot.app.actions import simulate_quest_action
    from owcopilot.content.models import ContentBundle
    from owcopilot.content.store import ContentStore

    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            quests={
                "q": _quest(
                    QuestLogic(
                        variables=[LogicVar(id="flag", type=LogicVarType.BOOL, default=False)],
                        branches=[Branch(id="b", from_stage="s1", condition="flag", to_stage="s3")],
                    )
                )
            }
        )
    )
    out = simulate_quest_action(root, quest_id="q")
    assert out["run"]["status"] == "deadlock"  # flag never true -> stuck
    with pytest.raises(ValueError, match="任务不存在"):
        simulate_quest_action(root, quest_id="ghost")


def test_simulate_dangling_branch_target_is_blocked_not_crash() -> None:
    # hardening: a branch pointing at a non-existent stage must yield a clean "blocked" outcome,
    # not a raw list.index ValueError from the linear-advance path
    quest = Quest(
        id="q",
        title="悬空",
        stages=[QuestStage(id="s1", summary="a")],
        logic=QuestLogic(
            branches=[Branch(id="b", from_stage="s1", condition="true", to_stage="ghost")]
        ),
    )
    run = simulate_quest(quest)
    assert run.status == "blocked" and "ghost" in run.message
