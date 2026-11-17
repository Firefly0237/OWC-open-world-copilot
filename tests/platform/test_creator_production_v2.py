"""New creator production contracts with explicit controlled development responses.

Only the remote transport is controlled. The V2 host, official LangGraph driver,
Harness, requirement gates, author interrupt protocol, SQLite and previews are real.
These are engineering checks, not real model results or simulated user research.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app
from owcopilot.service.routers import creative_v2
from owcopilot.workflow import v2_planning, v2_service
from owcopilot.workflow.collaboration_v2 import CollaborationStore
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.mechanic_ir_v2 import (
    DialogueDesignIRV3,
    MechanicDesignIRV2,
    QuestDraftV2,
)
from owcopilot.workflow.models import EventPlan, TaskInput

PREFIX = "/projects/demo/creative/v2"
MACHINE = {
    "stage_count": 1,
    "decision_count": 1,
    "decision_option_count": 2,
    "distinct_outcomes": True,
    "game_state_variables": ["trust"],
}


def narrative_case():
    """Controlled transport data only; the host compiles all runtime identifiers."""
    plan = EventPlan.model_validate(
        {
            "contract_version": 1,
            "premise": "A messenger must choose between a promise and a warning",
            "beats": ["Understand the promise", "Choose how to respond"],
            "events": [{"id": "offer", "summary": "Respond to the message", "causes": []}],
            "decisions": [
                {
                    "id": "respond",
                    "event_id": "offer",
                    "options": ["Help", "Refuse"],
                    "consequences": ["Trust rises", "Trust falls"],
                }
            ],
        }
    )
    quest = QuestDraftV2.model_validate(
        {
            "title": "A difficult promise",
            "objective": "Respond to the message",
            "stages": [{"id": "respond", "summary": "Choose whether to help"}],
        }
    )
    mechanics = MechanicDesignIRV2.model_validate(
        {
            "purpose": "Helping and refusing change the messenger's trust differently",
            "variables": [{"id": "trust", "name": "Trust", "type": "int", "default": 0}],
            "stages": [
                {
                    "stage_id": "respond",
                    "purpose": "Resolve the promise",
                    "decision_id": "respond",
                    "options": [
                        {
                            "purpose": route,
                            "outcome": outcome,
                            "effects": [{"var": "trust", "op": "inc", "value": change}],
                        }
                        for route, outcome, change in (
                            ("help", "The messenger accepts the promise", 1),
                            ("refuse", "The messenger refuses the promise", -1),
                        )
                    ],
                }
            ],
        }
    )
    dialogue = DialogueDesignIRV3.model_validate(
        {
            "schema_version": 3,
            "stages": [
                {
                    "stage_id": "respond",
                    "text": "Will you keep the promise?",
                    "options": [
                        {
                            "text": "Help",
                            "response_text": "You reach for the sealed letter.",
                            "ending_text": "The messenger places the letter in your hands.",
                        },
                        {
                            "text": "Refuse",
                            "response_text": "You push the letter back across the table.",
                            "ending_text": "The messenger leaves with the letter unopened.",
                        },
                    ],
                }
            ],
        }
    )
    return plan, quest, mechanics, dialogue


class ControlledProductionProvider:
    model = "controlled-development-creator"
    max_retries = 0

    def __init__(self, *, actual_stage_count=1):
        self.calls = []
        self.actual_stage_count = actual_stage_count

    def complete(self, *, system, user, model):
        request = json.loads(user)
        self.calls.append({"system": system, "user": deepcopy(request), "model": model})
        if "registered_roles" in request:
            # Explicit transport fixture for a planner result, not an automatic fallback.
            graph = create_narrative_graph("constrained").model_dump(mode="json")
            result = {
                "rationale": "Controlled development PlanIR uses explicit state contracts",
                "nodes": [
                    {key: node[key] for key in ("id", "operation", "guidance")}
                    for node in graph["nodes"]
                ],
                "data_edges": graph["data_edges"],
                "control_edges": graph["control_edges"],
                "outputs": graph["outputs"],
            }
        else:
            operation = request["requirements"]["operation"]
            plan, quest, mechanics, dialogue = narrative_case()
            if operation == "plan_events":
                result = plan.model_dump(mode="json")
            elif operation == "draft_quest":
                assert request["output_schema"]["title"] == "QuestDraftV2"
                result = quest.model_dump(mode="json")
                result["stages"] = (
                    [
                        {"id": f"stage_{index}", "summary": "Development extra stage"}
                        for index in range(self.actual_stage_count)
                    ]
                    if self.actual_stage_count != 1
                    else result["stages"]
                )
            elif operation == "logic_contract":
                assert request["output_schema"]["title"] == "MechanicDesignIRV2"
                assert request["inputs"]["quest"]["id"].startswith("q_")
                result = mechanics.model_dump(mode="json")
            elif operation == "dialogue_constrained":
                assert request["output_schema"]["title"] == "DialogueDesignIRV3"
                assert request["inputs"]["contract"]["schema_version"] == 2
                mapping = request["inputs"]["contract"]["mapping"]
                assert len(mapping["selector_variables"]) == 1
                assert mapping["selector_variables"][0].startswith("_owc_sel_")
                assert len(mapping["options"]) == 2
                result = dialogue.model_dump(mode="json")
            else:
                raise AssertionError("Unexpected controlled production role: " + operation)
        return json.dumps(result), 31, 17, 0, self.model


@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root), "alias": str(root)})
    )
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    return root, open_workflow_store(root)


def task_for(root, store, participation="delegate"):
    task = create_task_action(
        root,
        "demo",
        TaskInput(
            brief="Controlled development: one scene, two choices, actual different consequences",
            protected=["Keep the promise meaningful"],
            participation=participation,
        ),
    )["task"]
    return store.get_task(task["id"])


def prepare(root, store, task):
    return v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph("constrained").model_dump(mode="json"),
        machine_requirements=MACHINE,
    )


def wait_job(manager, job_id):
    cursor = 0
    for _ in range(60):
        _, cursor, terminal = manager.wait_events(job_id, cursor, timeout=0.5)
        if terminal:
            job = manager.get(job_id)
            assert job is not None and job.status == "done", job.error if job else "Missing job"
            return job
    raise AssertionError("Controlled creator job did not terminate within its bounded wait")


def test_actual_three_stages_are_rejected_when_author_explicitly_requires_one(studio):
    root, store = studio
    task = task_for(root, store)
    provider = ControlledProductionProvider(actual_stage_count=3)
    state = v2_service.execute_v2_run(
        store, root, "demo", prepare(root, store, task)["run_id"], provider=provider
    )
    assert state["run"]["status"] == "completed_with_skips", state
    assert state["feedback_source"] is None
    assert state["result"]["outcomes"]["quest"]["status"] == "failed"
    report = state["requirements_report"]["quest"]
    count = next(row for row in report["checks"] if row["key"] == "stage_count")
    assert count["expected"] == 1 and count["observed"] == 3 and count["status"] == "failed"
    assert [row["user"]["requirements"]["operation"] for row in provider.calls] == [
        "plan_events",
        "draft_quest",
    ]


def test_v2_idempotent_selector_gate_and_final_bound_have_actual_requirement_evidence(studio):
    root, store = studio
    task = task_for(root, store)
    provider = ControlledProductionProvider()
    state = v2_service.execute_v2_run(
        store, root, "demo", prepare(root, store, task)["run_id"], provider=provider
    )
    assert state["run"]["status"] == "completed", state
    result = state["result"]
    assert result["outcomes"]["decision_gate"]["status"] == "succeeded"
    assert result["outputs"]["report"]["coverage"]["mechanical_status"] == "validated"
    bound = result["outputs"]["bound"]
    final_report = state["requirements_report"]["audit"]
    assert final_report["can_continue"] is True
    assert final_report["observed_hashes"]["quest"] == content_hash(bound["quest"])
    assert final_report["observed_hashes"]["tree"] == content_hash(bound["tree"])
    assert final_report["semantic_status"] == final_report["author_satisfaction"] == "unknown"
    artifact = store.get_artifact(state["feedback_source"]["artifact_id"])
    assert artifact.payload == bound
    assert bound["quest"]["metadata"]["quest_draft_v2"]["source"]["schema_version"] == 2
    assert bound["quest"]["metadata"]["mechanic_ir_v2"]["compiler_version"] == "mechanic-ir-v2.1"
    assert bound["tree"]["metadata"]["mechanic_ir_v2"]["source_ir"]["schema_version"] == 3
    binding = bound["tree"]["metadata"]["player_presentation_v1"]["bindings"][0]
    selector = binding["selector_variable"]
    preview = v2_service.create_v2_preview(store, "demo", state["run_id"])
    assert preview["presentation"] == "player"
    assert preview["text"] == "Will you keep the promise?"
    assert [choice["label"] for choice in preview["choices"]] == ["Help", "Refuse"]
    assert preview["state"]["trust"] == 0
    choice_id = preview["choices"][0]["id"]
    assert choice_id == f"{binding['node_id']}:{binding['choice_index']}"
    selected = v2_service.step_v2_preview(
        store,
        "demo",
        preview["preview_id"],
        action_id="choose-help",
        expected_seq=0,
        choice_id=choice_id,
    )
    assert selected["state"]["trust"] == 0
    assert selected["state"][selector] == binding["option_value"]
    assert selected["text"] == "You reach for the sealed letter."
    assert selected["choices"] == []  # Reading the response needs no second decision.
    finished = v2_service.step_v2_preview(
        store,
        "demo",
        preview["preview_id"],
        action_id="apply-help",
        expected_seq=1,
    )
    assert finished["status"] == "completed" and finished["state"]["trust"] == 1
    assert finished["text"] == "The messenger places the letter in your hands."
    assert finished["choices"] == [] and finished["action_seq"] == 2
    settled = [step for step in finished["history"] if step["phase"] == "quest"]
    assert len(settled) == 1 and settled[0]["choice_id"] == binding["branch_id"]
    replayed = v2_service.step_v2_preview(
        store,
        "demo",
        preview["preview_id"],
        action_id="apply-help",
        expected_seq=1,
    )
    assert replayed == finished  # The accepted action cannot apply trust twice.
    assert len(provider.calls) == 4


def test_direction_waits_with_real_plan_and_native_resume_does_not_redispatch_plan(studio):
    root, store = studio
    task = task_for(root, store, "direction")
    provider = ControlledProductionProvider()
    prepared = prepare(root, store, task)
    waiting = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert waiting["run"]["status"] == "waiting_user", waiting
    assert len(provider.calls) == 1
    gate = waiting["collaboration"]["pending_gate"]
    assert gate["status"] == "pending"
    assert gate["gate"]["available_outputs"]["plan.plan"]["decisions"][0]["id"] == "respond"
    collaboration = CollaborationStore(store)
    collaboration.authorize_resume("demo", prepared["run_id"], expected_gate_hash=gate["gate_hash"])
    store.update_run(prepared["run_id"], status="queued")
    result = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert result["run"]["status"] == "completed", result
    assert result["feedback_source"] is not None
    operations = [row["user"]["requirements"]["operation"] for row in provider.calls]
    assert operations.count("plan_events") == 1 and len(operations) == 4
    assert result["collaboration"]["pending_gate"] is None
    assert result["collaboration"]["history"][0]["status"] == "consumed"


def test_author_natural_language_revise_changes_new_run_and_keeps_original_budget(
    studio, monkeypatch
):
    root, store = studio
    task = task_for(root, store, "direction")
    provider = ControlledProductionProvider()
    first = prepare(root, store, task)
    waiting = v2_service.execute_v2_run(store, root, "demo", first["run_id"], provider=provider)
    gate = waiting["collaboration"]["pending_gate"]
    assert waiting["run"]["status"] == "waiting_user"
    monkeypatch.setattr(creative_v2, "_require_real_allowed", lambda request: None)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **kwargs: provider)
    with TestClient(create_app()) as client:
        response = client.post(
            f"{PREFIX}/runs/{first['run_id']}/resume",
            json={
                "action": "revise",
                "expected_gate_hash": gate["gate_hash"],
                "guidance": {"plan": "Show the obligation clearly before the offer"},
            },
        )
        assert response.status_code == 202, response.text
        submitted = response.json()
        assert submitted["run_id"] != first["run_id"]
        wait_job(client.app.state.jobs_manager, submitted["job_id"])
        result = client.get(f"{PREFIX}/runs/{submitted['run_id']}").json()
        # Changing the already-reviewed upstream plan invalidates its approval.
        # The revised plan must be shown again before production can continue.
        assert result["run"]["status"] == "waiting_user", result
        revised_gate = result["collaboration"]["pending_gate"]
        continued = client.post(
            f"{PREFIX}/runs/{submitted['run_id']}/resume",
            json={"action": "continue", "expected_gate_hash": revised_gate["gate_hash"]},
        )
        assert continued.status_code == 202, continued.text
        wait_job(client.app.state.jobs_manager, continued.json()["job_id"])
        result = client.get(f"{PREFIX}/runs/{submitted['run_id']}").json()
    assert result["run"]["status"] == "completed", result
    assert result["requirements"]["machine"]["stage_count"] == 1
    assert result["requirements"]["machine"]["distinct_outcomes"] is True
    assert result["requirements"]["original_protected"] == task.request.protected
    original_request = HarnessStore(store).requests(first["run_id"])[0]
    updated_request = next(
        row for row in HarnessStore(store).requests(submitted["run_id"]) if row["node_id"] == "plan"
    )
    assert updated_request["budget_id"] == original_request["budget_id"]
    assert [original_request["attempt"], updated_request["attempt"]] == [1, 2]
    assert (
        "Show the obligation clearly" in json.loads(updated_request["request"]["user"])["guidance"]
    )
    assert store.get_run(first["run_id"]).cancel_requested is True


def test_creator_source_context_and_production_job_return_actual_frozen_inputs(studio, monkeypatch):
    root, store = studio
    provider = ControlledProductionProvider()
    monkeypatch.setattr(creative_v2, "_require_real_allowed", lambda request: None)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **kwargs: provider)
    monkeypatch.setattr(v2_planning, "build_real_provider", lambda **kwargs: provider)
    with TestClient(create_app()) as client:
        source = client.post(
            f"{PREFIX}/creator/sources",
            json={
                "title": "A production reference",
                "text": "The old bell reminds the messenger of a promise.",
                "allowed_uses": ["drafting"],
                "purpose": "reference",
                "canon_status": "unconfirmed",
            },
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["source"]["id"]
        context = client.get(f"{PREFIX}/creator/context")
        assert context.status_code == 200, context.text
        assert {"entities", "sources", "narrative_projects"} <= context.json().keys()
        assert any(row["id"] == source_id for row in context.json()["sources"])
        created = client.post(
            f"{PREFIX}/tasks",
            json={
                "brief": "Controlled development: adapt the promise into one playable stage",
                "participation": "delegate",
                "use_references": True,
                "reference_ids": [source_id],
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]
        submitted = client.post(
            f"{PREFIX}/tasks/{task_id}/production-jobs",
            json={
                "selection": "planned",
                "machine_requirements": MACHINE,
                "collaboration_policy": {"mode": "delegate"},
            },
        )
        assert submitted.status_code == 202, submitted.text
        assert set(submitted.json()) >= {"job_id", "status"}
        job = wait_job(client.app.state.jobs_manager, submitted.json()["job_id"])
        assert job.result["run"]["status"] == "completed", job.result
        detail = client.get(f"{PREFIX}/tasks/{task_id}").json()["runs"][0]
    assert detail["run"]["job_id"] == submitted.json()["job_id"]
    assert {row["source_id"] for row in detail["references"]} == {source_id}
    assert detail["requirements"]["scenario"] == "reference"
    assert detail["requirements"]["machine"]["stage_count"] == 1
    assert len(provider.calls) == 5  # One real planner contract plus four production dispatches.
    assert provider.calls[0]["user"]["references"][0]["source_id"] == source_id
    assert ContentStore(root).load().quests == {}
