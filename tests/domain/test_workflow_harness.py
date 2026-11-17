"""Production harness contracts; controlled providers are explicit engineering fixtures."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel, ConfigDict

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow.budget_policy import BudgetLedger, BudgetPolicy
from owcopilot.workflow.harness import BudgetOwner, Harness, HarnessStore, ToolRegistry
from owcopilot.workflow.models import TaskInput, TaskRecord
from owcopilot.workflow.store import BudgetExhaustedError, RunInterruptedError, WorkflowStore


@pytest.fixture
def fixture(tmp_path):
    workflow = WorkflowStore(tmp_path / "creative_workflows.sqlite3")
    # These original tests exercise an explicitly finite historical grant, not
    # the new default policy (which no longer has a three-dispatch lifetime cap).
    BudgetLedger(workflow).publish_policy(
        BudgetPolicy(id="historical-test", max_owner_dispatches=3, tool_round_limit=3),
        reason="explicit finite policy fixture",
    )
    bundle = ContentBundle()
    task = TaskRecord(
        project="test",
        request=TaskInput(brief="Explicit harness development input"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    workflow.save_task(task)
    run, _ = workflow.create_run(task)
    workflow.update_run(run.id, status="running")
    store = HarnessStore(workflow)
    owner = BudgetOwner(
        project="test", task_id=task.id, product_key="draft_quest", problem_key="original-output"
    )
    return workflow, store, task, run, owner


class TextProvider:
    model = "requested-model"
    max_retries = 0
    base_url = "https://user:private@provider.example/api?key=private"
    api_key = "NEVER_EXPORT"

    def __init__(self):
        self.calls = 0

    def complete(self, *, system, user, model):
        self.calls += 1
        return user, 12, 4, 3, "actual-response-model"


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doubled: int


def registry(body):
    tools = ToolRegistry()
    tools.register(
        "inspect",
        ToolInput,
        ToolOutput,
        body,
        roles={"inspector"},
        description="Inspect one explicit development value",
        version="2",
    )
    return tools


def test_request_reconstruction_and_replay_preserve_actual_inputs_and_missing_history(fixture):
    workflow, store, _, run, owner = fixture
    provider = TextProvider()
    context = {"author": {"text": "Keep the ending"}, "upstream": {"id": "a", "hash": "h"}}
    harness = Harness(store, run.id, provider)
    assert (
        harness.complete(
            owner=owner,
            node_id="writer_v2",
            role_id="writer",
            system="json input",
            user="actual input",
            context_manifest=context,
        )
        == "actual input"
    )
    context["author"]["text"] = "Changed afterwards"
    reopened = HarnessStore(WorkflowStore(workflow.path))
    request = reopened.requests(run.id)[0]
    assert reopened.reconstruct_request(request["id"]) == request
    assert request["request"]["user"] == "actual input"
    assert request["context_manifest"]["author"]["text"] == "Keep the ending"
    assert request["request"]["config"]["server_address"] == "provider.example"
    assert "private" not in str(request) and "NEVER_EXPORT" not in str(request)
    first = reopened.events(run.id, limit=1)
    second = reopened.events(run.id, after_seq=first[0]["seq"], limit=1)
    assert [row["type"] for row in first + second] == ["model/request", "model/result"]
    measurement = second[0]["payload"]["measurement"]
    assert measurement["input_tokens"] == 12 and measurement["cached_input_tokens"] == 3
    assert measurement["response_model"] == "actual-response-model"
    assert measurement["cost_usd"] is None
    assert provider.calls == 1  # Reconstruction/pagination did not dispatch anything.


def test_legacy_floor_and_atomic_shared_identity_survive_node_and_run_replacement(fixture):
    workflow, store, task, run, owner = fixture
    legacy = f"{task.id}:1:draft_quest"
    for _ in range(2):
        claimed = workflow.claim_attempt(legacy, run.id, "old_a", operation="draft_quest")
        workflow.settle_attempt(
            legacy,
            claimed,
            status="completed_failed",
            evidence={"host_validation": "explicit known fixture failure"},
        )
    owner = owner.model_copy(update={"legacy_budget_id": legacy})
    provider = TextProvider()
    harness = Harness(store, run.id, provider)

    def attempt(node):
        try:
            return harness.complete(
                owner=owner, node_id=node, role_id="writer", system="input", user="candidate"
            )
        except BudgetExhaustedError:
            return "exhausted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["renamed_writer", "another_role_instance"]))
    assert sorted(results) == ["candidate", "exhausted"]
    assert workflow.attempts(legacy) == 3 and provider.calls == 1
    workflow.update_run(run.id, status="completed")
    successor, _ = workflow.create_run(task)
    no_alias = owner.model_copy(update={"legacy_budget_id": None})
    with pytest.raises(BudgetExhaustedError):
        Harness(store, successor.id, provider).complete(
            owner=no_alias, node_id="graph_v3", role_id="replacement", system="input", user="x"
        )
    assert workflow.attempts(legacy) == 3 and provider.calls == 1


def test_native_tool_continuation_uses_same_budget_and_persists_denial_and_actual_result(fixture):
    workflow, store, _, run, owner = fixture
    bodies = []

    class Provider:
        model = "native-fixture"
        max_retries = 0
        supports_tools = True

        def __init__(self):
            self.calls = []

        def complete_with_tools(self, *, messages, tools, model):
            self.calls.append(messages)
            if len(self.calls) == 1:
                assert [item["function"]["name"] for item in tools] == ["inspect"]
                return ToolCallResponse(
                    tool_calls=[
                        ToolCall("deny", "erase_world", {}),
                        ToolCall("ok", "inspect", {"value": 2}),
                    ],
                    input_tokens=5,
                    output_tokens=2,
                    response_model="actual-native",
                )
            assert messages[-2]["tool_call_id"] == "deny"
            assert "tool_not_allowed" in messages[-2]["content"]
            assert messages[-1]["tool_call_id"] == "ok" and "4" in messages[-1]["content"]
            return ToolCallResponse(text="inspected", input_tokens=9, output_tokens=1)

    provider = Provider()
    tools = registry(lambda args: bodies.append(args) or {"doubled": args["value"] * 2})
    result = Harness(store, run.id, provider).run_tools(
        owner=owner,
        node_id="inspect_step",
        role_id="inspector",
        messages=[{"role": "user", "content": "Inspect the current evidence"}],
        registry=tools,
        context_manifest={"source": "explicit_fixture"},
    )
    assert result["status"] == "completed" and result["text"] == "inspected"
    assert bodies == [{"value": 2}]
    requests = store.requests(run.id)
    assert len(requests) == 2 and [row["attempt"] for row in requests] == [1, 2]
    assert requests[0]["request"]["messages"] == [
        {"role": "user", "content": "Inspect the current evidence"}
    ]
    assert requests[1]["request"]["messages"][-1]["tool_call_id"] == "ok"
    assert workflow.attempts(requests[0]["budget_id"]) == 2
    events = store.events(run.id)
    results = [row["payload"] for row in events if row["type"] == "tool/result"]
    assert results[0]["is_error"] and results[1]["value"] == {"doubled": 4}


def test_tool_loop_cannot_turn_continuations_into_fourth_dispatch(fixture):
    _, store, _, run, owner = fixture

    class Provider:
        model = "continuation-fixture"
        max_retries = 0
        supports_tools = True

        def __init__(self):
            self.calls = 0

        def complete_with_tools(self, **kwargs):
            self.calls += 1
            return ToolCallResponse(
                tool_calls=[ToolCall(str(self.calls), "inspect", {"value": self.calls})]
            )

    provider = Provider()
    harness = Harness(store, run.id, provider)
    tools = registry(lambda args: {"doubled": args["value"] * 2})
    result = harness.run_tools(
        owner=owner, node_id="a", role_id="inspector", messages=[], registry=tools
    )
    assert result["status"] == "budget_exhausted"
    assert len(store.requests(run.id)) == provider.calls == 3
    repeated = harness.run_tools(
        owner=owner, node_id="renamed", role_id="inspector", messages=[], registry=tools
    )
    assert repeated["status"] == "budget_exhausted" and provider.calls == 3


def test_role_and_strict_input_output_guards_do_not_accept_silent_schema_coercion():
    called = []
    tools = registry(lambda args: called.append(args) or {"doubled": "not an int"})
    assert tools.invoke("writer", "inspect", {"value": 2})["code"] == "tool_not_allowed"
    assert tools.invoke("inspector", "inspect", {"value": "2"})["code"] == "invalid_input"
    assert tools.invoke("inspector", "inspect", {"value": 2, "extra": 1})["is_error"]
    assert called == []
    assert tools.invoke("inspector", "inspect", {"value": 2})["code"] == "tool_failed"
    assert called == [{"value": 2}]


def test_late_cancelled_tool_settlement_is_recorded_without_publishing_or_refunding(fixture):
    workflow, store, _, run, owner = fixture
    cancelled = False

    class Provider:
        model = "cancel-fixture"
        max_retries = 0
        supports_tools = True

        def complete_with_tools(self, **kwargs):
            return ToolCallResponse(tool_calls=[ToolCall("only", "inspect", {"value": 3})])

    def body(args):
        nonlocal cancelled
        cancelled = True
        return {"doubled": args["value"] * 2}

    with pytest.raises(RunInterruptedError):
        Harness(store, run.id, Provider(), cancelled=lambda: cancelled).run_tools(
            owner=owner,
            node_id="inspect",
            role_id="inspector",
            messages=[],
            registry=registry(body),
        )
    events = store.events(run.id)
    last = events[-1]
    assert last["type"] == "tool/result"
    assert last["payload"]["status"] == "discarded"
    assert last["payload"]["observed_result"]["value"] == {"doubled": 6}
    request = store.requests(run.id)[0]
    assert workflow.attempts(request["budget_id"]) == 1
    assert len(store.requests(run.id)) == 1
