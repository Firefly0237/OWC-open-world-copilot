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
    assert "starbound" in spa.get("/review").text
    # API routes registered before the mount keep precedence
    assert spa.get("/health").json()["status"] == "ok"


def test_managed_world_name_doubles_as_project_id(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_PROJECTS_JSON", raising=False)
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    zero_config = TestClient(create_app())
    created = zero_config.post("/workspaces", json={"name": "盐汐"})
    assert created.status_code == 201
    overview = zero_config.get("/projects/盐汐/overview")
    assert overview.status_code == 200, overview.text
    assert zero_config.get("/projects/不存在的/overview").status_code == 404


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


def test_review_revise_over_rest_regenerates_in_place(client: TestClient) -> None:
    drafted = client.post(
        "/projects/demo/contents/quests:draft",
        json={"brief": "让 Mara 护送商队穿过山道", "llm_mode": "offline"},
    )
    assert drafted.status_code == 200, drafted.text
    item_id = drafted.json()["review_item_id"]

    revised = client.post(
        f"/projects/demo/review_items/{item_id}:revise",
        json={"feedback": "把目标写得更具体，加入失败后果", "operator": "lead"},
    )
    assert revised.status_code == 200, revised.text
    body = revised.json()
    assert body["item"]["status"] == "pending_review"  # revised, not auto-landed
    assert body["revised_payload"]["metadata"].get("revised_from_feedback") is True

    missing = client.post(
        "/projects/demo/review_items/nope:revise",
        json={"feedback": "x", "operator": "lead"},
    )
    assert missing.status_code == 404


def test_connection_settings_and_loopback_real_gate(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    # the endpoint deliberately re-reads the repo .env (status must match what a real
    # call sees); neutralize it here so the developer's own key can't leak into the test
    monkeypatch.setattr("owcopilot.service.api.load_dotenv", lambda: None)
    assert client.get("/settings/connection").json()["configured"] is False
    # loopback + no provider key: real is refused with setup guidance, not a 403
    blocked = client.post(
        "/projects/demo/jobs",
        json={"kind": "extraction", "params": {"title": "x", "text": "y", "llm_mode": "real"}},
    )
    assert blocked.status_code == 503
    updated = client.post(
        "/settings/connection",
        json={"base_url": "https://api.example.test", "api_key": "sk-test"},
    ).json()
    assert updated["configured"] is True
    assert updated["base_url"] == "https://api.example.test"
    # loopback + key configured: the gate opens (job is accepted; it may fail later
    # against the fake endpoint, which is job state, not an API error)
    accepted = client.post(
        "/projects/demo/jobs",
        json={"kind": "extraction", "params": {"title": "x", "text": "y", "llm_mode": "real"}},
    )
    assert accepted.status_code == 202


def test_theme_sweep_endpoint_returns_work_order(client: TestClient) -> None:
    response = client.post("/projects/demo/sweeps:run", json={"theme": "赌坊"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned_total"] >= 2
    assert any(f["ref"] == "entity:npc_mara" for f in body["hits"])
    assert "专项清查工作单" in body["markdown"]
    assert body["llm_used"] is False


def test_lorebook_download_renders_current_archive(client: TestClient) -> None:
    book = client.get("/projects/demo/lorebook", params={"fmt": "md"})
    assert book.status_code == 200, book.text
    assert "玛拉" in book.text
    assert "attachment" in book.headers["content-disposition"]

    docx = client.get("/projects/demo/lorebook", params={"fmt": "docx"})
    assert docx.status_code == 200
    assert docx.content[:2] == b"PK"  # OOXML is a zip container

    assert client.get("/projects/demo/lorebook", params={"fmt": "pdf"}).status_code == 422


def test_readiness_endpoint_scores_content(client: TestClient) -> None:
    res = client.get("/projects/demo/readiness")
    assert res.status_code == 200, res.text
    report = res.json()["readiness"]
    assert report["standard_version"] == "r1"
    # the two fixture NPCs have no character sheets, so neither is production-ready
    chars = [it for it in report["items"] if it["kind"] == "character"]
    assert chars and all(it["ready"] is False for it in chars)

    incomplete = client.get(
        "/projects/demo/readiness", params={"only_incomplete": True, "kind": "character"}
    )
    assert incomplete.status_code == 200
    items = incomplete.json()["readiness"]["items"]
    assert items and all(it["kind"] == "character" and not it["ready"] for it in items)


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


def test_reference_add_list_search_round_trip(client: TestClient) -> None:
    added = client.post(
        "/projects/demo/references",
        json={"title": "潮汐笔记", "text": "雾潮在满月时短暂退去，露出沉船的桅杆。"},
    )
    assert added.status_code == 201, added.text
    listed = client.get("/projects/demo/references").json()
    assert listed["count"] >= 1
    assert any(s["title"] == "潮汐笔记" for s in listed["sources"])
    searched = client.post("/projects/demo/references:search", json={"query": "雾潮 满月"})
    assert searched.status_code == 200
    assert "hits" in searched.json()


def test_ingest_rejects_bad_base64_and_unparseable(client: TestClient) -> None:
    bad = client.post(
        "/projects/demo/ingest",
        json={"filename": "rows.json", "content_base64": "@@@@notbase64@@@@"},
    )
    assert bad.status_code == 400
    # valid base64 but not a parseable strict-format payload -> 422, not a 500
    garbage = base64.b64encode(b"not a real table file").decode()
    unparseable = client.post(
        "/projects/demo/ingest",
        json={"filename": "rows.json", "content_base64": garbage},
    )
    assert unparseable.status_code == 422
