"""Dual-source permission integration; real stores, explicit development generation fixtures."""

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import DialogueTree
from owcopilot.inspiration.store import ReferenceStore
from owcopilot.workflow.project_v2 import NarrativeProjectStore
from owcopilot.workflow.store import WorkflowStore
from owcopilot.workflow.v2_service import _check_references, reference_snapshot
from tests.domain.test_workflow_project_v2 import artifact, outline, token


def setup(tmp_path, *, dependency=False):
    root = tmp_path / "content"
    workflow = WorkflowStore(root / "runtime" / "workflows.sqlite3")
    project_store = NarrativeProjectStore(workflow)
    project = project_store.create("demo", "Dual-source development contract")
    plan = outline()
    if dependency:
        plan["scenes"][1]["dependencies"] = ["start"]
    project_store.save_outline(
        "demo", project["id"], plan, expected_project_hash=token(project_store, project)
    )
    references = ReferenceStore(root)
    original = references.add_text(
        title="Explicit ReferenceStore development source",
        text="The messenger keeps the red letter.",
        allowed_uses=["drafting", "inspiration"],
    )
    return root, project_store, project, references, original.source


def bound_from_reference(root, store, project, scene_id, source, name):
    """Use production selection/snapshot and source text, without claiming an LLM ran."""
    value = artifact(store, name, delta=1)
    task = store.workflow.get_task(value.task_id)
    task = task.model_copy(
        update={
            "request": task.request.model_copy(
                update={
                    "brief": "Read the messenger letter",
                    "use_references": True,
                    "reference_ids": [source.id],
                }
            )
        }
    )
    store.workflow.save_task(task)
    references = reference_snapshot(root, task)
    assert references and references[0]["source_id"] == source.id
    context = store.generation_context("demo", project["id"], scene_id)
    tree = DialogueTree.model_validate(
        {
            "id": "dlg_" + name,
            "quest_id": value.payload["quest"]["id"],
            "root_node": "line",
            "nodes": {
                "line": {
                    "id": "line",
                    "text": references[0]["body"],
                    "choices": [
                        {
                            "text": "Acknowledge the actual source",
                            "effects": [{"var": "trust", "op": "inc", "value": 1}],
                        }
                    ],
                }
            },
        }
    )
    payload = {**value.payload, "tree": tree.model_dump(mode="json")}
    value = value.model_copy(
        update={"kind": "bound_dialogue", "payload": payload, "content_hash": content_hash(payload)}
    )
    state = {
        "narrative_project_id": project["id"],
        "narrative_scene_id": scene_id,
        "narrative_context": context,
        "references": references,
        "fixture_provenance": "development contract; production source selection; "
        "no LLM invocation",
    }
    with store.workflow.connection() as db:
        db.execute("UPDATE artifacts SET payload=? WHERE id=?", (value.model_dump_json(), value.id))
        db.execute(
            "CREATE TABLE IF NOT EXISTS creative_v2_runs("
            "run_id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO creative_v2_runs VALUES(?,?,?)",
            (value.run_id, "demo", json.dumps(state, ensure_ascii=False)),
        )
    return value, references


def bind(store, project, scene_id, value):
    return store.bind_scene_artifact(
        "demo",
        project["id"],
        scene_id,
        value.id,
        expected_project_hash=token(store, project),
        expected_artifact_hash=value.content_hash,
    )


def revoke_drafting(references, source):
    # The source store has no generic permission-edit facade. Persist only this
    # scratch source's explicit permission change, retaining source text/hash.
    revised = source.model_copy(update={"allowed_uses": ["inspiration"]})
    (references.sources_dir / (source.id + ".json")).write_text(
        revised.model_dump_json(), encoding="utf-8"
    )
    return revised


def test_consumed_reference_revocation_fences_new_work_but_preserves_action_receipt(tmp_path):
    root, store, project, references, source = setup(tmp_path)
    value, snapshots = bound_from_reference(root, store, project, "start", source, "start_original")
    bind(store, project, "start", value)
    unbound, _ = bound_from_reference(
        root, store, project, "start", source, "start_before_revocation"
    )
    assert all(
        not scene["sources"] for scene in store.get("demo", project["id"])["outline"]["scenes"]
    )
    preview = store.create_preview(
        "demo", project["id"], expected_project_hash=token(store, project)
    )
    played = store.step_preview(
        "demo",
        project["id"],
        preview["preview_id"],
        action_id="read-source",
        expected_seq=0,
        choice_id="line:0",
    )
    archive = store.save_archive(
        "demo", project["id"], preview["preview_id"], name="same-hash archive", expected_seq=1
    )
    project_hash = token(store, project)
    before = store.get_preview("demo", project["id"], preview["preview_id"])
    revoked = revoke_drafting(references, source)
    assert revoked.text_hash == source.text_hash and token(store, project) == project_hash
    operations = {
        "production_reference_check": lambda: _check_references(root, snapshots),
        "bind_previously_generated_content": lambda: bind(store, project, "start", unbound),
        "new_preview": lambda: store.create_preview(
            "demo", project["id"], expected_project_hash=project_hash
        ),
        "new_step": lambda: store.step_preview(
            "demo",
            project["id"],
            preview["preview_id"],
            action_id="new-effect",
            expected_seq=1,
            choice_id="do",
        ),
        "same_hash_restore": lambda: store.restore_archive(
            "demo",
            project["id"],
            archive["id"],
            expected_project_hash=project_hash,
            operation_id="first-restore-after-revocation",
        ),
        "same_hash_rebase": lambda: store.rebase_preview(
            "demo",
            project["id"],
            preview["preview_id"],
            expected_project_hash=project_hash,
            expected_preview_hash=played["preview_hash"],
        ),
    }
    refusals = {}
    for name, operation in operations.items():
        with pytest.raises(ValueError, match="参考许可") as caught:
            operation()
        refusals[name] = str(caught.value)
    reread = store.step_preview(
        "demo",
        project["id"],
        preview["preview_id"],
        action_id="read-source",
        expected_seq=0,
        choice_id="line:0",
    )
    assert reread["idempotent_replay"] is True
    assert {key: item for key, item in reread.items() if key != "idempotent_replay"} == played
    with pytest.raises(ValueError, match="不同参数"):
        store.step_preview(
            "demo",
            project["id"],
            preview["preview_id"],
            action_id="read-source",
            expected_seq=1,
            choice_id="do",
        )
    after = store.get_preview("demo", project["id"], preview["preview_id"])
    assert after == before and after["action_seq"] == 1 and after["state"]["trust"] == 1
    assert token(store, project) == project_hash
    (tmp_path / "revocation-evidence.json").write_text(
        json.dumps(
            {
                "project_hash_unchanged": project_hash,
                "source_hash_unchanged": source.text_hash,
                "refusals": refusals,
                "before": before,
                "after": after,
                "reread": reread,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_legacy_reference_binding_can_be_repaired_without_bypassing_dependency_permission(tmp_path):
    root, store, project, references, old_source = setup(tmp_path, dependency=True)
    old, old_snapshots = bound_from_reference(
        root, store, project, "start", old_source, "legacy_start"
    )
    bind(store, project, "start", old)
    # Simulate the persisted pre-guard shape; actual original V2 run keeps its
    # production ReferenceStore snapshot and is the fallback provenance source.
    current = store.get("demo", project["id"])
    binding = dict(current["scene_bindings"]["start"])
    binding.pop("reference_snapshot")
    binding.pop("binding_hash")
    binding["binding_hash"] = content_hash(binding)
    with store.workflow.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        store._update(
            db, store._project(db, "demo", project["id"]), scene_bindings={"start": binding}
        )
    replacement_source = references.add_text(
        title="New independently licensed reference",
        text="The messenger now keeps the blue letter.",
        allowed_uses=["drafting"],
    ).source
    dependent, _ = bound_from_reference(
        root, store, project, "left", replacement_source, "left_before_revocation"
    )
    project_hash = token(store, project)
    revoke_drafting(references, old_source)
    assert token(store, project) == project_hash
    with pytest.raises(ValueError, match="参考许可"):
        store.create_preview("demo", project["id"], expected_project_hash=project_hash)
    with pytest.raises(ValueError, match="参考许可"):
        bind(store, project, "left", dependent)
    # Rebuilding the target from new allowed input must not require permission to
    # reuse its revoked *old* binding. Its still-relevant dependencies do require it.
    fresh, fresh_snapshots = bound_from_reference(
        root, store, project, "start", replacement_source, "fresh_start"
    )
    repaired = bind(store, project, "start", fresh)
    assert repaired["scene_bindings"]["start"]["reference_snapshot"] == fresh_snapshots
    assert repaired["scene_bindings"]["start"]["artifact_id"] == fresh.id
    new_dependent, _ = bound_from_reference(
        root, store, project, "left", replacement_source, "fresh_left"
    )
    bind(store, project, "left", new_dependent)
    preview = store.create_preview(
        "demo", project["id"], expected_project_hash=token(store, project)
    )
    assert preview["state"]["trust"] == 0
    assert store.workflow.get_artifact(old.id).payload == old.payload
    with store.workflow.connection() as db:
        original = json.loads(
            db.execute(
                "SELECT payload FROM creative_v2_runs WHERE run_id=?", (old.run_id,)
            ).fetchone()[0]
        )
    assert original["references"] == old_snapshots
    (tmp_path / "repair-evidence.json").write_text(
        json.dumps(
            {
                "legacy_project_hash": project_hash,
                "original_references_preserved": original["references"],
                "replacement_references": fresh_snapshots,
                "repaired_artifact_id": fresh.id,
                "dependency_artifact_id": new_dependent.id,
                "preview": preview,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
