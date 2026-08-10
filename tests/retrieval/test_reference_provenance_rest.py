"""The rights declaration over REST (B2 / N2.2): both intake doors and the async job path."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.inspiration.models import Provenance  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402

_MANUSCRIPT = "沈青澜说道：灯不对。沈青澜与陆惊鸿前往枯叶林。"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("owcopilot.service.api.load_dotenv", lambda: None)
    return TestClient(create_app())


def _wait_done(client: TestClient, job_id: str, *, attempts: int = 100) -> dict[str, Any]:
    for _ in range(attempts):
        body: dict[str, Any] = client.get(f"/jobs/{job_id}").json()
        if body["status"] in {"done", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_extraction_run_refuses_third_party_material(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/extractions:run",
        json={
            "title": "别人的小说",
            "text": _MANUSCRIPT,
            "glean_rounds": 0,
            "provenance": Provenance.THIRD_PARTY_REFERENCE.value,
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "素材库" in detail
    assert "灵感库" in detail


def test_extraction_run_accepts_licensed_material(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/extractions:run",
        json={
            "title": "第一章",
            "text": _MANUSCRIPT,
            "glean_rounds": 0,
            "provenance": Provenance.LICENSED.value,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["stats"]["entities"] >= 2


def test_extraction_run_rejects_a_provenance_outside_the_vocabulary(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/extractions:run",
        json={"title": "第一章", "text": _MANUSCRIPT, "provenance": "public_domain"},
    )
    assert response.status_code == 422, response.text


def test_reference_library_takes_third_party_material(client: TestClient) -> None:
    created = client.post(
        "/projects/demo/references",
        json={
            "title": "别人的小说",
            "text": "一段用来取灵感的第三方文本，讲的是盐道与商会。",
            "provenance": Provenance.THIRD_PARTY_REFERENCE.value,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["source"]["provenance"] == Provenance.THIRD_PARTY_REFERENCE.value

    listed = client.get("/projects/demo/references").json()["sources"]
    assert [source["provenance"] for source in listed] == [Provenance.THIRD_PARTY_REFERENCE.value]


def test_extraction_job_carries_the_declaration(client: TestClient) -> None:
    """The frontend runs extraction as an async job, so provenance must survive that path too."""
    created = client.post(
        "/projects/demo/jobs",
        json={
            "kind": "extraction",
            "params": {
                "title": "第一章",
                "text": _MANUSCRIPT,
                "glean_rounds": 0,
                "provenance": Provenance.THIRD_PARTY_REFERENCE.value,
            },
        },
    )
    assert created.status_code == 202, created.text
    status = _wait_done(client, created.json()["job_id"])
    assert status["status"] == "failed"
    assert "灵感库" in status["error"]
