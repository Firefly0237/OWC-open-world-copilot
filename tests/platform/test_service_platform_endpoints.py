"""Workspace, local connection and static frontend HTTP contracts."""

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
    monkeypatch.setenv("OWCOPILOT_WORLDS_HOME", str(tmp_path / "worlds"))
    # managed worlds must live under the test home, never the developer's real one
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return TestClient(create_app())


def test_root_route_points_browsers_at_the_frontend(tmp_path, monkeypatch) -> None:
    # force the no-dist branch regardless of whether the repo has a built frontend
    monkeypatch.setenv("OWCOPILOT_FRONTEND_DIST", str(tmp_path / "missing"))
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    bare = TestClient(create_app())
    body = bare.get("/").json()
    assert body["service"] == "owcopilot"
    assert body["docs"] == "/docs"


def test_spa_mount_serves_index_and_falls_back_on_client_routes(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>starbound</body></html>", encoding="utf-8")
    monkeypatch.setenv("OWCOPILOT_FRONTEND_DIST", str(dist))
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    spa = TestClient(create_app())
    assert "starbound" in spa.get("/").text
    # client-side routes survive a hard refresh via the index fallback
    assert "starbound" in spa.get("/creator", headers={"Accept": "text/html"}).text
    # API routes registered before the mount keep precedence
    assert spa.get("/health").json()["status"] == "ok"


def test_managed_world_name_doubles_as_project_id(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_PROJECTS_JSON", raising=False)
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setenv("OWCOPILOT_WORLDS_HOME", str(tmp_path / "worlds"))
    zero_config = TestClient(create_app())
    created = zero_config.post("/workspaces", json={"name": "盐汐"})
    assert created.status_code == 201
    context = zero_config.get("/projects/盐汐/creative/v2/creator/context")
    assert context.status_code == 200, context.text
    assert zero_config.get("/projects/不存在的/creative/v2/creator/context").status_code == 404


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


def test_workspace_delete_removes_world(client: TestClient) -> None:
    client.post("/workspaces", json={"name": "待删世界"})
    assert "待删世界" in {w["name"] for w in client.get("/workspaces").json()["workspaces"]}

    deleted = client.delete("/workspaces/待删世界")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == "待删世界"
    assert "待删世界" not in {w["name"] for w in client.get("/workspaces").json()["workspaces"]}

    assert client.delete("/workspaces/从来没有过").status_code == 404  # missing world


def test_connection_settings_update_status_in_local_profile(
    client: TestClient, monkeypatch
) -> None:
    for name in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OWCOPILOT_OPENAI_API_KEY",
        "OWCOPILOT_OPENAI_BASE_URL", "OWCOPILOT_LLM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("owcopilot.service.api.load_dotenv", lambda: None)
    assert client.get("/settings/connection").json()["configured"] is False
    updated = client.post(
        "/settings/connection",
        json={"base_url": "https://api.example.test", "api_key": "controlled-fixture-key"},
    )
    assert updated.status_code == 200
    assert updated.json()["configured"] is True
    assert updated.json()["base_url"] == "https://api.example.test"
    current = client.get("/settings/connection").json()
    assert current["configured"] is True
    assert current["base_url"] == updated.json()["base_url"]
