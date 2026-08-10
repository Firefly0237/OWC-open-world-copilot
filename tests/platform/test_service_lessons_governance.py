"""FastAPI governance contract for calibration-derived prompt lessons."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue  # noqa: E402
from owcopilot.content.models import ContentBundle  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.storage.sqlite import SQLiteStore  # noqa: E402


def test_calibration_get_is_read_only_and_lessons_are_explicitly_governed(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    runtime = root / ".owcopilot" / "runtime.sqlite"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(runtime)
    try:
        queue = ReviewQueue(store)
        for index in range(3):
            item = queue.add(
                ReviewItem(
                    item_type=ReviewItemType.QUEST_DRAFT,
                    object_ref=f"quest:false-pass-{index}",
                    payload={"id": f"false-pass-{index}"},
                    status="pending_review",
                    critic_verdict="pass",
                    critic_score=0.9,
                    critic_primary_dim="completeness",
                )
            )
            queue.mark(item.id, "rejected", decided_by="editor")
    finally:
        store.close()

    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    client = TestClient(create_app())

    before = client.get("/projects/demo/lessons")
    assert before.status_code == 200
    assert before.json()["count"] == 0

    calibration = client.get("/projects/demo/review/calibration")
    assert calibration.status_code == 200
    assert len(calibration.json()["false_pass_items"]) == 3
    assert calibration.json()["lessons_written"] == 0
    assert client.get("/projects/demo/lessons").json()["count"] == 0

    learned = client.post("/projects/demo/review/calibration:learn", json={})
    assert learned.status_code == 200
    assert learned.json()["lessons_written"] == 1

    active = client.get("/projects/demo/lessons").json()
    assert active["count"] == 1
    assert active["lessons"][0]["false_pass_count"] == 3
    lesson_id = active["lessons"][0]["id"]

    learned_again = client.post("/projects/demo/review/calibration:learn", json={})
    assert learned_again.status_code == 200
    repeated = client.get("/projects/demo/lessons").json()["lessons"][0]
    assert repeated["false_pass_count"] == 3

    revoked = client.post(f"/projects/demo/lessons/{lesson_id}:revoke", json={"reason": "错误经验"})
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert client.get("/projects/demo/lessons").json()["count"] == 0

    archive = client.get("/projects/demo/lessons", params={"include_revoked": True}).json()
    assert archive["count"] == 1
    assert archive["lessons"][0]["revoked_at"]
    assert archive["lessons"][0]["revoked_reason"] == "错误经验"


def test_lessons_keep_legacy_shape_and_offer_snapshot_cursor_pages(tmp_path, monkeypatch) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    runtime = root / ".owcopilot" / "runtime.sqlite"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(runtime)
    try:
        for index in range(5):
            store.save_lesson(
                f"artifact-{index}",
                f"第 {index} 条经验",
                dimension=f"dimension-{index}",
            )
    finally:
        store.close()

    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    client = TestClient(create_app())

    legacy = client.get("/projects/demo/lessons")
    assert legacy.status_code == 200
    assert legacy.json()["count"] == 5
    assert len(legacy.json()["lessons"]) == 5
    assert "next_cursor" not in legacy.json()
    assert "snapshot_etag" not in legacy.json()

    first = client.get("/projects/demo/lessons", params={"limit": 2})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["count"] == 5
    assert first_body["total"] == 5
    assert len(first_body["lessons"]) == 2
    assert first_body["next_cursor"]
    assert first_body["snapshot_etag"]

    second = client.get(
        "/projects/demo/lessons",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["lessons"]) == 2
    assert second.json()["total"] == 5
    assert second.json()["snapshot_etag"] == first_body["snapshot_etag"]
    assert {lesson["id"] for lesson in first_body["lessons"]}.isdisjoint(
        {lesson["id"] for lesson in second.json()["lessons"]}
    )

    invalid = client.get("/projects/demo/lessons", params={"limit": 2, "cursor": "not-a-cursor"})
    assert invalid.status_code == 422

    changed = SQLiteStore(runtime)
    try:
        changed.save_lesson("new-artifact", "翻页期间新经验", dimension="new-dimension")
    finally:
        changed.close()
    stale = client.get(
        "/projects/demo/lessons",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert stale.status_code == 409
