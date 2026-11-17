"""Persistent author actions and real audit review, with isolated content and zero model calls."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import open_workflow_store
from owcopilot.audit.baseline import issue_fingerprint
from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.hash import content_hash
from owcopilot.content.models import Branch, ContentBundle, LogicVar, Quest, QuestLogic, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app
from owcopilot.workflow.models import Artifact

PREFIX = "/projects/demo/creative"


@pytest.fixture
def intent_api(tmp_path, monkeypatch):
    root = tmp_path / "content"
    quest = Quest(
        id="q",
        title="Keep the sacrifice",
        dialogue_refs=["missing_flat"],
        objective="Original objective",
        stages=[QuestStage(id="start", summary="Decide")],
        logic=QuestLogic(
            variables=[LogicVar(id="alive", default=True)],
            branches=[Branch(id="finish", from_stage="start", outcome="done")],
        ),
    )
    bundle = ContentBundle(quests={quest.id: quest})
    ContentStore(root).save(bundle)
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root), "alias": str(root)})
    )
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OWCOPILOT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        created = client.post(f"{PREFIX}/tasks", json={"brief": "Explicit author intent API check"})
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]
        store = open_workflow_store(root)
        task = store.get_task(task_id)
        run, _ = store.create_run(task)
        store.update_run(run.id, status="completed")
        payload = {"quest": quest.model_dump(mode="json")}
        baseline = Artifact(
            task_id=task_id,
            revision=1,
            run_id=run.id,
            node_id="draft_logic",
            kind="quest_with_logic",
            payload=payload,
            input_hash="explicit-fixture",
            content_hash=content_hash(payload),
        )
        store.append_artifact(baseline)
        errors = (
            AuditRunner(build_default_rule_registry())
            .run(AuditContext.from_bundle(bundle))
            .open_errors
        )
        target = next(issue for issue in errors if issue.rule_code == "MISSING_DIALOGUE_REF")
        request = {
            "expected_revision": 1,
            "baseline_artifact_id": baseline.id,
            "baseline_artifact_hash": baseline.content_hash,
            "allowed_paths": ["/quests/q/dialogue_refs"],
            "hard_locks": [{"path": "/quests/q/title"}],
            "formal_rules": [
                {"quest_id": "q", "spec": {"id": "alive", "pattern": "AG", "expression": "alive"}}
            ],
            "soft_text": ["保留人物的代价"],
            "modification_goal": "修复悬空平面对话引用",
            "target_error_fingerprints": [issue_fingerprint(target)],
        }
        yield client, root, store, task, run, baseline, request


def propose(client, task_id, body):
    response = client.post(f"{PREFIX}/tasks/{task_id}/intent-contracts", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def confirm(client, record):
    response = client.post(
        f"{PREFIX}/intent-contracts/{record['contract']['id']}:confirm",
        json={"expected_revision": 1, "expected_contract_hash": record["contract_hash"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def candidate_request(record, *, path="/quests/q/dialogue_refs", value=None):
    return {
        "expected_revision": 1,
        "expected_contract_hash": record["contract_hash"],
        "candidate": {
            "ops": [{"op": "replace", "path": path, "value": [] if value is None else value}]
        },
    }


def submit_review(client, record, body=None):
    response = client.post(
        f"{PREFIX}/intent-contracts/{record['contract']['id']}/candidate-reviews",
        json=body or candidate_request(record),
    )
    assert response.status_code == 201, response.text
    return response.json()


def decision_request(review, decision="accepted"):
    return {
        "expected_revision": 1,
        "expected_review_hash": review["review_hash"],
        "decision": decision,
    }


def test_propose_confirm_review_and_author_record_preserve_original_content_and_history(intent_api):
    client, root, store, task, run, baseline, body = intent_api
    frozen_task, frozen_artifact = (
        store.get_task(task.id).model_dump(),
        store.get_artifact(baseline.id).model_dump(),
    )
    canonical_hash = content_hash(ContentStore(root).load())
    proposed = propose(client, task.id, body)
    assert proposed["phase"] == "proposed" and proposed["contract"]["confirmed"] is False
    confirmed = confirm(client, proposed)
    assert confirmed["phase"] == "confirmed" and confirmed["contract"]["confirmed"] is True
    assert confirmed["contract"]["id"] != proposed["contract"]["id"]
    assert confirmed["contract"]["version"] == 2
    assert confirmed["contract"]["supersedes_contract_id"] == proposed["contract"]["id"]
    review = submit_review(client, confirmed)
    assert review["report"]["status"] == "ready_for_author"
    assert review["report"]["resolved_errors"] == body["target_error_fingerprints"]
    assert review["report"]["formal_results"][0]["after"]["status"] == "proven"
    assert review["report"]["goal_status"] == "withheld"
    assert review["report"]["soft_results"][0]["status"] == "unknown"
    assert content_hash(review["candidate"]) == review["candidate_hash"]
    assert content_hash(review["report"]["candidate_bundle"]) == review["report"]["after_hash"]
    endpoint = f"{PREFIX}/intent-reviews/{review['id']}/author-decision"
    accepted = client.post(endpoint, json=decision_request(review))
    assert accepted.status_code == 201, accepted.text
    recorded = accepted.json()
    assert recorded["status"] == "accepted_for_review_record"
    assert recorded["action"] == "explicit_author_api_action"
    assert recorded["writes_canon"] is recorded["publishes_main_candidate"] is False
    assert recorded["changes_task_revision"] is recorded["changes_method_acceptance"] is False
    assert recorded["goal_status"] == "withheld"
    assert client.post(endpoint, json=decision_request(review)).json() == recorded
    assert client.post(endpoint, json=decision_request(review, "rejected")).status_code == 409
    fetched = client.get(f"{PREFIX}/intent-reviews/{review['id']}").json()
    assert fetched["author_decision"]["decision_hash"] == recorded["decision_hash"]
    assert client.get(f"{PREFIX}/intent-contracts/{proposed['contract']['id']}").json() == proposed
    assert len(client.get(f"{PREFIX}/tasks/{task.id}/intent-contracts").json()["contracts"]) == 2
    assert content_hash(ContentStore(root).load()) == canonical_hash
    assert store.get_task(task.id).model_dump() == frozen_task
    assert store.get_artifact(baseline.id).model_dump() == frozen_artifact
    assert len(store.artifacts(task.id)) == 1
    assert store.workflows() == [] and store.model_calls(run.id) == []


def test_unconfirmed_false_bindings_out_of_scope_and_missing_targets_cannot_be_accepted(intent_api):
    client, _, _, task, _, _, body = intent_api
    endpoint = f"{PREFIX}/tasks/{task.id}/intent-contracts"
    assert client.post(endpoint, json={**body, "confirmed": True}).status_code == 422
    assert (
        client.post(endpoint, json={**body, "baseline_artifact_hash": "0" * 64}).status_code == 409
    )
    assert (
        client.post(endpoint, json={**body, "allowed_paths": ["/quests/q/absent"]}).status_code
        == 409
    )
    assert (
        client.post(
            endpoint, json={**body, "target_error_fingerprints": [], "modification_goal": ""}
        ).status_code
        == 422
    )
    proposed = propose(client, task.id, body)
    assert (
        client.post(
            f"{PREFIX}/intent-contracts/{proposed['contract']['id']}/candidate-reviews",
            json=candidate_request(proposed),
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{PREFIX}/intent-contracts/{proposed['contract']['id']}:confirm",
            json={"expected_revision": 1, "expected_contract_hash": "0" * 64},
        ).status_code
        == 409
    )
    confirmed = confirm(client, proposed)
    blocked = submit_review(
        client,
        confirmed,
        candidate_request(confirmed, path="/quests/q/title", value="Revive everyone"),
    )
    assert blocked["report"]["status"] == "blocked"
    assert (
        client.post(
            f"{PREFIX}/intent-reviews/{blocked['id']}/author-decision",
            json=decision_request(blocked),
        ).status_code
        == 409
    )
    rejected = client.post(
        f"{PREFIX}/intent-reviews/{blocked['id']}/author-decision",
        json=decision_request(blocked, "rejected"),
    )
    assert rejected.status_code == 201
    assert rejected.json()["status"] == "rejected_for_review_record"
    changed = {
        **body,
        "supersedes_contract_id": confirmed["contract"]["id"],
        "expected_parent_hash": confirmed["contract_hash"],
        "change_reason": "Inspect another target",
        "target_error_fingerprints": ["missing-real-target"],
    }
    revised = confirm(client, propose(client, task.id, changed))
    absent_target = submit_review(client, revised)
    assert absent_target["report"]["status"] == "blocked"
    assert any("目标错误不在基线" in reason for reason in absent_target["report"]["reasons"])
    assert (
        client.post(
            f"{PREFIX}/intent-reviews/{absent_target['id']}/author-decision",
            json=decision_request(absent_target),
        ).status_code
        == 409
    )


def test_new_author_intent_appends_versions_and_rejects_stale_parent_scope_and_task(intent_api):
    client, _, store, task, _, _, body = intent_api
    proposed = propose(client, task.id, body)
    confirmed = confirm(client, proposed)
    change = {
        **deepcopy(body),
        "supersedes_contract_id": confirmed["contract"]["id"],
        "expected_parent_hash": confirmed["contract_hash"],
        "allowed_paths": ["/quests/q/objective"],
        "modification_goal": "允许重写目标表达",
        "target_error_fingerprints": [],
    }
    endpoint = f"{PREFIX}/tasks/{task.id}/intent-contracts"
    assert client.post(endpoint, json=change).status_code == 422
    change["change_reason"] = "Author explicitly changes the desired objective"
    assert (
        client.post(endpoint, json={**change, "expected_parent_hash": "0" * 64}).status_code == 409
    )
    next_proposal = propose(client, task.id, change)
    assert (
        next_proposal["contract"]["version"] == 3
        and next_proposal["contract"]["confirmed"] is False
    )
    active_review = submit_review(client, confirmed)
    assert active_review["report"]["status"] == "ready_for_author"
    assert (
        client.post(
            f"{PREFIX}/intent-reviews/{active_review['id']}/author-decision",
            json=decision_request(active_review),
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"{PREFIX}/intent-contracts/{proposed['contract']['id']}:confirm",
            json={"expected_revision": 1, "expected_contract_hash": proposed["contract_hash"]},
        ).status_code
        == 409
    )
    assert (
        client.get(
            f"/projects/alias/creative/intent-contracts/{confirmed['contract']['id']}"
        ).status_code
        == 404
    )
    latest = confirm(client, next_proposal)
    assert (
        client.post(
            f"{PREFIX}/intent-contracts/{confirmed['contract']['id']}/candidate-reviews",
            json=candidate_request(confirmed),
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{PREFIX}/intent-reviews/{active_review['id']}/author-decision",
            json=decision_request(active_review),
        ).status_code
        == 409
    )
    assert (
        client.get(f"{PREFIX}/intent-reviews/{active_review['id']}").json()["author_decision"][
            "decision"
        ]
        == "accepted"
    )
    chain = client.get(f"{PREFIX}/tasks/{task.id}/intent-contracts").json()["contracts"]
    assert [record["contract"]["version"] for record in chain] == [1, 2, 3, 4]
    assert chain[0] == proposed and chain[1] == confirmed and chain[2] == next_proposal
    goal_review = submit_review(
        client,
        latest,
        candidate_request(latest, path="/quests/q/objective", value="A revised objective"),
    )
    assert goal_review["report"]["status"] == "ready_for_author"
    assert goal_review["report"]["goal_status"] == "withheld"
    assert (
        client.post(
            f"{PREFIX}/intent-reviews/{goal_review['id']}/author-decision",
            json={**decision_request(goal_review), "expected_review_hash": "0" * 64},
        ).status_code
        == 409
    )
    store.save_task(task.model_copy(update={"revision": 2}), expected_revision=1)
    assert (
        client.post(
            f"{PREFIX}/intent-reviews/{goal_review['id']}/author-decision",
            json=decision_request(goal_review),
        ).status_code
        == 409
    )
    assert (
        client.get(f"/projects/alias/creative/intent-reviews/{goal_review['id']}").status_code
        == 404
    )
