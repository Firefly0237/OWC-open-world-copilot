"""REST tests for Phase 3: WS-I asset linking, generic export, WS-K engine back-sync."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={"npc_x": Entity(id="npc_x", name="甲", type=EntityType.NPC)},
            quests={"q_edit": Quest(id="q_edit", title="将改", objective="旧目标")},
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setattr("owcopilot.service.api.load_dotenv", lambda: None)
    return TestClient(create_app())


def test_asset_attach_list_detach_round_trip(client: TestClient) -> None:
    attach = client.post(
        "/projects/demo/assets:attach",
        json={"object_ref": "entity:npc_x", "kind": "image", "uri": "art/x.png", "title": "立绘"},
    )
    assert attach.status_code == 201, attach.text
    asset_id = attach.json()["asset"]["id"]

    listed = client.get("/projects/demo/assets", params={"object_ref": "entity:npc_x"})
    assert listed.status_code == 200
    assert listed.json()["assets"][0]["uri"] == "art/x.png"

    detach = client.post("/projects/demo/assets:detach", params={"asset_id": asset_id})
    assert detach.status_code == 200 and detach.json()["removed"] is True
    assert (
        client.get("/projects/demo/assets", params={"object_ref": "entity:npc_x"}).json()["assets"]
        == []
    )


def test_asset_attach_rejects_empty_uri(client: TestClient) -> None:
    # pydantic min_length=1 means a blank uri is a 422 before it ever reaches the action
    resp = client.post(
        "/projects/demo/assets:attach",
        json={"object_ref": "entity:npc_x", "kind": "link", "uri": ""},
    )
    assert resp.status_code == 422


def test_generic_export_leaves_canon_untouched(client: TestClient, tmp_path) -> None:
    # a quest with logic so a script is actually emitted
    root = tmp_path / "content"
    store = ContentStore(root)
    bundle = store.load()
    resp = client.post("/projects/demo/exports", json={"target_engine": "generic"})
    assert resp.status_code == 200, resp.text
    kinds = {f["kind"] for f in resp.json()["manifest"]["files"]}
    assert "content_bundle" in kinds  # export ran without error
    assert bundle.quests  # canon untouched by export


def test_engine_import_queues_changes_for_review(client: TestClient) -> None:
    resp = client.post(
        "/projects/demo/engine:import",
        json={"quests": [{"id": "q_edit", "title": "将改", "objective": "引擎侧改过的目标"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] == ["q_edit"]
    assert body["review_item_id"]  # HITL: queued, not auto-landed


def test_import_recognize_table_dry_run_and_apply(client: TestClient) -> None:
    content = "id,name,type,home\nnpc_a,Aldric,npc,loc_keep\nloc_keep,Keep,location,\n"
    dry = client.post(
        "/projects/demo/import:recognize",
        json={"source_format": "table", "content": content, "filename": "cast.csv"},
    )
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert set(body["new"]) == {"npc_a", "loc_keep"}
    assert body["applied"] is False

    applied = client.post(
        "/projects/demo/import:recognize",
        json={
            "source_format": "table", "content": content, "filename": "cast.csv",
            "field_mapping": {"id_column": "id", "name_column": "name", "type_column": "type",
                              "relation_columns": {"home": "resides_in"}},
            "apply": True,
        },
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["applied"] is True and body["review_item_id"]
    assert "audit_preview" in body


def test_import_recognize_rejects_bad_format(client: TestClient) -> None:
    resp = client.post(
        "/projects/demo/import:recognize",
        json={"source_format": "rpgmaker", "content": "x"},
    )
    assert resp.status_code == 422  # pattern guard rejects before the action runs


def test_import_recognize_base64_upload_and_auto_sniff(client: TestClient) -> None:
    import base64

    b64 = base64.b64encode(b"id,name,type\nnpc_new,New,npc\n").decode()
    resp = client.post(
        "/projects/demo/import:recognize",
        json={"source_format": "auto", "content_base64": b64, "filename": "cast.csv"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_format"] == "table"
    assert "npc_new" in body["new"]


def test_import_apply_edited_plan(client: TestClient) -> None:
    rec = client.post(
        "/projects/demo/import:recognize",
        json={"source_format": "table", "content": "id,name,type\nnpc_p,P,npc\n"},
    ).json()
    resp = client.post("/projects/demo/import:apply", json={"plan": rec["plan"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True and body["review_item_id"]


def test_recognize_mapping_templates_crud(client: TestClient) -> None:
    save = client.post(
        "/projects/demo/recognize/mappings",
        json={"name": "t1", "mapping": {"id_column": "id", "name_column": "name"}},
    )
    assert save.status_code == 200, save.text
    assert "t1" in client.get("/projects/demo/recognize/mappings").json()["templates"]

    assert client.delete("/projects/demo/recognize/mappings/t1").status_code == 200
    assert "t1" not in client.get("/projects/demo/recognize/mappings").json()["templates"]
