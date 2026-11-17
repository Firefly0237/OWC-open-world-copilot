"""Explicit comparison conditions reach production reports without replaying creative work."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    LogicVar,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app
from owcopilot.workflow import runtime
from owcopilot.workflow.models import (
    ComparisonInput,
    TaskInput,
    TaskRecord,
    WorkflowNode,
    WorkflowSpec,
)
from owcopilot.workflow.operators import execute_operator


def resource_quest():
    return Quest(
        id="q",
        title="Keep the resource",
        stages=[QuestStage(id="start", summary="Make a decision")],
        logic=QuestLogic(
            variables=[LogicVar(id="gold", type="int", default=1)],
            branches=[Branch(id="finish", from_stage="start", outcome="finished")],
        ),
        metadata={
            "temporal_specs": [{"id": "baseline_floor", "pattern": "AG", "expression": "gold >= 1"}]
        },
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    root = tmp_path / "project"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root)}))
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OWCOPILOT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        yield client, root


def task_for(before, comparison=None):
    bundle = ContentBundle(quests={before.id: before})
    return TaskRecord(
        project="demo",
        request=TaskInput(
            brief="Compare explicit conditions without changing the story",
            protected=["Keep the difficult choice"],
            comparison=comparison,
        ),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )


def compare(task, before, after):
    bundle = ContentBundle(quests={before.id: before}) if before else ContentBundle()
    kind, report = execute_operator(
        WorkflowNode(id="comparison", operation="compare", depends_on=["logic"]),
        task=task,
        project=SimpleNamespace(bundle=bundle),
        gateway=None,
        inputs={
            "draft_logic": {"quest": after.model_dump(mode="json")},
            "comparison_baseline": {
                "source": "project" if before else "unavailable",
                "quest_id": before.id if before else after.id,
                "quest": before.model_dump(mode="json") if before else None,
            },
        },
    )
    assert kind == "behavior_report"
    return report


def test_api_persists_typed_explicit_comparison_conditions(api):
    client, root = api
    conditions = {
        "initial_state": {"permit": True, "gold": 2, "route": "north"},
        "formal_specs": [{"id": "declared", "pattern": "AG", "expression": "gold >= 0"}],
    }
    response = client.post(
        "/projects/demo/creative/tasks",
        json={"brief": "Explicit technical comparison input", "comparison": conditions},
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]
    persisted = open_workflow_store(root).get_task(task_id).request.comparison
    assert persisted is not None
    assert persisted.initial_state == conditions["initial_state"]
    assert type(persisted.initial_state["permit"]) is bool
    assert type(persisted.initial_state["gold"]) is int
    assert type(persisted.initial_state["route"]) is str
    assert persisted.formal_specs[0].expression == "gold >= 0"
    detail = client.get(f"/projects/demo/creative/tasks/{task_id}")
    assert detail.status_code == 200
    assert (
        detail.json()["task"]["request"]["comparison"]["initial_state"]
        == conditions["initial_state"]
    )


def test_api_rejects_unknown_rule_scope_and_non_scalar_or_coerced_initial_values(api):
    client, _ = api
    invalid = [
        {"formal_specs": [{"pattern": "AG", "expression": "true", "scope": "whole_world"}]},
        {"initial_state": {"gold": 1.0}},
        {"initial_state": {"permit": None}},
        {"initial_state": {"permit": [True]}},
        {"initial_state": {"permit": {"value": True}}},
    ]
    for conditions in invalid:
        response = client.post(
            "/projects/demo/creative/tasks",
            json={"brief": "Rejected explicit input", "comparison": conditions},
        )
        assert response.status_code == 422, response.text
    assert open_workflow_store(api[1]).list_tasks() == []


def test_operator_defaults_and_explicit_rules_use_real_checker():
    before = resource_quest()
    after = before.model_copy(deep=True)
    after.logic.stage_logic = [
        StageLogic(stage_id="start", effects_on_complete=[Effect(var="gold", value=0)])
    ]
    task = task_for(before)
    default = compare(task, before, after)
    assert default["intent"]["formal"][0]["change"] == "regressed"
    assert default["intent"]["protected"][0]["status"] == "withheld"

    explicit = task.model_copy(
        update={
            "request": task.request.model_copy(
                update={
                    "comparison": ComparisonInput(
                        formal_specs=[
                            {"id": "request_floor", "pattern": "AG", "expression": "gold >= 0"}
                        ]
                    )
                }
            )
        }
    )
    report = compare(explicit, before, after)
    assert report["intent"]["formal"][0]["spec"]["id"] == "request_floor"
    assert report["intent"]["formal"][0]["change"] == "preserved"
    cleared = explicit.model_copy(
        update={
            "request": explicit.request.model_copy(
                update={"comparison": ComparisonInput(formal_specs=[])}
            )
        }
    )
    assert compare(cleared, before, after)["intent"]["formal"] == []
    inherited = explicit.model_copy(
        update={"request": explicit.request.model_copy(update={"comparison": ComparisonInput()})}
    )
    assert compare(inherited, before, after)["intent"]["formal"] == default["intent"]["formal"]


def test_operator_custom_initial_withholds_proof_and_keeps_no_baseline():
    quest = Quest(
        id="gate",
        title="A guarded entrance",
        stages=[QuestStage(id="start", summary="Enter with a permit")],
        logic=QuestLogic(
            variables=[LogicVar(id="permit", default=False)],
            branches=[
                Branch(id="enter", from_stage="start", condition="permit", outcome="entered")
            ],
        ),
    )
    task = task_for(
        quest,
        ComparisonInput(
            initial_state={"permit": True},
            formal_specs=[{"pattern": "EF", "expression": "permit"}],
        ),
    )
    report = compare(task, quest, quest)
    assert report["scope"]["after_initial_state"] == {"permit": True}
    assert report["replay"]["status"] == "replayed"
    assert report["replay"]["outcome"] == "entered"
    assert report["intent"]["formal"][0]["after"]["status"] == "withheld"
    assert report["soft_lock"]["after"]["status"] == "withheld"
    assert report["after"]["status"] == "withheld"
    absent = compare(task, None, quest)
    assert absent["status"] == "no_baseline"
    assert "before_hash" not in absent
    assert absent["baseline"]["source"] == "unavailable"


def test_runtime_comparison_cache_changes_without_regenerating_content(tmp_path, monkeypatch):
    root = tmp_path / "project"
    before = resource_quest()
    after = before.model_copy(deep=True)
    after.logic.stage_logic = [
        StageLogic(stage_id="start", effects_on_complete=[Effect(var="gold", value=0)])
    ]
    ContentStore(root).save(ContentBundle(quests={before.id: before}))
    task_id = create_task_action(root, "demo", TaskInput(brief="Explicit comparison cache input"))[
        "task"
    ]["id"]
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    spec = WorkflowSpec(
        name="Comparison conditions are report inputs",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
            {"id": "comparison", "operation": "compare", "depends_on": ["logic"]},
            {
                "id": "summary",
                "operation": "package",
                "optional_dependencies": ["logic", "comparison"],
            },
        ],
    )
    executed = []
    original = runtime.execute_operator

    def controlled_creative_operation(node, **kwargs):
        executed.append(node.operation)
        if node.operation == "plan_events":
            return "event_plan", {"plan": {"premise": "fixture", "beats": ["finish"]}}
        if node.operation in {"draft_quest", "draft_logic"}:
            kind = "quest" if node.operation == "draft_quest" else "quest_with_logic"
            return kind, {"quest": after.model_dump(mode="json")}
        return original(node, **kwargs)

    def no_provider(**kwargs):
        raise AssertionError("Comparison configuration never authorizes a provider request")

    monkeypatch.setattr(runtime, "execute_operator", controlled_creative_operation)
    monkeypatch.setattr(runtime, "build_real_provider", no_provider)
    first = ComparisonInput(
        initial_state={"gold": 2},
        formal_specs=[{"id": "first", "pattern": "AG", "expression": "gold >= 0"}],
    )
    changed_initial = ComparisonInput(
        initial_state={"gold": 1},
        formal_specs=[{"id": "first", "pattern": "AG", "expression": "gold >= 0"}],
    )
    changed = ComparisonInput(
        initial_state={"gold": 1},
        formal_specs=[{"id": "changed", "pattern": "AG", "expression": "gold >= 1"}],
    )
    configurations = (None, first, changed_initial, changed, changed)
    last_run = None
    for revision, conditions in enumerate(configurations, start=1):
        if revision > 1:
            task = task.model_copy(
                update={
                    "revision": revision,
                    "request": task.request.model_copy(update={"comparison": conditions}),
                }
            )
            store.save_task(task, expected_revision=revision - 1)
        run, _ = store.create_run(task)
        store.update_run(run.id, workflow=spec.model_dump(mode="json"))
        assert runtime.execute_workflow(store, run.id, str(root))["status"] == "completed"
        outcomes = store.outcomes(run.id)
        assert store.model_calls(run.id) == []
        if revision > 1:
            assert all(outcomes[node].attempts == 0 for node in ("events", "quest", "logic"))
        report = store.get_artifact(outcomes["comparison"].artifact_id).payload
        if conditions is not None:
            assert report["initial_state"] == conditions.initial_state
            assert report["intent"]["formal"][0]["spec"]["id"] == conditions.formal_specs[0].id
        if revision == 5:
            assert outcomes["comparison"].attempts == 0
            assert outcomes["summary"].attempts == 0
        last_run = run

    assert executed.count("plan_events") == 1
    assert executed.count("draft_quest") == 1
    assert executed.count("draft_logic") == 1
    assert executed.count("compare") == 4
    assert executed.count("package") == 4
    completed_outcomes = store.outcomes(last_run.id)
    artifact_count = len(store.artifacts(task.id))
    assert runtime.execute_workflow(store, last_run.id, str(root))["status"] == "completed"
    assert store.outcomes(last_run.id) == completed_outcomes
    assert len(store.artifacts(task.id)) == artifact_count
    assert executed.count("compare") == 4
    assert store.model_calls(last_run.id) == []
