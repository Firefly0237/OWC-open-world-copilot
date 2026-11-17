"""Real graph execution and scoped admission with controlled narrative transport."""

import pytest
from fastapi.testclient import TestClient

from owcopilot.service.api import create_app
from owcopilot.service.routers import creative_v2
from owcopilot.workflow import v2_service, v2_skills
from owcopilot.workflow.evolution import ExecutionEvidence
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.method_trial_scope_v2 import (
    grant_trial_scope,
    revoke_trial_scope,
    task_trial_scopes,
)
from tests.platform import test_creator_production_v2 as fixtures

studio = fixtures.studio


def candidate_case(root, store):
    task = fixtures.task_for(root, store)
    skills = v2_skills.NarrativeEvolutionStore(store)
    family = skills.create_family("demo", "Controlled method candidate")
    parent = skills.add_version(
        "demo", family["id"], graph=create_narrative_graph("baseline").model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
    )
    version = skills.add_version(
        "demo", family["id"], graph=create_narrative_graph("constrained").model_dump(mode="json"),
        parent_version_id=parent["id"],
        expected_family_hash=skills.get_family("demo", family["id"])["family_hash"],
    )
    skills.authorize_trial(
        "demo", version["id"],
        expected_family_hash=skills.get_family("demo", family["id"])["family_hash"],
        allowed=True, authority="development_operator", note="Controlled engineering input",
    )
    return task, skills, version


def grant(store, task, version, operation="grant-once"):
    return grant_trial_scope(
        store, "demo", task.id, [version["id"]], machine_requirements=fixtures.MACHINE,
        authority="development_technical_trial", reason="Controlled candidate execution",
        operation_id=operation,
    )


def test_candidate_is_actually_consumed_and_revoke_blocks_dispatch(studio):
    root, store = studio
    task, skills, version = candidate_case(root, store)
    with pytest.raises(ValueError, match="本任务"):
        v2_service.prepare_skill_run(
            store, root, task, version["id"], machine_requirements=fixtures.MACHINE
        )
    permit = grant(store, task, version)
    prepared = v2_service.prepare_skill_run(
        store, root, task, version["id"], machine_requirements=fixtures.MACHINE,
        trial_scope_id=permit["id"], expected_trial_scope_hash=permit["permit_hash"],
    )
    state = v2_service.execute_v2_run(
        store, root, "demo", prepared["run_id"], provider=fixtures.ControlledProductionProvider()
    )
    assert state["run"]["status"] == "completed", state
    assert state["skill_version_id"] == version["id"]
    assert state["method_trial_scope_id"] == permit["id"]
    assert state["result"]["outcomes"]["decision_gate"]["status"] == "succeeded"
    assert "contract" in state["result"]["outcomes"]["dialogue"]["input_refs"]
    assert skills.get_version("demo", version["id"])["state"]["author_acceptance"] == "unknown"
    assert skills.get_family("demo", version["family_id"])["default_version_id"] is None
    revoke_trial_scope(
        store, "demo", permit["id"], expected_permit_hash=permit["permit_hash"],
        reason="Withdraw this task permit",
    )
    with pytest.raises(ValueError, match="撤销"):
        v2_skills.check_run_authorization(store, "demo", state)


def test_duplicate_grants_share_one_revocable_range_and_cannot_cross_tasks(studio):
    root, store = studio
    task, _, version = candidate_case(root, store)
    permit = grant(store, task, version)
    assert grant(store, task, version) == permit
    assert grant(store, task, version, "second-window") == permit
    assert len(task_trial_scopes(store, "demo", task.id)) == 1
    other = fixtures.task_for(root, store)
    with pytest.raises(ValueError, match="不覆盖"):
        v2_service.prepare_skill_run(
            store, root, other, version["id"], machine_requirements=fixtures.MACHINE,
            trial_scope_id=permit["id"], expected_trial_scope_hash=permit["permit_hash"],
        )
    revised = task.model_copy(update={"revision": task.revision + 1})
    store.save_task(revised, expected_revision=task.revision)
    with pytest.raises(ValueError, match="过期"):
        v2_service.prepare_skill_run(
            store, root, revised, version["id"], machine_requirements=fixtures.MACHINE,
            trial_scope_id=permit["id"], expected_trial_scope_hash=permit["permit_hash"],
        )


def test_both_http_production_entries_reject_unscoped_candidate(studio, monkeypatch):
    root, store = studio
    task, _, version = candidate_case(root, store)
    monkeypatch.setattr(creative_v2, "_require_real_allowed", lambda request: None)
    with TestClient(create_app()) as client:
        for endpoint in ("runs", "production-jobs"):
            response = client.post(
                f"/projects/demo/creative/v2/tasks/{task.id}/{endpoint}",
                json={"skill_version_id": version["id"], "machine_requirements": fixtures.MACHINE},
            )
            assert response.status_code == 409, response.text
        response = client.post(
            f"/projects/demo/creative/v2/tasks/{task.id}/trial-scopes",
            json={
                "expected_revision": task.revision, "version_ids": [version["id"]],
                "machine_requirements": fixtures.MACHINE, "operation_id": "http-grant",
                "reason": "Controlled HTTP permission", "actor_kind": "development_operator",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["permit"]["author_acceptance"] == "unknown"


def test_candidate_cannot_claim_default_identity_without_accepted_selection(studio):
    root, store = studio
    task, _, version = candidate_case(root, store)
    prepared = v2_service.prepare_v2_run(
        store, root, task, version["graph"], skill_version_id=version["id"],
        skill_authorization={"selection": "default", "state_hash": version["state"]["state_hash"]},
        machine_requirements=fixtures.MACHINE,
    )
    provider = fixtures.ControlledProductionProvider()
    result = v2_service.execute_v2_run(
        store, root, "demo", prepared["run_id"], provider=provider
    )
    assert result["run"]["status"] == "failed"
    assert "默认流程缺少" in result["run"]["error"]
    assert provider.calls == []


def test_scope_cannot_expand_requirements_or_restart_an_unscoped_legacy_run(studio):
    root, store = studio
    task, _, version = candidate_case(root, store)
    permit = grant(store, task, version)
    with pytest.raises(ValueError, match="不覆盖"):
        v2_skills.assert_skill_admission(
            store, task, version["id"],
            machine_requirements={**fixtures.MACHINE, "decision_count": 2},
            trial_scope_id=permit["id"], expected_trial_scope_hash=permit["permit_hash"],
        )
    old = v2_service.prepare_v2_run(
        store, root, task, version["graph"], skill_version_id=version["id"],
        skill_authorization={"selection": "trial", "state_hash": version["state"]["state_hash"]},
        machine_requirements=fixtures.MACHINE,
    )
    provider = fixtures.ControlledProductionProvider()
    result = v2_service.execute_v2_run(
        store, root, "demo", old["run_id"], provider=provider
    )
    assert result["run"]["status"] == "failed" and "缺少绑定本任务" in result["run"]["error"]
    assert provider.calls == []


def test_default_change_affects_new_selection_but_keeps_existing_run_authorization(studio):
    root, store = studio
    task, skills, version = candidate_case(root, store)
    permit = grant(store, task, version)
    source = v2_service.prepare_skill_run(
        store, root, task, version["id"], machine_requirements=fixtures.MACHINE,
        trial_scope_id=permit["id"], expected_trial_scope_hash=permit["permit_hash"],
    )
    completed = v2_service.execute_v2_run(
        store, root, "demo", source["run_id"], provider=fixtures.ControlledProductionProvider()
    )
    assert completed["run"]["status"] == "completed"
    family_id = version["family_id"]
    skills.decide_version(
        "demo", version["id"],
        expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
        decision="accepted", make_default=True,
        note="Controlled protocol state, not a real author evaluation",
    )
    next_task = fixtures.task_for(root, store)
    existing = v2_service.prepare_skill_run(
        store, root, next_task, version["id"], machine_requirements=fixtures.MACHINE,
    )
    assert existing["method_trial_scope_id"] is None
    replacement = skills.add_version(
        "demo", family_id, graph=version["graph"], parent_version_id=version["id"],
        description="Same execution graph with a revised scope description; controlled input",
        expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
    )
    # This version has the identical execution graph. Reuse actual engineering
    # evidence for that graph; no new narrative or user judgement is synthesized.
    assert replacement["execution_graph_hash"] == completed["execution_graph_hash"]
    skills.record_evaluation(
        "demo", replacement["id"],
        ExecutionEvidence(
            run_id=source["run_id"], graph_hash=completed["execution_graph_hash"],
            input_hash=completed["requirements"]["contract_hash"], status="completed",
            consumed_artifact_hashes=[
                row["content_hash"] for row in completed["result"]["artifacts"].values()
            ],
            checks={"same_execution_graph": True},
            source_kind="controlled_development",
        ),
    )
    skills.decide_version(
        "demo", replacement["id"],
        expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
        decision="accepted", make_default=True,
        note="Controlled replacement state, not a real author evaluation",
    )
    v2_skills.check_run_authorization(store, "demo", existing)
    final_task = fixtures.task_for(root, store)
    with pytest.raises(ValueError, match="试用或默认采用许可"):
        v2_skills.assert_skill_admission(
            store, final_task, version["id"], machine_requirements=fixtures.MACHINE
        )
    selected = v2_service.prepare_skill_run(
        store, root, final_task, replacement["id"], machine_requirements=fixtures.MACHINE
    )
    assert selected["skill_authorization"]["default_version_id_at_selection"] == replacement["id"]
