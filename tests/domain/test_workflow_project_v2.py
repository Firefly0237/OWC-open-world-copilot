"""Real SQLite/source/stepper project behavior using marked development content."""

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.workflow.models import Artifact, TaskInput, TaskRecord
from owcopilot.workflow.project_v2 import NarrativeProjectStore, SourceInput
from owcopilot.workflow.store import WorkflowStore


@pytest.fixture
def store(tmp_path):
    return NarrativeProjectStore(WorkflowStore(tmp_path / "workflows.sqlite3"))


def token(store, project):
    return store.get("demo", project["id"])["project_hash"]


def source(store, project, text, *, status="canon", format="text", **kwargs):
    return store.add_source(
        "demo",
        project["id"],
        SourceInput(
            title="Controlled development source",
            format=format,
            text=text,
            status=status,
            allowed_uses=["facts", "drafting", "inspiration"],
        ),
        expected_project_hash=token(store, project),
        **kwargs,
    )["source"]


def anchor(source):
    text = source["source"]["text"]
    return {"source_version_id": source["id"], "start": 0, "end": len(text), "quote": text}


def artifact(store, name, *, delta=0, project="demo", world="default", global_type="int"):
    bundle = ContentBundle()
    task = TaskRecord(
        project=project,
        request=TaskInput(brief="Controlled development scene " + name, world_id=world),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    workflow = store.workflow
    workflow.save_task(task)
    run, _ = workflow.create_run(task)
    workflow.update_run(run.id, status="completed")
    trust_default = 0 if global_type == "int" else False
    quest = Quest.model_validate(
        {
            "id": "q_" + name,
            "title": name,
            "stages": [{"id": "s", "summary": "Complete " + name}],
            "logic": {
                "variables": [
                    {"id": "trust", "type": global_type, "default": trust_default},
                    {
                        "id": "local_route",
                        "type": "enum",
                        "enum_values": ["unset", "done"],
                        "default": "unset",
                    },
                ],
                "branches": [
                    {
                        "id": "do",
                        "from_stage": "s",
                        "outcome": "Scene complete",
                        "effects": [
                            {"var": "local_route", "value": "done"},
                            {"var": "trust", "op": "inc", "value": delta},
                        ]
                        if global_type == "int"
                        else [],
                    }
                ],
            },
        }
    )
    value = Artifact(
        task_id=task.id,
        revision=1,
        run_id=run.id,
        node_id="fixture",
        kind="quest_with_logic",
        payload={"quest": quest.model_dump(mode="json")},
        input_hash=content_hash(name),
        content_hash=content_hash({"quest": quest.model_dump(mode="json")}),
    )
    with workflow.connection() as db:
        db.execute(
            "INSERT INTO artifacts VALUES(?,?,?)", (value.id, task.id, value.model_dump_json())
        )
    return value


def outline(source_anchor=None):
    return {
        "chapters": [
            {"id": "a", "title": "Before the choice"},
            {"id": "b", "title": "Delayed consequence"},
        ],
        "scenes": [
            {
                "id": name,
                "chapter_id": "b" if name == "join" else "a",
                "title": name,
                "sources": [source_anchor] if source_anchor else [],
                "merge_policy": "preserve_path_state" if name == "join" else None,
            }
            for name in ("start", "left", "right", "join")
        ],
        "variables": [{"id": "trust", "type": "int", "default": 0}],
        "entry_scene_id": "start",
        "branches": [
            {
                "id": "left",
                "source_scene": "start",
                "target_scene": "left",
                "label": "Help",
                "effects": [{"var": "trust", "op": "inc", "value": 2}],
            },
            {
                "id": "right",
                "source_scene": "start",
                "target_scene": "right",
                "label": "Refuse",
                "effects": [{"var": "trust", "op": "dec", "value": 2}],
            },
            {
                "id": "left_join",
                "source_scene": "left",
                "target_scene": "join",
                "label": "Continue",
            },
            {
                "id": "right_join",
                "source_scene": "right",
                "target_scene": "join",
                "label": "Continue",
            },
            {
                "id": "ending",
                "source_scene": "join",
                "label": "Finish",
                "ending_text": "The promise is remembered.",
            },
        ],
    }


def bind_all(store, project):
    for name in ("start", "left", "right", "join"):
        value = artifact(store, name, delta=1 if name == "left" else 0)
        store.bind_scene_artifact(
            "demo",
            project["id"],
            name,
            value.id,
            expected_project_hash=token(store, project),
            expected_artifact_hash=value.content_hash,
        )


def test_sources_conflict_and_adoption_preserve_provenance_instead_of_newest_wins(store):
    project = store.create("demo", "Development project")
    first = source(store, project, "The house owes the merchant.")
    second = source(store, project, "The merchant owes the house.")
    for value in (first, second):
        store.record_claim(
            "demo",
            project["id"],
            {
                "subject": "house",
                "predicate": "owes",
                "value": "merchant" if value == first else "nobody",
                "anchors": [anchor(value)],
                "interpretation": "model_interpretation",
            },
            expected_project_hash=token(store, project),
        )
    claims = store.claims("demo", project["id"])
    assert all(row["decision"] is None for row in claims)
    store.decide_claim(
        "demo",
        project["id"],
        claims[0]["id"],
        accept=True,
        expected_project_hash=token(store, project),
    )
    with pytest.raises(ValueError, match="冲突"):
        store.decide_claim(
            "demo",
            project["id"],
            claims[1]["id"],
            accept=True,
            expected_project_hash=token(store, project),
        )
    store.decide_claim(
        "demo",
        project["id"],
        claims[1]["id"],
        accept=True,
        reject_claim_ids=[claims[0]["id"]],
        expected_project_hash=token(store, project),
    )
    assert [row["decision"]["accepted"] for row in store.claims("demo", project["id"])] == [
        False,
        True,
    ]
    inspiration = source(store, project, "Maybe the house is innocent.", status="inspiration")
    proposal = store.record_claim(
        "demo",
        project["id"],
        {
            "subject": "house",
            "predicate": "innocent",
            "value": True,
            "anchors": [anchor(inspiration)],
            "interpretation": "model_interpretation",
        },
        expected_project_hash=token(store, project),
    )
    with pytest.raises(ValueError, match="灵感／草稿"):
        store.decide_claim(
            "demo",
            project["id"],
            proposal["claim"]["id"],
            accept=True,
            expected_project_hash=token(store, project),
        )
    yarn = source(
        store, project, "title: Start\n---\nGuide: Welcome\n===", format="yarn", status="draft"
    )
    assert yarn["adaptation"]["engine_executed"] is False
    assert any(row["id"] == "Start" for row in yarn["adaptation"]["recognition"]["entities"])


def test_cross_chapter_merge_keeps_one_paths_state_and_real_archive_resumes_it(store):
    project = store.create("demo", "Branching development project")
    store.save_outline(
        "demo", project["id"], outline(), expected_project_hash=token(store, project)
    )
    bind_all(store, project)
    preview = store.create_preview(
        "demo", project["id"], expected_project_hash=token(store, project)
    )
    pid = preview["preview_id"]
    first = store.step_preview(
        "demo", project["id"], pid, action_id="start-done", expected_seq=0, choice_id="do"
    )
    assert first["view"]["status"] == "waiting_transition"
    left = store.step_preview(
        "demo", project["id"], pid, action_id="choose-left", expected_seq=1, transition_id="left"
    )
    assert left["state"]["trust"] == 2 and left["state"]["local_route"] == "unset"
    completed = store.step_preview(
        "demo", project["id"], pid, action_id="left-done", expected_seq=2, choice_id="do"
    )
    assert completed["state"]["trust"] == 3
    archive = store.save_archive(
        "demo", project["id"], pid, name="Before the reunion", expected_seq=3
    )
    merged = store.step_preview(
        "demo", project["id"], pid, action_id="left-join", expected_seq=3, transition_id="left_join"
    )
    assert merged["scene_id"] == "join" and merged["state"]["trust"] == 3
    assert merged["state"]["local_route"] == "unset"
    assert "right" not in merged["branch_history"] and "right" not in merged["visited_scenes"]
    resumed = store.restore_archive(
        "demo", project["id"], archive["id"], expected_project_hash=token(store, project)
    )
    assert resumed["scene_id"] == "left" and resumed["state"]["trust"] == 3
    assert resumed["preview_id"] != pid
    store.step_preview(
        "demo", project["id"], pid, action_id="join-done", expected_seq=4, choice_id="do"
    )
    ending = store.step_preview(
        "demo", project["id"], pid, action_id="ending", expected_seq=5, transition_id="ending"
    )
    assert ending["view"]["status"] == "completed" and "promise" in ending["view"]["text"]
    assert ending["coverage"]["visited_scene_count"] == 3


def test_scope_hash_world_and_global_local_type_collisions_fail_closed(store):
    project = store.create("demo", "Scoped project")
    store.save_outline(
        "demo", project["id"], outline(), expected_project_hash=token(store, project)
    )
    other = artifact(store, "foreign", project="other")
    with pytest.raises(ValueError, match="作用域"):
        store.bind_scene_artifact(
            "demo",
            project["id"],
            "start",
            other.id,
            expected_project_hash=token(store, project),
            expected_artifact_hash=other.content_hash,
        )
    normal = artifact(store, "normal")
    store.bind_scene_artifact(
        "demo",
        project["id"],
        "start",
        normal.id,
        expected_project_hash=token(store, project),
        expected_artifact_hash=normal.content_hash,
    )
    wrong_world = artifact(store, "other_world", world="another")
    with pytest.raises(ValueError, match="world"):
        store.bind_scene_artifact(
            "demo",
            project["id"],
            "left",
            wrong_world.id,
            expected_project_hash=token(store, project),
            expected_artifact_hash=wrong_world.content_hash,
        )
    wrong_type = artifact(store, "wrong_type", global_type="bool")
    store.bind_scene_artifact(
        "demo",
        project["id"],
        "start",
        wrong_type.id,
        expected_project_hash=token(store, project),
        expected_artifact_hash=wrong_type.content_hash,
    )
    with pytest.raises(ValueError, match="类型"):
        store.create_preview("demo", project["id"], expected_project_hash=token(store, project))
    with pytest.raises(KeyError):
        store.get("other", project["id"])
    current = store.get("demo", project["id"])
    current["name"] = "Changed without updating the hash"
    with store.workflow.connection() as db:
        db.execute(
            "UPDATE narrative_projects_v2 SET payload=? WHERE id=?",
            (json.dumps(current), project["id"]),
        )
    with pytest.raises(ValueError, match="hash"):
        store.get("demo", project["id"])


def test_source_replacement_does_not_reuse_old_bound_scene_without_rebinding(store):
    project = store.create("demo", "Source-bound project")
    original = source(store, project, "A promise was made.")
    store.save_outline(
        "demo",
        project["id"],
        outline(anchor(original)),
        expected_project_hash=token(store, project),
    )
    bind_all(store, project)
    store.create_preview("demo", project["id"], expected_project_hash=token(store, project))
    replacement = source(
        store,
        project,
        "The promise was revoked.",
        source_id=original["source_id"],
        expected_source_hash=original["source_hash"],
    )
    assert replacement["parent_version_id"] == original["id"]
    with pytest.raises(ValueError, match="来源"):
        store.create_preview("demo", project["id"], expected_project_hash=token(store, project))
