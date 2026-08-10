"""Bounded engineering selector catalog contracts."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Quest  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def catalog_client(tmp_path, monkeypatch) -> tuple[TestClient, ContentStore]:
    root = tmp_path / "content"
    store = ContentStore(root)
    store.save(
        ContentBundle(
            quests={
                f"quest-{index:02d}": Quest(id=f"quest-{index:02d}", title=f"任务 {index:02d}")
                for index in range(45)
            }
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({"demo": str(root).replace("\\", "/")}),
    )
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")
    return TestClient(create_app()), store


def test_engineering_catalog_keeps_legacy_shape_and_pages_each_kind(catalog_client) -> None:
    client, _store = catalog_client
    legacy = client.get("/projects/demo/engineering/catalog")
    assert legacy.status_code == 200, legacy.text
    assert len(legacy.json()["quest_ids"]) == 45
    assert legacy.json()["total"] is None

    first = client.get(
        "/projects/demo/engineering/catalog",
        params={"kind": "quest", "limit": 20},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["kind"] == "quest"
    assert body["total"] == 45
    assert len(body["quest_ids"]) == 20
    assert body["dialogue_tree_ids"] == []
    assert body["style_rubrics"] == []
    assert body["next_cursor"]
    assert body["snapshot_etag"]

    second = client.get(
        "/projects/demo/engineering/catalog",
        params={"kind": "quest", "limit": 20, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["quest_ids"]) == 20

    for kind, key in (
        ("dialogue_tree", "dialogue_tree_ids"),
        ("style_rubric", "style_rubrics"),
    ):
        response = client.get(
            "/projects/demo/engineering/catalog",
            params={"kind": kind, "limit": 20},
        )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == kind
        assert response.json()[key] == []

    assert client.get("/projects/demo/engineering/catalog", params={"limit": 20}).status_code == 422
    assert (
        client.get(
            "/projects/demo/engineering/catalog",
            params={"kind": "quest", "limit": 20, "cursor": "bad"},
        ).status_code
        == 422
    )


def test_engineering_catalog_cursor_rejects_a_changed_world(catalog_client) -> None:
    client, store = catalog_client
    first = client.get(
        "/projects/demo/engineering/catalog",
        params={"kind": "quest", "limit": 20},
    ).json()

    bundle = store.load()
    bundle.quests["quest-new"] = Quest(id="quest-new", title="新任务")
    store.save(bundle)

    stale = client.get(
        "/projects/demo/engineering/catalog",
        params={"kind": "quest", "limit": 20, "cursor": first["next_cursor"]},
    )
    assert stale.status_code == 409
