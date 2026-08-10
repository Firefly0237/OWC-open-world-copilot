"""Cursor contract for the per-object media ledger."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.assets import AssetKind, AssetStore, attach  # noqa: E402
from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def asset_client(tmp_path, monkeypatch) -> tuple[TestClient, AssetStore]:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "keeper": Entity(id="keeper", name="守卷人", type=EntityType.NPC),
            }
        )
    )
    ledger = AssetStore(root)
    state = ledger.load()
    for index in range(25):
        attach(
            state,
            object_ref="entity:keeper",
            kind=AssetKind.IMAGE,
            uri=f"art/{index:02d}.png",
            title=f"素材 {index:02d}",
        )
    ledger.save(state)
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({"demo": str(root).replace("\\", "/")}),
    )
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")
    return TestClient(create_app()), ledger


def test_asset_pages_are_bounded_newest_first_and_keep_legacy_shape(asset_client) -> None:
    client, _ledger = asset_client

    legacy = client.get("/projects/demo/assets", params={"object_ref": "entity:keeper"})
    assert legacy.status_code == 200, legacy.text
    assert len(legacy.json()["assets"]) == 25
    assert "total" not in legacy.json()

    first = client.get(
        "/projects/demo/assets",
        params={"object_ref": "entity:keeper", "limit": 10},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 25
    assert len(body["assets"]) == 10
    assert body["assets"][0]["uri"] == "art/24.png"
    assert body["assets"][-1]["uri"] == "art/15.png"
    assert body["next_cursor"]
    assert body["snapshot_etag"]

    second = client.get(
        "/projects/demo/assets",
        params={
            "object_ref": "entity:keeper",
            "limit": 10,
            "cursor": body["next_cursor"],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["assets"][0]["uri"] == "art/14.png"

    assert client.get("/projects/demo/assets", params={"limit": 10}).status_code == 422
    assert (
        client.get(
            "/projects/demo/assets",
            params={"object_ref": "entity:keeper", "limit": 10, "cursor": "bad"},
        ).status_code
        == 422
    )


def test_asset_cursor_rejects_a_changed_ledger(asset_client) -> None:
    client, ledger = asset_client
    first = client.get(
        "/projects/demo/assets",
        params={"object_ref": "entity:keeper", "limit": 10},
    ).json()

    state = ledger.load()
    attach(
        state,
        object_ref="entity:keeper",
        kind=AssetKind.MAP,
        uri="maps/newest.png",
    )
    ledger.save(state)

    stale = client.get(
        "/projects/demo/assets",
        params={
            "object_ref": "entity:keeper",
            "limit": 10,
            "cursor": first["next_cursor"],
        },
    )
    assert stale.status_code == 409
