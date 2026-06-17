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
    # the real gate dotenv-loads exactly like the gateway; neutralize the repo .env so the
    # developer's real key can't leak in and turn a fail-closed assertion into a live call
    monkeypatch.setattr("owcopilot.service.api.load_dotenv", lambda: None)
    return TestClient(create_app())


def test_every_protected_route_requires_auth(monkeypatch) -> None:
    # Guard for the single global auth gate: every non-public API route must reject an
    # unauthenticated request. If a new endpoint ever lands outside the gate, this fails — which is
    # what lets us trust one dependency instead of a per-endpoint preamble. The gate runs before
    # body validation, so even POSTs with no body return 401 (not 422), keeping this check simple.
    import re

    from fastapi.routing import APIRoute

    monkeypatch.setenv("OWCOPILOT_API_KEY", "secret-guard")
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")  # don't trip the limiter mid-sweep
    app = create_app()
    guard_client = TestClient(app)
    public = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}

    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in public:
            continue
        # /platform/* is a separate auth domain (bearer JWT OR key, enforced inside the handler via
        # resolve_principal and covered by the platform test suite), deliberately exempt from the
        # global api-key gate this guard protects.
        if route.path.startswith("/platform/"):
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)  # dummy value for any {path_param}
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            resp = guard_client.request(method, path)  # deliberately no X-API-Key
            assert resp.status_code == 401, f"{method} {route.path} -> {resp.status_code}, want 401"
            checked += 1
    assert checked >= 80  # the whole API surface was exercised, not a couple of routes


def test_real_mode_on_loopback_needs_provider_not_service_key(client: TestClient) -> None:
    """Localhost owns the machine and the key: real mode is allowed without
    OWCOPILOT_API_KEY, but still needs a provider — so it's a 503 setup prompt, not a 403."""
    response = client.post(
        "/projects/demo/contents/quests:draft",
        json={"brief": "a quest", "llm_mode": "real"},
    )
    assert response.status_code == 503


def test_real_mode_fails_closed_for_remote_without_service_key(
    client: TestClient, monkeypatch
) -> None:
    """A non-loopback caller with no OWCOPILOT_API_KEY is refused outright (real spends money)."""
    monkeypatch.setattr("owcopilot.service.api._is_loopback", lambda request: False)
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
