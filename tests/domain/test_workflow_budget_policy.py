"""Scratch-only admission, unknown-outcome and backed-up migration contracts."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow.budget_policy import (
    BudgetExhaustedError,
    BudgetLedger,
    BudgetMigrationRequiredError,
    BudgetPolicy,
    BudgetUnknownError,
    migrate_budget_database,
)
from owcopilot.workflow.harness import BudgetOwner, Harness, HarnessStore, ToolRegistry
from owcopilot.workflow.models import TaskInput, TaskRecord
from owcopilot.workflow.store import WorkflowStore


@pytest.fixture
def scenario(tmp_path):
    workflow = WorkflowStore(tmp_path / "scratch.sqlite3")
    bundle = ContentBundle()
    task = TaskRecord(
        project="scratch",
        request=TaskInput(brief="Budget contract fixture"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    workflow.save_task(task)
    run, _ = workflow.create_run(task)
    owner = BudgetOwner(
        project="scratch", task_id=task.id, product_key="quest", problem_key="same-problem"
    )
    return workflow, task, run, owner


class Provider:
    model = "explicit-development-fixture"
    max_retries = 0

    def __init__(self):
        self.calls = 0
        self.error = None

    def complete(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return "actual fixture response", 2, 1, 0, self.model


def invoke(harness, owner, node="writer"):
    return harness.complete(owner=owner, node_id=node, role_id="writer", system="s", user="u")


def test_fourth_dispatch_keeps_original_counter_and_policy_survives_reopen(scenario):
    workflow, _, run, owner = scenario
    provider = Provider()
    trace = HarnessStore(workflow)
    harness = Harness(trace, run.id, provider)
    for index in range(4):
        assert invoke(harness, owner, f"renamed-{index}") == "actual fixture response"
    records = trace.requests(run.id)
    assert [item["attempt"] for item in records] == [1, 2, 3, 4]
    assert len({item["budget_id"] for item in records}) == 1
    budget_id = records[0]["budget_id"]
    reopened = BudgetLedger(WorkflowStore(workflow.path)).snapshot(budget_id)
    assert reopened["consumed"] == 4 and reopened["remaining"] is None
    assert all(item["budget_policy"]["policy"]["sdk_max_retries"] == 0 for item in records)


def test_versioned_grant_never_refunds_counter(scenario):
    workflow, _, run, owner = scenario
    ledger = BudgetLedger(workflow)
    ledger.publish_policy(BudgetPolicy(id="finite", max_owner_dispatches=1), reason="fixture cap")
    provider = Provider()
    trace = HarnessStore(workflow)
    harness = Harness(trace, run.id, provider)
    invoke(harness, owner)
    budget_id = trace.requests(run.id)[0]["budget_id"]
    with pytest.raises(BudgetExhaustedError):
        invoke(harness, owner, "new-node")
    ledger.publish_policy(
        BudgetPolicy(id="finite", version=2, max_owner_dispatches=5),
        reason="diagnosed extension",
        activate=False,
    )
    assert ledger.snapshot(budget_id)["remaining"] == 0  # Publishing alone changes no grant.
    grant = ledger.grant(budget_id, "finite", 2, reason="same owner continuation")
    assert grant["consumed_floor"] == 1
    for _ in range(4):
        invoke(harness, owner)
    assert ledger.snapshot(budget_id)["consumed"] == 5
    assert provider.calls == 5
    with pytest.raises(ValueError, match="不可改写"):
        ledger.publish_policy(
            BudgetPolicy(id="finite", version=2, max_owner_dispatches=6), reason="invalid overwrite"
        )


def test_unknown_timeout_blocks_replay_until_hash_bound_reconciliation(scenario):
    workflow, _, run, owner = scenario
    provider = Provider()
    provider.error = TimeoutError("acknowledgement missing")
    trace = HarnessStore(workflow)
    harness = Harness(trace, run.id, provider)
    with pytest.raises(TimeoutError):
        invoke(harness, owner)
    provider.error = None
    with pytest.raises(BudgetUnknownError):
        invoke(harness, owner, "renamed")
    assert provider.calls == 1
    budget_id = trace.requests(run.id)[0]["budget_id"]
    ledger = BudgetLedger(workflow)
    receipt = ledger.dispatches(budget_id)[0]
    assert receipt["status"] == "claimed_unknown"
    with pytest.raises(ValueError, match="不匹配"):
        ledger.reconcile_execution(
            receipt["id"],
            expected_receipt_hash="wrong",
            status="completed_failed",
            evidence={"receipt": "fixture provider lookup"},
        )
    ledger.reconcile_execution(
        receipt["id"],
        expected_receipt_hash=receipt["receipt_hash"],
        status="completed_failed",
        evidence={"provider_lookup": "explicit fixture rejected operation"},
    )
    invoke(harness, owner)
    assert provider.calls == 2 and workflow.attempts(budget_id) == 2
    assert ledger.dispatches(budget_id)[0]["events"][-1]["reconciliation"] is True


def test_concurrent_admission_does_not_overrun_finite_grant(scenario):
    workflow, _, run, owner = scenario
    BudgetLedger(workflow).publish_policy(
        BudgetPolicy(id="one", max_owner_dispatches=1), reason="atomic fixture"
    )
    provider = Provider()
    harness = Harness(HarnessStore(workflow), run.id, provider)

    def attempt(index):
        try:
            invoke(harness, owner, str(index))
            return "dispatched"
        except BudgetExhaustedError:
            return "denied"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count("dispatched") == 1 and provider.calls == 1


def test_execution_attempts_preserve_immutable_receipts_and_unknown_gate(scenario):
    workflow, _, _, _ = scenario
    ledger = BudgetLedger(workflow)
    first = ledger.claim_execution(
        "episode:original", "stable-owner", "request-hash", reason="first"
    )
    assert first["admitted"]
    duplicate = ledger.claim_execution(
        "episode:original", "stable-owner", "request-hash", reason="duplicate"
    )
    assert duplicate["admitted"] is False and duplicate["status"] == "claimed_unknown"
    ledger.finish_execution(
        first["id"], status="completed_failed", evidence={"compile_error": "known schema issue"}
    )
    second = ledger.claim_execution(
        "episode:original", "stable-owner", "request-hash", reason="fix schema boundary"
    )
    assert second["admitted"] and second["attempt"] == 2
    ledger.finish_execution(
        second["id"], status="completed", evidence={"candidate_id": "real fixture candidate"}
    )
    assert (
        ledger.execution_state("episode:original")["attempts"][0]["receipt_hash"]
        == first["receipt_hash"]
    )
    assert not ledger.claim_execution(
        "episode:original", "stable-owner", "request-hash", reason="duplicate"
    )["admitted"]
    with pytest.raises(ValueError, match="不能改owner"):
        ledger.claim_execution(
            "episode:original", "replacement-owner", "request-hash", reason="invalid"
        )
    imported = ledger.claim_execution(
        "legacy:crashed",
        "owner",
        "hash",
        reason="restore old claim",
        legacy_state="claimed_unknown",
    )
    assert not imported["admitted"]
    legacy_failed = ledger.claim_execution(
        "legacy:failed",
        "owner",
        "hash",
        reason="diagnosed parser fix",
        legacy_state="completed_failed",
    )
    assert legacy_failed["admitted"] and legacy_failed["attempt"] == 2


def test_run_limit_is_shared_across_owner_identities(scenario):
    workflow, _, run, owner = scenario
    BudgetLedger(workflow).publish_policy(
        BudgetPolicy(id="run-two", max_run_dispatches=2), reason="run cap fixture"
    )
    provider = Provider()
    harness = Harness(HarnessStore(workflow), run.id, provider)
    invoke(harness, owner)
    invoke(harness, owner.model_copy(update={"product_key": "other-real-product"}))
    with pytest.raises(BudgetExhaustedError, match="运行"):
        invoke(harness, owner.model_copy(update={"product_key": "third-real-product"}))
    assert provider.calls == 2


def test_explicit_backup_migration_preserves_legacy_counts_and_unknown_slots(scenario, tmp_path):
    workflow, _, run, _ = scenario
    with workflow.connection() as db:
        db.execute("DROP TABLE node_budgets")
        db.execute(
            "CREATE TABLE node_budgets(id TEXT PRIMARY KEY, "
            "attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 3))"
        )
        db.execute("INSERT INTO node_budgets VALUES('old-owner',3)")
        for attempt in range(1, 4):
            payload = {"status": "failed", "error": "observed bad request", "attempt": attempt}
            db.execute(
                "INSERT INTO model_calls VALUES(?,?,?,?,?)",
                (f"old-{attempt}", run.id, "old-owner", attempt, json.dumps(payload)),
            )
        db.execute("INSERT INTO node_budgets VALUES('crashed-owner',1)")
        db.execute("INSERT INTO node_budgets VALUES('generic-failed-owner',1)")
        db.execute(
            "INSERT INTO model_calls VALUES(?,?,?,?,?)",
            (
                "generic-failed",
                run.id,
                "generic-failed-owner",
                1,
                json.dumps({"status": "failed", "error": "adapter failed without acknowledgement"}),
            ),
        )
    with pytest.raises(BudgetMigrationRequiredError):
        workflow.claim_attempt("old-owner", run.id, "same-node")
    backup = tmp_path / "retained-backup.sqlite3"
    report = migrate_budget_database(workflow.path, backup, reason="fixture migration")
    assert report["before_hash"] == report["after_hash"] and report["integrity"] == "ok"
    with sqlite3.connect(backup) as old:
        assert (
            old.execute("SELECT attempts FROM node_budgets WHERE id='old-owner'").fetchone()[0] == 3
        )
        assert (
            "BETWEEN"
            in old.execute("SELECT sql FROM sqlite_schema WHERE name='node_budgets'").fetchone()[0]
        )
    assert workflow.claim_attempt("old-owner", run.id, "same-node") == 4
    with pytest.raises(BudgetUnknownError):
        workflow.claim_attempt("crashed-owner", run.id, "same-node")
    with pytest.raises(BudgetUnknownError):
        workflow.claim_attempt("generic-failed-owner", run.id, "same-node")
    with pytest.raises(ValueError):
        migrate_budget_database(workflow.path, backup, reason="must not overwrite")


def test_native_round_limit_is_policy_bound_without_a_three_dispatch_ceiling(scenario):
    from pydantic import BaseModel

    class Input(BaseModel):
        value: int

    class Output(BaseModel):
        result: int

    class Native(Provider):
        supports_tools = True

        def complete_with_tools(self, **kwargs):
            self.calls += 1
            return ToolCallResponse(
                tool_calls=[ToolCall(str(self.calls), "read", {"value": self.calls})]
            )

    workflow, _, run, owner = scenario
    BudgetLedger(workflow).publish_policy(
        BudgetPolicy(id="native-four", tool_round_limit=4), reason="configured native boundary"
    )
    registry = ToolRegistry()
    registry.register(
        "read", Input, Output, lambda args: {"result": args["value"]}, roles={"reader"}
    )
    provider = Native()
    trace = HarnessStore(workflow)
    result = Harness(trace, run.id, provider).run_tools(
        owner=owner, node_id="native", role_id="reader", messages=[], registry=registry
    )
    assert result["status"] == "continuation_limit" and provider.calls == 4
    assert [item["attempt"] for item in trace.requests(run.id)] == [1, 2, 3, 4]
