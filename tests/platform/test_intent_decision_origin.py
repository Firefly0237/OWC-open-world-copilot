"""Declared operator provenance stays distinct from content and method acceptance."""

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.store import ContentStore
from owcopilot.workflow.intent_store import IntentStore
from tests.platform.test_workflow_intent_api import (
    PREFIX,
    candidate_request,
    confirm,
    decision_request,
    propose,
    submit_review,
)
from tests.platform.test_workflow_intent_api import (
    intent_api as intent_api,
)


def _confirm_as(client, proposed, actor):
    return client.post(
        f"{PREFIX}/intent-contracts/{proposed['contract']['id']}:confirm",
        json={
            "expected_revision": 1,
            "expected_contract_hash": proposed["contract_hash"],
            "actor_kind": actor,
        },
    )


@pytest.mark.parametrize("choice", ["accepted", "rejected"])
def test_development_origin_is_sealed_and_cannot_be_replayed_as_author(intent_api, choice):
    client, root, store, task, run, baseline, body = intent_api
    before = content_hash(ContentStore(root).load())
    proposed = propose(client, task.id, body)
    confirmation = _confirm_as(client, proposed, "development_operator")
    assert confirmation.status_code == 201, confirmation.text
    confirmed = confirmation.json()
    assert confirmed["actor_kind"] == "development_operator"
    assert confirmed["action"] == "explicit_development_operator_confirmation"
    assert "actor_kind" not in confirmed["contract"]
    assert _confirm_as(client, proposed, "development_operator").status_code == 409
    review = submit_review(client, confirmed)
    endpoint = f"{PREFIX}/intent-reviews/{review['id']}/author-decision"
    request = {**decision_request(review, choice), "actor_kind": "development_operator"}
    response = client.post(endpoint, json=request)
    assert response.status_code == 201, response.text
    decision = response.json()
    assert decision["actor_kind"] == "development_operator"
    assert decision["action"] == "explicit_development_operator_api_action"
    assert decision["decision"] == choice and decision["goal_status"] == "withheld"
    assert decision["changes_method_acceptance"] is False
    assert client.post(endpoint, json=request).json() == decision
    assert client.post(endpoint, json=decision_request(review, choice)).status_code == 409
    assert client.get(f"{PREFIX}/intent-reviews/{review['id']}").json()[
        "author_decision"
    ] == decision
    assert client.get(f"{PREFIX}/intent-contracts/{proposed['contract']['id']}").json() == proposed
    assert content_hash(ContentStore(root).load()) == before
    assert store.get_task(task.id).revision == 1
    assert store.get_artifact(baseline.id).content_hash == baseline.content_hash
    assert len(store.artifacts(task.id)) == 1 and store.model_calls(run.id) == []


def test_operator_origin_does_not_bypass_review_or_accept_invalid_actor(intent_api):
    client, _, store, task, _, _, body = intent_api
    proposed = propose(client, task.id, body)
    assert _confirm_as(client, proposed, "system").status_code == 422
    confirmed = _confirm_as(client, proposed, "development_operator").json()
    blocked = submit_review(
        client,
        confirmed,
        candidate_request(confirmed, path="/quests/q/title", value="Changed without permission"),
    )
    assert blocked["report"]["status"] == "blocked"
    endpoint = f"{PREFIX}/intent-reviews/{blocked['id']}/author-decision"
    assert client.post(
        endpoint,
        json={**decision_request(blocked), "actor_kind": "development_operator"},
    ).status_code == 409
    assert client.post(
        endpoint,
        json={**decision_request(blocked, "rejected"), "actor_kind": "system"},
    ).status_code == 422
    # Candidate review is a mechanical operation, not a confirmation; it must not
    # silently accept an actor field that it does not record or use.
    assert client.post(
        f"{PREFIX}/intent-contracts/{confirmed['contract']['id']}/candidate-reviews",
        json={**candidate_request(confirmed), "actor_kind": "development_operator"},
    ).status_code == 422
    intent = IntentStore(store)
    with pytest.raises(ValueError, match="操作主体"):
        intent.confirm(
            "demo", "absent", expected_revision=1, expected_contract_hash="0" * 64,
            validate=lambda *_: None, actor_kind="system",
        )
    with pytest.raises(ValueError, match="操作主体"):
        intent.decide(
            "demo", "absent", expected_revision=1, expected_review_hash="0" * 64,
            decision="rejected", note="", validate=lambda *_: None, actor_kind="system",
        )


def _legacy_record(record, key):
    payload = {k: v for k, v in record.items() if k not in {"actor_kind", key}}
    return {**payload, key: content_hash(payload)}


def test_legacy_hashes_read_without_rewriting_or_claiming_operator_origin(intent_api):
    client, _, store, task, _, _, body = intent_api
    proposed = propose(client, task.id, body)
    new_confirmation = confirm(client, proposed)
    assert new_confirmation["actor_kind"] == "author"
    assert new_confirmation["action"] == "explicit_author_confirmation"
    # Reconstruct the exact pre-extension shape in this temporary development DB.
    legacy_confirmation = _legacy_record(new_confirmation, "contract_hash")
    with store.connection() as db:
        db.execute(
            "UPDATE creative_intent_contracts SET payload=? WHERE id=?",
            (json.dumps(legacy_confirmation), legacy_confirmation["contract"]["id"]),
        )
    assert client.get(
        f"{PREFIX}/intent-contracts/{legacy_confirmation['contract']['id']}"
    ).json() == legacy_confirmation
    review = submit_review(client, legacy_confirmation)
    endpoint = f"{PREFIX}/intent-reviews/{review['id']}/author-decision"
    current = client.post(endpoint, json=decision_request(review)).json()
    legacy_decision = _legacy_record(current, "decision_hash")
    with store.connection() as db:
        db.execute(
            "UPDATE creative_intent_decisions SET payload=? WHERE review_id=?",
            (json.dumps(legacy_decision), review["id"]),
        )
    assert client.post(endpoint, json=decision_request(review)).json() == legacy_decision
    assert client.post(
        endpoint,
        json={**decision_request(review), "actor_kind": "development_operator"},
    ).status_code == 409
    assert client.get(f"{PREFIX}/intent-reviews/{review['id']}").json()[
        "author_decision"
    ] == legacy_decision
    with store.connection() as db:
        saved = json.loads(db.execute(
            "SELECT payload FROM creative_intent_decisions WHERE review_id=?", (review["id"],)
        ).fetchone()[0])
    assert saved == legacy_decision and "actor_kind" not in saved


def test_tampered_operator_origin_fails_existing_record_hash_validation(intent_api):
    client, _, store, task, _, _, body = intent_api
    proposed = propose(client, task.id, body)
    confirmed = _confirm_as(client, proposed, "development_operator").json()
    review = submit_review(client, confirmed)
    endpoint = f"{PREFIX}/intent-reviews/{review['id']}/author-decision"
    decision = client.post(
        endpoint, json={**decision_request(review), "actor_kind": "development_operator"}
    ).json()
    with store.connection() as db:
        db.execute(
            "UPDATE creative_intent_decisions SET payload=? WHERE review_id=?",
            (json.dumps({**decision, "actor_kind": "author"}), review["id"]),
        )
    response = client.get(f"{PREFIX}/intent-reviews/{review['id']}")
    assert response.status_code == 409 and "hash" in response.text
