"""REST tests for the async job runner + SSE event stream (offline, $0)."""

from __future__ import annotations

import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402

_MANUSCRIPT = "沈青澜说道：灯不对。沈青澜与陆惊鸿前往枯叶林。陆惊鸿带上了旧罗盘。"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_a": Entity(
                    id="npc_a", name="沈青澜", type=EntityType.NPC, description="守灯人。"
                )
            }
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app())


def _wait_done(client: TestClient, job_id: str, *, attempts: int = 100) -> dict:
    for _ in range(attempts):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] in {"done", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_extraction_job_runs_async_with_chunk_progress(client: TestClient) -> None:
    created = client.post(
        "/projects/demo/jobs",
        json={"kind": "extraction", "params": {"title": "第一章", "text": _MANUSCRIPT}},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    body = _wait_done(client, job_id)
    assert body["status"] == "done", body
    assert body["result"]["stats"]["entities"] >= 2
    event_types = [event["type"] for event in body["events"]]
    assert "chunk" in event_types  # progress callback reached the job buffer
    assert event_types[-1] == "done"


def test_build_overview_job_indexes_community_reports(client: TestClient) -> None:
    created = client.post(
        "/projects/demo/jobs",
        json={"kind": "build_overview", "params": {"llm_mode": "offline"}},
    )
    assert created.status_code == 202, created.text
    body = _wait_done(client, created.json()["job_id"])
    assert body["status"] == "done", body
    result = body["result"]
    assert result["community_count"] >= 1
    # a global synthesis report is always produced on top of the per-community ones
    assert any(report["level"] == "global" for report in result["reports"])


def test_job_events_sse_replays_and_terminates(client: TestClient) -> None:
    created = client.post(
        "/projects/demo/jobs",
        json={"kind": "theme_sweep", "params": {"theme": "守灯", "use_llm": False}},
    )
    job_id = created.json()["job_id"]
    _wait_done(client, job_id)

    with client.stream("GET", f"/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]
    assert any(line == "event: done" for line in lines)  # terminal event closes the stream


def test_job_rejects_unknown_params_and_unknown_job(client: TestClient) -> None:
    bad = client.post(
        "/projects/demo/jobs",
        json={"kind": "extraction", "params": {"title": "x", "text": "y", "evil": 1}},
    )
    assert bad.status_code == 400
    assert "evil" in bad.json()["detail"]
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/events").status_code == 404


def test_overview_and_archive_read_endpoints(client: TestClient) -> None:
    overview = client.get("/projects/demo/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["overview"]["counts"]["entities"] == 1
    archive = client.get("/projects/demo/archive")
    assert archive.status_code == 200
    names = [row["name"] for row in archive.json()["inventory"]["entities"]]
    assert names == ["沈青澜"]
    assert client.get("/projects/ghost/overview").status_code == 404
