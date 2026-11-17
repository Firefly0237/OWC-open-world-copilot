"""Learning jobs and explicit acceptance, using controlled responses rather than live models."""

from __future__ import annotations

import json
from threading import Event

from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import feedback_action, update_workflow_action
from owcopilot.service.api import create_app
from owcopilot.service.routers import workflows
from owcopilot.workflow import learning_runtime
from owcopilot.workflow.memory import accepted_workflow_context
from owcopilot.workflow.models import FeedbackInput, WorkflowPatchInput
from tests.domain.test_workflow_learning import method, source_trace


class ControlledProvider:
    def __init__(self, entered=None, release=None, before_response=None):
        self.calls = []
        self.entered, self.release, self.before_response = entered, release, before_response

    def complete(self, *, system, user, model):
        self.calls.append({"system": system, "user": user})
        if self.entered:
            self.entered.set()
        if self.release:
            assert self.release.wait(5), "The test must release the controlled provider"
        if self.before_response:
            self.before_response()
        return method().model_dump_json(), 100, 50


def test_frozen_learning_input_acceptance_and_edited_method_are_separate(tmp_path, monkeypatch):
    store, run, _ = source_trace(tmp_path)
    store.update_run(run.id, job_id="original-generation-job")
    provider = ControlledProvider()
    monkeypatch.setattr(learning_runtime, "build_real_provider", lambda **kwargs: provider)
    before = learning_runtime.prepare_method_learning(store, "demo", run.id, expected_revision=1)
    result = learning_runtime.execute_method_learning(
        store, run.id, str(tmp_path), expected_revision=1
    )
    assert result["status"] == "completed", result
    assert len(provider.calls) == 1
    assert result["attempts"] == 1
    assert result["workflow"]["acceptance_status"] == "pending"
    assert accepted_workflow_context(store, project="demo") == []
    repeated = learning_runtime.execute_method_learning(
        store, run.id, str(tmp_path), expected_revision=1
    )
    assert repeated["workflow"]["id"] == result["workflow"]["id"]
    assert len(provider.calls) == 1
    assert store.learning_source(run.id) == before
    assert store.get_run(run.id).job_id == "original-generation-job"
    assert "studio_learn_method" not in json.dumps(before["trace"], ensure_ascii=False)
    record = result["workflow"]
    accepted = update_workflow_action(
        store,
        "demo",
        record["id"],
        WorkflowPatchInput(
            acceptance_status="accepted",
            expected_hash=record["content_hash"],
        ),
    )["workflow"]
    assert accepted["acceptance_record"]["content_acceptance"] == "unknown"
    assert len(accepted_workflow_context(store, project="demo")) == 1
    edited = update_workflow_action(
        store,
        "demo",
        record["id"],
        WorkflowPatchInput(
            expected_hash=accepted["content_hash"],
            name="Explicitly edited method",
        ),
    )["workflow"]
    assert edited["memory_kind"] == "manual_template"
    assert edited["acceptance_status"] == "pending"
    assert accepted_workflow_context(store, project="demo") == []


def test_late_revision_cannot_publish_induced_memory(tmp_path, monkeypatch):
    store, run, _ = source_trace(tmp_path)

    def revise():
        task = store.get_task(run.task_id)
        feedback_action(
            store,
            task.project,
            task.id,
            FeedbackInput(
                expected_revision=task.revision,
                text="A new author request",
                target_operation="draft_quest",
            ),
        )

    monkeypatch.setattr(
        learning_runtime,
        "build_real_provider",
        lambda **kwargs: ControlledProvider(before_response=revise),
    )
    result = learning_runtime.execute_method_learning(
        store, run.id, str(tmp_path), expected_revision=1
    )
    assert result["status"] == "interrupted"
    assert store.induced_workflow(run.id) is None
    assert len(store.model_calls(run.id)) == 2  # one source call, one controlled induction


def test_learning_job_is_nonblocking_reused_and_restart_is_visible(tmp_path, monkeypatch):
    store, run, _ = source_trace(tmp_path)
    entered, release = Event(), Event()
    provider = ControlledProvider(entered, release)
    monkeypatch.setattr(learning_runtime, "build_real_provider", lambda **kwargs: provider)
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(tmp_path), "alias": str(tmp_path)})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setattr(workflows, "_store", lambda project: store)
    monkeypatch.setattr(workflows, "_require_real_allowed", lambda request: None)
    prefix = f"/projects/demo/creative/runs/{run.id}"
    with TestClient(create_app()) as client:
        try:
            response = client.post(prefix + ":learn-method", json={"expected_revision": 1})
            assert response.status_code == 202, response.text
            assert entered.wait(3)
            job_id = response.json()["job_id"]
            again = client.post(prefix + ":learn-method", json={"expected_revision": 1})
            assert again.json()["job_id"] == job_id
            assert client.get(prefix + "/method-learning").json()["status"] == "running"
            assert len(provider.calls) == 1
            release.set()
            manager = client.app.state.jobs_manager
            cursor = 0
            for _ in range(10):
                _, cursor, terminal = manager.wait_events(job_id, cursor, timeout=1)
                if terminal:
                    break
            result = client.get(prefix + "/method-learning").json()
            assert result["status"] == "completed", result
            assert (
                client.post(
                    f"/projects/alias/creative/runs/{run.id}:learn-method",
                    json={"expected_revision": 1},
                ).status_code
                == 404
            )
        finally:
            release.set()
    # A persisted job id with no live manager and no completed result is an interruption.
    store2, run2, _ = source_trace(tmp_path / "restart")
    store2.update_run(run2.id, learning_job_id="lost-job")
    monkeypatch.setattr(workflows, "_store", lambda project: store2)
    with TestClient(create_app()) as client:
        assert client.app.state.jobs_manager.get("lost-job") is None
        status = client.get(f"/projects/demo/creative/runs/{run2.id}/method-learning").json()
        assert status["status"] == "interrupted"
        assert status["attempts"] == 0
