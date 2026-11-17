"""Production wiring contract; labelled fixture transports, never Docker/model evidence."""

import json

import pytest

from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow.generated_sandbox_v2 import DockerSandbox
from owcopilot.workflow.generated_story_tools_v2 import (
    TOOL_NAME,
    GeneratedStoryTools,
    route_cases,
    route_spec,
)
from owcopilot.workflow.harness import Harness
from owcopilot.workflow.project_v2 import NarrativeProjectStore
from owcopilot.workflow.store import WorkflowStore

SOURCE = "def run(payload):\n    return {'reachable': [], 'unreachable': []}"
ACTUAL = {
    "scenes": ["start", "middle", "orphan"],
    "links": [{"source": "start", "target": "middle"}],
    "entry": "start",
}
ACTUAL_RESULT = {"reachable": ["start", "middle"], "unreachable": ["orphan"]}


class FixtureSandbox:
    """Fixed observations only: no generated source evaluation, no Docker claims."""

    policy = DockerSandbox().policy

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def run(self, source, arguments, *, cancelled=None):
        assert source == SOURCE
        assert not cancelled or not cancelled()
        self.calls.append(arguments)
        if self.fail:
            value = {"reachable": [], "unreachable": []}
        elif arguments == ACTUAL:
            value = ACTUAL_RESULT
        elif arguments["entry"] == "A":
            value = {"reachable": ["A", "B", "C"], "unreachable": ["D"]}
        else:
            value = {"reachable": [], "unreachable": ["A", "B"]}
        return {
            "status": "completed",
            "value": value,
            "error": "",
            "fixture": "transport-only; no program execution",
        }


class FixtureProvider:
    model = "labelled-engineering-transport"
    max_retries = 0
    supports_tools = True

    def __init__(self, *, wrong_arguments=False, unknown=False):
        self.synthesis = []
        self.native_calls = 0
        self.wrong_arguments = wrong_arguments
        self.unknown = unknown

    def complete(self, *, system, user, model):
        self.synthesis.append(json.loads(user))
        if self.unknown:
            raise TimeoutError("labelled fixture: response lost after unknown dispatch")
        return (
            json.dumps({"description": "fixture source, no real model evidence", "source": SOURCE}),
            1,
            1,
            0,
            model,
        )

    def complete_with_tools(self, **kwargs):
        self.native_calls += 1
        if self.native_calls == 1:
            arguments = json.loads(kwargs["messages"][-1]["content"])
            assert arguments == ACTUAL
            if self.wrong_arguments:
                arguments = {**arguments, "entry": "missing"}
            return ToolCallResponse(tool_calls=[ToolCall("fixture-call", TOOL_NAME, arguments)])
        return ToolCallResponse(
            text="Observed fixture tool feedback", input_tokens=1, output_tokens=1
        )


def setup(tmp_path):
    workflow = WorkflowStore(tmp_path / "generated-story-fixture.sqlite3")
    projects = NarrativeProjectStore(workflow)
    target = projects.create("scratch", "Fixture actual project")
    outline = {
        "chapters": [{"id": "chapter", "title": "Chapter"}],
        "scenes": [
            {"id": scene, "chapter_id": "chapter", "title": scene} for scene in ACTUAL["scenes"]
        ],
        "branches": [
            {"id": "branch", "source_scene": "start", "target_scene": "middle", "label": "Continue"}
        ],
        "entry_scene_id": "start",
    }
    target = projects.save_outline(
        "scratch", target["id"], outline, expected_project_hash=target["project_hash"]
    )
    return workflow, projects, target


def changed(projects, target):
    outline = target["outline"]
    outline["scenes"][0]["title"] += " changed"
    return projects.save_outline(
        "scratch", target["id"], outline, expected_project_hash=target["project_hash"]
    )


def test_real_store_harness_wiring_and_restart_reuse_then_technical_revoke(tmp_path):
    workflow, projects, target = setup(tmp_path)
    sandbox, provider = FixtureSandbox(), FixtureProvider()
    service = GeneratedStoryTools(workflow, sandbox=sandbox)
    row, dispatched = service.prepare("scratch", target["id"], target["project_hash"])
    assert dispatched
    result = service.execute("scratch", row["id"], provider=provider)
    assert result["status"] == "passed", result.get("error")
    assert result["native_result"] == ACTUAL_RESULT
    assert len(sandbox.calls) == 3 and len(provider.synthesis) == 1
    assert result["arguments"] == ACTUAL and result["author_acceptance"] == "unknown"
    assert result["technical_authorization"]["actor_kind"] == "development_operator"
    assert len(result["native_receipt"]["executions"]) == 1
    same, dispatched = service.prepare("scratch", target["id"], target["project_hash"])
    assert not dispatched and same["run_id"] == result["run_id"]

    target = changed(projects, target)
    restored = GeneratedStoryTools(workflow, sandbox=sandbox)
    row, _ = restored.prepare("scratch", target["id"], target["project_hash"])
    reuse_provider = FixtureProvider()
    reused = restored.execute("scratch", row["id"], provider=reuse_provider)
    assert reused["status"] == "passed", reused.get("error")
    assert reused["code_reused"] and reused["version_id"] == result["version_id"]
    assert reused["owner"] == result["owner"] and reuse_provider.synthesis == []
    assert len(restored.versions("scratch")["versions"]) == 1
    revoked = restored.revoke("scratch", reused["activation_id"], reason="technical fixture revoke")
    assert revoked["capability"]["authority"] == "development_operator"
    target = changed(projects, target)
    with pytest.raises(ValueError, match="撤销"):
        restored.prepare("scratch", target["id"], target["project_hash"])


def test_failed_validation_revises_once_only_with_reason_and_same_owner(tmp_path):
    workflow, _, target = setup(tmp_path)
    sandbox = FixtureSandbox(fail=True)
    service = GeneratedStoryTools(workflow, sandbox=sandbox)
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    failed = service.execute("scratch", row["id"], provider=FixtureProvider())
    assert failed["status"] == "failed" and failed["validation"]["status"] == "failed"
    with pytest.raises(ValueError, match="attempt_reason"):
        service.prepare("scratch", target["id"], target["project_hash"])
    row, dispatched = service.prepare(
        "scratch",
        target["id"],
        target["project_hash"],
        attempt_reason="fixture diagnosed wrong partition; correct code",
    )
    assert dispatched and row["owner"] == failed["owner"]
    sandbox.fail = False
    provider = FixtureProvider()
    result = service.execute("scratch", row["id"], provider=provider)
    assert result["status"] == "passed", result.get("error")
    assert result["attempts"][0]["run_id"] == failed["run_id"]
    assert provider.synthesis[0]["prior_program"]["id"] == failed["version_id"]
    assert provider.synthesis[0]["execution_feedback"]["id"] == failed["validation"]["id"]
    assert len(provider.synthesis) == 1


def test_wrong_native_input_cannot_pass_as_actual_story_analysis(tmp_path):
    workflow, _, target = setup(tmp_path)
    service = GeneratedStoryTools(workflow, sandbox=FixtureSandbox())
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    result = service.execute("scratch", row["id"], provider=FixtureProvider(wrong_arguments=True))
    assert result["status"] == "failed" and "唯一真实native" in result["error"]
    assert result["native_result"] is None and result["native_receipt"]["calls"]


def test_unknown_dispatch_is_persisted_and_never_replayed(tmp_path):
    workflow, projects, target = setup(tmp_path)
    service = GeneratedStoryTools(workflow, sandbox=FixtureSandbox())
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    provider = FixtureProvider(unknown=True)
    result = service.execute("scratch", row["id"], provider=provider)
    assert result["status"] == "unknown", result.get("error")
    original, dispatched = service.prepare(
        "scratch", target["id"], target["project_hash"], attempt_reason="must not replay unknown"
    )
    assert not dispatched and original["run_id"] == result["run_id"]
    assert len(provider.synthesis) == 1
    target = changed(projects, target)
    with pytest.raises(ValueError, match="未知"):
        service.prepare("scratch", target["id"], target["project_hash"])


def test_native_unknown_preserves_actual_tool_observation_without_claiming_completion(tmp_path):
    class UnknownContinuation(FixtureProvider):
        def complete_with_tools(self, **kwargs):
            if self.native_calls == 1:
                raise TimeoutError("fixture lost native continuation response")
            return super().complete_with_tools(**kwargs)

    workflow, _, target = setup(tmp_path)
    service = GeneratedStoryTools(workflow, sandbox=FixtureSandbox())
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    result = service.execute("scratch", row["id"], provider=UnknownContinuation())
    assert result["status"] == "unknown", result.get("error")
    assert result["native_result"] is None
    observed = result["native_receipt"]["events"]
    assert any(
        e["type"] == "tool/result" and e["payload"].get("value") == ACTUAL_RESULT for e in observed
    )
    original, dispatched = service.prepare(
        "scratch",
        target["id"],
        target["project_hash"],
        attempt_reason="unknown continuation must not replay",
    )
    assert not dispatched and original["run_id"] == row["run_id"]


def test_stale_outline_and_lost_job_fail_closed_without_model_call(tmp_path):
    workflow, projects, target = setup(tmp_path)
    service = GeneratedStoryTools(workflow, sandbox=FixtureSandbox())
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    target = changed(projects, target)
    provider = FixtureProvider()
    failed = service.execute("scratch", row["id"], provider=provider)
    assert failed["status"] == "failed" and "hash" in failed["error"]
    assert provider.synthesis == [] and provider.native_calls == 0
    new, _ = service.prepare("scratch", target["id"], target["project_hash"])
    lost = service.mark_lost_job("scratch", new["id"])
    assert lost["status"] == "unknown"
    assert service.get("scratch", new["id"])["status"] == "unknown"


def test_explicit_rollback_creates_new_activation_without_resurrecting_revoked_id(tmp_path):
    workflow, _, target = setup(tmp_path)
    service = GeneratedStoryTools(workflow, sandbox=FixtureSandbox())
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    first = service.execute("scratch", row["id"], provider=FixtureProvider())
    assert first["status"] == "passed", first.get("error")
    # Explicitly prepare another version through the same production core APIs.
    # This is fixture transport evidence for lifecycle, not real model execution.
    owner = service.owner("scratch")
    run, _ = workflow.create_run(workflow.get_task(owner.task_id))
    version = service.generated.propose(
        Harness(service.trace, run.id, FixtureProvider()),
        owner=owner,
        node_id="program_synthesis",
        spec=route_spec("scratch"),
        prior_version_id=first["version_id"],
    )
    validation = service.generated.validate("scratch", version["id"], route_cases())
    service.generated.revoke(
        "scratch",
        first["activation_id"],
        capabilities=service.capabilities,
        reason="fixture technical version replacement",
        actor_kind="development_operator",
    )
    prepared = service.generated.prepare_capability(
        "scratch",
        version["id"],
        validation["id"],
        catalog=service.catalog,
        capabilities=service.capabilities,
        policy=service.policy("scratch"),
    )
    second = service._authorize("scratch", prepared)
    service.revoke("scratch", second["activation"]["id"], reason="fixture revoke second")
    rolled = service.rollback(
        "scratch",
        second["activation"]["id"],
        first["version_id"],
        reason="explicit diagnosed rollback",
    )
    assert rolled["activation"]["version_id"] == first["version_id"]
    assert rolled["activation"]["id"] != first["activation_id"]
    assert rolled["activation"]["status"] == "enabled"
    assert service.generated._activation("scratch", first["activation_id"])["status"] == "revoked"
    assert service._control("scratch")["disabled"] is False


def test_first_validated_version_explicit_reactivation_is_fresh_and_never_model_retry(tmp_path):
    workflow, projects, target = setup(tmp_path)
    sandbox, provider = FixtureSandbox(), FixtureProvider()
    service = GeneratedStoryTools(workflow, sandbox=sandbox)
    row, _ = service.prepare("scratch", target["id"], target["project_hash"])
    first = service.execute("scratch", row["id"], provider=provider)
    assert first["status"] == "passed", first.get("error")
    service.revoke("scratch", first["activation_id"], reason="fixture revoke only version")
    old = service.generated._activation("scratch", first["activation_id"])
    old_cap = service.capabilities.get("scratch", old["capability_id"], old["capability_version"])
    target = changed(projects, target)
    with pytest.raises(ValueError, match="撤销"):
        service.prepare("scratch", target["id"], target["project_hash"])
    dispatches_before = len(service.trace.requests(first["run_id"]))
    calls_before = len(sandbox.calls)
    restored = service.activate(
        "scratch",
        first["version_id"],
        reason="explicit first-version permission restoration",
        actor_kind="development_operator",
    )
    fresh = restored["activation"]
    assert fresh["id"] != old["id"] and fresh["capability_version"] != old["capability_version"]
    assert fresh["version_id"] == old["version_id"] and fresh["status"] == "enabled"
    assert service.generated._activation("scratch", old["id"]) == old
    assert (
        service.capabilities.get("scratch", old["capability_id"], old["capability_version"])
        == old_cap
    )
    assert restored["activation_request"]["actor_kind"] == "development_operator"
    assert restored["activation_request"]["author_acceptance"] == "unknown"
    assert (
        restored["activation_request"]["descriptor_hash"]
        == restored["capability"]["descriptor_hash"]
    )
    assert len(service.trace.requests(first["run_id"])) == dispatches_before
    assert len(sandbox.calls) == calls_before
    repeated = service.activate("scratch", first["version_id"], reason="same active version again")
    assert repeated["idempotent_replay"] and repeated["activation"] == fresh
    assert repeated["activation_request"] == restored["activation_request"]
    assert len(service.generated.versions("scratch", "branch_reachability")) == 1
    reconstructed = GeneratedStoryTools(workflow, sandbox=sandbox)
    assert (
        reconstructed.versions("scratch")["versions"][0]["activation_request"]
        == restored["activation_request"]
    )
