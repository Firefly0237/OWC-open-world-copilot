from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_x": Entity(
                    id="npc_x",
                    name="老周",
                    type=EntityType.NPC,
                    description="他在巷子里组织赌博",
                )
            },
            quests={
                "quest_x": Quest(
                    id="quest_x",
                    title="巡逻",
                    giver_npc="npc_missing",
                    objective="巡逻街巷",
                )
            },
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({"demo": str(root).replace("\\", "/")}),
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app())


def test_audit_and_compliance_mutations_offer_bounded_summary_responses(client: TestClient) -> None:
    full_audit = client.post("/projects/demo/audits", json={"persist": True})
    compact_audit = client.post(
        "/projects/demo/audits",
        params={"include_details": False},
        json={"persist": True},
    )
    assert full_audit.status_code == compact_audit.status_code == 200
    assert full_audit.json()["issues"]
    assert compact_audit.json()["issues"] == []
    assert compact_audit.json()["totals"] == full_audit.json()["totals"]

    full_scan = client.post("/projects/demo/compliance:scan", json={})
    compact_scan = client.post(
        "/projects/demo/compliance:scan",
        params={"include_details": False},
        json={},
    )
    assert full_scan.status_code == compact_scan.status_code == 200
    assert full_scan.json()["report"]["cases"]
    assert compact_scan.json()["report"]["cases"] == []
    assert compact_scan.json()["report"]["total"] == full_scan.json()["report"]["total"]


def test_mapping_mutations_can_skip_the_growing_template_dictionary(client: TestClient) -> None:
    mapping = {"id_column": "id", "name_column": "name"}
    full = client.post(
        "/projects/demo/recognize/mappings",
        json={"name": "full", "mapping": mapping},
    )
    compact = client.post(
        "/projects/demo/recognize/mappings",
        params={"include_details": False},
        json={"name": "compact", "mapping": mapping},
    )
    assert full.status_code == compact.status_code == 200
    assert "templates" in full.json()
    assert compact.json() == {"project": "demo", "saved": "compact"}

    deleted = client.delete(
        "/projects/demo/recognize/mappings/compact",
        params={"include_details": False},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"project": "demo", "deleted": "compact"}
