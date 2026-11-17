"""Real feedback HTTP/storage contracts over explicit candidate inputs, with zero model calls."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app
from owcopilot.workflow.models import Artifact, WorkflowSpec

PREFIX = "/projects/demo/creative"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root)}))
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    with TestClient(create_app()) as client:
        yield client, root, open_workflow_store(root)


def candidate(client, store):
    response = client.post(f"{PREFIX}/tasks", json={"brief": "改变说法，保留选择后果"})
    assert response.status_code == 201, response.text
    task = store.get_task(response.json()["task"]["id"])
    run, _ = store.create_run(task)
    spec = WorkflowSpec(
        name="Explicit HTTP contract input; not a generated workflow",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "dialogue", "operation": "draft_dialogue", "depends_on": ["events"]},
            {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
            {"id": "binding", "operation": "bind_dialogue", "depends_on": ["logic", "dialogue"]},
            {"id": "audit", "operation": "audit", "optional_dependencies": ["binding"]},
            {"id": "package", "operation": "package", "optional_dependencies": ["audit"]},
        ],
    )
    store.update_run(run.id, workflow=spec.model_dump(mode="json"), status="completed")
    payload = {
        "quest": {
            "id": "candidate_quest",
            "title": "半句口信",
            "objective": "说明尚未讲出的后半句",
            "stages": [{"id": "answer", "summary": "决定如何回应"}],
            "logic": {},
        },
        "tree": {
            "id": "candidate_tree",
            "quest_id": "candidate_quest",
            "root_node": "door",
            "nodes": {
                "door": {
                    "id": "door",
                    "text": "我没有把后半句转达给你。",
                    "choices": [{"text": "请把话说完。", "next_node": None}],
                }
            },
        },
    }
    artifact = Artifact(
        task_id=task.id,
        revision=task.revision,
        run_id=run.id,
        node_id="bind_dialogue",
        kind="bound_dialogue",
        payload=payload,
        input_hash=content_hash({"explicit_test_input": True}),
        content_hash=content_hash(payload),
    )
    # Persist explicit inputs, without claiming that a model generated or executed them.
    store.append_artifact(artifact)
    return task, run, artifact


def anchor(artifact, **changes):
    return {
        "artifact_id": artifact.id,
        "artifact_hash": artifact.content_hash,
        "path": "/tree/nodes/door/text",
        **changes,
    }


def test_bound_dialogue_preview_feedback_routes_to_dialogue_and_preserves_source(studio):
    client, root, store = studio
    task, run, artifact = candidate(client, store)
    original = deepcopy(artifact.payload)
    base = ContentStore(root).load().model_dump(mode="json")
    preview = client.post(f"{PREFIX}/tasks/{task.id}/preview", json={"artifact_id": artifact.id})
    assert preview.status_code == 201, preview.text
    preview_body = preview.json()
    response = client.post(
        f"{PREFIX}/tasks/{task.id}/feedback",
        json={
            "text": "只把这句话说得更克制，保留后果",
            "expected_revision": 1,
            "anchor": anchor(
                artifact,
                preview_id=preview_body["preview_id"],
                action_seq=preview_body["action_seq"],
            ),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["feedback"]["target_operation"] == "draft_dialogue"
    assert set(body["affected_operations"]) == {
        "draft_dialogue",
        "bind_dialogue",
        "audit",
        "package",
    }
    resolved = body["feedback"]["anchor"]
    assert resolved["target_text"] == original["tree"]["nodes"]["door"]["text"]
    assert resolved["source_hash"] == artifact.content_hash
    assert resolved["artifact_id"] == artifact.id
    assert body["feedback"]["edit_scope_status"] == "location_only_not_a_field_edit_permission"
    persisted = store.get_task(task.id).feedback[-1]
    assert persisted["anchor"] == resolved
    source = store.get_artifact(artifact.id)
    assert source.payload == original and source.content_hash == artifact.content_hash
    assert ContentStore(root).load().model_dump(mode="json") == base
    assert store.model_calls(run.id) == []


def test_anchor_conflicts_are_http_409_and_do_not_advance_revision(studio):
    client, _, store = studio
    task, run, artifact = candidate(client, store)
    _, other_run, other = candidate(client, store)
    original = {
        "text": "改变语气",
        "expected_revision": 1,
        "anchor": anchor(artifact),
    }
    for changes in (
        {"anchor": anchor(artifact, artifact_hash="0" * 64)},
        {"anchor": anchor(other)},
        {"scope": "project"},
        {"scope": "workflow"},
        {"target_operation": "draft_quest"},
    ):
        response = client.post(f"{PREFIX}/tasks/{task.id}/feedback", json={**original, **changes})
        assert response.status_code == 409, response.text
        assert store.get_task(task.id).revision == 1
        assert store.get_task(task.id).feedback == []
    assert store.preferences() == []
    changed = client.post(
        f"{PREFIX}/tasks/{task.id}/feedback",
        json={"text": "先修订对白", "expected_revision": 1, "target_operation": "draft_dialogue"},
    )
    assert changed.status_code == 200, changed.text
    stale = client.post(
        f"{PREFIX}/tasks/{task.id}/feedback", json={**original, "expected_revision": 2}
    )
    assert stale.status_code == 409, stale.text
    assert store.get_task(task.id).revision == 2
    assert store.model_calls(run.id) == [] and store.model_calls(other_run.id) == []


def test_feedback_without_anchor_keeps_the_existing_default_operation(studio):
    client, _, store = studio
    task, run, artifact = candidate(client, store)
    original = deepcopy(artifact.payload)
    response = client.post(
        f"{PREFIX}/tasks/{task.id}/feedback",
        json={"text": "把任务目标写得更简洁", "expected_revision": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["feedback"]["target_operation"] == "draft_quest"
    assert "draft_dialogue" not in body["affected_operations"]
    assert "anchor" not in body["feedback"]
    assert "edit_scope_status" not in body["feedback"]
    assert store.get_artifact(artifact.id).payload == original
    assert store.model_calls(run.id) == []
