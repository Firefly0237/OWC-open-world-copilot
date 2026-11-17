"""Atomic, replayable application of an explicitly accepted shadow review to drafts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..storage.world_lock import world_write_lock
from .edit_continuity import application_impact, project_review_changes, validate_continuation
from .intent_models import IntentContract
from .intent_review import intent_baseline_bundle
from .intent_store import IntentStore, _decode, _sealed
from .models import Artifact, RunRecord, new_id, now
from .store import WorkflowStore


def apply_intent_review(
    root: str | Path,
    project: str,
    review_id: str,
    *,
    expected_review_hash: str,
    expected_decision_hash: str,
    continuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A repeat returns its original receipt, even though that apply advanced the task."""
    from ..app.actions.intent_workflow import _source_validator
    from ..app.actions.workflow import _KIND_OPERATIONS, _revision_feedback, open_workflow_store

    with world_write_lock(root):
        workflow = open_workflow_store(root)
        store = IntentStore(workflow)
        with workflow.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS creative_intent_applications(
                command_key TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL,
                review_id TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("BEGIN IMMEDIATE")
            review = store._review(db, project, review_id)
            if review["review_hash"] != expected_review_hash:
                raise ValueError("候选报告hash已改变")
            row = db.execute(
                "SELECT payload FROM creative_intent_decisions WHERE review_id=? AND project=?",
                (review_id, project),
            ).fetchone()
            if row is None:
                raise ValueError("应用前需要作者明确选择候选")
            decision = _decode(row["payload"], "decision_hash")
            if decision["decision_hash"] != expected_decision_hash:
                raise ValueError("作者决定hash不匹配")
            if decision["decision"] != "accepted":
                raise ValueError("被拒绝的候选不能应用")
            key = content_hash([project, review_id, expected_review_hash, decision["id"]])
            saved = db.execute(
                "SELECT payload FROM creative_intent_applications WHERE command_key=?", (key,)
            ).fetchone()
            if saved:
                original = _decode(saved["payload"], "receipt_hash")
                if continuation is not None:
                    supplied = {
                        key: value for key, value in continuation.items() if value is not None
                    }
                    recorded = original.get("continuation", {})
                    if any(recorded.get(key) != value for key, value in supplied.items()):
                        raise ValueError("同一应用不能改用另一处继续位置，请读取原回执")
                return original
            record = store._contract(db, project, review["contract_id"])
            store._require_active(db, project, record, review["contract_hash"])
            contract = IntentContract.model_validate(record["contract"])
            task, baseline = store._bound_source(
                db, project, contract, review["task_revision"], _source_validator(root)
            )
            if review["report"]["status"] != "ready_for_author":
                raise ValueError("候选没有通过技术门禁")
            continuation_record = validate_continuation(db, project, task, baseline, continuation)
            before = intent_baseline_bundle(task, baseline)
            after = ContentBundle.model_validate(review["report"]["candidate_bundle"])
            payload = dict(baseline.payload)
            projection = before.model_copy(deep=True)
            if baseline.kind not in {"quest", "quest_with_logic", "bound_dialogue"}:
                raise ValueError("not_publishable: 当前草稿产物类型不支持")
            quest_id = payload["quest"]["id"]
            if quest_id not in after.quests:
                raise ValueError("not_publishable: 不能删除整个草稿任务")
            projection.quests[quest_id] = after.quests[quest_id]
            payload["quest"] = after.quests[quest_id].model_dump(mode="json")
            if baseline.kind == "bound_dialogue":
                tree_id = payload["tree"]["id"]
                if tree_id not in after.dialogue_trees:
                    raise ValueError("not_publishable: 不能删除整个对白树")
                projection.dialogue_trees[tree_id] = after.dialogue_trees[tree_id]
                payload["tree"] = after.dialogue_trees[tree_id].model_dump(mode="json")
            if content_hash(projection) != content_hash(after):
                raise ValueError("not_publishable: 候选包含此草稿集合之外的改动")
            if content_hash(before) != review["report"]["before_hash"] or (
                content_hash(after) != review["report"]["after_hash"]
            ):
                raise ValueError("审查报告与实际候选内容不一致")
            payload = project_review_changes(
                baseline.payload, before.model_dump(mode="json"), after.model_dump(mode="json")
            )
            feedback = _revision_feedback(
                workflow,
                task,
                text=contract.modification_goal,
                scope="content",
                target=_KIND_OPERATIONS[baseline.kind],
            )
            feedback["intent_review_id"] = review_id
            revised = task.model_copy(
                update={
                    "revision": task.revision + 1,
                    "feedback": [*task.feedback, feedback],
                    "preserved_budgets": WorkflowStore._preserved_failure_budgets(
                        db, task.id, task.preserved_budgets
                    ),
                }
            )
            artifact = Artifact(
                task_id=task.id,
                revision=revised.revision,
                run_id=baseline.run_id,
                node_id=baseline.node_id,
                kind=baseline.kind,
                payload=payload,
                input_hash=content_hash([expected_review_hash, expected_decision_hash]),
                content_hash=content_hash(payload),
            )
            receipt = _sealed(
                {
                    "id": new_id(),
                    "command_key": key,
                    "project": project,
                    "task_id": task.id,
                    "review_id": review_id,
                    "review_hash": expected_review_hash,
                    "decision_id": decision["id"],
                    "decision_hash": expected_decision_hash,
                    "source_artifact_id": baseline.id,
                    "source_artifact_hash": baseline.content_hash,
                    "artifact_id": artifact.id,
                    "artifact_hash": artifact.content_hash,
                    "previous_revision": task.revision,
                    "revision": revised.revision,
                    "contract_id": contract.id,
                    "contract_rebound": False,
                    "writes_canon": False,
                    "changes_method_acceptance": False,
                    "goal_status": "withheld",
                    "decision_actor_kind": decision.get("actor_kind", "author"),
                    "decision_action": decision.get("action"),
                    "continuation": continuation_record,
                    "impact": application_impact(
                        db,
                        revised,
                        baseline,
                        artifact,
                        contract,
                        review,
                        feedback,
                        continuation_record,
                    ),
                    "created_at": now(),
                },
                "receipt_hash",
            )
            db.execute(
                "UPDATE tasks SET payload=? WHERE id=?", (revised.model_dump_json(), task.id)
            )
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?)",
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.model_dump_json(),
                ),
            )
            for row in db.execute(
                "SELECT payload FROM runs WHERE task_id=?", (task.id,)
            ).fetchall():
                run = RunRecord.model_validate_json(row["payload"])
                if run.status in {"queued", "running", "waiting_user"}:
                    run.cancel_requested = True
                    if run.status != "running":
                        run.status = "cancelled"
                    db.execute(
                        "UPDATE runs SET payload=? WHERE id=?", (run.model_dump_json(), run.id)
                    )
                    workflow._event(db, run.id, "revision_changed", {"revision": revised.revision})
            db.execute(
                "INSERT INTO creative_intent_applications VALUES(?,?,?,?,?)",
                (
                    key,
                    project,
                    task.id,
                    review_id,
                    json.dumps(receipt, ensure_ascii=False),
                ),
            )
            workflow._event(db, baseline.run_id, "intent_candidate_applied", receipt)
            return receipt
