"""Bounded analytics summary and drill-down contracts."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def analytics_client(tmp_path, monkeypatch) -> tuple[TestClient, ContentStore]:
    root = tmp_path / "content"
    entities: dict[str, Entity] = {}
    for index in range(45):
        faction_id = f"faction-{index:02d}"
        npc_id = f"npc-{index:02d}"
        entities[faction_id] = Entity(
            id=faction_id,
            name=f"势力 {index:02d}",
            type=EntityType.FACTION,
            description="已有势力简介。",
        )
        entities[npc_id] = Entity(
            id=npc_id,
            name=f"角色 {index:02d}",
            type=EntityType.NPC,
            description="",
        )
    store = ContentStore(root)
    store.save(ContentBundle(entities=entities))
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({"demo": str(root).replace("\\", "/")}),
    )
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")
    return TestClient(create_app()), store


def test_analytics_summary_omits_rows_and_drilldowns_are_cursor_paged(analytics_client) -> None:
    client, _store = analytics_client

    summary = client.get("/projects/demo/analytics/summary")
    assert summary.status_code == 200, summary.text
    analytics = summary.json()["analytics"]
    assert analytics["faction_count"] == 45
    assert analytics["underdeveloped_faction_count"] == 45
    assert analytics["gap_counts"]["entities_without_description"] == 45
    assert "factions" not in analytics
    assert "gaps" not in analytics

    factions = client.get("/projects/demo/analytics/factions", params={"limit": 20})
    assert factions.status_code == 200, factions.text
    faction_page = factions.json()
    assert faction_page["total"] == 45
    assert len(faction_page["factions"]) == 20
    assert faction_page["next_cursor"]

    second = client.get(
        "/projects/demo/analytics/factions",
        params={"limit": 20, "cursor": faction_page["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["factions"]) == 20

    underdeveloped = client.get(
        "/projects/demo/analytics/factions",
        params={"underdeveloped": True, "limit": 10},
    )
    assert underdeveloped.status_code == 200
    assert underdeveloped.json()["total"] == 45

    gaps = client.get(
        "/projects/demo/analytics/gaps/entities_without_description",
        params={"limit": 20},
    )
    assert gaps.status_code == 200, gaps.text
    assert gaps.json()["total"] == 45
    assert len(gaps.json()["ids"]) == 20
    assert gaps.json()["next_cursor"]

    assert client.get("/projects/demo/analytics/gaps/not-real").status_code == 404
    assert (
        client.get(
            "/projects/demo/analytics/factions",
            params={"limit": 20, "cursor": "bad"},
        ).status_code
        == 422
    )


def test_analytics_cursor_rejects_a_changed_world(analytics_client) -> None:
    client, store = analytics_client
    first = client.get("/projects/demo/analytics/factions", params={"limit": 20}).json()

    bundle = store.load()
    bundle.entities["faction-new"] = Entity(
        id="faction-new",
        name="新势力",
        type=EntityType.FACTION,
        description="新建势力。",
    )
    store.save(bundle)

    stale = client.get(
        "/projects/demo/analytics/factions",
        params={"limit": 20, "cursor": first["next_cursor"]},
    )
    assert stale.status_code == 409
