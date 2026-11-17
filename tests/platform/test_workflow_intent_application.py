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
from owcopilot.workflow.intent_application import apply_intent_review, preview_intent_application
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


def test_application_preview_is_read_only_stable_and_matches_actual_projection(tmp_path):
    root, store, task, baseline, review, decision = prepared(tmp_path)
    with store.connection() as db:
        before = list(db.iterdump())
    kwargs = {
        "expected_review_hash": review["review_hash"],
        "continuation": {"reading_path": "/quest/objective"},
    }
    preview = preview_intent_application(root, "p", review["id"], **kwargs)
    assert preview == preview_intent_application(root, "p", review["id"], **kwargs)
    with store.connection() as db:
        assert list(db.iterdump()) == before
    assert preview["writes_draft"] is False and preview["writes_canon"] is False
    assert preview["impact"]["target"]["artifact_id"] is None
    assert preview["impact"]["changes"] == [
        {
            "path": "/quest/objective",
            "before_exists": True,
            "after_exists": True,
            "before": "Before",
            "after": "After",
        }
    ]
    assert preview["impact"]["reading"]["current_text_will_change"] is True
    assert preview["impact"]["references"]["cross_source_semantics"] == "unknown"
    applied = apply_intent_review(
        root,
        "p",
        review["id"],
        expected_decision_hash=decision["decision_hash"],
        expected_preview_hash=preview["preview_hash"],
        **kwargs,
    )
    assert applied["artifact_hash"] == preview["candidate_artifact_hash"]
    assert applied["impact"]["changes"] == preview["impact"]["changes"]
    with pytest.raises(ValueError, match="过期"):
        preview_intent_application(root, "p", review["id"], **kwargs)


def test_preview_does_not_authorize_apply_and_changed_context_requires_recheck(tmp_path):
    root, store, task, _, review, decision = prepared(tmp_path)
    preview = preview_intent_application(
        root,
        "p",
        review["id"],
        expected_review_hash=review["review_hash"],
        continuation={"reading_path": "/quest/objective"},
    )
    with pytest.raises(ValueError, match="修改影响或继续位置已变化"):
        apply_intent_review(
            root,
            "p",
            review["id"],
            expected_review_hash=review["review_hash"],
            expected_decision_hash=decision["decision_hash"],
            expected_preview_hash=preview["preview_hash"],
            continuation={"reading_path": "/quest/title"},
        )
    assert store.get_task(task.id).revision == 1 and len(store.artifacts(task.id)) == 1
    with store.connection() as db:
        db.execute("DELETE FROM creative_intent_decisions WHERE review_id=?", (review["id"],))
    preview_intent_application(root, "p", review["id"], expected_review_hash=review["review_hash"])
    with pytest.raises(ValueError, match="明确选择"):
        apply_intent_review(
            root,
            "p",
            review["id"],
            expected_review_hash=review["review_hash"],
            expected_decision_hash=decision["decision_hash"],
        )


def test_preview_uses_current_source_guards_and_rejects_unpublishable_candidate(tmp_path):
    root, store, _, _, review, _ = prepared(tmp_path)
    with pytest.raises(ValueError, match="hash已改变"):
        preview_intent_application(root, "p", review["id"], expected_review_hash="0" * 64)
    bundle = ContentStore(root).load()
    bundle.quests["q"].title = "Changed source"
    ContentStore(root).save(bundle)
    with pytest.raises(ValueError, match="项目基线已改变"):
        preview_intent_application(
            root, "p", review["id"], expected_review_hash=review["review_hash"]
        )
    root2, _, _, _, review2, _ = prepared(tmp_path / "other", other_field=True)
    with pytest.raises(ValueError, match="not_publishable"):
        preview_intent_application(
            root2, "p", review2["id"], expected_review_hash=review2["review_hash"]
        )
