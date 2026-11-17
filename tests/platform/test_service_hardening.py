"""Authentication, API-surface and storage contracts for the narrative service."""

from __future__ import annotations

import re

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.storage import SQLiteStore  # noqa: E402


def test_every_protected_route_requires_auth(monkeypatch) -> None:
    # Guard for the single global auth gate: every non-public API route must reject an
    # unauthenticated request. If a new endpoint ever lands outside the gate, this fails — which is
    # what lets us trust one dependency instead of a per-endpoint preamble. The gate runs before
    # body validation, so even POSTs with no body return 401 (not 422), keeping this check simple.
    monkeypatch.setenv("OWCOPILOT_API_KEY", "secret-guard")
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")  # don't trip the limiter mid-sweep
    app = create_app()
    guard_client = TestClient(app)
    public = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
    http_methods = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}

    # FastAPI 0.141 keeps included APIRouters nested instead of flattening them into app.routes.
    # OpenAPI is the public recursive view of every documented operation, so sweeping it prevents
    # an auth regression from disappearing merely because a router is included another level down.
    schema_paths = app.openapi()["paths"]
    assert "/projects/{project}/creative/v2/tasks" in schema_paths
    checked = 0
    expected = 0
    for route_path, path_item in sorted(schema_paths.items()):
        if route_path in public:
            continue
        expected += len(http_methods.intersection(path_item))
        path = re.sub(r"\{[^}]+\}", "x", route_path)  # dummy value for any {path_param}
        for method in sorted(http_methods.intersection(path_item)):
            resp = guard_client.request(method, path)  # deliberately no X-API-Key
            assert resp.status_code == 401, (
                f"{method.upper()} {route_path} -> {resp.status_code}, want 401"
            )
            checked += 1
    assert checked == expected and checked > 0


def test_every_openapi_operation_has_a_domain_tag() -> None:
    schema = create_app().openapi()
    http_methods = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
    untagged: list[str] = []
    checked = 0
    for route_path, path_item in sorted(schema["paths"].items()):
        for method in sorted(http_methods.intersection(path_item)):
            operation = path_item[method]
            if not operation.get("tags"):
                untagged.append(f"{method.upper()} {route_path}")
            checked += 1

    assert "/projects/{project}/creative/v2/tasks" in schema["paths"]
    assert checked == sum(len(http_methods.intersection(item)) for item in schema["paths"].values())
    assert untagged == []


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


@pytest.mark.parametrize("method,path", [
    ("GET", "/projects/sample/analytics/summary"),
    ("GET", "/projects/sample/assets"),
    ("POST", "/projects/sample/contents/quests:draft"),
    ("POST", "/projects/sample/audits"),
    ("GET", "/projects/sample/collab"),
    ("POST", "/projects/sample/compliance:scan"),
    ("PUT", "/projects/sample/entities/entity"),
    ("POST", "/projects/sample/engine:import"),
    ("GET", "/projects/sample/engineering/catalog"),
    ("POST", "/projects/sample/exports"),
    ("GET", "/projects/sample/localization"),
    ("GET", "/platform/me"),
    ("GET", "/projects/sample/references"),
    ("POST", "/projects/sample/impact:analyze"),
    ("POST", "/projects/sample/ask"),
    ("GET", "/projects/sample/review_items"),
    ("GET", "/templates"),
    ("GET", "/projects/sample/overview"),
])
def test_retired_http_surfaces_are_not_exposed(method, path, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OWCOPILOT_FRONTEND_DIST", str(tmp_path / "missing"))
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    client = TestClient(create_app())
    assert client.request(method, path).status_code == 404
