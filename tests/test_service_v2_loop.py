"""REST tests for the v2 close-the-loop endpoints (registered-project mode, offline, $0)."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    Relation,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="Mara", type=EntityType.NPC, description="Scout."
                ),
                "loc_fort": Entity(
                    id="loc_fort",
                    name="Border Fort",
                    type=EntityType.LOCATION,
                    description="A fort.",
                ),
            },
            relations=[Relation(source="npc_mara", target="loc_fort", kind="located_in")],
            quests={
                "quest_patrol": Quest(
                    id="quest_patrol",
                    title="Patrol the Border",
                    giver_npc="npc_ghost",  # seeded error
                    location="loc_fort",
                    objective="Walk the border line.",
                    localization_keys=["quest.quest_patrol.objective"],
                )
            },
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app())


def test_impact_endpoint(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/impact:analyze",
        json={"changes": [{"change_type": "entity_delete", "target_ref": "entity:loc_fort"}]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    refs = {item["target_ref"] for item in payload["must_change"]}
    assert refs, "expected direct-impact targets"


def test_impact_unknown_change_type_is_422(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/impact:analyze",
        json={"changes": [{"change_type": "nuke", "target_ref": "entity:loc_fort"}]},
    )
    assert response.status_code == 422


def test_suggest_apply_rollback_over_rest(client: TestClient) -> None:
    audit = client.post("/projects/demo/audits", json={"persist": True})
    assert audit.status_code == 200
    issues = client.get("/projects/demo/issues", params={"rule_code": "UNKNOWN_ENTITY_REF"}).json()[
        "issues"
    ]
    assert issues
    issue_id = issues[0]["id"]

    suggest = client.post(f"/projects/demo/issues/{issue_id}/suggestions", json={})
    assert suggest.status_code == 200
    body = suggest.json()
    assert body["used_llm"] is False
    assert body["candidates"]
    patch_id = body["candidates"][0]["patch_id"]

    applied = client.post(f"/projects/demo/patches/{patch_id}:apply", json={"operator": "lead"})
    assert applied.status_code == 200
    assert applied.json()["applied"] is True
    assert applied.json()["post_audit_open_errors"] == 0

    # second apply conflicts (status already moved on)
    again = client.post(f"/projects/demo/patches/{patch_id}:apply", json={"operator": "lead"})
    assert again.status_code == 409

    rolled = client.post(f"/projects/demo/patches/{patch_id}:rollback", json={"operator": "lead"})
    assert rolled.status_code == 200
    assert rolled.json()["rolled_back"] is True


def test_suggest_unknown_issue_is_404(client: TestClient) -> None:
    response = client.post("/projects/demo/issues/nope/suggestions", json={})
    assert response.status_code == 404


def test_draft_endpoint_queues_pending_review(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/contents/quests:draft",
        json={"brief": "Escort the salt caravan to the fort"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["quest"]["origin"] == "ai_draft"
    assert payload["quest"]["review_status"] == "pending_review"
    assert payload["review_item_id"]


def test_barks_endpoint(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/assist/barks:batch",
        json={
            "speaker_ids": ["npc_mara"],
            "topic": "spotted an intruder",
            "variants_per_speaker": 2,
            "max_chars": 60,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["accepted"]) == 2
    assert len(payload["review_item_ids"]) == 2


def test_barks_unknown_speaker_is_422(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/assist/barks:batch",
        json={"speaker_ids": ["npc_nobody"], "topic": "hello"},
    )
    assert response.status_code == 422


def test_export_endpoint_writes_under_project_runtime(client: TestClient, tmp_path) -> None:
    audit = client.post("/projects/demo/audits", json={"persist": True})
    assert audit.status_code == 200
    issues = client.get("/projects/demo/issues", params={"rule_code": "UNKNOWN_ENTITY_REF"}).json()[
        "issues"
    ]
    suggest = client.post(f"/projects/demo/issues/{issues[0]['id']}/suggestions", json={})
    patch_id = suggest.json()["candidates"][0]["patch_id"]
    applied = client.post(f"/projects/demo/patches/{patch_id}:apply", json={"operator": "lead"})
    assert applied.status_code == 200

    response = client.post("/projects/demo/exports", json={"target_engine": "generic"})
    assert response.status_code == 200
    payload = response.json()
    output_dir = payload["output_dir"]
    assert ".owcopilot" in output_dir and output_dir.endswith("generic")
    manifest = payload["manifest"]
    assert manifest["files"], "manifest lists exported files"


def test_unregistered_project_is_404(client: TestClient) -> None:
    response = client.post(
        "/projects/ghost/impact:analyze",
        json={"changes": [{"change_type": "entity_delete", "target_ref": "entity:x"}]},
    )
    assert response.status_code == 404
