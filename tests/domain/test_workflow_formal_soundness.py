"""Concrete-state semantics and node-local error containment, without model calls."""

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.content.store import ContentStore
from owcopilot.logic.temporal import PropertyPattern, SpecStatus, TemporalSpec, check_temporal_spec
from owcopilot.workflow import runtime
from owcopilot.workflow.models import TaskInput, WorkflowSpec
from owcopilot.workflow.runtime import execute_workflow


def test_search_range_cannot_prove_a_saturated_counter_invariant():
    quest = Quest(
        id="counter",
        title="counter",
        stages=[QuestStage(id="s", summary="increment")],
        logic=QuestLogic(
            variables=[LogicVar(id="n", type=LogicVarType.INT, default=0)],
            stage_logic=[
                StageLogic(stage_id="s", effects_on_complete=[Effect(var="n", op="inc", value=1)])
            ],
            branches=[Branch(id="loop", from_stage="s", to_stage="s")],
        ),
    )
    result = check_temporal_spec(
        quest,
        TemporalSpec(pattern=PropertyPattern.AG, expression="n <= 2", int_ranges={"n": (0, 2)}),
    )
    assert result.truncated is True
    assert result.status is SpecStatus.VIOLATED and result.strong is True
    assert result.counterexample_witness == ["loop", "loop", "loop"]


def test_search_range_cannot_invent_a_reachability_witness():
    quest = Quest(
        id="jump",
        title="jump",
        stages=[QuestStage(id="s", summary="set a concrete value")],
        logic=QuestLogic(
            variables=[LogicVar(id="n", type=LogicVarType.INT, default=0)],
            branches=[
                Branch(
                    id="end", from_stage="s", outcome="done", effects=[Effect(var="n", value=100)]
                )
            ],
        ),
    )
    absent = check_temporal_spec(
        quest,
        TemporalSpec(pattern=PropertyPattern.EF, expression="n == 2", int_ranges={"n": (0, 2)}),
    )
    concrete = check_temporal_spec(
        quest,
        TemporalSpec(pattern=PropertyPattern.EF, expression="n == 100", int_ranges={"n": (0, 2)}),
    )
    assert absent.holds is False and absent.strong is False and absent.truncated is True
    assert concrete.status is SpecStatus.PROVEN and concrete.strong is True
    assert concrete.counterexample_witness == ["end"]


def test_invalid_comparison_baseline_does_not_abort_independent_reports(tmp_path, monkeypatch):
    root = tmp_path / "project"
    ContentStore(root).save(ContentBundle())
    task_id = create_task_action(root, "test", TaskInput(brief="explicit technical test input"))[
        "task"
    ]["id"]
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    run, _ = store.create_run(task)
    quest = Quest(
        id="candidate",
        title="candidate",
        stages=[QuestStage(id="s", summary="finish")],
        logic=QuestLogic(branches=[Branch(id="end", from_stage="s", outcome="done")]),
    )
    spec = WorkflowSpec(
        name="baseline failure isolation",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
            {
                "id": "compare",
                "operation": "compare",
                "depends_on": ["logic"],
                "parameters": {"baseline_quest_id": "absent"},
            },
            {"id": "audit", "operation": "audit", "optional_dependencies": ["compare", "logic"]},
            {
                "id": "pack",
                "operation": "package",
                "optional_dependencies": ["audit", "compare", "quest"],
            },
        ],
    )
    store.update_run(run.id, workflow=spec.model_dump(mode="json"))

    def no_provider(**kwargs):
        raise AssertionError("This comparison contract never authorizes a model request")

    original = runtime.execute_operator

    def controlled_upstream(node, **kwargs):
        if node.operation == "plan_events":
            return "event_plan", {"plan": {"premise": "fixture", "beats": ["finish"]}}
        if node.operation in {"draft_quest", "draft_logic"}:
            kind = "quest" if node.operation == "draft_quest" else "quest_with_logic"
            return kind, {"quest": quest.model_dump(mode="json")}
        return original(node, **kwargs)

    monkeypatch.setattr(runtime, "execute_operator", controlled_upstream)
    monkeypatch.setattr("owcopilot.workflow.runtime.build_real_provider", no_provider)
    result = execute_workflow(store, run.id, str(root))
    outcomes = store.outcomes(run.id)
    assert result["status"] == "completed_with_skips"
    assert outcomes["compare"].status == "skipped_nonretryable"
    assert outcomes["compare"].attempts == 1
    assert outcomes["audit"].status == outcomes["pack"].status == "succeeded"
    assert store.model_calls(run.id) == []
