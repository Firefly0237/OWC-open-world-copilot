"""Local proposals retain exact provenance after their own scene publication."""

import json
from copy import deepcopy

import pytest

from owcopilot.app.actions.intent_workflow import (
    IntentConfirmInput,
    IntentContractInput,
    confirm_intent_contract_action,
    create_intent_contract_action,
)
from owcopilot.inspiration.store import ReferenceStore
from owcopilot.workflow.edit_continuity import check_patch_narrative_snapshot
from owcopilot.workflow.intent_proposal_v2 import propose_intent_patch
from owcopilot.workflow.intent_store import IntentStore
from owcopilot.workflow.project_v2 import ProjectOutline, _seal
from owcopilot.workflow.v2_service import check_narrative_snapshot
from tests.platform.test_edit_continuity import apply_text, setup_case


def published_case(tmp_path, *, use_reference=False):
    case = setup_case(tmp_path, use_reference=use_reference)
    _, store, task, artifact, _, _, _, _ = case
    with store.connection() as db:
        state = json.loads(
            db.execute(
                "SELECT payload FROM creative_v2_runs WHERE run_id=?", (artifact.run_id,)
            ).fetchone()[0]
        )
        state.update(
            project="demo",
            task_id=task.id,
            run_id=artifact.run_id,
            revision=1,
            feedback_source={
                "artifact_id": artifact.id,
                "artifact_hash": artifact.content_hash,
                "kind": artifact.kind,
            },
        )
        db.execute(
            "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
            (json.dumps(state), artifact.run_id),
        )
    return case, state


def contract_for(case, artifact=None):
    root, store, task, original, _, _, preview, _ = case
    artifact = artifact or original
    task = store.get_task(task.id)
    path = f"/dialogue_trees/tree/nodes/{preview['cursor']}/text"
    existing = IntentStore(store).list_contracts("demo", task.id)
    parent = existing[-1] if existing else None
    draft = create_intent_contract_action(
        root,
        "demo",
        task.id,
        IntentContractInput(
            expected_revision=task.revision,
            baseline_artifact_id=artifact.id,
            baseline_artifact_hash=artifact.content_hash,
            allowed_paths=[path],
            modification_goal="Controlled development input: change only this line",
            supersedes_contract_id=parent["contract"]["id"] if parent else None,
            expected_parent_hash=parent["contract_hash"] if parent else None,
            change_reason="Controlled successor input" if parent else "",
        ),
    )
    confirmed = confirm_intent_contract_action(
        root,
        "demo",
        draft["contract"]["id"],
        IntentConfirmInput(
            expected_revision=task.revision,
            expected_contract_hash=draft["contract_hash"],
            actor_kind="development_operator",
        ),
    )
    return confirmed, path


class ControlledPatchTransport:
    model = "controlled-patch-transport"
    max_retries = 0

    def __init__(self, path, callback=None):
        self.path, self.callback, self.calls = path, callback, 0

    def complete(self, *, system, user, model):
        self.calls += 1
        if self.callback:
            self.callback()
        return (
            json.dumps(
                {
                    "ops": [
                        {"op": "replace", "path": self.path, "value": "A controlled new line."}
                    ],
                }
            ),
            10,
            8,
            0,
            self.model,
        )


def propose(case, provider, confirmed):
    root, store, task, _, _, _, _, _ = case
    return propose_intent_patch(
        store,
        root,
        "demo",
        confirmed["contract"]["id"],
        expected_revision=store.get_task(task.id).revision,
        expected_contract_hash=confirmed["contract_hash"],
        provider=provider,
    )


def test_published_artifact_can_propose_without_weakening_global_guard(tmp_path):
    case, state = published_case(tmp_path)
    _, store, task, artifact, _, project, _, _ = case
    with pytest.raises(ValueError, match="场景版本已变化"):
        check_narrative_snapshot(store, "demo", state)
    proof = check_patch_narrative_snapshot(store, "demo", state, task, artifact)
    assert proof["project_hash"] == project["project_hash"]
    assert proof["source_artifact_id"] == artifact.id and proof["application_ids"] == []
    confirmed, path = contract_for(case)
    provider = ControlledPatchTransport(path)
    review = propose(case, provider, confirmed)
    assert propose(case, provider, confirmed) == review and provider.calls == 1
    assert review["report"]["status"] == "ready_for_author"
    assert review["author_acceptance"] == "unknown" and review["applied"] is False
    assert store.get_task(task.id).revision == 1
    assert store.get_artifact(artifact.id).payload == artifact.payload
    with store.connection() as db:
        saved = json.loads(
            db.execute("SELECT payload FROM creative_intent_ai_proposals").fetchone()[0]
        )
        assert saved["narrative_patch_scope"] == proof
        assert db.execute("SELECT COUNT(*) FROM creative_intent_decisions").fetchone()[0] == 0


def test_text_application_and_explicit_rebinding_keep_local_proposal_scope(tmp_path):
    case, state = published_case(tmp_path)
    _, store, task, original, projects, project, _, _ = case
    receipt, _, _ = apply_text(case)
    target = store.get_artifact(receipt["artifact_id"])
    with pytest.raises(ValueError, match="同一基线"):
        check_patch_narrative_snapshot(store, "demo", state, store.get_task(task.id), target)
    project = projects.bind_scene_artifact(
        "demo",
        project["id"],
        "s",
        target.id,
        expected_project_hash=project["project_hash"],
        expected_artifact_hash=target.content_hash,
    )
    proof = check_patch_narrative_snapshot(store, "demo", state, store.get_task(task.id), target)
    assert proof["source_artifact_id"] == original.id
    assert proof["application_ids"] == [receipt["id"]] and proof["publication_versions"] == 2
    confirmed, path = contract_for(case, target)
    provider = ControlledPatchTransport(path)
    review = propose(case, provider, confirmed)
    assert provider.calls == 1 and review["report"]["goal_status"] == "withheld"
    assert projects.get("demo", project["id"]) == project


@pytest.mark.parametrize("change", ["outline", "restored_description", "empty_version", "scope"])
def test_other_project_history_never_uses_published_artifact_exception(tmp_path, change):
    case, state = published_case(tmp_path)
    _, store, task, artifact, projects, project, _, _ = case
    if change == "outline":
        outline = deepcopy(project["outline"])
        outline["scenes"][0]["brief"] = "A different creative input"
        projects.save_outline(
            "demo",
            project["id"],
            ProjectOutline.model_validate(outline),
            expected_project_hash=project["project_hash"],
        )
    else:
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if change == "restored_description":
                changed = projects._update(db, project, description="Temporary changed input")
                projects._update(db, changed, description=project["description"])
            elif change == "scope":
                binding = deepcopy(project["scene_bindings"]["s"])
                binding["generation_scope"]["branch_history"] = ["invented"]
                binding = _seal(
                    {k: v for k, v in binding.items() if k != "binding_hash"}, "binding_hash"
                )
                projects._update(db, project, scene_bindings={"s": binding})
            else:
                projects._update(db, project)
    confirmed, path = contract_for(case)
    provider = ControlledPatchTransport(path)
    with pytest.raises(ValueError):
        propose(case, provider, confirmed)
    assert provider.calls == 0
    assert store.get_task(task.id).revision == 1
    assert store.get_artifact(artifact.id).content_hash == artifact.content_hash


def test_current_source_permission_is_still_required_before_proposal(tmp_path):
    case, _ = published_case(tmp_path, use_reference=True)
    root, _, _, _, _, _, _, _ = case
    confirmed, path = contract_for(case)
    provider = ControlledPatchTransport(path)
    references = ReferenceStore(root)
    source = references.list_sources()[0]
    (references.sources_dir / (source.id + ".json")).write_text(
        source.model_copy(update={"allowed_uses": ["inspiration"]}).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="许可已撤销"):
        propose(case, provider, confirmed)
    assert provider.calls == 0


def test_project_changes_during_transport_discard_candidate_without_retry(tmp_path):
    case, _ = published_case(tmp_path)
    _, store, _, _, projects, project, _, _ = case
    confirmed, path = contract_for(case)

    def change_project():
        with store.connection() as db:
            projects._update(db, project, description="Changed during the remote request")

    provider = ControlledPatchTransport(path, callback=change_project)
    with pytest.raises(ValueError):
        propose(case, provider, confirmed)
    assert provider.calls == 1
    with store.connection() as db:
        saved = json.loads(
            db.execute("SELECT payload FROM creative_intent_ai_proposals").fetchone()[0]
        )
        assert saved["status"] == "discarded"
        assert db.execute("SELECT COUNT(*) FROM creative_intent_reviews").fetchone()[0] == 0


def test_missing_real_publication_evidence_or_world_base_is_rejected(tmp_path):
    case, state = published_case(tmp_path)
    _, store, task, artifact, _, _, _, _ = case
    missing = {**state, "feedback_source": None}
    with pytest.raises(ValueError, match="实际发布产物"):
        check_patch_narrative_snapshot(store, "demo", missing, task, artifact)
    wrong_base = task.model_copy(update={"base_hash": "0" * 64})
    with pytest.raises(ValueError):
        check_patch_narrative_snapshot(store, "demo", state, wrong_base, artifact)
