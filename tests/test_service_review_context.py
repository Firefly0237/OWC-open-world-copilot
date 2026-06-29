"""Tests for IN-4: GET /projects/{project}/review_items/{item_id}:context endpoint.

Covers:
- All required fields present
- false_pass_rate null when sample < 20
- 404 behavior consistent with :decide endpoint
- No side effects (GET is idempotent)
- Non-quest_draft items return object_ref as summary
- refine_trail reflection extracted correctly
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("OWCOPILOT_ALLOW_OFFLINE_LLM", "1")

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import ContentBundle  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


def _setup_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    """Create project dir, register via env var, return (client, content_root)."""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    projects = json.dumps({"demo": str(root).replace("\\", "/")})
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", projects)
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    client = TestClient(create_app())
    return client, root


def _insert_review_item(
    root: Path,
    *,
    item_id: str,
    item_type: str,
    object_ref: str,
    payload: dict,
    status: str = "pending_review",
    critic_verdict: str | None = None,
    critic_score: float | None = None,
) -> None:
    """Insert a review item directly into the project's SQLite store."""
    from owcopilot.storage.sqlite import SQLiteStore

    db_path = root / ".owcopilot" / "runtime.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_path)
    store.save_review_item({
        "id": item_id,
        "item_type": item_type,
        "object_ref": object_ref,
        "payload": payload,
        "issue_refs": ["issue_1"],
        "status": status,
        "critic_verdict": critic_verdict,
        "critic_score": critic_score,
    })


def test_context_endpoint_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[硬] All required fields present in response."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_001",
        item_type="quest_draft",
        object_ref="quest_001",
        payload={
            "title": "Test Quest",
            "objective": "Do the thing",
            "stages": [
                {"id": "s1", "summary": "First stage scene with action"},
                {"id": "s2", "summary": "Second stage scene with outcome"},
            ],
            "refine_trail": [
                {"verdict": "revise", "score": 0.6, "reflection": "needs more drama"},
            ],
        },
        critic_verdict="pass",
        critic_score=0.85,
    )
    resp = client.get("/projects/demo/review_items/item_001:context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["item_id"] == "item_001"
    assert data["item_type"] == "quest_draft"
    assert data["status"] == "pending_review"
    assert "payload_summary" in data
    assert "issue_refs" in data
    assert "critic_verdict" in data
    assert "critic_score" in data
    assert "refine_trail_last_reflection" in data
    assert "calibration_context" in data


def test_quest_draft_payload_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """quest_draft payload_summary extracts title, objective, stages (max 2)."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_002",
        item_type="quest_draft",
        object_ref="quest_002",
        payload={
            "title": "The Dragon Quest",
            "objective": "Slay the dragon",
            "stages": [
                {"id": "s1", "summary": "Find the cave where the dragon lives"},
                {"id": "s2", "summary": "Enter the cave and face the dragon"},
                {"id": "s3", "summary": "This third stage should NOT appear"},
            ],
        },
    )
    resp = client.get("/projects/demo/review_items/item_002:context")
    assert resp.status_code == 200
    data = resp.json()
    ps = data["payload_summary"]
    assert ps["title"] == "The Dragon Quest"
    assert ps["objective"] == "Slay the dragon"
    assert len(ps["stages"]) == 2  # at most 2 stages
    assert ps["stages"][0]["id"] == "s1"
    assert ps["stages"][1]["id"] == "s2"


def test_description_truncated_to_100_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage description is truncated to 100 characters."""
    client, root = _setup_project(tmp_path, monkeypatch)
    long_desc = "A" * 200
    _insert_review_item(
        root,
        item_id="item_003",
        item_type="quest_draft",
        object_ref="quest_003",
        payload={
            "stages": [{"id": "s1", "summary": long_desc}],
        },
    )
    resp = client.get("/projects/demo/review_items/item_003:context")
    assert resp.status_code == 200
    stage = resp.json()["payload_summary"]["stages"][0]
    assert len(stage["description"]) <= 100


def test_false_pass_rate_null_when_insufficient_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[硬] calibration sample < 20 -> false_pass_rate is null, sufficient_sample is false."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_004",
        item_type="quest_draft",
        object_ref="quest_004",
        payload={},
    )
    resp = client.get("/projects/demo/review_items/item_004:context")
    assert resp.status_code == 200
    cal = resp.json()["calibration_context"]
    assert cal["sufficient_sample"] is False
    assert cal["false_pass_rate"] is None


def test_404_consistent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[硬] Non-existent item_id -> 404 with same detail format as :decide endpoint."""
    client, _ = _setup_project(tmp_path, monkeypatch)
    resp = client.get("/projects/demo/review_items/nonexistent_id:context")
    assert resp.status_code == 404
    assert "review item not found" in resp.json()["detail"].lower()


def test_endpoint_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[硬] Multiple calls to :context do not change DB state."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_005",
        item_type="quest_draft",
        object_ref="quest_005",
        payload={"title": "Idempotent Quest"},
    )
    resp1 = client.get("/projects/demo/review_items/item_005:context")
    resp2 = client.get("/projects/demo/review_items/item_005:context")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()


def test_refine_trail_last_reflection_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """refine_trail_last_reflection returns last step's reflection."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_006",
        item_type="quest_draft",
        object_ref="quest_006",
        payload={
            "refine_trail": [
                {"round": 0, "verdict": "revise", "reflection": "first try"},
                {"round": 1, "verdict": "pass", "reflection": "much better now"},
            ],
        },
    )
    resp = client.get("/projects/demo/review_items/item_006:context")
    assert resp.status_code == 200
    assert resp.json()["refine_trail_last_reflection"] == "much better now"


def test_refine_trail_empty_returns_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty refine_trail -> refine_trail_last_reflection is null."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_007",
        item_type="quest_draft",
        object_ref="quest_007",
        payload={},  # no refine_trail
    )
    resp = client.get("/projects/demo/review_items/item_007:context")
    assert resp.status_code == 200
    assert resp.json()["refine_trail_last_reflection"] is None


def test_non_quest_draft_item_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-quest_draft items return object_ref as summary."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_008",
        item_type="bark_variant",
        object_ref="npc_guard_001",
        payload={"variants": ["Hello!", "Hey there!"]},
    )
    resp = client.get("/projects/demo/review_items/item_008:context")
    assert resp.status_code == 200
    ps = resp.json()["payload_summary"]
    assert ps["summary"] == "npc_guard_001"
    assert ps["title"] is None
    assert ps["stages"] == []


def test_issue_refs_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_refs list is returned correctly."""
    client, root = _setup_project(tmp_path, monkeypatch)
    _insert_review_item(
        root,
        item_id="item_009",
        item_type="quest_draft",
        object_ref="quest_009",
        payload={},
    )
    resp = client.get("/projects/demo/review_items/item_009:context")
    assert resp.status_code == 200
    assert "issue_1" in resp.json()["issue_refs"]
