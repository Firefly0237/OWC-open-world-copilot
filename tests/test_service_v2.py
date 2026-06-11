"""v2 project API endpoints.

These tests use inline ContentBundle payloads so the REST layer can be verified without a server,
filesystem project registry, network calls, or real LLM credentials.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="install with: pip install -e '.[serve]'")

from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


def _content_bundle() -> dict:
    return {
        "entities": {
            "npc_aldric": {
                "id": "npc_aldric",
                "name": "Aldric",
                "type": "npc",
                "description": "Caravan master who hires scouts for Northwatch.",
            },
            "location_northwatch": {
                "id": "location_northwatch",
                "name": "Northwatch",
                "type": "location",
                "description": "A fortified trade town on the northern road.",
            },
        },
        "quests": {
            "quest_missing_caravan": {
                "id": "quest_missing_caravan",
                "title": "Missing Caravan",
                "giver_npc": "npc_missing",
                "location": "location_northwatch",
                "objective": "Find the missing caravan before nightfall.",
            }
        },
    }


def _write_project(content_root, content: dict | None = None) -> None:
    ContentStore(content_root).save(ContentBundle.model_validate(content or _content_bundle()))


def _register_project(monkeypatch, project: str, content_root) -> None:
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps({project: str(content_root)}))


def test_project_audit_runs_default_rules_and_persists_issues() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/projects/demo/audits",
        json={"content": _content_bundle()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "demo"
    assert len(body["content_hash"]) == 64
    assert body["totals"]["error"] >= 1
    assert body["audit_run"]["rule_set_version"] == "v2.0"
    assert body["audit_run"]["totals"] == body["totals"]
    assert body["issues"][0]["rule_code"] == "UNKNOWN_ENTITY_REF"
    assert body["issues"][0]["target_ref"] == "quest:quest_missing_caravan"
    assert body["cost_budget"]["used_usd"] == 0.0

    persisted = client.get("/projects/demo/issues")
    assert persisted.status_code == 200
    assert persisted.json()["issues"] == body["issues"]
    assert persisted.json()["cost_budget"]["used_usd"] == 0.0


def test_project_audit_can_skip_persistence() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/projects/demo/audits",
        json={"content": _content_bundle(), "persist": False},
    )

    assert response.status_code == 200
    persisted = client.get("/projects/demo/issues")
    assert persisted.status_code == 200
    assert persisted.json()["issues"] == []


def test_project_context_pack_returns_ranked_refs_from_inline_content() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/projects/demo/context:pack",
        json={"content": _content_bundle(), "query": "Aldric caravan", "budget_tokens": 120},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "demo"
    assert "entity:npc_aldric" in body["refs"]
    assert body["hits"]
    assert all({"ref", "title", "body", "score", "source"} <= set(hit) for hit in body["hits"])
    assert body["cost_budget"]["used_usd"] == 0.0


def test_project_ask_returns_grounded_answer_and_telemetry() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/projects/demo/ask",
        json={"content": _content_bundle(), "query": "Who is Aldric?", "budget_tokens": 120},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "demo"
    assert body["answer"]["refused"] is False
    assert body["answer"]["citations"][0]["ref"] == "entity:npc_aldric"
    assert body["telemetry"]["calls"] == 1
    assert body["cost_budget"]["over_budget"] is False

    over_budget = client.post(
        "/projects/demo/ask",
        json={
            "content": _content_bundle(),
            "query": "Who is Aldric?",
            "budget_tokens": 120,
            "max_cost_usd": 0,
        },
    )
    assert over_budget.status_code == 200
    assert over_budget.json()["cost_budget"]["over_budget"] is True


def test_project_ask_refuses_when_no_context_is_available() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/projects/demo/ask",
        json={"content": {}, "query": "Who is Aldric?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]["refused"] is True
    assert body["answer"]["citations"] == []
    assert body["telemetry"]["calls"] == 0
    assert body["cost_budget"]["used_usd"] == 0.0


def test_registered_project_audit_persists_issues_to_sqlite(tmp_path, monkeypatch) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)
    _register_project(monkeypatch, "demo", content_root)
    client = TestClient(create_app())

    response = client.post("/projects/demo/audits", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["issues"][0]["rule_code"] == "UNKNOWN_ENTITY_REF"

    persisted = client.get("/projects/demo/issues?rule_code=UNKNOWN_ENTITY_REF&status=open")
    assert persisted.status_code == 200
    persisted_body = persisted.json()
    assert persisted_body["project"] == "demo"
    assert [issue["rule_code"] for issue in persisted_body["issues"]] == ["UNKNOWN_ENTITY_REF"]


def test_registered_project_context_pack_and_ask_use_project_context(
    tmp_path,
    monkeypatch,
) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)
    _register_project(monkeypatch, "demo", content_root)
    client = TestClient(create_app())

    context_response = client.post(
        "/projects/demo/context:pack",
        json={"query": "Aldric caravan", "budget_tokens": 120},
    )
    ask_response = client.post(
        "/projects/demo/ask",
        json={"query": "Who is Aldric?", "budget_tokens": 120},
    )

    assert context_response.status_code == 200
    assert "entity:npc_aldric" in context_response.json()["refs"]
    assert ask_response.status_code == 200
    assert ask_response.json()["answer"]["citations"][0]["ref"] == "entity:npc_aldric"


def test_unregistered_project_without_inline_content_is_404() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/projects/missing/context:pack",
        json={"query": "Aldric"},
    )

    assert response.status_code == 404
