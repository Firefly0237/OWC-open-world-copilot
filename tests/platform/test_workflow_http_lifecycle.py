"""HTTP cancellation and durable SSE cursors with explicit offline transport responses."""

from __future__ import annotations

import json
from threading import Event

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import open_workflow_store
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app
from owcopilot.service.routers import workflows
from owcopilot.workflow import runtime
from owcopilot.workflow.models import NodeOutcome, WorkflowSpec

PREFIX = "/projects/demo/creative"


class GatedProvider:
    """Only transport is controlled; operators, writer, tracing and storage stay real."""

    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.calls = []

    def complete(self, *, system, user, model):
        self.calls.append({"system": system, "user": user, "model": model})
        if len(self.calls) == 1:
            response = {
                "contract_version": 1,
                "premise": "An offer needs a response",
                "conflict": "Accepting the offer creates an obligation",
                "beats": ["An offer arrives"],
                "events": [{"id": "offer", "summary": "An offer arrives"}],
            }
        elif len(self.calls) == 2:
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("The cancellation check did not release its transport gate")
            response = {
                "id": "late_quest",
                "title": "An offer",
                "objective": "Respond to the offer",
                "dialogue_refs": [],
                "stages": [{"id": "respond", "summary": "Respond to the offer"}],
            }
        else:
            raise AssertionError("This check does not authorize another transport dispatch")
        return json.dumps(response), 1, 1


@pytest.fixture
def local_studio(tmp_path, monkeypatch):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root)}))
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    with TestClient(create_app()) as client:
        yield client, open_workflow_store(root)


def _create_task(client):
    response = client.post(
        f"{PREFIX}/tasks",
        json={"brief": "Explicit HTTP lifecycle check", "participation": "delegate"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _wait_for_job(manager, job_id):
    cursor = 0
    for _ in range(20):
        _, cursor, terminal = manager.wait_events(job_id, cursor, timeout=0.25)
        if terminal:
            return manager.get(job_id)
    raise AssertionError("The isolated background job did not terminate")


def _frames(text):
    frames = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()
        if "data" in fields:
            fields["data"] = json.loads(fields["data"])
            frames.append(fields)
    return frames


def _journal_snapshot(store, run_id):
    # Read the full persisted fixture independently of the production 500-row page limit.
    with store.connection() as db:
        rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def test_http_cancel_discards_inflight_candidate_without_refunding_or_dispatching_successors(
    local_studio, monkeypatch
):
    client, store = local_studio
    provider = GatedProvider()
    monkeypatch.setattr(runtime, "build_real_provider", lambda **kwargs: provider)
    # This isolated test authorizes its controlled provider, never a real network provider.
    monkeypatch.setattr(workflows, "_require_real_allowed", lambda request: None)
    task_view = _create_task(client)
    task = store.get_task(task_view["id"])
    run, _ = store.create_run(task)
    spec = WorkflowSpec(
        name="Focused cancellation graph",
        nodes=[
            {"id": "plan_events", "operation": "plan_events"},
            {
                "id": "draft_quest",
                "operation": "draft_quest",
                "depends_on": ["plan_events"],
            },
            {
                "id": "package",
                "operation": "package",
                "optional_dependencies": ["plan_events", "draft_quest"],
            },
        ],
    )
    # A frozen graph is the input here; interpretation/planning are outside this check.
    store.update_run(run.id, workflow=spec.model_dump(mode="json"))
    budget = f"{task.id}:1:draft_quest"
    job_id = None
    try:
        started = client.post(f"{PREFIX}/tasks/{task.id}/runs")
        assert started.status_code == 202, started.text
        assert started.json()["run_id"] == run.id
        job_id = started.json()["job_id"]
        assert provider.entered.wait(5)
        assert store.attempts(budget) == 1
        assert store.outcomes(run.id)["draft_quest"].status == "running"
        completed_before_cancel = store.artifacts(task.id)
        assert [artifact.kind for artifact in completed_before_cancel] == ["event_plan"]
        cursor = store.events(run.id)[-1]["seq"]

        cancelled = client.post(f"{PREFIX}/runs/{run.id}:cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["run"]["cancel_requested"] is True
    finally:
        provider.release.set()
        if job_id:
            _wait_for_job(client.app.state.jobs_manager, job_id)

    assert len(provider.calls) == 2
    assert store.get_run(run.id).status == "cancelled"
    assert store.attempts(budget) == 1
    assert store.attempts(f"{task.id}:1:package") == 0
    outcomes = store.outcomes(run.id)
    assert outcomes["plan_events"].status == "succeeded"
    assert outcomes["draft_quest"].status == "cancelled"
    assert outcomes["draft_quest"].artifact_id is None
    assert "迟到产物未发布" in outcomes["draft_quest"].error
    assert outcomes["package"].status == "cancelled"
    assert [artifact.id for artifact in store.artifacts(task.id)] == [
        artifact.id for artifact in completed_before_cancel
    ]
    calls = store.model_calls(run.id)
    assert len(calls) == 2
    assert all(call["status"] == "done" for call in calls)
    job = client.app.state.jobs_manager.get(job_id)
    assert job.status == "done"  # Transport completion is distinct from business cancellation.
    assert job.result["status"] == "cancelled"
    assert client.post(f"{PREFIX}/runs/{run.id}:continue", json={}).status_code == 409
    assert store.attempts(budget) == 1

    observed = client.get(f"{PREFIX}/runs/{run.id}/events", params={"after_seq": cursor}).json()
    assert observed["status"] == "cancelled"
    assert any(
        event["event_type"] == "node_outcome"
        and event["payload"].get("node_id") == "draft_quest"
        and event["payload"].get("status") == "cancelled"
        for event in observed["events"]
    )
    empty = client.get(
        f"{PREFIX}/runs/{run.id}/events", params={"after_seq": observed["after_seq"]}
    ).json()
    assert empty["events"] == []
    assert empty["after_seq"] == observed["after_seq"]


def test_creative_sse_resumes_persisted_business_sequence_without_creating_work(local_studio):
    client, store = local_studio
    task_view = _create_task(client)
    task = store.get_task(task_view["id"])
    run, _ = store.create_run(task)
    store.update_run(run.id, status="running")
    store.complete_node(
        run.id,
        NodeOutcome(
            node_id="audit",
            operation="audit",
            status="succeeded",
            budget_id=f"{task.id}:1:audit",
        ),
    )
    # One journal fixture crosses the existing page boundary; this is not a load benchmark.
    with store.connection() as db:
        for index in range(501):
            store._event(db, run.id, "progress", {"step": index})
    store.update_run(run.id, status="completed")
    events = _journal_snapshot(store, run.id)
    query_seq, header_seq = events[0]["seq"], events[1]["seq"]
    expected = [event for event in events if event["seq"] > max(query_seq, header_seq)]
    assert len(expected) > 500
    endpoint = f"{PREFIX}/runs/{run.id}/events/stream"
    response = client.get(
        endpoint,
        params={"after_seq": query_seq},
        headers={"Last-Event-ID": str(header_seq)},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _frames(response.text)
    business = [frame for frame in frames if frame["event"] == "workflow_event"]
    assert [frame["data"] for frame in business] == expected
    assert [int(frame["id"]) for frame in business] == [event["seq"] for event in expected]
    last_seq = expected[-1]["seq"]
    assert frames[-1] == {
        "event": "run_state",
        "data": {"run_id": run.id, "status": "completed", "after_seq": last_seq},
    }
    query_ahead = client.get(
        endpoint,
        params={"after_seq": last_seq},
        headers={"Last-Event-ID": str(header_seq)},
    )
    assert query_ahead.status_code == 200, query_ahead.text
    assert _frames(query_ahead.text) == [frames[-1]]

    # A reconnect only supplies the last received business ID; it never submits a run.
    replay = client.get(endpoint, headers={"Last-Event-ID": str(last_seq)})
    assert replay.status_code == 200, replay.text
    assert _frames(replay.text) == [frames[-1]]
    assert _journal_snapshot(store, run.id) == events
    assert len(store.list_runs(task.id)) == 1
    assert store.get_run(run.id).job_id is None
    assert store.model_calls(run.id) == []
    assert store.attempts(f"{task.id}:1:audit") == 0
