"""Read-only job status and buffered SSE contracts used by narrative submissions."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from owcopilot.service.api import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OWCOPILOT_FRONTEND_DIST", str(tmp_path / "missing"))
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app())


def _completed_job(client: TestClient) -> tuple[str, dict]:
    def runner(emit):
        emit("stage", {"stage": "controlled"})
        return {"marker": "observed-result"}

    job = client.app.state.jobs_manager.submit("narrative-contract", runner)
    for _ in range(100):
        response = client.get(f"/jobs/{job.id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"done", "failed"}:
            return job.id, body
        time.sleep(0.01)
    raise AssertionError("controlled job did not terminate")


def test_job_observer_reports_actual_result_and_buffered_events(client: TestClient) -> None:
    _job_id, body = _completed_job(client)
    assert body["status"] == "done", body
    assert body["result"] == {"marker": "observed-result"}
    assert any(event["type"] == "stage" for event in body["events"])
    assert body["events"][-1]["type"] == "done"


def test_job_events_sse_replays_and_terminates(client: TestClient) -> None:
    job_id, _body = _completed_job(client)
    with client.stream("GET", f"/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]
    assert "event: stage" in lines
    assert "event: done" in lines


def test_unknown_job_is_404_and_legacy_submission_is_retired(client: TestClient) -> None:
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/events").status_code == 404
    assert client.post("/projects/missing/jobs", json={}).status_code == 404
