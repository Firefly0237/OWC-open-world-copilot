from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    DialogueNode,
    DialogueTree,
    LocalizedText,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.inspiration.store import ReferenceStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.service.pagination import PaginationCursorError, paginate  # noqa: E402


def test_cursor_page_reaches_every_item_and_detects_a_stale_snapshot() -> None:
    items = [{"id": f"issue-{index:03d}"} for index in range(205)]

    first = paginate(items, limit=100)
    second = paginate(items, limit=100, cursor=first.next_cursor)
    third = paginate(items, limit=100, cursor=second.next_cursor)

    observed = [*first.items, *second.items, *third.items]
    assert [item["id"] for item in observed] == [item["id"] for item in items]
    assert first.total == 205
    assert third.next_cursor is None
    assert first.snapshot_etag == second.snapshot_etag == third.snapshot_etag

    changed = [*items, {"id": "issue-new"}]
    with pytest.raises(PaginationCursorError, match="发生变化") as caught:
        paginate(changed, limit=100, cursor=first.next_cursor)
    assert caught.value.stale is True


def test_issue_rest_pagination_is_bounded_filter_stable_and_opaque(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_PROJECTS_JSON", raising=False)
    app = create_app()
    app.state.v2_issues["demo"] = [
        {
            "id": f"issue-{index:03d}",
            "severity": "error" if index % 2 else "warn",
            "rule_code": "PAGED",
            "status": "open",
        }
        for index in range(75)
    ]
    client = TestClient(app)

    first = client.get("/projects/demo/issues?severity=error&limit=20")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 37
    assert len(body["issues"]) == 20
    assert body["next_cursor"]
    assert body["snapshot_etag"]
    assert "issue-001" in {issue["id"] for issue in body["issues"]}

    second = client.get(
        "/projects/demo/issues",
        params={"severity": "error", "limit": 20, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert len(second_body["issues"]) == 17
    assert second_body["next_cursor"] is None
    assert second_body["snapshot_etag"] == body["snapshot_etag"]

    invalid = client.get("/projects/demo/issues?cursor=not-a-real-cursor")
    assert invalid.status_code == 422

    app.state.v2_issues["demo"].append(
        {"id": "issue-new", "severity": "error", "rule_code": "PAGED", "status": "open"}
    )
    stale = client.get(
        "/projects/demo/issues",
        params={"severity": "error", "limit": 20, "cursor": body["next_cursor"]},
    )
    assert stale.status_code == 409


def test_large_editor_collections_use_cursor_pages_and_keep_legacy_shapes(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "content"
    bundle = ContentBundle(
        localized_texts={
            f"loc-{index:03d}": LocalizedText(
                id=f"loc-{index:03d}",
                text_key=f"quest.key.{index:03d}",
                locale="zh-CN",
                text=f"文案 {index}",
            )
            for index in range(65)
        },
        dialogue_trees={
            f"tree-{index:03d}": DialogueTree(
                id=f"tree-{index:03d}",
                title=f"对话树 {index}",
                root_node="root",
                nodes={"root": DialogueNode(id="root", text=f"台词 {index}")},
            )
            for index in range(65)
        },
    )
    ContentStore(root).save(bundle)

    references = ReferenceStore(root)
    for index in range(65):
        references.add_text(title=f"参考 {index:03d}", text=f"素材正文 {index}")

    snapshot_dir = root / ".snapshots"
    snapshot_dir.mkdir(parents=True)
    for index in range(65):
        snapshot_id = f"20260808_120000_{index:06d}"
        (snapshot_dir / f"{snapshot_id}.json").write_text(
            json.dumps(
                {
                    "id": snapshot_id,
                    "label": f"快照 {index}",
                    "created_at": f"2026-08-08T12:00:{index % 60:02d}+00:00",
                    "content_hash": f"hash-{index}",
                    "bundle": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    client = TestClient(create_app())

    legacy = {
        "references": client.get("/projects/demo/references").json(),
        "localization": client.get("/projects/demo/localization").json(),
        "dialogues": client.get("/projects/demo/dialogue_trees").json(),
        "snapshots": client.get("/projects/demo/snapshots").json(),
    }
    assert len(legacy["references"]["sources"]) == 65
    assert len(legacy["localization"]["overview"]["rows"]) == 65
    assert len(legacy["dialogues"]["dialogues"]["trees"]) == 65
    assert len(legacy["snapshots"]["snapshots"]) == 65
    assert all("next_cursor" not in body for body in legacy.values())

    collections = [
        ("/projects/demo/references", lambda body: body["sources"], {}),
        (
            "/projects/demo/localization",
            lambda body: body["overview"]["rows"],
            {"focus": "quest.key.060"},
        ),
        (
            "/projects/demo/dialogue_trees",
            lambda body: body["dialogues"]["trees"],
            {"focus": "tree-060"},
        ),
        ("/projects/demo/snapshots", lambda body: body["snapshots"], {}),
    ]
    for path, select, extra_params in collections:
        cursor = None
        observed: list[dict] = []
        etag = None
        while True:
            params = {"limit": 20, **extra_params}
            if cursor:
                params["cursor"] = cursor
            response = client.get(path, params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 65
            assert len(select(body)) <= 20
            observed.extend(select(body))
            etag = etag or body["snapshot_etag"]
            assert body["snapshot_etag"] == etag
            cursor = body["next_cursor"]
            if not cursor:
                break
        assert len(observed) == 65

    localized_first = client.get(
        "/projects/demo/localization", params={"limit": 20, "focus": "quest.key.060"}
    ).json()
    dialogue_first = client.get(
        "/projects/demo/dialogue_trees", params={"limit": 20, "focus": "tree-060"}
    ).json()
    assert localized_first["overview"]["rows"][0]["text_key"] == "quest.key.060"
    assert dialogue_first["dialogues"]["trees"][0]["id"] == "tree-060"

    invalid = client.get("/projects/demo/snapshots", params={"limit": 20, "cursor": "bad"})
    assert invalid.status_code == 422

    reference_first = client.get("/projects/demo/references", params={"limit": 20}).json()
    references.add_text(title="参考新增", text="游标之后新增的素材")
    stale = client.get(
        "/projects/demo/references",
        params={"limit": 20, "cursor": reference_first["next_cursor"]},
    )
    assert stale.status_code == 409


def test_localization_assignment_round_trips_through_legacy_and_paged_overviews(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            localized_texts={
                "greet-en": LocalizedText(
                    id="greet-en", text_key="ui.greet", locale="en", text="Hello"
                )
            }
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    client = TestClient(create_app())

    assigned = client.post(
        "/projects/demo/localization:assign",
        json={"text_key": "ui.greet", "locale": "en", "assignee": "bob"},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["entry"]["assignee"] == "bob"

    legacy = client.get("/projects/demo/localization")
    assert legacy.status_code == 200, legacy.text
    legacy_body = legacy.json()
    legacy_row = legacy_body["overview"]["rows"][0]
    assert "en" in legacy_row["status"]  # the pre-existing per-locale status contract stays intact
    assert legacy_row["assignee"]["en"] == "bob"
    assert "next_cursor" not in legacy_body

    paged = client.get(
        "/projects/demo/localization",
        params={"limit": 1, "focus": "ui.greet"},
    )
    assert paged.status_code == 200, paged.text
    assert paged.json()["overview"]["rows"][0]["assignee"]["en"] == "bob"

    cleared = client.post(
        "/projects/demo/localization:assign",
        json={"text_key": "ui.greet", "locale": "en", "assignee": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["entry"]["assignee"] == ""
    refreshed = client.get("/projects/demo/localization", params={"limit": 1})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["overview"]["rows"][0]["assignee"]["en"] == ""
