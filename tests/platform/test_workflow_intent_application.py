"""Real shadow reviews apply only publishable draft changes, atomically and idempotently."""

import json

import pytest

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
from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.workflow.intent_application import apply_intent_review
from owcopilot.workflow.models import Artifact, TaskInput


def prepared(tmp_path, *, other_field=False):
    root = tmp_path / "world"
    quest = Quest(
        id="q", title="Preserve", objective="Before", stages=[QuestStage(id="s", summary="Go")]
    )
    other = Quest(id="other", title="Other")
    bundle = ContentBundle(quests={"q": quest, "other": other})
    ContentStore(root).save(bundle)
    task_id = create_task_action(root, "p", TaskInput(brief="Explicit development input"))["task"][
        "id"
    ]
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    run, _ = store.create_run(task)
    store.update_run(run.id, status="completed")
    payload = {"quest": quest.model_dump(mode="json")}
    baseline = Artifact(
        task_id=task_id,
        revision=1,
        run_id=run.id,
        node_id="draft_quest",
        kind="quest",
        payload=payload,
        input_hash="fixture",
        content_hash=content_hash(payload),
    )
    store.append_artifact(baseline)
    path = "/quests/other/title" if other_field else "/quests/q/objective"
    proposed = create_intent_contract_action(
        root,
        "p",
        task_id,
        IntentContractInput(
            expected_revision=1,
            baseline_artifact_id=baseline.id,
            baseline_artifact_hash=baseline.content_hash,
            allowed_paths=[path],
            modification_goal="Explicit author change in isolated fixture",
        ),
    )
    contract = confirm_intent_contract_action(
        root,
        "p",
        proposed["contract"]["id"],
        IntentConfirmInput(
            expected_revision=1,
            expected_contract_hash=proposed["contract_hash"],
        ),
    )
    review = create_intent_candidate_review_action(
        root,
        "p",
        contract["contract"]["id"],
        IntentCandidateReviewInput.model_validate(
            {
                "expected_revision": 1,
                "expected_contract_hash": contract["contract_hash"],
                "candidate": {"ops": [{"op": "replace", "path": path, "value": "After"}]},
            }
        ),
    )
    assert review["report"]["status"] == "ready_for_author"
    decision = decide_intent_candidate_action(
        root,
        "p",
        review["id"],
        IntentAuthorDecisionInput(
            expected_revision=1,
            expected_review_hash=review["review_hash"],
            decision="accepted",
        ),
    )
    return root, store, task, baseline, review, decision


def test_actual_apply_replays_after_revision_advance_and_never_changes_canon(tmp_path):
    root, store, task, baseline, review, decision = prepared(tmp_path)
    canon = content_hash(ContentStore(root).load())
    kwargs = {
        "expected_review_hash": review["review_hash"],
        "expected_decision_hash": decision["decision_hash"],
    }
    applied = apply_intent_review(root, "p", review["id"], **kwargs)
    assert applied == apply_intent_review(root, "p", review["id"], **kwargs)
    assert store.get_task(task.id).revision == 2
    assert len(store.artifacts(task.id)) == 2
    assert store.get_artifact(applied["artifact_id"]).payload["quest"]["objective"] == "After"
    assert store.get_artifact(baseline.id).payload["quest"]["objective"] == "Before"
    assert content_hash(ContentStore(root).load()) == canon
    assert applied["writes_canon"] is False and applied["contract_rebound"] is False
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM creative_intent_applications").fetchone()[0] == 1
        old = json.loads(
            db.execute(
                "SELECT payload FROM creative_intent_contracts ORDER BY version DESC"
            ).fetchone()[0]
        )
        assert old["contract"]["task_revision"] == 1
    with pytest.raises(KeyError):
        apply_intent_review(root, "other-project", review["id"], **kwargs)


def test_outside_artifact_changes_are_not_silently_lost_or_published(tmp_path):
    root, store, task, _, review, decision = prepared(tmp_path, other_field=True)
    with pytest.raises(ValueError, match="not_publishable"):
        apply_intent_review(
            root,
            "p",
            review["id"],
            expected_review_hash=review["review_hash"],
            expected_decision_hash=decision["decision_hash"],
        )
    assert store.get_task(task.id).revision == 1
    assert len(store.artifacts(task.id)) == 1
