"""Version-bound editing and continuation using explicit development inputs."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.intent_workflow import (
    IntentAuthorDecisionInput,
    IntentCandidateReviewInput,
    IntentConfirmInput,
    IntentContractInput,
    confirm_intent_contract_action,
    create_intent_candidate_review_action,
    create_intent_contract_action,
    decide_intent_candidate_action,
)
from owcopilot.app.actions.workflow import (
    create_task_action,
    open_workflow_store,
    start_preview_action,
    step_preview_action,
)
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.store import ReferenceStore
from owcopilot.service.api import create_app
from owcopilot.workflow.anchors import FeedbackAnchor, resolve_feedback_anchor
from owcopilot.workflow.edit_continuity import (
    ApplicationContinuation,
    continue_application_preview,
    continue_original_task_preview,
    list_applications,
    project_domain_projection,
    project_review_changes,
    read_application,
    read_task_preview,
    step_original_task_preview,
)
from owcopilot.workflow.intent_application import apply_intent_review
from owcopilot.workflow.intent_store import _sealed
from owcopilot.workflow.mechanic_ir_v2 import compile_mechanic_design, compile_mechanic_dialogue
from owcopilot.workflow.models import Artifact, PreviewInput, PreviewStepInput, TaskCreateInput
from owcopilot.workflow.project_v2 import NarrativeProjectStore, ProjectOutline
from tests.domain.test_mechanic_ir_v2 import design_case


def setup_case(tmp_path, *, use_reference=False):
    root = tmp_path / "world"
    ContentStore(root).save(ContentBundle())
    store = open_workflow_store(root)
    task_id = create_task_action(
        root,
        "demo",
        TaskCreateInput(brief="Controlled development input", origin_kind="development"),
    )["task"]["id"]
    task = store.get_task(task_id)
    references = []
    if use_reference:
        from owcopilot.workflow.v2_service import reference_snapshot

        source = (
            ReferenceStore(root)
            .add_text(
                title="Controlled source",
                text="The witness brings an unopened letter.",
                allowed_uses=["drafting"],
            )
            .source
        )
        task.request = task.request.model_copy(
            update={
                "use_references": True,
                "reference_ids": [source.id],
            }
        )
        store.save_task(task)
        references = reference_snapshot(root, task)
    run, _ = store.create_run(task)
    store.update_run(run.id, status="completed")
    plan, quest, ir, prose = design_case()
    compilation = compile_mechanic_design(ir, plan, quest)
    dialogue = compile_mechanic_dialogue(prose, compilation, plan, tree_id="tree")
    payload = {
        "quest": compilation.quest.model_dump(mode="json"),
        "tree": dialogue.tree.model_dump(mode="json"),
    }
    artifact = Artifact(
        task_id=task_id,
        revision=1,
        run_id=run.id,
        node_id="dialogue",
        kind="bound_dialogue",
        payload=payload,
        input_hash="controlled",
        content_hash=content_hash(payload),
    )
    store.append_artifact(artifact)
    projects = NarrativeProjectStore(store)
    project = projects.create("demo", "Controlled development scene")
    outline = ProjectOutline.model_validate(
        {
            "chapters": [{"id": "c", "title": "Chapter"}],
            "scenes": [{"id": "s", "title": "Scene", "chapter_id": "c", "brief": "Investigate"}],
            "entry_scene_id": "s",
            "branches": [
                {
                    "id": "end",
                    "source_scene": "s",
                    "label": "End",
                    "ending_text": "The hearing closes.",
                }
            ],
        }
    )
    project = projects.save_outline(
        "demo", project["id"], outline, expected_project_hash=project["project_hash"]
    )
    context = projects.generation_context("demo", project["id"], "s")
    with store.connection() as db:
        db.execute(
            "CREATE TABLE creative_v2_runs(run_id TEXT PRIMARY KEY,project TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO creative_v2_runs VALUES(?,?,?)",
            (
                run.id,
                "demo",
                json.dumps(
                    {
                        "narrative_project_id": project["id"],
                        "narrative_scene_id": "s",
                        "narrative_context": context,
                        "references": references,
                        "graph": {
                            "nodes": [
                                {"id": "dialogue", "operation": "dialogue_constrained"},
                                {"id": "audit", "operation": "coverage_audit"},
                            ],
                            "data_edges": [{"source_node": "dialogue", "target_node": "audit"}],
                            "control_edges": [],
                        },
                    }
                ),
            ),
        )
    project = projects.bind_scene_artifact(
        "demo",
        project["id"],
        "s",
        artifact.id,
        expected_project_hash=project["project_hash"],
        expected_artifact_hash=artifact.content_hash,
    )
    task_preview = start_preview_action(
        store, "demo", task.id, PreviewInput(artifact_id=artifact.id)
    )
    first_choice = task_preview["choices"][0]["id"]
    task_preview = step_preview_action(
        store,
        "demo",
        task_preview["preview_id"],
        PreviewStepInput(action_id="choose", expected_seq=0, choice_id=first_choice),
    )
    project_preview = projects.create_preview(
        "demo", project["id"], expected_project_hash=project["project_hash"]
    )
    project_preview = projects.step_preview(
        "demo",
        project["id"],
        project_preview["preview_id"],
        action_id="choose",
        expected_seq=0,
        choice_id=first_choice,
    )
    return root, store, task, artifact, projects, project, task_preview, project_preview


def apply_text(case, *, path=None, value="A newly specified line.", continuation=None):
    root, store, task, artifact, _, _, task_preview, _ = case
    cursor = task_preview["cursor"]
    artifact_path = path or f"/tree/nodes/{cursor}/text"
    parts = artifact_path.split("/")
    bundle_path = "/dialogue_trees/tree/" + "/".join(parts[2:])
    proposed = create_intent_contract_action(
        root,
        "demo",
        task.id,
        IntentContractInput(
            expected_revision=1,
            baseline_artifact_id=artifact.id,
            baseline_artifact_hash=artifact.content_hash,
            allowed_paths=[bundle_path],
            modification_goal="Controlled development change; retain all mechanisms",
            soft_text=["The witness remains cautious"],
        ),
    )
    confirmed = confirm_intent_contract_action(
        root,
        "demo",
        proposed["contract"]["id"],
        IntentConfirmInput(
            expected_revision=1,
            expected_contract_hash=proposed["contract_hash"],
            actor_kind="development_operator",
        ),
    )
    review = create_intent_candidate_review_action(
        root,
        "demo",
        confirmed["contract"]["id"],
        IntentCandidateReviewInput.model_validate(
            {
                "expected_revision": 1,
                "expected_contract_hash": confirmed["contract_hash"],
                "candidate": {"ops": [{"op": "replace", "path": bundle_path, "value": value}]},
            }
        ),
    )
    assert review["report"]["status"] == "ready_for_author", review["report"]
    decision = decide_intent_candidate_action(
        root,
        "demo",
        review["id"],
        IntentAuthorDecisionInput(
            expected_revision=1,
            expected_review_hash=review["review_hash"],
            decision="accepted",
            actor_kind="development_operator",
        ),
    )
    kwargs = {
        "expected_review_hash": review["review_hash"],
        "expected_decision_hash": decision["decision_hash"],
        "continuation": continuation,
    }
    receipt = apply_intent_review(root, "demo", review["id"], **kwargs)
    return receipt, kwargs, review


def test_application_recovers_exact_diff_context_and_same_result_without_second_apply(tmp_path):
    case = setup_case(tmp_path)
    root, store, task, artifact, _, _, preview, _ = case
    path = f"/tree/nodes/{preview['cursor']}/text"
    receipt, kwargs, review = apply_text(
        case,
        continuation={
            "reading_path": path,
            "task_preview_id": preview["preview_id"],
            "task_preview_seq": 1,
        },
    )
    assert receipt == apply_intent_review(root, "demo", review["id"], **kwargs)
    assert len(list_applications(store, "demo", task.id)["applications"]) == 1
    recovered = read_application(store, "demo", receipt["id"])
    assert recovered["target_current"] and recovered["artifact"]["id"] == receipt["artifact_id"]
    impact = recovered["impact"]
    assert [row["path"] for row in impact["changes"]] == [path]
    assert [row["id"] for row in impact["production"]["affected_nodes"]] == ["dialogue", "audit"]
    assert impact["reading"]["current_text_will_change"] is True
    assert impact["unconfirmed"] and receipt["goal_status"] == "withheld"
    assert receipt["decision_actor_kind"] == "development_operator"
    assert store.get_artifact(artifact.id).content_hash == artifact.content_hash
    assert store.get_task(task.id).revision == 2
    with pytest.raises(ValueError, match="另一处"):
        apply_intent_review(
            root,
            "demo",
            review["id"],
            **{**kwargs, "continuation": {"reading_path": "/tree/nodes/other/text"}},
        )


def test_task_fork_uses_new_text_without_advancing_or_replaying_state(tmp_path):
    case = setup_case(tmp_path)
    _, store, _, _, _, _, preview, _ = case
    receipt, _, _ = apply_text(case)
    fork = continue_application_preview(
        store, "demo", receipt["id"], preview_id=preview["preview_id"], expected_seq=1
    )
    assert fork["preview_id"] != preview["preview_id"]
    assert fork["text"] == "A newly specified line."
    assert fork["state"] == preview["state"] and fork["action_seq"] == 1
    assert fork["history"] == preview["history"]
    assert (
        continue_application_preview(
            store, "demo", receipt["id"], preview_id=preview["preview_id"], expected_seq=1
        )
        == fork
    )
    assert read_task_preview(store, "demo", preview["preview_id"])["text"] != fork["text"]


def test_project_binding_and_exact_text_fork_retain_history_and_show_actual_new_objects(tmp_path):
    case = setup_case(tmp_path)
    _, store, _, _, projects, project, _, old = case
    receipt, _, _ = apply_text(
        case,
        continuation={
            "narrative_project_id": project["id"],
            "narrative_scene_id": "s",
            "narrative_project_hash": project["project_hash"],
            "project_preview_id": old["preview_id"],
            "project_preview_hash": old["preview_hash"],
        },
    )
    target = store.get_artifact(receipt["artifact_id"])
    current = projects.bind_scene_artifact(
        "demo",
        project["id"],
        "s",
        target.id,
        expected_project_hash=project["project_hash"],
        expected_artifact_hash=target.content_hash,
    )
    args = {
        "expected_project_hash": current["project_hash"],
        "expected_preview_hash": old["preview_hash"],
    }
    assessment = projects.preview_compatibility("demo", project["id"], old["preview_id"], **args)
    assert assessment["status"] == "compatible", assessment
    assert assessment["evidence"]["edit_projection"]["current_text_will_change"] is True
    fork = projects.rebase_preview("demo", project["id"], old["preview_id"], **args)
    assert fork["view"]["text"] == "A newly specified line."
    assert fork["state"] == old["state"] and fork["action_seq"] == old["action_seq"]
    assert fork["view"]["history"] == old["view"]["history"]
    assert (
        projects.rebase_preview("demo", project["id"], old["preview_id"], **args)["preview_id"]
        == fork["preview_id"]
    )
    assert (
        projects.get_preview("demo", project["id"], old["preview_id"])["view"]["text"]
        != fork["view"]["text"]
    )
    assert (
        projects.bind_scene_artifact(
            "demo",
            project["id"],
            "s",
            target.id,
            expected_project_hash=current["project_hash"],
            expected_artifact_hash=target.content_hash,
        )["project_hash"]
        == current["project_hash"]
    )


def test_committed_text_is_refused_but_original_versions_can_continue_in_isolation(tmp_path):
    case = setup_case(tmp_path)
    _, store, task, artifact, projects, project, task_preview, old = case
    committed = task_preview["history"][0]["cursor"]
    receipt, _, _ = apply_text(case, path=f"/tree/nodes/{committed}/text")
    target = store.get_artifact(receipt["artifact_id"])
    with pytest.raises(ValueError, match="确认推进"):
        continue_application_preview(
            store, "demo", receipt["id"], preview_id=task_preview["preview_id"], expected_seq=1
        )
    current = projects.bind_scene_artifact(
        "demo",
        project["id"],
        "s",
        target.id,
        expected_project_hash=project["project_hash"],
        expected_artifact_hash=target.content_hash,
    )
    args = {
        "expected_project_hash": current["project_hash"],
        "expected_preview_hash": old["preview_hash"],
    }
    assert (
        projects.preview_compatibility("demo", project["id"], old["preview_id"], **args)["status"]
        == "incompatible"
    )
    with pytest.raises(ValueError, match="确认推进"):
        projects.rebase_preview("demo", project["id"], old["preview_id"], **args)
    fixed = projects.continue_original_preview("demo", project["id"], old["preview_id"], **args)
    assert (
        fixed["version_mode"] == "fixed_historical"
        and fixed["project_hash"] == project["project_hash"]
    )
    advanced = projects.step_preview(
        "demo", project["id"], fixed["preview_id"], action_id="old-continue", expected_seq=1
    )
    assert advanced["action_seq"] == 2
    with pytest.raises(ValueError, match="旧版隔离"):
        projects.generation_context("demo", project["id"], "s", preview_id=fixed["preview_id"])
    original_task = continue_original_task_preview(
        store, "demo", task_preview["preview_id"], expected_seq=1
    )
    assert original_task["version_mode"] == "fixed_historical"
    stepped = step_original_task_preview(
        store,
        "demo",
        original_task["preview_id"],
        PreviewStepInput(action_id="old-continue", expected_seq=1),
    )
    assert stepped["action_seq"] == 2 and store.get_task(task.id).revision == 2
    assert store.get_artifact(artifact.id).content_hash == artifact.content_hash


def test_projection_refuses_mechanics_or_ending_mapping_changes_and_context_is_strict(tmp_path):
    case = setup_case(tmp_path)
    _, store, _, artifact, _, _, preview, _ = case
    changed = deepcopy(artifact.payload)
    changed["quest"]["logic"]["branches"][0]["effects"][0]["value"] = 99
    with pytest.raises(ValueError, match="机制或结构"):
        project_domain_projection(
            store.get_preview(preview["preview_id"])["session"], artifact.payload, changed
        )
    with pytest.raises(ValueError, match="成组"):
        ApplicationContinuation(task_preview_id=preview["preview_id"])


def test_field_application_preserves_absent_legacy_defaults():
    raw = {"quest": {"id": "q", "title": "Original"}}
    before = {"quests": {"q": {"id": "q", "title": "Original", "objective": ""}}}
    after = deepcopy(before)
    after["quests"]["q"]["title"] = "Changed"
    projected = project_review_changes(raw, before, after)
    assert projected == {"quest": {"id": "q", "title": "Changed"}}
    assert raw == {"quest": {"id": "q", "title": "Original"}}


def test_fixed_versions_still_fence_revoked_source_permissions(tmp_path):
    root, store, _, _, projects, project, task_preview, old = setup_case(
        tmp_path, use_reference=True
    )
    fixed_task = continue_original_task_preview(
        store, "demo", task_preview["preview_id"], expected_seq=1
    )
    fixed_project = projects.continue_original_preview(
        "demo",
        project["id"],
        old["preview_id"],
        expected_project_hash=project["project_hash"],
        expected_preview_hash=old["preview_hash"],
    )
    references = ReferenceStore(root)
    source = references.list_sources()[0]
    (references.sources_dir / (source.id + ".json")).write_text(
        source.model_copy(update={"allowed_uses": ["inspiration"]}).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="许可已撤销"):
        step_original_task_preview(
            store,
            "demo",
            fixed_task["preview_id"],
            PreviewStepInput(action_id="forbidden", expected_seq=1),
        )
    with pytest.raises(ValueError, match="许可已撤销"):
        projects.step_preview(
            "demo",
            project["id"],
            fixed_project["preview_id"],
            action_id="forbidden",
            expected_seq=1,
        )
    assert read_task_preview(store, "demo", fixed_task["preview_id"])["action_seq"] == 1


def test_application_http_readback_and_forks_use_the_formal_paths(tmp_path, monkeypatch):
    case = setup_case(tmp_path)
    root, _, task, _, _, _, preview, _ = case
    receipt, _, _ = apply_text(case)
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root)}))
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    prefix = "/projects/demo/creative/v2"
    with TestClient(create_app()) as client:
        found = client.get(f"{prefix}/tasks/{task.id}/edit-applications")
        assert found.status_code == 200 and found.json()["applications"][0]["id"] == receipt["id"]
        recovered = client.get(f"{prefix}/intent-applications/{receipt['id']}")
        assert recovered.status_code == 200 and recovered.json()["target_current"]
        fork = client.post(
            f"{prefix}/intent-applications/{receipt['id']}/continue-preview",
            json={"preview_id": preview["preview_id"], "expected_seq": 1},
        )
        assert fork.status_code == 200, fork.text
        refreshed = client.get(f"{prefix}/previews/{fork.json()['preview_id']}")
        assert refreshed.json()["text"] == "A newly specified line."
        invalid = client.post(
            f"{prefix}/intent-applications/{receipt['id']}/continue-preview",
            json={
                "preview_id": preview["preview_id"],
                "expected_seq": 1,
                "impact": {"all_branches_changed": True},
            },
        )
        assert invalid.status_code == 422


def test_fixed_preview_cannot_supply_current_feedback_even_at_matching_revision(tmp_path):
    _, store, task, artifact, _, _, preview, _ = setup_case(tmp_path)
    fixed = continue_original_task_preview(store, "demo", preview["preview_id"], expected_seq=1)
    assert fixed["revision"] == task.revision == 1
    with pytest.raises(ValueError, match="旧版隔离"):
        resolve_feedback_anchor(
            task,
            artifact,
            FeedbackAnchor(
                artifact_id=artifact.id,
                artifact_hash=artifact.content_hash,
                path=f"/tree/nodes/{fixed['cursor']}/text",
                preview_id=fixed["preview_id"],
                action_seq=1,
            ),
            store.get_preview(fixed["preview_id"]),
        )


def test_formal_export_includes_only_current_tasks_recorded_links(tmp_path, monkeypatch):
    from owcopilot.workflow import v2_service
    from owcopilot.workflow.method_trial_scope_v2 import _initialize
    from owcopilot.workflow.v2_evidence import export_v2_evidence

    case = setup_case(tmp_path)
    root, store, task, artifact, _, _, preview, _ = case
    receipt, _, _ = apply_text(case)
    new = continue_application_preview(
        store, "demo", receipt["id"], preview_id=preview["preview_id"], expected_seq=1
    )
    old = continue_original_task_preview(store, "demo", preview["preview_id"], expected_seq=1)
    # Only the source run's display lookup is controlled. Export reads the real
    # scratch tables and uses the actual persisted application/fork records.
    monkeypatch.setattr(
        v2_service,
        "v2_detail",
        lambda *_: {
            "task_id": task.id,
            "graph": {"nodes": []},
        },
    )
    missing = export_v2_evidence(store, root, "demo", artifact.run_id)["intent"]["task_links"]
    assert missing["coverage"]["method_trial_scopes"] == "not_recorded"
    assert missing["method_trial_scopes"] == []
    _initialize(store)
    with store.connection() as db:
        for scope_id, project, target_task in [
            ("own", "demo", task.id),
            ("other-task", "demo", "different-task"),
            ("other-project", "another", task.id),
        ]:
            permit = _sealed(
                {"id": scope_id, "project": project, "task_id": target_task}, "permit_hash"
            )
            db.execute(
                "INSERT INTO method_trial_scopes VALUES(?,?,?)",
                (scope_id, project, json.dumps(permit)),
            )
            for table, hash_key in [
                ("method_trial_scope_events", "event_hash"),
                ("method_trial_scope_claims", "claim_hash"),
                ("method_trial_scope_results", "receipt_hash"),
            ]:
                row = _sealed({"scope_id": scope_id, "status": "revoked"}, hash_key)
                if table == "method_trial_scope_events":
                    db.execute(
                        "INSERT INTO method_trial_scope_events(scope_id,payload) VALUES(?,?)",
                        (scope_id, json.dumps(row)),
                    )
                else:
                    db.execute(
                        f"INSERT INTO {table} VALUES(?,?,?)", (scope_id, project, json.dumps(row))
                    )
        db.execute(
            "CREATE TABLE creative_v2_stage_changes(parent_run_id TEXT,gate_hash TEXT,"
            "project TEXT,request_hash TEXT,next_run_id TEXT,payload TEXT)"
        )
        for ident, target_task in [("source", task.id), ("other", "different-task")]:
            change = _sealed(
                {
                    "project": "demo",
                    "task_id": target_task,
                    "parent_run_id": ident,
                    "next_run_id": ident + "-successor",
                    "policy_version": 2,
                },
                "receipt_hash",
            )
            db.execute(
                "INSERT INTO creative_v2_stage_changes VALUES(?,?,?,?,?,?)",
                (ident, "gate", "demo", "request", ident + "-successor", json.dumps(change)),
            )
    result = export_v2_evidence(store, root, "demo", artifact.run_id)
    links = result["intent"]["task_links"]
    assert len(links["edit_preview_forks"]) == len(links["original_preview_forks"]) == 1
    assert {row["session"]["id"] for row in links["derived_task_previews"]} == {
        new["preview_id"],
        old["preview_id"],
    }
    assert [row["id"] for row in links["method_trial_scopes"]] == ["own"]
    for key in ["method_trial_events", "method_trial_claims", "method_trial_results"]:
        assert [row["scope_id"] for row in links[key]] == ["own"]
    assert [row["parent_run_id"] for row in links["stage_changes"]] == ["source"]
    assert result["intent"]["applications"][0]["id"] == receipt["id"]
