"""Cursor pagination contract for the canonical archive endpoint (offline, $0)."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    DialogueNode,
    DialogueRef,
    DialogueTree,
    Entity,
    EntityType,
    QuestEventReference,
    Relation,
    StyleGuide,
    StyleRubric,
    StyleRubricElement,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def paged_archive(tmp_path, monkeypatch) -> tuple[TestClient, ContentStore, ContentBundle]:
    root = tmp_path / "content"
    bundle = ContentBundle(
        entities={
            f"npc_{index:03d}": Entity(
                id=f"npc_{index:03d}",
                name=f"守灯人 {index:03d}",
                type=EntityType.NPC,
                description="archive pagination fixture",
            )
            for index in range(45)
        },
        dialogues={
            "line_005": DialogueRef(id="line_005", text_key="line.key.005", text="第五句"),
            "line_other": DialogueRef(id="line_other", text_key="line.key.other", text="别处"),
        },
        dialogue_trees={
            "tree_005": DialogueTree(
                id="tree_005",
                root_node="node_005",
                nodes={
                    "node_005": DialogueNode(id="node_005", text_key="line.key.005", text="第五句")
                },
            ),
            "tree_other": DialogueTree(
                id="tree_other",
                root_node="node_other",
                nodes={
                    "node_other": DialogueNode(
                        id="node_other", text_key="line.key.other", text="别处"
                    )
                },
            ),
        },
        relations=[Relation(source="npc_000", kind="knows", target="npc_001", valid_from=3)],
        style_guides={
            "guide_archive": StyleGuide(
                id="guide_archive",
                body="档案语气应当克制。",
                rules=["不要网络梗"],
            )
        },
        quest_event_refs={
            "qer_archive": QuestEventReference(
                id="qer_archive",
                quest_id="quest_archive",
                event_id="event_archive",
                note="Archive event reference",
            )
        },
        style_rubrics={
            "rubric_archive": StyleRubric(
                id="rubric_archive",
                label="档案风格量表",
                abstract_tag="克制",
                elements=[StyleRubricElement(id="concise", text="叙述是否简洁")],
            )
        },
    )
    store = ContentStore(root)
    store.save(bundle)
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    return TestClient(create_app()), store, bundle


def test_archive_keeps_legacy_shape_without_pagination_params(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
) -> None:
    client, _, _ = paged_archive
    response = client.get("/projects/demo/archive")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"project", "inventory"}
    assert len(body["inventory"]["entities"]) == 45
    assert set(body["inventory"]["dialogue_tree_payloads"]) == {"tree_005", "tree_other"}


def test_archive_cursor_pages_filter_and_focus_page(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
) -> None:
    client, _, _ = paged_archive
    first = client.get(
        "/projects/demo/archive", params={"kind": "entity", "q": "守灯人", "limit": 20}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 45
    assert body["counts"]["entity"] == 45
    assert body["offset"] == 0
    assert body["page_cursor"] is None
    assert body["previous_cursor"] is None
    assert len(body["items"]) == 20
    assert body["dialogue_tree_payloads"] == {}

    second = client.get(
        "/projects/demo/archive",
        params={
            "kind": "entity",
            "q": "守灯人",
            "limit": 20,
            "cursor": body["next_cursor"],
        },
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["offset"] == 20
    assert second_body["page_cursor"]
    assert second_body["previous_cursor"]
    assert second_body["items"][0]["id"] == "npc_020"

    previous = client.get(
        "/projects/demo/archive",
        params={
            "kind": "entity",
            "q": "守灯人",
            "limit": 20,
            "cursor": second_body["previous_cursor"],
        },
    )
    assert previous.status_code == 200, previous.text
    previous_body = previous.json()
    assert previous_body["offset"] == 0
    assert previous_body["previous_cursor"] is None
    assert previous_body["items"] == body["items"]

    focused = client.get(
        "/projects/demo/archive",
        params={"kind": "entity", "focus": "entity:npc_037", "limit": 20},
    )
    assert focused.status_code == 200, focused.text
    focused_body = focused.json()
    assert focused_body["offset"] == 20
    assert focused_body["page_cursor"]
    assert focused_body["previous_cursor"]
    assert "npc_037" in {item["id"] for item in focused_body["items"]}


def test_archive_limits_dialogue_payloads_to_current_dialogue_page(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
) -> None:
    client, _, _ = paged_archive
    dialogue = client.get(
        "/projects/demo/archive",
        params={"kind": "dialogue", "q": "line.key.005", "limit": 20},
    )
    assert dialogue.status_code == 200, dialogue.text
    assert [row["id"] for row in dialogue.json()["items"]] == ["line_005"]
    assert set(dialogue.json()["dialogue_tree_payloads"]) == {"tree_005"}

    trees = client.get("/projects/demo/archive", params={"kind": "dialogue_tree", "limit": 1})
    assert trees.status_code == 200, trees.text
    tree_ids = {row["id"] for row in trees.json()["items"]}
    assert set(trees.json()["dialogue_tree_payloads"]) == tree_ids


@pytest.mark.parametrize(
    ("kind", "focus", "field", "expected"),
    [
        ("relation", "relation:npc_000:knows:npc_001", "kind", "knows"),
        ("style_guide", "style_guide:guide_archive", "id", "guide_archive"),
        (
            "quest_event_ref",
            "quest_event_ref:qer_archive",
            "id",
            "qer_archive",
        ),
        ("style_rubric", "style_rubric:rubric_archive", "id", "rubric_archive"),
    ],
)
def test_archive_pages_every_remaining_canon_type_and_resolves_focus(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
    kind: str,
    focus: str,
    field: str,
    expected: str,
) -> None:
    client, _, _ = paged_archive
    response = client.get(
        "/projects/demo/archive",
        params={"kind": kind, "focus": focus, "limit": 1},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == kind
    assert body["total"] == 1
    assert body["counts"][kind] == 1
    assert body["items"][0][field] == expected


def test_archive_searches_nested_style_rubric_elements(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
) -> None:
    client, _, _ = paged_archive
    response = client.get(
        "/projects/demo/archive",
        params={"kind": "style_rubric", "q": "叙述是否简洁", "limit": 20},
    )

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == ["rubric_archive"]


def test_archive_pages_graph_refs_for_bounded_overview_drilldown(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
) -> None:
    client, _, _ = paged_archive
    first = client.get("/projects/demo/archive", params={"kind": "graph_ref", "limit": 1})

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == body["counts"]["graph_ref"]
    assert body["total"] > 1
    assert len(body["items"]) == 1
    assert isinstance(body["items"][0], str)
    assert body["next_cursor"]

    second = client.get(
        "/projects/demo/archive",
        params={"kind": "graph_ref", "limit": 1, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["items"] != body["items"]


def test_archive_rejects_invalid_and_stale_cursors(
    paged_archive: tuple[TestClient, ContentStore, ContentBundle],
) -> None:
    client, store, bundle = paged_archive
    invalid = client.get(
        "/projects/demo/archive",
        params={"kind": "entity", "limit": 20, "cursor": "not-a-cursor"},
    )
    assert invalid.status_code == 422

    first = client.get("/projects/demo/archive", params={"kind": "entity", "limit": 20}).json()
    bundle.entities["npc_999"] = Entity(
        id="npc_999", name="新增守灯人", type=EntityType.NPC, description="changed"
    )
    store.save(bundle)
    stale = client.get(
        "/projects/demo/archive",
        params={"kind": "entity", "limit": 20, "cursor": first["next_cursor"]},
    )
    assert stale.status_code == 409
