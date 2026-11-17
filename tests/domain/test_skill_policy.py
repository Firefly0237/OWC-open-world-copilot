"""Controlled state fixtures test decisions and accounting, never author quality or training."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from owcopilot.content.hash import content_hash
from owcopilot.workflow.execution_records import ExecutionRecords
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import BudgetOwner, HarnessStore
from owcopilot.workflow.models import RunRecord, TaskInput, TaskRecord
from owcopilot.workflow.skill_cards import SkillCards
from owcopilot.workflow.skill_policy import (
    SkillPolicyChoice,
    isolate_maintenance_cost,
    offline_maintenance_trajectories,
    record_maintenance_decision,
    record_maintenance_outcome,
)
from owcopilot.workflow.store import WorkflowStore
from owcopilot.workflow.v2_evidence import export_v2_evidence


@pytest.fixture
def case(tmp_path):
    store = WorkflowStore(tmp_path / "controlled.sqlite")
    task = TaskRecord(
        project="local",
        request=TaskInput(brief="Controlled metadata"),
        base_version="fixture",
        base_hash="0" * 64,
        base_bundle={},
        origin_kind="development",
    )
    store.save_task(task)
    run, _created = store.create_run(task)
    run = store.update_run(run.id, status="completed")
    graph = create_narrative_graph().model_dump(mode="json")
    state = {
        "run_id": run.id,
        "project": task.project,
        "task_id": task.id,
        "revision": task.revision,
        "graph": graph,
        "graph_hash": content_hash(graph),
        "references": [],
        "summarize_skill": True,
        "skill_card_bindings": [],
        "run": run.model_dump(mode="json"),
        "result": {
            "outcomes": {"plan": {"status": "succeeded", "outputs": {"fixture": "metadata"}}}
        },
    }
    with store.connection() as db:
        db.execute(
            "CREATE TABLE creative_v2_runs(run_id TEXT PRIMARY KEY,project TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO creative_v2_runs VALUES(?,?,?)", (run.id, task.project, json.dumps(state))
        )
    HarnessStore(store)
    return store, task, state


def save_state(store, state):
    with store.connection() as db:
        db.execute(
            "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
            (json.dumps(state), state["run_id"]),
        )


def add_binding(store, task, state):
    cards = SkillCards(store)
    card, _created = cards.add_candidate(
        task.project,
        {
            "name": "Check current choices",
            "purpose": "Keep the declared choice count",
            "steps": [
                {
                    "id": "count",
                    "title": "Check",
                    "instruction": "Use the current declared choice count.",
                    "operation": "draft_quest",
                }
            ],
            "applies_to": ["Declared alternatives"],
        },
        {"kind": "import"},
    )
    version = card["versions"][0]
    selected = {
        "card_id": card["id"],
        "version_id": version["id"],
        "version_hash": version["version_hash"],
    }
    state["skill_card_bindings"] = [{**selected, "selection": "explicit_task"}]
    save_state(store, state)
    return card, selected


@pytest.mark.parametrize(
    "bound,requested,budget,expected",
    [
        (True, True, {}, "retain"),
        (False, True, {}, "summarize"),
        (False, False, {}, "no_change"),
        (False, True, {"remaining_dispatches": 0}, "no_change"),
    ],
)
def test_rules_skip_ordinary_reuse_and_obey_author_and_budget_mask(
    case, bound, requested, budget, expected
):
    store, task, state = case
    card = None
    if bound:
        card, _selected = add_binding(store, task, state)
    state["summarize_skill"] = requested
    save_state(store, state)
    first = record_maintenance_decision(store, task.project, state, budget=budget)
    repeated = record_maintenance_decision(store, task.project, state, budget=budget)
    assert first["id"] == repeated["id"] and first["created_at"] == repeated["created_at"]
    assert first["selected_action_id"] == expected
    assert first["selection_probability"] is None
    assert len(ExecutionRecords(store).for_task(task.project, task.id)) == 1
    if card:
        assert SkillCards(store).detail(task.project, card["id"])["active_version_id"] is None
    if expected in {"retain", "no_change"}:
        before = export_v2_evidence(store, None, task.project, state["run_id"])
        assert (
            isolate_maintenance_cost(before, before["recording"]["decisions"][0])["dispatch_count"]
            is None
        )
        assert offline_maintenance_trajectories(before)["transitions"][0]["actual_action"] is None
        outcome = record_maintenance_outcome(
            store, task.project, first["id"], status="not_dispatched", new_dispatch=False
        )
        assert (
            record_maintenance_outcome(
                store, task.project, first["id"], status="not_dispatched", new_dispatch=False
            )["event_hash"]
            == outcome["event_hash"]
        )
        after = export_v2_evidence(store, None, task.project, state["run_id"])
        cost = isolate_maintenance_cost(after, after["recording"]["decisions"][0])
        assert cost["dispatch_count"] == 0 and cost["host_duration_seconds"] is None
        assert (
            offline_maintenance_trajectories(after)["transitions"][0]["actual_action"] == expected
        )


def test_update_needs_recorded_ground_and_live_target_without_changing_adoption(case):
    store, task, state = case
    card, selected = add_binding(store, task, state)
    feedback = {
        "scope": "workflow",
        "target_operation": "workflow_spec",
        "revision": task.revision,
        "text": "Controlled method feedback",
    }
    task = task.model_copy(update={"feedback": [feedback]})
    store.save_task(task)
    reference = "feedback:" + content_hash(feedback)
    decision = record_maintenance_decision(
        store, task.project, state, update_target=selected, feedback_refs=[reference]
    )
    assert decision["selected_action_id"] == "update"
    assert SkillCards(store).detail(task.project, card["id"])["active_version_id"] is None
    SkillCards(store).enable(task.project, card["id"], card["card_hash"], False)
    with pytest.raises(ValueError, match="停用"):
        record_maintenance_decision(
            store, task.project, state, update_target=selected, feedback_refs=[reference]
        )
    with pytest.raises(ValueError, match="工作流反馈"):
        record_maintenance_decision(
            store, task.project, state, feedback_refs=["invented-feedback-id"]
        )


@pytest.mark.parametrize("production", [True, False])
def test_partial_run_can_summarize_real_production_but_audit_alone_cannot(case, production):
    store, task, state = case
    store.update_run(state["run_id"], status="completed_with_skips")
    if not production:
        state["result"]["outcomes"] = {"audit": {"status": "succeeded", "outputs": {"report": {}}}}
        save_state(store, state)
    decision = record_maintenance_decision(store, task.project, state)
    assert decision["selected_action_id"] == ("summarize" if production else "no_change")
    assert decision["observation"]["source_status"] == "completed_with_skips"


def test_plugin_cannot_select_masked_action_or_invent_behavior_probability(case):
    store, task, state = case
    state["summarize_skill"] = False
    save_state(store, state)

    class InvalidPolicy:
        id, version = "controlled-invalid", "1"

        def select(self, observation, feasible_actions, budget):
            assert {row["id"] for row in feasible_actions} == {"no_change"}
            return SkillPolicyChoice(action_id="summarize", reason="Forbidden proposal")

    with pytest.raises(ValueError, match="许可外"):
        record_maintenance_decision(store, task.project, state, policy=InvalidPolicy())
    with pytest.raises(ValidationError):
        SkillPolicyChoice(
            action_id="no_change", reason="Unsupported propensity", selection_probability=1.0
        )
    assert ExecutionRecords(store).for_task(task.project, task.id) == []


def dispatch(store, task, run_id, *, role, source_id=None, tokens=10):
    harness = HarnessStore(store)
    request = harness.claim(
        BudgetOwner(project=task.project, task_id=task.id, product_key=role, problem_key=role),
        run_id,
        role,
        {"role_id": role, "config": {"execution_origin": "controlled_transport"}},
        {"source_run_id": source_id, "purpose": "procedural_skill_candidate"} if source_id else {},
    )
    harness.finish(
        request,
        {
            "request_id": request["id"],
            "status": "completed",
            "measurement": {
                "input_tokens": tokens,
                "output_tokens": 3,
                "cached_input_tokens": 0,
            },
        },
        role,
        settlement="succeeded",
    )
    return request


def test_offline_costs_only_charge_explicit_fresh_summary_and_never_create_rewards(case):
    store, task, state = case
    source_request = dispatch(store, task, state["run_id"], role="plan_events", tokens=100)
    decision = record_maintenance_decision(store, task.project, state)
    summary = RunRecord(task_id=task.id, revision=task.revision, status="completed")
    with store.connection() as db:
        db.execute(
            "INSERT INTO runs VALUES(?,?,?)", (summary.id, task.id, summary.model_dump_json())
        )
    summary_request = dispatch(
        store, task, summary.id, role="skill_summary", source_id=state["run_id"], tokens=7
    )
    ExecutionRecords(store).bind_run(task.project, decision["id"], summary.id)
    original_outcome = record_maintenance_outcome(
        store,
        task.project,
        decision["id"],
        status="completed",
        summary_run_id=summary.id,
        new_dispatch=True,
        technical_status="completed",
    )
    replay = record_maintenance_outcome(
        store,
        task.project,
        decision["id"],
        status="completed",
        summary_run_id=summary.id,
        new_dispatch=False,
        technical_status="completed",
    )
    assert replay["event_hash"] == original_outcome["event_hash"]
    assert replay["payload"]["new_dispatch"] is True
    other = RunRecord(task_id=task.id, revision=task.revision, status="completed")
    with store.connection() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?)", (other.id, task.id, other.model_dump_json()))
        count = db.execute(
            "SELECT count(*) FROM creative_harness_events "
            "WHERE event_type='skill/maintenance_outcome' "
            "AND json_extract(payload,'$.payload.decision_id')=?",
            (decision["id"],),
        ).fetchone()[0]
    assert count == 1
    with pytest.raises(ValueError, match="不可覆盖"):
        record_maintenance_outcome(
            store,
            task.project,
            decision["id"],
            status="completed",
            summary_run_id=other.id,
            new_dispatch=False,
            technical_status="completed",
        )
    exported = export_v2_evidence(store, None, task.project, state["run_id"])
    rows = offline_maintenance_trajectories(exported)
    transition = rows["transitions"][0]
    assert transition["costs"]["charged_request_ids"] == [summary_request["id"]]
    assert source_request["id"] not in transition["costs"]["charged_request_ids"]
    assert transition["costs"]["dispatch_count"] == 1 and transition["costs"]["total_tokens"] == 10
    assert transition["technical_result"] == "completed"
    assert transition["reward"] is None and not transition["training_eligible"]
    assert transition["author_feedback"]["quality"] == "unknown"
    ambiguous = deepcopy(exported)
    duplicate = deepcopy(ambiguous["recording"]["decisions"][0])
    duplicate["id"] = "another-maintenance-choice"
    ambiguous["recording"]["decisions"].append(duplicate)
    assert isolate_maintenance_cost(ambiguous, decision)["cost_status"] == "not_attested"
    actual = ambiguous["recording"]["decisions"][0]
    assert (
        isolate_maintenance_cost(ambiguous, actual)["cost_status"]
        == "ambiguous_decision_attribution"
    )
    with pytest.raises(ValueError, match="不可覆盖"):
        record_maintenance_outcome(
            store,
            task.project,
            decision["id"],
            status="failed",
            summary_run_id=summary.id,
            new_dispatch=True,
            technical_status="failed",
        )
