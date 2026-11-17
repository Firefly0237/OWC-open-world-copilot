"""Real checkpoint and successor behavior using explicitly controlled transport."""

import json

import pytest
from fastapi.testclient import TestClient

from owcopilot.content.hash import content_hash
from owcopilot.service.api import create_app
from owcopilot.service.routers import creative_v2
from owcopilot.workflow import v2_service
from owcopilot.workflow.collaboration_v2 import CollaborationStore
from owcopilot.workflow.harness import HarnessStore
from tests.platform.test_creator_production_v2 import (
    PREFIX,
    ControlledProductionProvider,
    prepare,
    task_for,
    wait_job,
)
from tests.platform.test_creator_production_v2 import (
    studio as studio,
)


def paused(studio):
    root, store = studio
    task = task_for(root, store, "direction")
    provider = ControlledProductionProvider()
    source = prepare(root, store, task)
    waiting = v2_service.execute_v2_run(store, root, "demo", source["run_id"], provider=provider)
    assert waiting["run"]["status"] == "waiting_user"
    return root, store, task, provider, waiting


def change(root, store, source, policy, *, guidance=None, gate_hash=None):
    gate = source["collaboration"]["pending_gate"]
    return v2_service.revise_stage_run(
        store,
        root,
        "demo",
        source["run_id"],
        expected_gate_hash=gate_hash or gate["gate_hash"],
        expected_policy_hash=source["collaboration"]["policy_hash"],
        collaboration_policy=policy,
        guidance=guidance or {},
        actor_kind="development_operator",
    )


def test_policy_change_reuses_matching_outputs_and_denied_role_cannot_continue(studio):
    root, store, task, provider, first = paused(studio)
    policy = {
        "mode": "delegate",
        "allowed_operations": [
            node["operation"]
            for node in first["graph"]["nodes"]
            if node["operation"] != "logic_contract"
        ],
    }
    successor = change(root, store, first, policy)
    assert successor["graph_hash"] == first["graph_hash"]
    assert successor["collaboration"]["policy_version"] == 2
    assert successor["collaboration"]["reviewed_nodes"] == []
    assert successor["stage_change"]["actor_kind"] == "development_operator"
    result = v2_service.execute_v2_run(store, root, "demo", successor["run_id"], provider=provider)
    assert result["result"]["outcomes"]["plan"]["status"] == "reused"
    assert result["run"]["status"] == "waiting_user"
    gate = result["collaboration"]["pending_gate"]
    assert gate["gate"]["permission_required"] is True and gate["node_id"] == "logic"
    assert [row["user"]["requirements"]["operation"] for row in provider.calls] == [
        "plan_events",
        "draft_quest",
    ]
    with pytest.raises(ValueError, match="未获执行许可"):
        CollaborationStore(store).authorize_resume(
            "demo",
            result["run_id"],
            expected_gate_hash=gate["gate_hash"],
            actor_kind="development_operator",
        )
    final = change(root, store, result, {"mode": "delegate"})
    finished = v2_service.execute_v2_run(store, root, "demo", final["run_id"], provider=provider)
    assert finished["run"]["status"] == "completed", finished
    assert finished["result"]["outcomes"]["quest"]["status"] == "reused"
    assert len(provider.calls) == 4
    original = v2_service.v2_detail(store, "demo", first["run_id"])
    assert original["run"]["status"] == "cancelled"
    assert original["collaboration"]["history"][-1]["status"] == "superseded"
    assert original["collaboration"]["history"][-1]["decision"] is None
    assert store.get_task(task.id).revision == 1


def test_policy_change_receipt_is_unique_and_queue_recovery_uses_same_run(studio, monkeypatch):
    root, store, _, provider, first = paused(studio)
    policy = {"mode": "delegate"}
    prepared = change(root, store, first, policy)
    assert prepared["run"]["status"] == "queued" and prepared["run"]["job_id"] is None
    monkeypatch.setattr(creative_v2, "_require_real_allowed", lambda request: None)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **kwargs: provider)
    gate = first["collaboration"]["pending_gate"]
    payload = {
        "action": "revise",
        "expected_gate_hash": gate["gate_hash"],
        "expected_policy_hash": first["collaboration"]["policy_hash"],
        "collaboration_policy": policy,
        "actor_kind": "development_operator",
    }
    with TestClient(create_app()) as client:
        recovered = client.post(f"{PREFIX}/runs/{first['run_id']}/resume", json=payload)
        assert recovered.status_code == 202, recovered.text
        assert recovered.json()["run_id"] == prepared["run_id"]
        wait_job(client.app.state.jobs_manager, recovered.json()["job_id"])
        calls = len(provider.calls)
        replay = client.post(f"{PREFIX}/runs/{first['run_id']}/resume", json=payload)
        assert replay.status_code == 202 and replay.json()["run_id"] == prepared["run_id"]
        assert len(provider.calls) == calls
        conflict = client.post(
            f"{PREFIX}/runs/{first['run_id']}/resume",
            json={**payload, "collaboration_policy": {"mode": "edit"}},
        )
        assert conflict.status_code == 409
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM creative_v2_stage_changes").fetchone()[0] == 1


def test_changed_guidance_invalidates_its_actual_inputs_preserving_owner(studio):
    root, store, _, provider, first = paused(studio)
    revised = change(
        root,
        store,
        first,
        {"mode": "direction"},
        guidance={"plan": "Make the obligation clear before the messenger speaks"},
    )
    result = v2_service.execute_v2_run(store, root, "demo", revised["run_id"], provider=provider)
    assert result["run"]["status"] == "waiting_user"
    assert result["result"]["outcomes"]["plan"]["status"] == "succeeded"
    assert result["stage_change"]["affected_nodes"] == sorted(
        node["id"] for node in first["graph"]["nodes"]
    )
    before = HarnessStore(store).requests(first["run_id"])[0]
    after = HarnessStore(store).requests(revised["run_id"])[0]
    assert before["budget_id"] == after["budget_id"]
    assert [before["attempt"], after["attempt"]] == [1, 2]


def test_stale_task_or_policy_does_not_create_a_successor(studio):
    root, store, task, _, first = paused(studio)
    gate = first["collaboration"]["pending_gate"]
    with pytest.raises(ValueError, match="约定版本"):
        v2_service.revise_stage_run(
            store,
            root,
            "demo",
            first["run_id"],
            expected_gate_hash=gate["gate_hash"],
            expected_policy_hash="0" * 64,
            collaboration_policy={"mode": "delegate"},
            guidance={},
        )
    task.revision += 1
    store.save_task(task)
    with pytest.raises(ValueError, match="当前版本"):
        change(root, store, first, {"mode": "delegate"})
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM creative_v2_stage_changes").fetchone()[0] == 0


def test_revoked_method_identity_is_checked_before_deriving_another_run(studio, monkeypatch):
    from owcopilot.workflow import v2_skills

    root, store, _, _, first = paused(studio)

    def revoked(*args):
        raise ValueError("本次方法试用许可已撤销")

    monkeypatch.setattr(v2_skills, "check_run_authorization", revoked)
    with pytest.raises(ValueError, match="撤销"):
        change(root, store, first, {"mode": "delegate"})
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM creative_v2_stage_changes").fetchone()[0] == 0


def test_legacy_policy_hash_is_not_rewritten_by_stage_projection(studio):
    root, store, task, _, first = paused(studio)
    controls = CollaborationStore(store)
    detail = controls.detail("demo", first["run_id"])
    legacy = {
        key: value
        for key, value in detail["policy"].items()
        if key not in {"allowed_operations", "review_operations"}
    }
    with store.connection() as db:
        state = json.loads(
            db.execute(
                "SELECT payload FROM creative_v2_collaboration WHERE run_id=?", (first["run_id"],)
            ).fetchone()[0]
        )
        state.update(policy=legacy, policy_hash=content_hash(legacy))
        db.execute(
            "UPDATE creative_v2_collaboration SET payload=? WHERE run_id=?",
            (json.dumps(state), first["run_id"]),
        )
    projected = controls.detail("demo", first["run_id"])
    assert projected["policy_hash"] == content_hash(legacy)
    assert {row["id"] for row in projected["stage_agreements"]} == {
        "planning",
        "mechanics",
        "dialogue",
        "checks",
        "methods",
    }


def test_system_checks_execute_despite_legacy_author_permission_exclusions(studio):
    root, store, _, provider, source = paused(studio)
    policy = {
        "mode": "delegate",
        "allowed_operations": [
            node["operation"]
            for node in source["graph"]["nodes"]
            if node["operation"] != "coverage_audit"
        ],
    }
    successor = change(root, store, source, policy)
    result = v2_service.execute_v2_run(store, root, "demo", successor["run_id"], provider=provider)
    assert result["run"]["status"] == "completed", result
    assert result["result"]["outcomes"]["audit"]["status"] == "succeeded"
    assert "report" in result["result"]["outputs"]
    checks = next(
        row for row in result["collaboration"]["stage_agreements"] if row["id"] == "checks"
    )
    assert all(
        row["allowed"] and row["system_required"] and not row["review_before"]
        for row in checks["operations"]
    )


def test_author_stage_guidance_cannot_rewrite_system_checks(studio):
    root, store, _, _, source = paused(studio)
    audit = next(
        node["id"] for node in source["graph"]["nodes"] if node["operation"] == "coverage_audit"
    )
    with pytest.raises(ValueError, match="系统检查"):
        change(root, store, source, {"mode": "delegate"}, guidance={audit: "Skip the check"})
