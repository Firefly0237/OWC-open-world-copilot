"""Cursor contract for the local managed-world catalog."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.service.routers import workspaces as workspaces_router  # noqa: E402


@pytest.fixture()
def catalog(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[dict[str, str]]]:
    rows = [
        {"name": f"world-{index:03d}", "path": f"C:/worlds/world-{index:03d}"}
        for index in range(67)
    ]
    monkeypatch.setattr(
        workspaces_router,
        "list_managed_worlds",
        lambda: [dict(row) for row in rows],
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app()), rows


def test_workspace_legacy_shape_and_focused_cursor_walk(
    catalog: tuple[TestClient, list[dict[str, str]]],
) -> None:
    client, rows = catalog
    legacy = client.get("/workspaces")
    assert legacy.status_code == 200
    assert legacy.json() == {"workspaces": rows}

    focus = "world-055"
    first = client.get("/workspaces", params={"limit": 30, "focus": focus})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total"] == len(rows)
    assert len(first_body["workspaces"]) == 30
    assert first_body["workspaces"][0]["name"] == focus
    assert first_body["next_cursor"]
    assert first_body["snapshot_etag"]

    seen = list(first_body["workspaces"])
    cursor = first_body["next_cursor"]
    while cursor:
        response = client.get(
            "/workspaces",
            params={"limit": 30, "focus": focus, "cursor": cursor},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["snapshot_etag"] == first_body["snapshot_etag"]
        seen.extend(body["workspaces"])
        cursor = body["next_cursor"]

    assert len(seen) == len(rows)
    assert {row["name"] for row in seen} == {row["name"] for row in rows}


def test_workspace_cursor_validation_and_stale_snapshot(
    catalog: tuple[TestClient, list[dict[str, str]]],
) -> None:
    client, rows = catalog
    assert client.get("/workspaces", params={"limit": 0}).status_code == 422
    assert (
        client.get("/workspaces", params={"limit": 30, "cursor": "not-a-cursor"}).status_code == 422
    )

    first = client.get("/workspaces", params={"limit": 30})
    cursor = first.json()["next_cursor"]
    rows.append({"name": "world-new", "path": "C:/worlds/world-new"})
    stale = client.get("/workspaces", params={"limit": 30, "cursor": cursor})
    assert stale.status_code == 409
    assert "发生变化" in stale.json()["detail"]
