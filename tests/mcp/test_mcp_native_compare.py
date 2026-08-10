from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from owcopilot.agent import ReActAgent
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.evaluation.capability_decision import (
    ArtifactBindings,
    CapabilityDecisionCard,
    FixedBudget,
    HeldoutRunManifest,
    MetricGate,
)
from owcopilot.evaluation.mcp_native_compare import (
    ArmObservation,
    ArmResourceUsage,
    ComparisonContractError,
    ExpectedAuditResult,
    ExpectedFailureSemantics,
    FailureSemantics,
    MCPBoundaryEvidence,
    NativeMCPComparisonCase,
    agent_arm_observation,
    comparison_dataset_digest,
    expected_audit_result,
    registry_with_tool_timing,
    run_native_mcp_comparison,
    skill_semantic_contract_digest,
    validate_recomputable_report,
)
from owcopilot.fakes import scripted_chat_model
from owcopilot.mcp_client import (
    MCPCallError,
    MCPErrorCode,
    MCPInvocationTrace,
    MCPObservation,
    MCPPhase,
    default_local_config,
)
from owcopilot.mcp_client.agent_route import registry_with_mcp_audit
from owcopilot.skills import default_skill_registry

MCP_INSTALLED = importlib.util.find_spec("mcp") is not None
REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_SHA256 = "d" * 64
SEMANTIC_SHA256 = "e" * 64

AUDIT_RESULT: dict[str, Any] = {
    "content_hash": "a" * 64,
    "audit_run": {"id": "volatile-run"},
    "issues": [
        {
            "id": "volatile-issue",
            "audit_run_id": "volatile-run",
            "rule_code": "UNKNOWN_ENTITY_REFERENCE",
            "severity": "error",
            "category": "reference",
            "target_ref": "quest:q_missing",
            "message": "runtime prose is not evidence identity",
            "evidence": [
                {
                    "kind": "field_path",
                    "target_ref": "quest:q_missing",
                    "path": "giver_npc",
                    "data": {},
                }
            ],
            "status": "open",
        }
    ],
    "open_errors": 1,
    "cost_budget": {},
    "total": 1,
    "truncation_notice": None,
}


def _usage(*, model_calls: int = 2, tool_calls: int = 1) -> ArmResourceUsage:
    return ArmResourceUsage(
        model_calls=model_calls,
        tool_calls=tool_calls,
        retrieval_calls=0,
        total_tokens=0,
        context_tokens=0,
        peak_concurrency=1,
        candidate_count=0,
    )


def _boundary() -> MCPBoundaryEvidence:
    return MCPBoundaryEvidence(
        protocol_version="2025-11-25",
        server_name="owcopilot-local-consumer",
        schema_fingerprint="b" * 64,
        content_bytes=512,
        untrusted_input=True,
    )


def _cases(
    expected: ExpectedAuditResult,
    *,
    semantic_sha256: str = SEMANTIC_SHA256,
) -> tuple[NativeMCPComparisonCase, ...]:
    common = {
        "task": "Run audit_project once and report the checked result.",
        "candidate_route": "mcp-stdio",
        "semantic_contract_sha256": semantic_sha256,
        "expected": expected,
    }
    return (
        NativeMCPComparisonCase(
            case_id="enable-stdio-boundary",
            partition="enable",
            arm_order="native_first",
            **common,
        ),
        NativeMCPComparisonCase(
            case_id="disable-same-process",
            partition="disable",
            arm_order="mcp_first",
            **common,
        ),
        NativeMCPComparisonCase(
            case_id="fault-call-timeout",
            partition="fault",
            arm_order="native_first",
            expected_mcp_failure=ExpectedFailureSemantics(
                decision_key="mcp_call_timeout",
                code="CALL_TIMEOUT",
                phase="call",
                retryable=False,
                outcome_unknown=True,
            ),
            **common,
        ),
    )


def _card(cases: tuple[NativeMCPComparisonCase, ...]) -> CapabilityDecisionCard:
    return CapabilityDecisionCard(
        capability_id="mcp.audit-project-route",
        policy_version="mcp-route-v1",
        frozen_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
        real_problem=(
            "Some audit consumers require a separately trusted process and standard discovery."
        ),
        reproducible_failure_ids=("mcp-call-timeout-dev",),
        baseline="native-in-process",
        candidates=("mcp-stdio",),
        enable_if=("cross-process discovery and trust boundary are required",),
        disable_if=("the caller and tool share one trusted process",),
        budget=FixedBudget(
            model_calls=3,
            tool_calls=2,
            retrieval_calls=0,
            total_tokens=100,
            context_tokens=100,
            deadline_ms=60_000,
            max_concurrency=1,
            candidate_window=0,
        ),
        primary_metric=MetricGate(
            name="task_success", direction="min", threshold=0.9, unit="rate"
        ),
        guardrails=(
            MetricGate(
                name="evidence_parity", direction="min", threshold=1.0, unit="rate"
            ),
        ),
        failure_semantics={
            "mcp_call_timeout": "typed timeout; outcome unknown; no hidden native fallback"
        },
        fallback="remain failed when the MCP outcome is unknown; never replay natively",
        bindings=ArtifactBindings(
            dataset_sha256=comparison_dataset_digest(cases),
            revision_sha256="a" * 64,
            policy_sha256="c" * 64,
            model_sha256=MODEL_SHA256,
        ),
        dev_case_ids=("dev-mcp-route",),
        heldout_case_ids=tuple(case.case_id for case in cases),
        enable_case_ids=("enable-stdio-boundary",),
        disable_case_ids=("disable-same-process",),
        fault_case_ids=("fault-call-timeout",),
    )


def _manifest(card: CapabilityDecisionCard) -> HeldoutRunManifest:
    return HeldoutRunManifest(
        decision_sha256=card.digest(),
        policy_version=card.policy_version,
        started_at=card.frozen_at + timedelta(minutes=1),
        case_ids=card.heldout_case_ids,
        budget=card.budget,
        bindings=card.bindings,
    )


def test_paired_report_covers_enable_disable_fault_without_preselected_winner() -> None:
    expected = expected_audit_result(AUDIT_RESULT)
    cases = _cases(expected)
    card = _card(cases)
    manifest = _manifest(card)
    runner_inputs: list[tuple[str, int, int]] = []

    def native_runner(
        case: NativeMCPComparisonCase, budget: FixedBudget
    ) -> ArmObservation:
        runner_inputs.append((f"native:{case.case_id}", id(case), id(budget)))
        return ArmObservation(
            model_sha256=MODEL_SHA256,
            semantic_contract_sha256=SEMANTIC_SHA256,
            transport="in_process",
            completed=True,
            usage=_usage(),
            result=AUDIT_RESULT,
            tool_latency_ms=4.0,
            transport_latency_ms=0.0,
        )

    def mcp_runner(case: NativeMCPComparisonCase, budget: FixedBudget) -> ArmObservation:
        runner_inputs.append((f"mcp:{case.case_id}", id(case), id(budget)))
        if case.partition == "fault":
            return ArmObservation(
                model_sha256=MODEL_SHA256,
                semantic_contract_sha256=SEMANTIC_SHA256,
                transport="stdio",
                completed=False,
                usage=_usage(),
                failure=FailureSemantics(
                    code="CALL_TIMEOUT",
                    phase="call",
                    retryable=False,
                    outcome_unknown=True,
                    fallback_used=False,
                ),
                tool_latency_ms=7.0,
                transport_latency_ms=7.0,
            )
        return ArmObservation(
            model_sha256=MODEL_SHA256,
            semantic_contract_sha256=SEMANTIC_SHA256,
            transport="stdio",
            completed=True,
            usage=_usage(),
            result=AUDIT_RESULT,
            tool_latency_ms=9.0,
            transport_latency_ms=9.0,
            boundary=_boundary(),
        )

    report = run_native_mcp_comparison(
        card,
        manifest,
        cases,
        candidate_route="mcp-stdio",
        native_runner=native_runner,
        mcp_runner=mcp_runner,
    )

    assert report.winner_predeclared is False
    assert report.native_role == "same_process_low_overhead_baseline"
    assert report.mcp_value_condition == (
        "cross_process_standard_discovery_contract_and_trust_boundary"
    )
    assert report.enable_if == card.enable_if
    assert report.disable_if == card.disable_if
    assert report.no_measured_net_benefit_policy == "default_to_native_simple_route"
    assert report.negative_result_policy == "retain_without_posthoc_override"
    assert report.aggregate.native_task_success.value == 1.0
    assert report.aggregate.mcp_task_success.value == pytest.approx(2 / 3, abs=1e-6)
    assert report.aggregate.open_errors_parity.model_dump() == {
        "numerator": 2,
        "denominator": 2,
        "value": 1.0,
    }
    assert report.aggregate.evidence_parity.value == 1.0
    assert report.aggregate.expected_fault_semantics.value == 1.0
    assert [item.metrics.transport_overhead_ms for item in report.cases] == [5.0, 5.0, 3.0]
    assert report.cases[-1].mcp.decision_failure_semantic == (
        "typed timeout; outcome unknown; no hidden native fallback"
    )
    assert "fallback_used=false" in next(iter(report.aggregate.mcp_failure_semantics))
    assert all(item.native.model_sha256 == item.mcp.model_sha256 for item in report.cases)
    assert all(
        item.native.semantic_contract_sha256 == item.mcp.semantic_contract_sha256
        for item in report.cases
    )
    for case in cases:
        paired = [entry for entry in runner_inputs if entry[0].endswith(case.case_id)]
        assert paired[0][1:] == paired[1][1:]
    validate_recomputable_report(card, report)


def test_report_recompute_rejects_metric_or_frozen_dataset_tampering() -> None:
    expected = expected_audit_result(AUDIT_RESULT)
    cases = _cases(expected)
    card = _card(cases)

    def runner(
        case: NativeMCPComparisonCase, budget: FixedBudget
    ) -> ArmObservation:
        if case.partition == "fault":
            return ArmObservation(
                model_sha256=MODEL_SHA256,
                semantic_contract_sha256=SEMANTIC_SHA256,
                transport="stdio",
                completed=False,
                usage=_usage(),
                failure=FailureSemantics(
                    code="CALL_TIMEOUT", phase="call", outcome_unknown=True
                ),
                tool_latency_ms=1.0,
                transport_latency_ms=1.0,
            )
        return ArmObservation(
            model_sha256=MODEL_SHA256,
            semantic_contract_sha256=SEMANTIC_SHA256,
            transport="stdio",
            completed=True,
            usage=_usage(),
            result=AUDIT_RESULT,
            tool_latency_ms=1.0,
            transport_latency_ms=1.0,
            boundary=_boundary(),
        )

    def native_runner(
        case: NativeMCPComparisonCase, budget: FixedBudget
    ) -> ArmObservation:
        observed = runner(case, budget)
        return observed.model_copy(
            update={
                "transport": "in_process",
                "result": AUDIT_RESULT,
                "failure": None,
                "completed": True,
                "transport_latency_ms": 0.0,
                "boundary": None,
            }
        )

    report = run_native_mcp_comparison(
        card,
        _manifest(card),
        cases,
        candidate_route="mcp-stdio",
        native_runner=native_runner,
        mcp_runner=runner,
    )
    bad_native = report.cases[0].native.model_copy(update={"task_success": False})
    bad_case = report.cases[0].model_copy(update={"native": bad_native})
    bad_report = report.model_copy(update={"cases": (bad_case, *report.cases[1:])})
    with pytest.raises(ComparisonContractError, match="success is not recomputable"):
        validate_recomputable_report(card, bad_report)

    drifted = cases[0].model_copy(update={"task": "post-hoc changed task"})
    with pytest.raises(ComparisonContractError, match="dataset drifted"):
        run_native_mcp_comparison(
            card,
            _manifest(card),
            (drifted, *cases[1:]),
            candidate_route="mcp-stdio",
            native_runner=native_runner,
            mcp_runner=runner,
        )


class _TimeoutClient:
    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPCallError:
        return MCPCallError(
            code=MCPErrorCode.CALL_TIMEOUT,
            phase=MCPPhase.CALL,
            message="remote detail must not reach evaluation output",
            details={"outcome": "unknown"},
            untrusted_input=True,
        )


@pytest.mark.skipif(not MCP_INSTALLED, reason="optional MCP SDK ([mcp]) not installed")
def test_real_agent_comparison_crosses_stdio_and_preserves_semantics(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle())
    native_sqlite = tmp_path / "native.sqlite"
    mcp_sqlite = tmp_path / "mcp.sqlite"
    native_base = default_skill_registry(
        content_root=str(content_root), sqlite_path=str(native_sqlite)
    )
    semantic_sha256 = skill_semantic_contract_digest(native_base.get("audit_project"))
    expected = ExpectedAuditResult(open_errors=0, evidence=())
    cases = _cases(expected, semantic_sha256=semantic_sha256)
    card = _card(cases).model_copy(
        update={
            "bindings": _card(cases).bindings.model_copy(
                update={"revision_sha256": "f" * 64}
            )
        }
    )
    # Rebuild after model_copy because the card digest is the manifest binding.
    manifest = _manifest(card)
    native_timings: list[float] = []

    def native_runner(
        case: NativeMCPComparisonCase, budget: FixedBudget
    ) -> ArmObservation:
        timed_registry = registry_with_tool_timing(
            native_base, "audit_project", native_timings.append
        )
        agent = ReActAgent(
            model=scripted_chat_model([("audit_project", {}), "Native audit complete."]),
            registry=timed_registry,
            max_steps=3,
            max_model_calls=budget.model_calls,
            max_tool_calls=budget.tool_calls,
            allowed_skills={"audit_project"},
        )
        result = agent.run(case.task)
        return agent_arm_observation(
            result,
            model_calls=agent.model_calls_used,
            model_sha256=MODEL_SHA256,
            semantic_contract_sha256=semantic_sha256,
            transport="in_process",
            tool_latency_ms=native_timings[-1],
        )

    def mcp_runner(case: NativeMCPComparisonCase, budget: FixedBudget) -> ArmObservation:
        traces: list[MCPInvocationTrace] = []
        config = default_local_config(
            content_root=content_root,
            sqlite_path=mcp_sqlite,
            python_executable=sys.executable,
            source_root=REPO_ROOT / "src",
            working_directory=REPO_ROOT,
        )
        routed = registry_with_mcp_audit(
            default_skill_registry(content_root=str(content_root), sqlite_path=str(mcp_sqlite)),
            config,
            client=_TimeoutClient() if case.partition == "fault" else None,
            trace_sink=traces.append,
        )
        assert skill_semantic_contract_digest(routed.get("audit_project")) == semantic_sha256
        agent = ReActAgent(
            model=scripted_chat_model([("audit_project", {}), "MCP audit complete."]),
            registry=routed,
            max_steps=3,
            max_model_calls=budget.model_calls,
            max_tool_calls=budget.tool_calls,
            allowed_skills={"audit_project"},
        )
        result = agent.run(case.task)
        return agent_arm_observation(
            result,
            model_calls=agent.model_calls_used,
            model_sha256=MODEL_SHA256,
            semantic_contract_sha256=semantic_sha256,
            transport="stdio",
            mcp_traces=traces,
        )

    report = run_native_mcp_comparison(
        card,
        manifest,
        cases,
        candidate_route="mcp-stdio",
        native_runner=native_runner,
        mcp_runner=mcp_runner,
    )

    assert report.aggregate.native_task_success.value == 1.0
    assert report.aggregate.mcp_task_success.model_dump() == {
        "numerator": 2,
        "denominator": 3,
        "value": pytest.approx(2 / 3, abs=1e-6),
    }
    assert report.aggregate.open_errors_parity.value == 1.0
    assert report.aggregate.evidence_parity.value == 1.0
    assert report.aggregate.expected_fault_semantics.value == 1.0
    assert all(item.native.usage.model_calls == 2 for item in report.cases)
    assert all(item.native.usage.tool_calls == 1 for item in report.cases)
    assert all(item.mcp.usage.model_calls == 2 for item in report.cases)
    assert all(item.mcp.usage.tool_calls == 1 for item in report.cases)
    for item in report.cases[:2]:
        assert item.mcp.boundary is not None
        assert item.mcp.boundary.server_name == "owcopilot-local-consumer"
        assert item.mcp.boundary.untrusted_input is True
        assert item.mcp.transport_latency_ms > 0
    assert report.cases[-1].mcp.failure == FailureSemantics(
        code="CALL_TIMEOUT",
        phase="call",
        retryable=False,
        outcome_unknown=True,
        fallback_used=False,
    )
    assert "remote detail" not in report.model_dump_json()


def test_agent_adapter_uses_checked_mcp_metadata_not_remote_text() -> None:
    observation = MCPObservation(
        tool_name="audit_project",
        data=AUDIT_RESULT,
        protocol_version="2025-11-25",
        server_name="owcopilot-local-consumer",
        schema_fingerprint="b" * 64,
        content_bytes=512,
    )
    content_root = Path(".")
    routed = registry_with_mcp_audit(
        default_skill_registry(content_root=str(content_root)),
        default_local_config(content_root=content_root, sqlite_path=Path("runtime.sqlite")),
        client=_ObservationClient(observation),
    )
    native = default_skill_registry(content_root=str(content_root)).get("audit_project")
    assert skill_semantic_contract_digest(routed.get("audit_project")) == (
        skill_semantic_contract_digest(native)
    )


class _ObservationClient:
    def __init__(self, observation: MCPObservation) -> None:
        self.observation = observation

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPObservation:
        return self.observation
