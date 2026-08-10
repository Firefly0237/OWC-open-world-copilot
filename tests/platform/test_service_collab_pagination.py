from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.collab import (  # noqa: E402
    CollabState,
    CollabStore,
    Comment,
    acquire_lock,
    assign,
)
from owcopilot.content.models import ContentBundle  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


def _client_with_long_thread(tmp_path, monkeypatch) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    object_ref = "quest:long-thread"
    state = CollabState(
        comments={
            object_ref: [
                Comment(
                    id=f"comment-{index:03d}",
                    object_ref=object_ref,
                    author=f"author-{index % 3}",
                    body=f"comment {index:03d}",
                    at=f"2026-08-08T12:{index:02d}:00+00:00",
                )
                for index in range(45)
            ]
        }
    )
    assign(state, object_ref=object_ref, assignee="bob", by="alice", note="follow up")
    acquire_lock(state, object_ref=object_ref, holder="alice")
    CollabStore(root).save(state)

    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app())


def test_collab_comments_keep_legacy_shape_and_offer_object_scoped_cursor_pages(
    tmp_path, monkeypatch
) -> None:
    client = _client_with_long_thread(tmp_path, monkeypatch)
    path = "/projects/demo/collab"
    object_ref = "quest:long-thread"

    legacy = client.get(path, params={"object_ref": object_ref})
    assert legacy.status_code == 200, legacy.text
    legacy_body = legacy.json()
    assert set(legacy_body) == {"project", "object_ref", "assignment", "comments", "lock"}
    assert len(legacy_body["comments"]) == 45
    assert legacy_body["comments"][0]["body"] == "comment 000"

    first = client.get(path, params={"object_ref": object_ref, "limit": 20})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["assignment"]["assignee"] == "bob"
    assert first_body["lock"]["holder"] == "alice"
    assert first_body["total"] == 45
    assert len(first_body["comments"]) == 20
    assert first_body["comments"][0]["body"] == "comment 044"
    assert first_body["comments"][-1]["body"] == "comment 025"
    assert first_body["next_cursor"]
    assert first_body["snapshot_etag"]

    observed = list(first_body["comments"])
    cursor = first_body["next_cursor"]
    while cursor:
        response = client.get(
            path,
            params={"object_ref": object_ref, "limit": 20, "cursor": cursor},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["snapshot_etag"] == first_body["snapshot_etag"]
        assert body["assignment"]["assignee"] == "bob"
        assert body["lock"]["holder"] == "alice"
        observed.extend(body["comments"])
        cursor = body["next_cursor"]
    assert len(observed) == 45
    assert len({comment["id"] for comment in observed}) == 45
    assert observed[-1]["body"] == "comment 000"

    invalid = client.get(path, params={"object_ref": object_ref, "limit": 20, "cursor": "bad"})
    assert invalid.status_code == 422
    missing_scope = client.get(path, params={"limit": 20})
    assert missing_scope.status_code == 422


def test_collab_cursor_turns_stale_after_a_new_comment(tmp_path, monkeypatch) -> None:
    client = _client_with_long_thread(tmp_path, monkeypatch)
    path = "/projects/demo/collab"
    object_ref = "quest:long-thread"
    first = client.get(path, params={"object_ref": object_ref, "limit": 20}).json()

    posted = client.post(
        f"{path}/comments",
        json={"object_ref": object_ref, "author": "carol", "body": "brand new"},
    )
    assert posted.status_code == 201, posted.text

    stale = client.get(
        path,
        params={"object_ref": object_ref, "limit": 20, "cursor": first["next_cursor"]},
    )
    assert stale.status_code == 409

    refreshed = client.get(path, params={"object_ref": object_ref, "limit": 20})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["comments"][0]["body"] == "brand new"
    assert refreshed.json()["total"] == 46
