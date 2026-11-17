"""Real Docker checks are opt-in; code/providers here are labelled engineering fixtures."""

import json
import os

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow.capabilities_v2 import AdapterCatalog, CapabilityPolicy, CapabilityStore
from owcopilot.workflow.generated_sandbox_v2 import DockerSandbox, SandboxLimits, inspect_program
from owcopilot.workflow.generated_tools_v2 import (
    GeneratedToolSpec,
    GeneratedToolStore,
    ToolValidationCase,
)
from owcopilot.workflow.harness import BudgetOwner, Harness, HarnessStore, ToolRegistry
from owcopilot.workflow.models import TaskInput, TaskRecord
from owcopilot.workflow.store import WorkflowStore

real_docker = pytest.mark.skipif(
    os.getenv("OWC_DOCKER_ACCEPTANCE") != "1",
    reason="requires explicitly authorized real Docker; no mock fallback",
)


def spec():
    return GeneratedToolSpec(
        project="scratch",
        id="branch_counter",
        tool_name="count_branch_options",
        purpose="Count supplied option counts without filesystem, network or side effects",
        input_schema={
            "type": "object",
            "properties": {
                "counts": {"type": "array", "items": {"type": "integer"}, "maxItems": 10}
            },
            "required": ["counts"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"total": {"type": "integer"}},
            "required": ["total"],
            "additionalProperties": False,
        },
        roles=("branch_reader",),
    )


def setup(tmp_path):
    workflow = WorkflowStore(tmp_path / "generated-scratch.sqlite3")
    task = TaskRecord(
        project="scratch",
        request=TaskInput(brief="Explicit generated tool engineering fixture"),
        base_version="v1",
        base_hash=content_hash({}),
        base_bundle={},
    )
    workflow.save_task(task)
    run, _ = workflow.create_run(task)
    return workflow, task, run


def test_source_is_statically_rejected_before_any_execution(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    for source in (
        "import os\ndef run(payload): return {}",
        "open(" + repr(str(sentinel)) + ",'w')\ndef run(payload): return {}",
        "def run(payload): return payload.__class__",
        "def run(payload): return eval('1+1')",
    ):
        with pytest.raises(ValueError):
            inspect_program(source)
    assert not sentinel.exists()
    assert (
        inspect_program("def run(payload):\n    return {'total': sum(payload['counts'])}")[
            "entrypoint"
        ]
        == "run"
    )


def test_fixture_version_has_explicit_provenance_and_immutable_hash(tmp_path):
    workflow, _, _ = setup(tmp_path)
    store = GeneratedToolStore(workflow, DockerSandbox())
    version = store.record_engineering_fixture(
        spec(),
        "def run(payload):\n    return {'total': sum(payload['counts'])}",
        label="supplied fixture code, not model generated",
    )
    assert version["source_ref"]["kind"] == "engineering_fixture"
    assert store.get("scratch", version["id"])["version_hash"] == version["version_hash"]
    with workflow.connection() as db:
        damaged = {**version, "source": "different"}
        db.execute(
            "UPDATE generated_tool_versions_v2 SET payload=? WHERE id=?",
            (json.dumps(damaged), version["id"]),
        )
    with pytest.raises(ValueError, match="hash"):
        store.get("scratch", version["id"])


@real_docker
def test_real_container_isolation_and_bounded_timeout_output_cancel(tmp_path):
    sandbox = DockerSandbox(limits=SandboxLimits(timeout_seconds=2.0, max_output_bytes=2048))
    probe = sandbox.probe()
    assert probe["status"] == "completed" and probe["cleanup"] == "removed"
    observed = probe["value"]
    assert observed["uid"] == observed["gid"] == 65534
    assert observed["cap_eff"] == "0000000000000000" and observed["no_new_privs"] == "1"
    assert observed["seccomp"] == "2" and observed["root_write_denied"]
    assert observed["external_network_denied"] and not observed["docker_socket_present"]
    assert "DEEPSEEK_API_KEY" not in observed["environment_names"]
    assert probe["observed_config"]["mounts"] == []
    timeout = sandbox.run("def run(payload):\n    while True:\n        pass", {})
    assert timeout["status"] == "timeout" and timeout["cleanup"] == "removed"
    output = sandbox.run("def run(payload):\n    return {'value': 'x' * 10000}", {})
    assert output["status"] == "program_error" and "output limit" in output["error"]
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 2

    cancelled_result = sandbox.run(
        "def run(payload):\n    while True:\n        pass", {}, cancelled=cancelled
    )
    assert cancelled_result["status"] == "cancelled" and cancelled_result["cleanup"] == "removed"
    (tmp_path / "real-isolation-receipts.json").write_text(
        json.dumps([probe, timeout, output, cancelled_result], indent=2), encoding="utf-8"
    )


class NativeFixture:
    model = "explicit-native-engineering-fixture"
    max_retries = 0
    supports_tools = True

    def __init__(self, expect_error=False):
        self.calls = 0
        self.expect_error = expect_error

    def complete_with_tools(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ToolCallResponse(
                tool_calls=[
                    ToolCall("actual-container-call", "count_branch_options", {"counts": [2, 3]})
                ]
            )
        result = json.loads(kwargs["messages"][-1]["content"])
        assert result["is_error"] is self.expect_error
        if not self.expect_error:
            assert result["value"] == {"total": 5}
        return ToolCallResponse(
            text="actual fixture tool observation", input_tokens=1, output_tokens=1
        )


def preauthorize(prepared):
    return CapabilityPolicy(
        project="scratch",
        allowed_roles=("branch_reader",),
        allowed_read_scopes=("generated:pure_json",),
        preauthorized_descriptor_hashes=(prepared["capability"]["descriptor_hash"],),
    )


@real_docker
def test_real_validation_native_call_revoke_and_older_code_rollback(tmp_path):
    workflow, task, run = setup(tmp_path)
    sandbox = DockerSandbox()
    generated = GeneratedToolStore(workflow, sandbox)
    catalog = AdapterCatalog()
    capabilities = CapabilityStore(workflow, catalog)
    base_policy = CapabilityPolicy(
        project="scratch",
        allowed_roles=("branch_reader",),
        allowed_read_scopes=("generated:pure_json",),
    )
    old = generated.record_engineering_fixture(
        spec(),
        "def run(payload):\n    return {'total': sum(payload['counts'])}",
        label="explicit old fixture",
    )
    newer = generated.record_engineering_fixture(
        spec(),
        "def run(payload):\n    return {'total': sum(payload['counts']) + 0}",
        label="explicit newer fixture",
        prior_version_id=old["id"],
    )
    case = ToolValidationCase(id="two-scenes", arguments={"counts": [2, 3]}, expected={"total": 5})
    old_check = generated.validate("scratch", old["id"], [case])
    new_check = generated.validate("scratch", newer["id"], [case])
    assert old_check["status"] == new_check["status"] == "passed"
    prepared = generated.prepare_capability(
        "scratch",
        newer["id"],
        new_check["id"],
        catalog=catalog,
        capabilities=capabilities,
        policy=base_policy,
    )
    assert prepared["capability"]["descriptor"]["adapter_binding"]["kind"] == "generated_container"
    policy = preauthorize(prepared)
    activated = generated.enable(
        "scratch",
        prepared["activation"]["id"],
        capabilities=capabilities,
        policy=policy,
        authority="preauthorized",
    )
    # Persisted code/validation/activation can be restored in a new catalog; no
    # model call, regenerated code or fresh permission grant is needed.
    catalog = AdapterCatalog()
    capabilities = CapabilityStore(workflow, catalog)
    restored_bindings = generated.restore_bindings(
        "scratch", catalog=catalog, capabilities=capabilities, policy=policy
    )
    assert restored_bindings[0]["activation_id"] == activated["activation"]["id"]
    owner = BudgetOwner(
        project="scratch",
        task_id=task.id,
        product_key="generated-tool-fixture",
        problem_key="branch-counting",
    )
    trace = HarnessStore(workflow)
    registry = ToolRegistry()
    result = capabilities.run_tools(
        Harness(trace, run.id, NativeFixture()),
        owner=owner,
        node_id="consumer",
        role_id="branch_reader",
        messages=[{"role": "user", "content": "Count actual supplied branch options"}],
        policy=policy,
        registry=registry,
    )
    assert result["status"] == "completed"
    generated.revoke(
        "scratch",
        activated["activation"]["id"],
        capabilities=capabilities,
        reason="explicit technical revoke check",
    )
    before = len(
        [
            event
            for event in generated.events("scratch", spec().id)
            if event["action"] == "program_executed"
        ]
    )
    blocked = capabilities.run_tools(
        Harness(trace, run.id, NativeFixture(expect_error=True)),
        owner=owner,
        node_id="stale-registry",
        role_id="branch_reader",
        messages=[],
        policy=policy,
        registry=registry,
    )
    assert blocked["status"] == "completed"
    assert (
        len(
            [
                event
                for event in generated.events("scratch", spec().id)
                if event["action"] == "program_executed"
            ]
        )
        == before
    )
    rollback = generated.rollback(
        "scratch",
        activated["activation"]["id"],
        old["id"],
        old_check["id"],
        capabilities=capabilities,
        catalog=catalog,
        policy=base_policy,
        reason="explicit older code rollback check",
    )
    rollback_policy = preauthorize(rollback)
    restored = generated.enable(
        "scratch",
        rollback["activation"]["id"],
        capabilities=capabilities,
        policy=rollback_policy,
        authority="preauthorized",
    )
    assert restored["activation"]["id"] != activated["activation"]["id"]
    assert restored["activation"]["version_id"] == old["id"]
    assert (
        capabilities.get(
            "scratch",
            activated["activation"]["capability_id"],
            activated["activation"]["capability_version"],
        )["descriptor"]["status"]
        == "revoked"
    )
    result = capabilities.run_tools(
        Harness(trace, run.id, NativeFixture()),
        owner=owner,
        node_id="rollback-consumer",
        role_id="branch_reader",
        messages=[],
        policy=rollback_policy,
    )
    assert result["status"] == "completed"
    assert [event["type"] for event in trace.events(run.id)].count("generated_tool/execution") == 2
    (tmp_path / "real-lifecycle-receipts.json").write_text(
        json.dumps(
            {
                "old": old,
                "new": newer,
                "old_check": old_check,
                "new_check": new_check,
                "events": generated.events("scratch", spec().id),
                "harness_events": trace.events(run.id),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@real_docker
def test_real_schema_failure_cannot_be_registered(tmp_path):
    workflow, _, _ = setup(tmp_path)
    store = GeneratedToolStore(workflow, DockerSandbox())
    version = store.record_engineering_fixture(
        spec(),
        "def run(payload):\n    return {'total': 'wrong type'}",
        label="explicit invalid fixture",
    )
    check = store.validate(
        "scratch",
        version["id"],
        [ToolValidationCase(id="strict", arguments={"counts": [2]}, expected={"total": 2})],
    )
    assert (
        check["status"] == "failed"
        and "strict output schema rejected" in check["cases"][0]["error"]
    )
    catalog = AdapterCatalog()
    capabilities = CapabilityStore(workflow, catalog)
    with pytest.raises(ValueError, match="passed"):
        store.prepare_capability(
            "scratch",
            version["id"],
            check["id"],
            catalog=catalog,
            capabilities=capabilities,
            policy=CapabilityPolicy(
                project="scratch",
                allowed_roles=("branch_reader",),
                allowed_read_scopes=("generated:pure_json",),
            ),
        )
    assert capabilities.list("scratch") == []
