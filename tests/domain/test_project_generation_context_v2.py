"""New longline generation boundary, with real persisted development path evidence."""

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.workflow.models import Artifact, TaskInput, TaskRecord
from owcopilot.workflow.project_v2 import NarrativeProjectStore
from owcopilot.workflow.store import WorkflowStore


@pytest.mark.parametrize("boundary", ["missing_dependency", "prospective_branch"])
def test_generation_reads_actual_history_or_rejects_missing_evidence(tmp_path, boundary):
    """No old project tests are reopened; target B deliberately has no artifact."""
    workflow = WorkflowStore(tmp_path / "development.sqlite3")
    store = NarrativeProjectStore(workflow)
    project = store.create("demo", "Controlled longline production")
    source_result = store.add_source(
        "demo",
        project["id"],
        {
            "title": "Controlled branch source",
            "format": "text",
            "text": "The messenger carries a letter from the earlier scene.",
            "status": "canon",
            "allowed_uses": ["drafting", "facts"],
            "scope": {"scene_ids": ["b"], "branch_ids": ["go"]},
        },
        expected_project_hash=project["project_hash"],
    )
    source, project = source_result["source"], source_result["project"]
    raw = source["source"]["text"]
    anchor = {"source_version_id": source["id"], "start": 0, "end": len(raw), "quote": raw}
    project = store.save_outline(
        "demo",
        project["id"],
        {
            "chapters": [
                {"id": "first", "title": "The promise"},
                {"id": "later", "title": "Its later cost"},
            ],
            "scenes": [
                {"id": "a", "chapter_id": "first", "title": "Receive the letter"},
                {
                    "id": "b",
                    "chapter_id": "later",
                    "title": "Keep the promise",
                    "dependencies": ["a"],
                    "sources": [anchor],
                    "precondition": "trust >= 2",
                },
            ],
            "variables": [{"id": "trust", "type": "int", "default": 0}],
            "entry_scene_id": "a",
            "branches": [
                {
                    "id": "go",
                    "source_scene": "a",
                    "target_scene": "b",
                    "label": "Keep the letter",
                    "effects": [{"var": "trust", "op": "inc", "value": 2}],
                },
                {
                    "id": "ending",
                    "source_scene": "b",
                    "label": "Finish",
                    "ending_text": "The promise has consequences.",
                },
            ],
        },
        expected_project_hash=project["project_hash"],
    )
    if boundary == "missing_dependency":
        with pytest.raises(ValueError, match="前史依赖"):
            store.generation_context("demo", project["id"], "b")
        return

    claim_result = store.record_claim(
        "demo",
        project["id"],
        {
            "subject": "messenger",
            "predicate": "knows",
            "value": "UNADOPTED_SECRET_VALUE",
            "anchors": [anchor],
            "scope": {"scene_ids": ["b"], "branch_ids": ["go"]},
            "interpretation": "model_interpretation",
        },
        expected_project_hash=project["project_hash"],
    )
    project = claim_result["project"]
    bundle = ContentBundle()
    task = TaskRecord(
        project="demo",
        request=TaskInput(brief="Controlled development first scene"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    workflow.save_task(task)
    run, _ = workflow.create_run(task)
    workflow.update_run(run.id, status="completed")
    quest = Quest.model_validate(
        {
            "id": "first_scene",
            "title": "A letter arrives",
            "objective": "The messenger already read the red letter",
            "stages": [{"id": "read", "summary": "Read the actual letter"}],
            "logic": {
                "variables": [{"id": "trust", "type": "int", "default": 0}],
                "branches": [
                    {
                        "id": "read",
                        "from_stage": "read",
                        "outcome": "Letter read",
                        "effects": [{"var": "trust", "op": "inc", "value": 1}],
                    }
                ],
            },
        }
    )
    payload = {"quest": quest.model_dump(mode="json")}
    artifact = Artifact(
        task_id=task.id,
        revision=1,
        run_id=run.id,
        node_id="controlled_first_scene",
        kind="quest_with_logic",
        payload=payload,
        input_hash=content_hash("development first scene"),
        content_hash=content_hash(payload),
    )
    with workflow.connection() as db:
        db.execute(
            "INSERT INTO artifacts VALUES(?,?,?)",
            (artifact.id, task.id, artifact.model_dump_json()),
        )
    project = store.bind_scene_artifact(
        "demo",
        project["id"],
        "a",
        artifact.id,
        expected_project_hash=project["project_hash"],
        expected_artifact_hash=artifact.content_hash,
    )
    assert "b" not in project["scene_bindings"]
    with pytest.raises(ValueError, match="前置|范围"):
        store.generation_context("demo", project["id"], "b")
    started = store.create_preview(
        "demo", project["id"], expected_project_hash=project["project_hash"]
    )
    waiting = store.step_preview(
        "demo",
        project["id"],
        started["preview_id"],
        action_id="read-letter",
        expected_seq=0,
        choice_id="read",
    )
    assert waiting["view"]["status"] == "waiting_transition" and waiting["state"]["trust"] == 1
    context = store.generation_context(
        "demo", project["id"], "b", preview_id=started["preview_id"], transition_id="go"
    )
    assert context["path_state"]["trust"] == 3 and context["branch_history"] == ["go"]
    assert context["sources"][0]["source_hash"] == source["source_hash"]
    prior = context["history_dependencies"][0]
    assert prior["artifact_hash"] == artifact.content_hash and prior["payload"] == payload
    assert "red letter" in prior["payload"]["quest"]["objective"]
    binding = context["generation_binding"]
    assert binding["preview_hash"] == waiting["preview_hash"] and binding["action_seq"] == 1
    assert binding["prospective"] is True and binding["transition_id"] == "go"
    assert context["accepted_facts"] == []
    assert context["proposed_or_unresolved"][0]["usable_as_world_fact"] is False
    assert "UNADOPTED_SECRET_VALUE" not in json.dumps(context)
    original = store.get_preview("demo", project["id"], started["preview_id"])
    assert original["action_seq"] == 1 and original["state"]["trust"] == 1
    assert original["branch_history"] == [] and original["scene_id"] == "a"
    assert "b" not in store.get("demo", project["id"])["scene_bindings"]
    with pytest.raises(ValueError, match="实际可走"):
        store.generation_context(
            "demo",
            project["id"],
            "b",
            preview_id=started["preview_id"],
            transition_id="unplayed-route",
        )
