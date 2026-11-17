"""Local admission and responsible-actor contracts; no model/container calls."""


import pytest

from owcopilot.workflow.generated_sandbox_v2 import DockerSandbox
from owcopilot.workflow.generated_tools_v2 import (
    GeneratedToolStore,
    GenerationProviderAdmissionError,
)
from owcopilot.workflow.harness import BudgetOwner, Harness, HarnessStore
from tests.domain import test_workflow_capabilities_v2 as capability_fixtures
from tests.domain.test_generated_tools_v2 import setup, spec

capability = capability_fixtures.fixture


def test_local_configuration_admission_fails_before_dispatch_claim(tmp_path):
    workflow, task, run = setup(tmp_path)

    class LocalConfigurationFixture:
        max_retries = 0
        calls = 0

        def chat_model(self):
            raise ValueError("explicit local SDK construction fixture")

        def complete(self, **kwargs):
            self.calls += 1
            raise AssertionError("model must not be invoked")

    provider = LocalConfigurationFixture()
    trace = HarnessStore(workflow)
    harness = Harness(trace, run.id, provider)
    owner = BudgetOwner(
        project="scratch", task_id=task.id, product_key="generated", problem_key="original"
    )
    store = GeneratedToolStore(workflow, DockerSandbox())
    with pytest.raises(GenerationProviderAdmissionError):
        store.propose(harness, owner=owner, node_id="program", spec=spec())
    assert trace.requests(run.id) == [] and provider.calls == 0
    assert workflow.attempts("harness:" + owner.identity) == 0
    event = trace.events(run.id)[-1]
    assert event["type"] == "provider/local_admission_failed"
    assert event["payload"]["outbound_started"] is False
    assert store.versions("scratch", spec().id) == []


def test_operator_revocation_is_distinct_and_still_fences_native_body(capability):
    workflow, store, descriptor, policy, owner, run, calls, _ = capability
    enabled = capability_fixtures.enabled(store, descriptor, policy)

    def revoke():
        record = store.revoke(
            "test",
            descriptor.id,
            "1",
            expected_hash=enabled["record_hash"],
            reason="technical operator fixture",
            actor_kind="development_operator",
        )
        assert (
            record["authority"] == "development_operator"
            and record["actor_kind"] == "development_operator"
        )

    harness = Harness(
        HarnessStore(workflow), run.id, capability_fixtures.ToolProvider(before=revoke)
    )
    store.run_tools(
        harness,
        owner=owner,
        node_id="operator-fence",
        role_id="planner",
        messages=[],
        policy=policy,
    )
    assert calls == []
    result = next(
        event["payload"] for event in harness.store.events(run.id) if event["type"] == "tool/result"
    )
    assert result["is_error"] and "撤销" in result["message"]
    final = store.events("test")[-1]
    assert final["action"] == "revoked" and final["authority"] == "development_operator"
    assert final["actor_kind"] == "development_operator"
