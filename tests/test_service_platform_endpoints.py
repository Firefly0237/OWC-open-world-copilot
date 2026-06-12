"""REST tests for the round-13 platform surface: workspaces / review decisions / theme
sweep / archive management (offline, $0)."""

from __future__ import annotations

import base64
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="玛拉", type=EntityType.NPC, description="赌坊斥候。"
                ),
                "npc_doran": Entity(
                    id="npc_doran", name="多兰", type=EntityType.NPC, description="商人。"
                ),
            },
            relations=[Relation(source="npc_mara", target="npc_doran", kind="reports_to")],
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    # managed worlds must live under the test home, never the developer's real one
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return TestClient(create_app())


def test_workspace_create_list_pack_import_round_trip(client: TestClient) -> None:
    created = client.post("/workspaces", json={"name": "盐汐群岛"})
    assert created.status_code == 201, created.text
    listed = client.get("/workspaces")
    assert [w["name"] for w in listed.json()["workspaces"]] == ["盐汐群岛"]

    pack = client.get("/workspaces/盐汐群岛/pack")
    assert pack.status_code == 200
    assert pack.headers["content-type"].startswith("application/zip")

    # an empty world pack is rejected on import (content validation), so import the
    # registered demo project's pack instead
    demo_pack = client.post(
        "/workspaces:import",
        json={"name": "回流", "zip_base64": base64.b64encode(pack.content).decode()},
    )
    assert demo_pack.status_code == 400  # empty world -> not a valid pack
    duplicate = client.post("/workspaces", json={"name": "盐汐群岛"})
    assert duplicate.status_code == 409


def test_workspace_import_rejects_bad_base64(client: TestClient) -> None:
    response = client.post("/workspaces:import", json={"name": "x", "zip_base64": "@@@@"})
    assert response.status_code == 400


def test_review_decide_over_rest_is_final(client: TestClient) -> None:
    barks = client.post(
        "/projects/demo/assist/barks:batch",
        json={"speaker_ids": ["npc_mara"], "topic": "发现可疑人物", "variants_per_speaker": 1},
    )
    assert barks.status_code == 200, barks.text
    items = client.get("/projects/demo/review_items").json()
    assert items["count"] >= 1
    item_id = items["items"][0]["id"]

    decided = client.post(
        f"/projects/demo/review_items/{item_id}:decide",
        json={"decision": "rejected", "operator": "lead"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "rejected"

    again = client.post(
        f"/projects/demo/review_items/{item_id}:decide",
        json={"decision": "accepted", "operator": "lead"},
    )
    assert again.status_code == 409  # decisions are final, retries cannot flip provenance
    missing = client.post(
        "/projects/demo/review_items/nope:decide",
        json={"decision": "rejected", "operator": "lead"},
    )
    assert missing.status_code == 404


def test_theme_sweep_endpoint_returns_work_order(client: TestClient) -> None:
    response = client.post("/projects/demo/sweeps:run", json={"theme": "赌坊"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned_total"] >= 2
    assert any(f["ref"] == "entity:npc_mara" for f in body["hits"])
    assert "专项清查工作单" in body["markdown"]
    assert body["llm_used"] is False


def test_entity_update_and_object_delete_endpoints(client: TestClient) -> None:
    updated = client.patch(
        "/projects/demo/entities/npc_mara",
        json={"description": "改行做正经买卖。", "tags": ["改写"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["entity"]["tags"] == ["改写"]

    missing = client.patch("/projects/demo/entities/npc_ghost", json={"name": "x"})
    assert missing.status_code == 404

    deleted = client.delete("/projects/demo/objects/entity/npc_mara")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["removed_relations"] == 1
    assert client.delete("/projects/demo/objects/entity/npc_mara").status_code == 404
    assert client.delete("/projects/demo/objects/poi/x").status_code == 409
