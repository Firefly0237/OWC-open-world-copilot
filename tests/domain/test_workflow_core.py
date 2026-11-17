"""Production contracts and state transitions; these are not model-quality evaluations."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.budget_policy import BudgetLedger, BudgetPolicy
from owcopilot.workflow.models import Artifact, NodeOutcome, TaskInput, TaskRecord, WorkflowSpec
from owcopilot.workflow.planning import validate_bindings
from owcopilot.workflow.runtime import _run_step
from owcopilot.workflow.store import BudgetExhaustedError, WorkflowStore


def setup_run(tmp_path):
    store = WorkflowStore(tmp_path / "studio.sqlite3")
    task = TaskRecord(
        project="test",
        request=TaskInput(brief="创作一个分支"),
        base_version="v1",
        base_hash=content_hash({}),
        base_bundle={},
    )
    store.save_task(task)
    run, _ = store.create_run(task)
    return store, task, run


def test_schema_and_typed_required_dependencies():
    spec = WorkflowSpec(
        name="test",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "report", "operation": "package", "optional_dependencies": ["quest"]},
        ],
    )
    assert validate_bindings(spec) == spec
    with pytest.raises(ValueError, match="必需"):
        validate_bindings(
            WorkflowSpec(
                name="bad",
                nodes=[
                    {"id": "quest", "operation": "draft_quest"},
                ],
            )
        )
    with pytest.raises(ValueError, match="无环"):
        WorkflowSpec(
            name="cycle",
            nodes=[
                {"id": "a", "operation": "draft_quest", "depends_on": ["b"]},
                {"id": "b", "operation": "plan_events", "depends_on": ["a"]},
            ],
        )


def test_budget_atomic_and_persistent(tmp_path):
    store, _, run = setup_run(tmp_path)
    BudgetLedger(store).publish_policy(
        BudgetPolicy(id="finite-fixture", max_owner_dispatches=3), reason="atomic finite cap"
    )

    def claim(_):
        try:
            attempt = store.claim_attempt("logical-node", run.id, "node")
            store.settle_attempt(
                "logical-node",
                attempt,
                status="completed_failed",
                evidence={"known_failure": "fixture"},
            )
            return attempt
        except BudgetExhaustedError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        result = list(executor.map(claim, range(4)))
    admitted = sorted(value for value in result if value is not None)
    assert admitted == list(range(1, len(admitted) + 1)) and 1 <= len(admitted) <= 3
    # Concurrent callers may encounter a still-unknown claim and must not replay
    # it. Finish the known fixture slots sequentially to test persistent cap.
    while store.attempts("logical-node") < 3:
        attempt = claim(None)
        assert attempt is not None
    reopened = WorkflowStore(store.path)
    assert reopened.attempts("logical-node") == 3
    with pytest.raises(BudgetExhaustedError):
        reopened.claim_attempt("logical-node", run.id, "renamed-node")


def test_hidden_second_dispatch_is_rejected(tmp_path):
    store, _, run = setup_run(tmp_path)
    store.claim_attempt("logical-node", run.id, "node")
    store.begin_model_call(run.id, "logical-node", "node", 1, {"input": "explicit test input"})
    with pytest.raises(BudgetExhaustedError, match="隐藏"):
        store.begin_model_call(run.id, "logical-node", "node", 1, {"input": "duplicate"})
    assert len(store.model_calls(run.id)) == 1


def test_revision_cancels_late_publication_atomically(tmp_path):
    store, task, run = setup_run(tmp_path)
    next_task = task.model_copy(update={"revision": 2})
    store.commit_revision(next_task, expected_revision=1, preference={"text": "项目要求"})
    artifact = Artifact(
        task_id=task.id,
        revision=1,
        run_id=run.id,
        node_id="audit",
        kind="audit_report",
        payload={},
        input_hash="input",
        content_hash="result",
    )
    store.complete_node(
        run.id,
        NodeOutcome(
            node_id="audit",
            operation="audit",
            status="succeeded",
            budget_id="budget",
            artifact_id=artifact.id,
        ),
        artifact,
    )
    assert store.artifacts(task.id) == []
    assert store.outcomes(run.id)["audit"].status == "cancelled"
    assert len(store.preferences()) == 1
    with pytest.raises(ValueError):
        store.commit_revision(next_task, expected_revision=1, preference={"text": "duplicate"})
    assert len(store.preferences()) == 1


def test_runtime_diagnosed_continuation_then_independent_operation(tmp_path):
    store, task, run = setup_run(tmp_path)

    def unavailable(error):
        raise ConnectionError("controlled transient failure; no model was called")

    failed = _run_step(
        store,
        run,
        task,
        "bad",
        "test_failure",
        "input",
        unavailable,
        lambda *_: None,
        kind="test_error",
    )
    assert failed.status == "skipped_nonretryable"
    assert failed.attempts == 1
    succeeded = _run_step(
        store,
        run,
        task,
        "independent",
        "package",
        "input",
        lambda _: ("package", {"usable": True}),
        lambda *_: None,
        kind="package",
    )
    assert succeeded.status == "succeeded"
    other_run, _ = store.create_run(task)
    failed_again = _run_step(
        store,
        other_run,
        task,
        "renamed",
        "test_failure",
        "input",
        unavailable,
        lambda *_: None,
        kind="test_error",
    )
    assert failed_again.attempts == 2
    assert store.model_calls(run.id) == []


def test_nonretryable_input_does_not_spend_three_attempts(tmp_path):
    store, task, run = setup_run(tmp_path)

    def invalid(error):
        raise ValueError("unknown existing project entity")

    result = _run_step(
        store,
        run,
        task,
        "node",
        "input_check",
        "input",
        invalid,
        lambda *_: None,
        kind="validation",
    )
    assert result.status == "skipped_nonretryable"
    assert result.attempts == 1


def test_explicit_reopen_preserves_old_budget_and_requires_authorization(tmp_path):
    store, task, run = setup_run(tmp_path)
    ledger = BudgetLedger(store)
    ledger.publish_policy(BudgetPolicy(id="old-cap", max_owner_dispatches=3), reason="fixture cap")
    old_budget = f"{task.id}:{task.revision}:package"
    for _ in range(3):
        attempt = store.claim_attempt(old_budget, run.id, "node")
        store.settle_attempt(
            old_budget, attempt, status="completed_failed", evidence={"known_failure": "fixture"}
        )
    run = store.update_run(run.id, budget_overrides={"package": old_budget + ":authorized"})
    with pytest.raises(ValueError, match="授权"):
        _run_step(
            store,
            run,
            task,
            "node",
            "package",
            "input",
            lambda _: ("package", {"ok": True}),
            lambda *_: None,
            kind="package",
        )
    run = store.update_run(run.id, budget_authorization_note="explicit user permission")
    with pytest.raises(ValueError, match="不能换预算身份"):
        _run_step(
            store,
            run,
            task,
            "node",
            "package",
            "input",
            lambda _: ("package", {"ok": True}),
            lambda *_: None,
            kind="package",
        )
    ledger.publish_policy(
        BudgetPolicy(id="old-cap", version=2, max_owner_dispatches=5),
        reason="diagnosed extension",
        activate=False,
    )
    ledger.grant(old_budget, "old-cap", 2, reason="extend original owner")
    run = store.update_run(run.id, budget_overrides={})
    result = _run_step(
        store,
        run,
        task,
        "node",
        "package",
        "input",
        lambda _: ("package", {"ok": True}),
        lambda *_: None,
        kind="package",
    )
    assert result.status == "succeeded"
    assert store.attempts(old_budget) == 4
    assert store.attempts(old_budget + ":authorized") == 0


def test_local_permission_failure_stops_after_one_attempt(tmp_path):
    store, task, run = setup_run(tmp_path)

    def denied(error):
        outer = ConnectionError("provider wrapper")
        outer.__cause__ = PermissionError(13, "socket denied")
        raise outer

    result = _run_step(
        store,
        run,
        task,
        "node",
        "permission_check",
        "input",
        denied,
        lambda *_: None,
        kind="validation",
    )
    assert result.status == "skipped_nonretryable"
    assert result.attempts == 1
    assert "执行环境权限拒绝" in result.error
    assert store.model_calls(run.id) == []


def test_revision_keeps_unresolved_node_budget(tmp_path):
    store, task, prior_run = setup_run(tmp_path)
    old_budget = f"{task.id}:1:draft_logic"
    for _ in range(2):
        attempt = store.claim_attempt(old_budget, prior_run.id, "logic")
        store.settle_attempt(
            old_budget, attempt, status="completed_failed", evidence={"known_failure": "fixture"}
        )
    revised = task.model_copy(
        update={"revision": 2, "preserved_budgets": {"draft_logic": old_budget}}
    )
    store.save_task(revised)
    run, _ = store.create_run(revised)
    invocations = []

    def still_invalid(error):
        invocations.append(error)
        raise RuntimeError("same unresolved technical problem")

    for _ in range(2):
        result = _run_step(
            store,
            run,
            revised,
            "logic",
            "draft_logic",
            "changed-input",
            still_invalid,
            lambda *_: None,
            kind="quest_with_logic",
        )
        assert result.status == "skipped_nonretryable"
    assert len(invocations) == 2
    assert store.attempts(old_budget) == 4
    assert store.attempts(f"{task.id}:2:draft_logic") == 0
