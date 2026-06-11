"""Service hardening tests: real-mode fail-closed, shared cache reuse, storage pragmas."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.storage import SQLiteStore  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="Mara", type=EntityType.NPC, description="Scout."
                )
            }
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return TestClient(create_app())


def test_real_mode_fails_closed_without_service_key(client: TestClient) -> None:
    """llm_mode=real spends money — without OWCOPILOT_API_KEY the API must refuse."""
    response = client.post(
        "/projects/demo/contents/quests:draft",
        json={"brief": "a quest", "llm_mode": "real"},
    )
    assert response.status_code == 403
    assert "OWCOPILOT_API_KEY" in response.json()["detail"]


def test_real_mode_requires_provider_config(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_API_KEY", "k")
    response = client.post(
        "/projects/demo/contents/quests:draft",
        json={"brief": "a quest", "llm_mode": "real"},
        headers={"X-API-Key": "k"},
    )
    assert response.status_code == 503


def test_offline_mode_unaffected_by_fail_closed(client: TestClient) -> None:
    response = client.post("/projects/demo/contents/quests:draft", json={"brief": "a quest"})
    assert response.status_code == 200


def test_ask_uses_shared_cache_across_requests(client: TestClient) -> None:
    """Second identical question must be a client-side cache hit (calls recorded, $0 provider)."""
    body = {"query": "Who is Mara?"}
    first = client.post("/projects/demo/ask", json=body).json()
    second = client.post("/projects/demo/ask", json=body).json()
    assert first["answer"]["answer"] == second["answer"]["answer"]
    assert second["telemetry"]["cache_hit_rate"] == 1.0


def test_suggest_request_supports_budget_field(client: TestClient) -> None:
    client.post("/projects/demo/audits", json={"persist": True})
    response = client.post("/projects/demo/issues/nope/suggestions", json={"max_cost_usd": 0.01})
    assert response.status_code == 404  # field accepted; unknown issue still 404


def test_sqlite_uses_wal_for_file_databases(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "rt.sqlite")
    try:
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        for index in (
            "idx_patches_status",
            "idx_review_items_status",
        ):
            row = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()
            assert row is not None, index
    finally:
        store.close()
