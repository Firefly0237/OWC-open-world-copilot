"""REST tests for the creator endpoints: extraction, dialogue trees, flavor (offline, $0)."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="玛拉", type=EntityType.NPC, description="斥候。"
                ),
                "npc_doran": Entity(
                    id="npc_doran", name="多兰", type=EntityType.NPC, description="商人。"
                ),
            }
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # real gate dotenv-loads like the gateway; neutralize the repo .env in tests
    monkeypatch.setattr("owcopilot.service.api.load_dotenv", lambda: None)
    return TestClient(create_app())


def test_extraction_run_then_submit_round_trip(client: TestClient) -> None:
    run = client.post(
        "/projects/demo/extractions:run",
        json={"title": "第一章", "text": "沈青澜说道：灯不对。沈青澜与陆惊鸿前往枯叶林。"},
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["stats"]["entities"] >= 2
    submitted = client.post(
        "/projects/demo/extractions:submit",
        json={"draft": body["draft"], "include_beats_as_quests": True},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["review_item_id"]


def test_extraction_submit_rejects_malformed_draft(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/extractions:submit", json={"draft": {"id": 1, "bundle": "nope"}}
    )
    assert response.status_code == 422


def test_dialogue_tree_endpoint_drafts_into_review(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/assist/dialogue_trees:draft",
        json={"participant_ids": ["npc_mara", "npc_doran"], "brief": "雨夜对质"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["structure_problems"] == []
    assert body["review_item_id"]
    assert len(body["tree"]["nodes"]) >= 3


def test_dialogue_tree_endpoint_rejects_unknown_participant(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/assist/dialogue_trees:draft",
        json={"participant_ids": ["npc_ghost"], "brief": "测试"},
    )
    assert response.status_code == 422


def test_flavor_endpoint_batches_into_review(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/assist/flavor:batch",
        json={"category": "item", "names": ["雾隐灯", "枯叶军徽"], "theme": "异象"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["accepted"]) == 2
    assert body["review_item_id"]


def test_creator_endpoints_real_mode_on_loopback_needs_provider(client: TestClient) -> None:
    # loopback (TestClient) is the owner: real is allowed without OWCOPILOT_API_KEY, but with
    # no provider configured it's a 503 setup prompt rather than a 403
    response = client.post(
        "/projects/demo/extractions:run",
        json={"title": "t", "text": "x", "llm_mode": "real"},
    )
    assert response.status_code == 503


def test_creator_endpoints_real_mode_fail_closed_for_remote(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr("owcopilot.service.api._is_loopback", lambda request: False)
    response = client.post(
        "/projects/demo/extractions:run",
        json={"title": "t", "text": "x", "llm_mode": "real"},
    )
    assert response.status_code == 403  # remote caller, no OWCOPILOT_API_KEY
