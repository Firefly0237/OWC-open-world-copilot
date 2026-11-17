"""Unrun candidate checks; controlled providers are explicit engineering fixtures."""

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow.capabilities_v2 import (
    AdapterCatalog,
    CapabilityPolicy,
    CapabilityStore,
    UnsupportedSchema,
    compile_schema,
)
from owcopilot.workflow.harness import BudgetOwner, Harness, HarnessStore, ToolRegistry
from owcopilot.workflow.models import TaskInput, TaskRecord
from owcopilot.workflow.store import RunInterruptedError, WorkflowStore


class BranchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quest_id: str


class BranchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    branch_count: int
    state_keys: list[str]


@pytest.fixture
def fixture(tmp_path):
    workflow = WorkflowStore(tmp_path / "workflow.sqlite3")
    bundle = ContentBundle()
    task = TaskRecord(
        project="test",
        request=TaskInput(brief="Explicit capability development fixture"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    workflow.save_task(task)
    run, _ = workflow.create_run(task)
    workflow.update_run(run.id, status="running")
    catalog = AdapterCatalog()
    calls = []
    runtime = {"cancelled": False, "on_execute": None}
    frozen = {"q": {"choices": ["ask", "leave"], "state": {"has_pass": False}}}

    def inspect(arguments):
        calls.append(arguments)
        if runtime["on_execute"]:
            runtime["on_execute"]()
        value = frozen[arguments["quest_id"]]
        return {"branch_count": len(value["choices"]), "state_keys": sorted(value["state"])}

    catalog.register_python(
        "branch-reader",
        project="test",
        input_model=BranchInput,
        output_model=BranchOutput,
        execute=inspect,
        roles={"planner"},
        read_scopes={"task:frozen"},
    )
    descriptor = catalog.describe("branch-reader", "inspect_branches", project="test")
    policy = CapabilityPolicy(
        project="test", allowed_roles=("planner",), allowed_read_scopes=("task:frozen",)
    )
    store = CapabilityStore(workflow, catalog)
    owner = BudgetOwner(
        project="test", task_id=task.id, product_key="event_plan", problem_key="original-output"
    )
    return workflow, store, descriptor, policy, owner, run, calls, runtime


def enabled(store, descriptor, policy, *, authority="explicit_author"):
    record = store.discover("test", descriptor)
    checked = store.validate(
        "test", descriptor.id, "1", expected_hash=record["record_hash"], policy=policy
    )
    return store.enable(
        "test",
        descriptor.id,
        "1",
        expected_hash=checked["record_hash"],
        policy=policy,
        authority=authority,
    )


class ToolProvider:
    model = "controlled-provider-fixture"
    supports_tools = True
    max_retries = 0

    def __init__(self, name="inspect_branches", before=None):
        self.calls = 0
        self.name, self.before = name, before

    def complete_with_tools(self, *, messages, tools, model):
        self.calls += 1
        if self.calls == 1:
            if self.before:
                self.before()
            return ToolCallResponse(
                tool_calls=[
                    ToolCall(
                        call_id="fixture-call",
                        name=self.name,
                        arguments={"quest_id": "q"},
                    )
                ]
            )
        return ToolCallResponse(text="Fixture finished")

    def complete(self, *, system, user, model):
        self.calls += 1
        return "Fixture text", 1, 1, 0, self.model


def test_missing_discovery_is_pending_durable_project_scoped_and_not_a_new_adapter(fixture):
    workflow, store, _, policy, _, _, calls, _ = fixture
    pending = store.discover_missing(
        "test", "future_tool", "Need a capability that has not been supplied", roles=["planner"]
    )
    reopened = CapabilityStore(WorkflowStore(workflow.path), AdapterCatalog())
    assert (
        reopened.get("test", pending["descriptor"]["id"])["record_hash"] == pending["record_hash"]
    )
    assert reopened.list("other") == []
    with pytest.raises(KeyError):
        reopened.get("other", pending["descriptor"]["id"])
    rejected = store.validate(
        "test",
        pending["descriptor"]["id"],
        "1",
        expected_hash=pending["record_hash"],
        policy=policy,
    )
    assert rejected["descriptor"]["status"] == "discovered"
    assert "adapter" in rejected["validation_errors"][0]
    assert calls == []
    with pytest.raises(ValueError, match="LocalStdioMCPClient"):
        store.catalog.register_configured_mcp(
            "not-configured",
            project="test",
            client=object(),
            discovered_tool={},
            roles={"planner"},
            read_scopes={"task:frozen"},
        )


def test_strict_schema_preserves_optional_absence_nested_constraints_and_alias_keys():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["model_dump", "nested"],
        "properties": {
            "model_dump": {"type": "integer", "minimum": 1},
            "optional": {"type": "string"},
            "nested": {"$ref": "#/$defs/Item"},
        },
        "$defs": {
            "Item": {
                "type": "object",
                "additionalProperties": False,
                "required": ["labels"],
                "properties": {
                    "labels": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "enum": ["a", "b"]},
                    }
                },
            }
        },
    }
    model = compile_schema(schema)
    valid = {"model_dump": 2, "nested": {"labels": ["a"]}}
    assert model.model_validate(valid, strict=True).model_dump(mode="json") == valid
    assert model.model_json_schema() == schema
    for invalid in [
        {**valid, "optional": None},
        {**valid, "model_dump": True},
        {**valid, "nested": {"labels": ["c"]}},
        {**valid, "extra": 2},
        {**valid, "nested": {"labels": []}},
    ]:
        with pytest.raises(ValidationError):
            model.model_validate(invalid, strict=True)
    for unsupported in [
        {"type": "object", "properties": {}},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"x": {"type": "string", "format": "uri"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"x": {"type": ["string", "null"], "enum": ["a"]}},
        },
    ]:
        with pytest.raises(UnsupportedSchema):
            compile_schema(unsupported)


def test_grants_require_exact_policy_and_current_hash_and_contract_is_immutable(fixture):
    _, store, descriptor, policy, _, _, calls, _ = fixture
    record = store.discover("test", descriptor)
    denied = store.validate(
        "test",
        descriptor.id,
        "1",
        expected_hash=record["record_hash"],
        policy=policy.model_copy(update={"allowed_read_scopes": ()}),
    )
    assert denied["descriptor"]["status"] == "discovered"
    assert "作用域" in denied["validation_errors"][0]
    with pytest.raises(ValueError, match="状态已改变"):
        store.validate(
            "test", descriptor.id, "1", expected_hash=record["record_hash"], policy=policy
        )
    checked = store.validate(
        "test", descriptor.id, "1", expected_hash=denied["record_hash"], policy=policy
    )
    with pytest.raises(ValueError, match="预授权"):
        store.enable(
            "test",
            descriptor.id,
            "1",
            expected_hash=checked["record_hash"],
            policy=policy,
            authority="preauthorized",
        )
    granted_policy = policy.model_copy(
        update={"preauthorized_descriptor_hashes": (descriptor.descriptor_hash,)}
    )
    accepted = store.enable(
        "test",
        descriptor.id,
        "1",
        expected_hash=checked["record_hash"],
        policy=granted_policy,
        authority="preauthorized",
    )
    assert accepted["authority"] == "preauthorized"
    with pytest.raises(ValueError, match="合同不可变"):
        store.discover("test", descriptor.model_copy(update={"description": "changed"}))
    registry = ToolRegistry()
    store.inject("test", "planner", registry, policy)
    assert registry.schemas("writer") == []
    assert (
        registry.invoke("writer", descriptor.tool_name, {"quest_id": "q"})["code"]
        == "tool_not_allowed"
    )
    assert registry.invoke("planner", descriptor.tool_name, {"quest_id": "q"})["is_error"] is True
    assert calls == []  # Even enabled adapters cannot bypass their Harness facade.


def test_enabled_adapter_is_really_consumed_through_harness_and_shared_budget(fixture):
    workflow, store, descriptor, policy, owner, run, calls, _ = fixture
    enabled(store, descriptor, policy)
    provider = ToolProvider()
    harness = Harness(HarnessStore(workflow), run.id, provider)
    result = store.run_tools(
        harness,
        owner=owner,
        node_id="planner_a",
        role_id="planner",
        messages=[{"role": "user", "content": "Inspect fixture q"}],
        policy=policy,
    )
    assert result["status"] == "completed" and provider.calls == 2
    assert calls == [{"quest_id": "q"}]
    events = harness.store.events(run.id)
    actual = next(event["payload"]["value"] for event in events if event["type"] == "tool/result")
    assert actual == {"branch_count": 2, "state_keys": ["has_pass"]}
    requests = harness.store.requests(run.id)
    assert (
        requests[0]["context_manifest"]["capabilities"]["injected"][0]["schema_hash"]
        == descriptor.schema_hash
    )
    assert requests[0]["budget_id"] == requests[1]["budget_id"]
    harness.complete(
        owner=owner,
        node_id="renamed_planner",
        role_id="planner",
        system="fixture",
        user="third dispatch",
    )
    harness.complete(
        owner=owner,
        node_id="another_name",
        role_id="planner",
        system="fixture",
        user="fourth dispatch on original persisted owner",
    )
    requests = harness.store.requests(run.id)
    assert provider.calls == 4
    assert len({request["budget_id"] for request in requests}) == 1
    assert [request["attempt"] for request in requests] == [1, 2, 3, 4]


def test_revocation_after_injection_prevents_body_and_keeps_trace(fixture):
    workflow, store, descriptor, policy, owner, run, calls, _ = fixture
    accepted = enabled(store, descriptor, policy)

    def revoke():
        store.revoke(
            "test",
            descriptor.id,
            "1",
            expected_hash=accepted["record_hash"],
            reason="Explicit fixture withdrawal",
        )

    provider = ToolProvider(before=revoke)
    harness = Harness(HarnessStore(workflow), run.id, provider)
    store.run_tools(
        harness, owner=owner, node_id="planner", role_id="planner", messages=[], policy=policy
    )
    assert calls == []
    result = next(
        event["payload"] for event in harness.store.events(run.id) if event["type"] == "tool/result"
    )
    assert result["is_error"] is True and "撤销" in result["message"]
    revoked = store.get("test", descriptor.id)
    with pytest.raises(ValueError, match="撤销"):
        store.enable(
            "test",
            descriptor.id,
            "1",
            expected_hash=revoked["record_hash"],
            policy=policy,
            authority="explicit_author",
        )


def test_unknown_native_call_becomes_pending_gap_without_execution(fixture):
    workflow, store, descriptor, policy, owner, run, calls, _ = fixture
    enabled(store, descriptor, policy)
    harness = Harness(HarnessStore(workflow), run.id, ToolProvider(name="future_tool"))
    store.run_tools(
        harness, owner=owner, node_id="planner", role_id="planner", messages=[], policy=policy
    )
    gap = next(
        record
        for record in store.list("test")
        if record["descriptor"]["tool_name"] == "future_tool"
    )
    assert (
        gap["descriptor"]["status"] == "discovered" and gap["descriptor"]["adapter_binding"] is None
    )
    assert gap["descriptor"]["source_ref"]["run_id"] == run.id
    assert calls == []


def test_capability_cancellation_discards_late_result_without_continuation(fixture):
    workflow, store, descriptor, policy, owner, run, calls, runtime = fixture
    enabled(store, descriptor, policy)
    runtime["on_execute"] = lambda: runtime.update(cancelled=True)
    provider = ToolProvider()
    harness = Harness(
        HarnessStore(workflow), run.id, provider, cancelled=lambda: runtime["cancelled"]
    )
    with pytest.raises(RunInterruptedError):
        store.run_tools(
            harness, owner=owner, node_id="planner", role_id="planner", messages=[], policy=policy
        )
    assert len(calls) == 1 and provider.calls == 1
    late = next(
        event["payload"] for event in harness.store.events(run.id) if event["type"] == "tool/result"
    )
    assert late["status"] == "discarded" and late["observed_result"]["value"]["branch_count"] == 2
