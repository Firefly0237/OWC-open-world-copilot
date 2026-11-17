"""Restore a real stepper mid-dialogue; these are explicit development fixtures."""

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import DialogueTree
from owcopilot.workflow.models import new_id
from tests.domain import test_workflow_project_v2 as project_fixtures

store = project_fixtures.store


def test_restored_preview_owns_new_action_namespace_and_keeps_history(store):
    project = store.create("demo", "Action namespace development case")
    store.save_outline(
        "demo",
        project["id"],
        project_fixtures.outline(),
        expected_project_hash=project_fixtures.token(store, project),
    )
    original = project_fixtures.artifact(store, "namespace", delta=4)
    tree = DialogueTree.model_validate(
        {
            "id": "dialogue-namespace",
            "quest_id": original.payload["quest"]["id"],
            "root_node": "intro",
            "nodes": {
                "intro": {"id": "intro", "text": "Approach.", "next_node": "choice"},
                "choice": {
                    "id": "choice",
                    "text": "Choose the route.",
                    "choices": [
                        {
                            "text": "Commit",
                            "next_node": "mid",
                            "effects": [{"var": "local_route", "value": "done"}],
                        },
                        {
                            "text": "Wait",
                            "next_node": "mid",
                            "effects": [{"var": "local_route", "value": "unset"}],
                        },
                    ],
                },
                "mid": {"id": "mid", "text": "Continue carefully.", "next_node": "tail"},
                "tail": {"id": "tail", "text": "Now carry out the choice."},
            },
        }
    )
    payload = {**original.payload, "tree": tree.model_dump(mode="json")}
    bound = original.model_copy(
        update={
            "id": new_id(),
            "kind": "bound_dialogue",
            "payload": payload,
            "content_hash": content_hash(payload),
        }
    )
    store.workflow.append_artifact(bound)
    store.bind_scene_artifact(
        "demo",
        project["id"],
        "start",
        bound.id,
        expected_project_hash=project_fixtures.token(store, project),
        expected_artifact_hash=bound.content_hash,
    )

    def raw_preview(preview_id):
        with store.workflow.connection() as db:
            return db.execute(
                "SELECT payload FROM narrative_previews_v2 WHERE id=?", (preview_id,)
            ).fetchone()[0]

    preview = store.create_preview(
        "demo", project["id"], expected_project_hash=project_fixtures.token(store, project)
    )
    preview_id = preview["preview_id"]
    at_choice = store.step_preview(
        "demo", project["id"], preview_id, action_id="reused", expected_seq=0
    )
    assert at_choice["view"]["text"] == "Choose the route."
    choice_archive = store.save_archive(
        "demo", project["id"], preview_id, name="Before choice", expected_seq=1
    )
    at_mid = store.step_preview(
        "demo",
        project["id"],
        preview_id,
        action_id="select",
        expected_seq=1,
        choice_id="choice:0",
    )
    assert at_mid["view"]["text"] == "Continue carefully."
    assert at_mid["state"]["local_route"] == "done" and at_mid["state"]["trust"] == 0
    mid_archive = store.save_archive(
        "demo", project["id"], preview_id, name="Mid-dialogue", expected_seq=2
    )
    original_raw = raw_preview(preview_id)
    original_domain = json.loads(original_raw)["domain"]
    archives_before = store.archives("demo", project["id"])

    restored = store.restore_archive(
        "demo",
        project["id"],
        mid_archive["id"],
        expected_project_hash=project_fixtures.token(store, project),
        operation_id="restore-mid",
    )
    restored_id = restored["preview_id"]
    assert restored_id != preview_id and restored["action_seq"] == 2
    restored_domain = json.loads(raw_preview(restored_id))["domain"]
    for field in ("cursor", "values", "history", "action_seq"):
        assert restored_domain[field] == original_domain[field]

    # The old ID belonged to intro -> choice. In this new preview it must advance
    # mid -> tail, rather than report outer sequence progress around a domain no-op.
    advanced = store.step_preview(
        "demo", project["id"], restored_id, action_id="reused", expected_seq=2
    )
    advanced_domain = json.loads(raw_preview(restored_id))["domain"]
    assert advanced["action_seq"] == 3 and advanced["view"]["text"] == "Now carry out the choice."
    assert advanced_domain["cursor"] == "tail" and advanced_domain["action_seq"] == 3
    assert advanced_domain["history"][:-1] == original_domain["history"]
    assert len(advanced_domain["history"]) == len(original_domain["history"]) + 1
    assert advanced["state"] == at_mid["state"]
    replay = store.step_preview(
        "demo", project["id"], restored_id, action_id="reused", expected_seq=2
    )
    assert replay["idempotent_replay"] is True
    assert replay["preview_hash"] == advanced["preview_hash"]
    with pytest.raises(ValueError, match="不同参数"):
        store.step_preview(
            "demo",
            project["id"],
            restored_id,
            action_id="reused",
            expected_seq=2,
            choice_id="choice:1",
        )

    store.step_preview(
        "demo", project["id"], restored_id, action_id="finish-dialogue", expected_seq=3
    )
    completed = store.step_preview(
        "demo", project["id"], restored_id, action_id="settle", expected_seq=4, choice_id="do"
    )
    assert completed["state"]["trust"] == 4
    settled_replay = store.step_preview(
        "demo", project["id"], restored_id, action_id="settle", expected_seq=4, choice_id="do"
    )
    assert settled_replay["state"]["trust"] == 4
    assert settled_replay["preview_hash"] == completed["preview_hash"]

    # A different restored preview may reuse the same old ID with a different,
    # currently legal choice. Conflicting reuse is rejected within that new scope.
    other = store.restore_archive(
        "demo",
        project["id"],
        choice_archive["id"],
        expected_project_hash=project_fixtures.token(store, project),
        operation_id="restore-choice",
    )
    other_result = store.step_preview(
        "demo",
        project["id"],
        other["preview_id"],
        action_id="reused",
        expected_seq=1,
        choice_id="choice:1",
    )
    assert other_result["action_seq"] == 2
    assert other_result["view"]["text"] == "Continue carefully."
    assert other_result["state"]["local_route"] == "unset"
    with pytest.raises(ValueError, match="不同参数"):
        store.step_preview(
            "demo",
            project["id"],
            other["preview_id"],
            action_id="reused",
            expected_seq=1,
            choice_id="choice:0",
        )
    assert raw_preview(preview_id) == original_raw
    assert store.archives("demo", project["id"]) == archives_before
