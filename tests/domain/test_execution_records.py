"""Selection history and cost views retain failures and unknown dispatches."""

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.workflow.execution_records import (
    DecisionReceipt,
    ExecutionRecords,
    summarize_dispatches,
)
from owcopilot.workflow.models import TaskInput, TaskRecord
from owcopilot.workflow.store import WorkflowStore


def task_store(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    bundle = ContentBundle()
    task = TaskRecord(
        project="test",
        request=TaskInput(brief="registered task"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    store.save_task(task)
    return store, task


def test_receipt_history_is_immutable_and_run_binding_is_versioned(tmp_path):
    store, task = task_store(tmp_path)
    records = ExecutionRecords(store)
    receipt = DecisionReceipt(
        project=task.project,
        task_id=task.id,
        task_revision=1,
        input_hash=content_hash(task.request),
        policy_id="bounded",
        policy_version="1",
        observation={"base_hash": task.base_hash},
        candidates=[{"id": "fresh"}],
        selected_action_id="fresh",
        reason="no compatible asset",
        budget={},
    )
    first = records.append_decision(receipt)
    assert records.append_decision(receipt) == first
    with pytest.raises(ValueError, match="不可改写"):
        records.append_decision(receipt.model_copy(update={"reason": "rewrite"}))
    run, _ = store.create_run(task)
    records.bind_run(task.project, receipt.id, run.id)
    records.bind_run(task.project, receipt.id, run.id)
    assert records.for_task(task.project, task.id)[0]["run_ids"] == [run.id]
    revised = task.model_copy(update={"revision": 2})
    store.save_task(revised)
    with pytest.raises(ValueError, match="不一致"):
        records.append_decision(receipt.model_copy(update={"id": "old-revision"}))


def request(identity, attempt):
    return {
        "id": identity,
        "budget_id": "same-owner",
        "attempt": attempt,
        "budget_receipt_id": f"receipt-{attempt}",
        "node_id": "writer",
        "owner": {"task_id": "task"},
        "request_hash": "same-prompt",
        "request": {"role_id": "writer", "config": {}},
    }


def result(identity, *, status="done", tokens=10):
    return {
        "type": "model/result",
        "payload": {
            "request_id": identity,
            "status": status,
            "measurement": {"input_tokens": tokens, "output_tokens": 3, "cached_input_tokens": 4},
        },
    }


def test_actual_retries_count_but_duplicate_events_do_not_and_missing_usage_is_not_zero():
    requests = [request("one", 1), request("two", 2), request("unknown", 3)]
    first = result("one", status="failed")
    summary = summarize_dispatches(requests, [first, first, result("two")])
    assert summary["known_totals"]["input_tokens"] == 20
    assert summary["known_totals"]["cached_input_tokens"] == 8
    assert summary["known_totals"]["uncached_input_tokens"] == 12
    assert summary["total_tokens"] is None
    assert summary["unknown_usage_request_ids"] == ["unknown"]
    assert summary["training_eligible"] is False
    assert summary["quality"]["Q2"] == "unknown"


def test_conflicting_settlements_do_not_choose_the_cheapest_event():
    summary = summarize_dispatches([request("one", 1)], [result("one"), result("one", tokens=11)])
    assert not summary["usage_complete"]
    assert summary["dispatches"][0]["settlement_status"] == "conflict"
