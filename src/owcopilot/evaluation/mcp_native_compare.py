"""Paired native-tool versus MCP-tool evaluation with frozen, recomputable evidence.

This module evaluates MCP as an execution *boundary*, not as an assumed quality upgrade.  The
native arm is the same-process, low-overhead baseline.  The MCP arm is valuable only when a task
needs cross-process standard discovery, a checked tool contract, or an explicit trust boundary;
its extra latency remains a measured cost and may be positive or negative.

The comparison is deliberately paired and pre-registered:

* both arms receive the exact same :class:`NativeMCPComparisonCase` and frozen
  :class:`~owcopilot.evaluation.capability_decision.FixedBudget`;
* the card's model hash and a transport-independent skill-semantic hash must match both arms;
* the complete case set (task text, gold audit result, partition, order, and expected fault) is
  hashed into ``card.bindings.dataset_sha256``;
* enable, disable, and fault cases must all be present, and their labels must match the frozen
  :class:`~owcopilot.evaluation.capability_decision.CapabilityDecisionCard` partition;
* the report retains the raw stable observations needed to recompute task success, audit parity,
  wall latency, estimated transport overhead, and typed failure semantics.

``transport_overhead_ms`` is an explicit estimate, not a protocol microbenchmark: it subtracts
the measured in-process native tool time from the MCP client's end-to-end boundary time (spawn /
initialize / discover / contract-check / call / decode).  Both raw terms are retained.  The wall
delta separately measures the whole agent/task path.  Neither delta is clamped and no winner is
encoded by this evaluator.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..skills import Skill, SkillRegistry
from .capability_decision import (
    ArtifactBindings,
    CapabilityDecisionCard,
    FixedBudget,
    HeldoutRunManifest,
    validate_heldout_run,
)

if TYPE_CHECKING:
    from ..agent import AgentResult
    from ..mcp_client import MCPInvocationTrace


ComparisonPartition = Literal["enable", "disable", "fault"]
ComparisonArm = Literal["native", "mcp"]
ComparisonTransport = Literal["in_process", "stdio"]
ArmOrder = Literal["native_first", "mcp_first"]


class ComparisonContractError(ValueError):
    """A run cannot support a fair, frozen native/MCP comparison."""


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _round_ms(value: float) -> float:
    return round(value, 6)


class AuditEvidenceSignature(BaseModel):
    """Stable audit evidence, excluding run ids, timestamps, prose, and transport metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_code: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    category: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpectedAuditResult(BaseModel):
    """Frozen gold result used for task success; parity never treats native output as gold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_errors: int = Field(ge=0)
    evidence: tuple[AuditEvidenceSignature, ...]


class ExpectedFailureSemantics(BaseModel):
    """Typed MCP fault fixed before execution and mapped by the decision card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_key: str = Field(min_length=1)
    code: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    retryable: bool = False
    outcome_unknown: bool = False
    fallback_used: Literal[False] = False


class NativeMCPComparisonCase(BaseModel):
    """One paired case.  Arm order is frozen so warm-up/order effects cannot be chosen later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    partition: ComparisonPartition
    task: str = Field(min_length=1)
    candidate_route: str = Field(min_length=1)
    arm_order: ArmOrder
    semantic_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected: ExpectedAuditResult
    expected_mcp_failure: ExpectedFailureSemantics | None = None

    @model_validator(mode="after")
    def validate_fault_expectation(self) -> NativeMCPComparisonCase:
        if self.partition == "fault" and self.expected_mcp_failure is None:
            raise ComparisonContractError("fault cases require expected MCP failure semantics")
        if self.partition != "fault" and self.expected_mcp_failure is not None:
            raise ComparisonContractError(
                "only fault cases may declare expected MCP failure semantics"
            )
        return self

    @property
    def task_sha256(self) -> str:
        return hashlib.sha256(self.task.encode("utf-8")).hexdigest()


class ArmResourceUsage(BaseModel):
    """Observed use against the same frozen cap independently applied to each A/B arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    retrieval_calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    peak_concurrency: int = Field(ge=1)
    candidate_count: int = Field(ge=0)


class FailureSemantics(BaseModel):
    """Content-free failure facts; remote free text is intentionally not reportable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    retryable: bool = False
    outcome_unknown: bool = False
    fallback_used: bool = False


class MCPBoundaryEvidence(BaseModel):
    """Factual proof emitted after initialize/discovery/contract/decode checks succeed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_version: str = Field(min_length=1)
    server_name: str = Field(min_length=1)
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_bytes: int = Field(ge=0)
    untrusted_input: Literal[True]
    risk_flags: tuple[str, ...] = ()


class ArmObservation(BaseModel):
    """Runner return value before the harness adds wall timing and task scoring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport: ComparisonTransport
    completed: bool
    usage: ArmResourceUsage
    result: dict[str, Any] | None = None
    failure: FailureSemantics | None = None
    tool_latency_ms: float = Field(ge=0.0)
    transport_latency_ms: float = Field(ge=0.0)
    boundary: MCPBoundaryEvidence | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> ArmObservation:
        if (self.result is None) == (self.failure is None):
            raise ComparisonContractError("an arm must return exactly one of result or failure")
        if self.failure is not None and self.completed:
            raise ComparisonContractError("a failed tool task cannot be marked completed")
        if self.transport == "in_process":
            if self.transport_latency_ms != 0.0 or self.boundary is not None:
                raise ComparisonContractError(
                    "native in-process observations cannot claim MCP boundary latency/evidence"
                )
        return self


class ArmRecord(BaseModel):
    """Stable scored arm record retained in the result artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: ComparisonArm
    transport: ComparisonTransport
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed: bool
    task_success: bool
    open_errors: int | None = Field(default=None, ge=0)
    evidence: tuple[AuditEvidenceSignature, ...] | None = None
    wall_latency_ms: float = Field(ge=0.0)
    tool_latency_ms: float = Field(ge=0.0)
    transport_latency_ms: float = Field(ge=0.0)
    usage: ArmResourceUsage
    failure: FailureSemantics | None = None
    decision_failure_semantic: str | None = None
    boundary: MCPBoundaryEvidence | None = None


class CaseMetrics(BaseModel):
    """Derived paired metrics.  Signed deltas preserve either arm's possible advantage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    native_task_success: bool
    mcp_task_success: bool
    open_errors_parity: bool | None
    evidence_parity: bool | None
    wall_latency_delta_ms: float
    transport_overhead_ms: float
    model_call_usage_parity: bool
    tool_call_usage_parity: bool
    failure_semantics_parity: bool
    expected_fault_semantics_match: bool | None


class CaseComparison(BaseModel):
    """Frozen input plus both raw arms and their recomputable metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: NativeMCPComparisonCase
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native: ArmRecord
    mcp: ArmRecord
    metrics: CaseMetrics


class RecomputableRate(BaseModel):
    """A rate that retains its numerator and denominator (including an empty denominator)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None


class AggregateMetrics(BaseModel):
    """Aggregate metrics computed only from :class:`CaseComparison` raw records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_count: int = Field(ge=1)
    native_task_success: RecomputableRate
    mcp_task_success: RecomputableRate
    open_errors_parity: RecomputableRate
    evidence_parity: RecomputableRate
    expected_fault_semantics: RecomputableRate
    model_call_usage_parity: RecomputableRate
    tool_call_usage_parity: RecomputableRate
    native_mean_wall_latency_ms: float = Field(ge=0.0)
    mcp_mean_wall_latency_ms: float = Field(ge=0.0)
    mean_wall_latency_delta_ms: float
    mean_transport_overhead_ms: float
    native_failure_semantics: dict[str, int]
    mcp_failure_semantics: dict[str, int]


class NativeMCPComparisonReport(BaseModel):
    """Bound result artifact.  It records evidence, not a preselected route winner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["native-mcp-comparison/v1"] = "native-mcp-comparison/v1"
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: HeldoutRunManifest
    case_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    bindings: ArtifactBindings
    budget: FixedBudget
    native_route: str = Field(min_length=1)
    mcp_route: str = Field(min_length=1)
    enable_if: tuple[str, ...] = Field(min_length=1)
    disable_if: tuple[str, ...] = Field(min_length=1)
    budget_interpretation: Literal["same_frozen_cap_per_arm"] = "same_frozen_cap_per_arm"
    native_role: Literal["same_process_low_overhead_baseline"] = (
        "same_process_low_overhead_baseline"
    )
    mcp_value_condition: Literal[
        "cross_process_standard_discovery_contract_and_trust_boundary"
    ] = "cross_process_standard_discovery_contract_and_trust_boundary"
    no_measured_net_benefit_policy: Literal["default_to_native_simple_route"] = (
        "default_to_native_simple_route"
    )
    negative_result_policy: Literal["retain_without_posthoc_override"] = (
        "retain_without_posthoc_override"
    )
    winner_predeclared: Literal[False] = False
    cases: tuple[CaseComparison, ...]
    aggregate: AggregateMetrics

    def digest(self) -> str:
        """Stable digest of this complete result artifact, including raw timing records."""
        return _canonical_digest(self.model_dump(mode="json"))


class ArmRunner(Protocol):
    """One arm executor.  Both runners receive the same case object and frozen budget."""

    def __call__(
        self, case: NativeMCPComparisonCase, budget: FixedBudget
    ) -> ArmObservation: ...


def skill_semantic_contract_digest(skill: Skill) -> str:
    """Hash domain/no-progress semantics while deliberately excluding transport implementation.

    Descriptions, handler identity, and MCP's bounded page-size argument are transport-facing and
    therefore excluded.  Output parity still catches a page cap that changes the domain result.
    """
    return _canonical_digest(
        {
            "name": skill.name,
            "cost_tier": skill.cost_tier.value,
            "side_effect": skill.side_effect.value,
            "semantic_projection_name": skill.semantic_projection_name,
            "state_marker_policy": skill.state_marker_policy,
        }
    )


def comparison_dataset_digest(cases: Sequence[NativeMCPComparisonCase]) -> str:
    """Hash the complete paired dataset in case-id order for preregistration binding."""
    if not cases:
        raise ComparisonContractError("the comparison dataset cannot be empty")
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ComparisonContractError("comparison case ids must be unique")
    payload = [
        case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.case_id)
    ]
    return _canonical_digest(payload)


def audit_evidence_signatures(result: Mapping[str, Any]) -> tuple[AuditEvidenceSignature, ...]:
    """Project an ``audit_project`` payload into stable, order-independent evidence signatures."""
    issues = result.get("issues")
    if not isinstance(issues, list):
        raise ComparisonContractError("audit result 'issues' must be a list")
    signatures: list[AuditEvidenceSignature] = []
    for index, raw_issue in enumerate(issues):
        if not isinstance(raw_issue, Mapping):
            raise ComparisonContractError(f"audit issue {index} must be an object")
        required = ("rule_code", "target_ref", "severity", "category")
        values = {name: raw_issue.get(name) for name in required}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ComparisonContractError(
                f"audit issue {index} lacks stable rule/target/severity/category fields"
            )
        evidence = raw_issue.get("evidence", [])
        if not isinstance(evidence, list):
            raise ComparisonContractError(f"audit issue {index} evidence must be a list")
        # Evidence entries are a semantic set for parity; JSON object key and list order are not
        # transport semantics, so canonicalize each entry and sort before hashing the collection.
        try:
            canonical_evidence = sorted(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for item in evidence
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ComparisonContractError(
                f"audit issue {index} evidence is not canonical JSON"
            ) from exc
        signatures.append(
            AuditEvidenceSignature(
                rule_code=str(values["rule_code"]),
                target_ref=str(values["target_ref"]),
                severity=str(values["severity"]),
                category=str(values["category"]),
                evidence_sha256=_canonical_digest(canonical_evidence),
            )
        )
    return tuple(
        sorted(
            signatures,
            key=lambda item: (
                item.rule_code,
                item.target_ref,
                item.severity,
                item.category,
                item.evidence_sha256,
            ),
        )
    )


def expected_audit_result(result: Mapping[str, Any]) -> ExpectedAuditResult:
    """Freeze a checked audit payload as explicit gold for a later held-out case."""
    open_errors = result.get("open_errors")
    if isinstance(open_errors, bool) or not isinstance(open_errors, int) or open_errors < 0:
        raise ComparisonContractError("audit result 'open_errors' must be a non-negative integer")
    return ExpectedAuditResult(
        open_errors=open_errors,
        evidence=audit_evidence_signatures(result),
    )


def registry_with_tool_timing(
    registry: SkillRegistry,
    skill_name: str,
    trace_sink: Callable[[float], None],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> SkillRegistry:
    """Return a registry copy that times one native handler without changing its contract."""
    original = registry.get(skill_name)

    def timed_handler(**kwargs: Any) -> dict[str, Any]:
        started = clock()
        try:
            return original.handler(**kwargs)
        finally:
            trace_sink(_round_ms(max(0.0, (clock() - started) * 1000.0)))

    timed = replace(original, handler=timed_handler)
    copied = SkillRegistry()
    for skill in registry:
        copied.register(timed if skill.name == skill_name else skill)
    return copied


def _boundary_from_result(result: Mapping[str, Any]) -> MCPBoundaryEvidence | None:
    raw = result.get("_mcp")
    if not isinstance(raw, Mapping):
        return None
    try:
        risk_flags_raw = raw.get("risk_flags", [])
        if not isinstance(risk_flags_raw, list) or not all(
            isinstance(flag, str) for flag in risk_flags_raw
        ):
            return None
        if raw.get("untrusted_input") is not True:
            return None
        return MCPBoundaryEvidence(
            protocol_version=str(raw["protocol_version"]),
            server_name=str(raw["server_name"]),
            schema_fingerprint=str(raw["schema_fingerprint"]),
            content_bytes=int(raw["content_bytes"]),
            untrusted_input=True,
            risk_flags=tuple(risk_flags_raw),
        )
    except (KeyError, TypeError, ValueError):
        return None


def agent_arm_observation(
    result: AgentResult,
    *,
    model_calls: int,
    model_sha256: str,
    semantic_contract_sha256: str,
    transport: ComparisonTransport,
    tool_latency_ms: float | None = None,
    mcp_traces: Sequence[MCPInvocationTrace] = (),
    retrieval_calls: int = 0,
    total_tokens: int = 0,
    context_tokens: int = 0,
) -> ArmObservation:
    """Adapt one real :class:`ReActAgent` result into the transport-neutral runner contract.

    MCP error codes come from the content-free adapter trace, never from parsing or retaining the
    agent-facing exception text.  A successful MCP output also carries the checked protocol,
    server, schema, and untrusted-input facts produced by the consumer boundary.
    """
    audit_steps = [step for step in result.steps if step.action == "audit_project"]
    usage = ArmResourceUsage(
        model_calls=model_calls,
        tool_calls=result.step_count,
        retrieval_calls=retrieval_calls,
        total_tokens=total_tokens,
        context_tokens=context_tokens,
        peak_concurrency=1,
        candidate_count=0,
    )
    matching_traces = [trace for trace in mcp_traces if trace.tool_name == "audit_project"]
    trace = matching_traces[-1] if matching_traces else None
    measured_tool_ms = (
        tool_latency_ms
        if tool_latency_ms is not None
        else (float(audit_steps[-1].latency_ms) if audit_steps else 0.0)
    )
    boundary_latency_ms = float(trace.latency_ms) if trace is not None else 0.0

    if not audit_steps:
        return ArmObservation(
            model_sha256=model_sha256,
            semantic_contract_sha256=semantic_contract_sha256,
            transport=transport,
            completed=False,
            usage=usage,
            failure=FailureSemantics(
                code="AUDIT_TOOL_NOT_CALLED",
                phase="agent",
                fallback_used=False,
            ),
            tool_latency_ms=max(0.0, measured_tool_ms),
            transport_latency_ms=(
                max(0.0, boundary_latency_ms) if transport == "stdio" else 0.0
            ),
        )

    step = audit_steps[-1]
    if step.is_error or step.result is None:
        if trace is not None and trace.outcome == "error":
            failure = FailureSemantics(
                code=trace.error_code or "MCP_TOOL_ERROR",
                phase=trace.phase or "call",
                retryable=trace.retryable,
                outcome_unknown=trace.outcome_unknown,
                fallback_used=False,
            )
        else:
            failure = FailureSemantics(
                code="AGENT_TOOL_ERROR",
                phase="agent",
                fallback_used=False,
            )
        return ArmObservation(
            model_sha256=model_sha256,
            semantic_contract_sha256=semantic_contract_sha256,
            transport=transport,
            completed=False,
            usage=usage,
            failure=failure,
            tool_latency_ms=max(0.0, measured_tool_ms),
            transport_latency_ms=(
                max(0.0, boundary_latency_ms) if transport == "stdio" else 0.0
            ),
        )

    payload = dict(step.result)
    return ArmObservation(
        model_sha256=model_sha256,
        semantic_contract_sha256=semantic_contract_sha256,
        transport=transport,
        completed=result.stop_reason == "finished" and bool(result.final_answer.strip()),
        usage=usage,
        result=payload,
        tool_latency_ms=max(0.0, measured_tool_ms),
        transport_latency_ms=(max(0.0, boundary_latency_ms) if transport == "stdio" else 0.0),
        boundary=_boundary_from_result(payload) if transport == "stdio" else None,
    )


def _validate_usage(usage: ArmResourceUsage, budget: FixedBudget, *, arm: ComparisonArm) -> None:
    observed = {
        "model_calls": usage.model_calls,
        "tool_calls": usage.tool_calls,
        "retrieval_calls": usage.retrieval_calls,
        "total_tokens": usage.total_tokens,
        "context_tokens": usage.context_tokens,
        "peak_concurrency": usage.peak_concurrency,
        "candidate_count": usage.candidate_count,
    }
    ceilings = {
        "model_calls": budget.model_calls,
        "tool_calls": budget.tool_calls,
        "retrieval_calls": budget.retrieval_calls,
        "total_tokens": budget.total_tokens,
        "context_tokens": budget.context_tokens,
        "peak_concurrency": budget.max_concurrency,
        "candidate_count": budget.candidate_window,
    }
    exceeded = [name for name, value in observed.items() if value > ceilings[name]]
    if exceeded:
        raise ComparisonContractError(
            f"{arm} arm exceeded frozen budget field(s): {', '.join(exceeded)}"
        )


def _failure_matches(
    actual: FailureSemantics | None, expected: ExpectedFailureSemantics
) -> bool:
    return actual is not None and (
        actual.code,
        actual.phase,
        actual.retryable,
        actual.outcome_unknown,
        actual.fallback_used,
    ) == (
        expected.code,
        expected.phase,
        expected.retryable,
        expected.outcome_unknown,
        expected.fallback_used,
    )


def _record_arm(
    *,
    arm: ComparisonArm,
    observation: ArmObservation,
    wall_latency_ms: float,
    case: NativeMCPComparisonCase,
    card: CapabilityDecisionCard,
) -> ArmRecord:
    expected_transport: ComparisonTransport = "in_process" if arm == "native" else "stdio"
    if observation.transport != expected_transport:
        raise ComparisonContractError(
            f"{arm} arm reported {observation.transport}, expected {expected_transport}"
        )
    if card.bindings.model_sha256 is None:
        raise ComparisonContractError("native/MCP comparison requires a frozen model_sha256")
    if observation.model_sha256 != card.bindings.model_sha256:
        raise ComparisonContractError(f"{arm} arm model binding drifted")
    if observation.semantic_contract_sha256 != case.semantic_contract_sha256:
        raise ComparisonContractError(f"{arm} arm semantic contract drifted")
    _validate_usage(observation.usage, card.budget, arm=arm)
    if wall_latency_ms > card.budget.deadline_ms:
        raise ComparisonContractError(f"{arm} arm exceeded the frozen task deadline")
    if arm == "mcp" and observation.result is not None and observation.boundary is None:
        raise ComparisonContractError(
            "successful MCP result lacks checked protocol/server/schema trust-boundary evidence"
        )

    open_errors: int | None = None
    evidence: tuple[AuditEvidenceSignature, ...] | None = None
    if observation.result is not None:
        raw_open_errors = observation.result.get("open_errors")
        if (
            isinstance(raw_open_errors, bool)
            or not isinstance(raw_open_errors, int)
            or raw_open_errors < 0
        ):
            raise ComparisonContractError(
                f"{arm} audit result 'open_errors' must be a non-negative integer"
            )
        open_errors = raw_open_errors
        evidence = audit_evidence_signatures(observation.result)

    task_success = (
        observation.completed
        and observation.failure is None
        and open_errors == case.expected.open_errors
        and evidence == case.expected.evidence
    )
    decision_semantic: str | None = None
    if observation.failure is not None:
        expected_failure = case.expected_mcp_failure if arm == "mcp" else None
        if expected_failure is not None and _failure_matches(observation.failure, expected_failure):
            decision_semantic = card.failure_semantics[expected_failure.decision_key]
        else:
            decision_semantic = card.failure_semantics.get(observation.failure.code)

    return ArmRecord(
        arm=arm,
        transport=observation.transport,
        model_sha256=observation.model_sha256,
        semantic_contract_sha256=observation.semantic_contract_sha256,
        completed=observation.completed,
        task_success=task_success,
        open_errors=open_errors,
        evidence=evidence,
        wall_latency_ms=_round_ms(wall_latency_ms),
        tool_latency_ms=_round_ms(observation.tool_latency_ms),
        transport_latency_ms=_round_ms(observation.transport_latency_ms),
        usage=observation.usage,
        failure=observation.failure,
        decision_failure_semantic=decision_semantic,
        boundary=observation.boundary,
    )


def compute_case_metrics(
    case: NativeMCPComparisonCase, native: ArmRecord, mcp: ArmRecord
) -> CaseMetrics:
    """Recompute every paired metric from stable case/arm records."""
    if native.arm != "native" or mcp.arm != "mcp":
        raise ComparisonContractError("case records must be ordered native then MCP")
    expected_fault_match = (
        _failure_matches(mcp.failure, case.expected_mcp_failure)
        if case.expected_mcp_failure is not None
        else None
    )
    return CaseMetrics(
        native_task_success=native.task_success,
        mcp_task_success=mcp.task_success,
        open_errors_parity=(
            native.open_errors == mcp.open_errors
            if native.open_errors is not None and mcp.open_errors is not None
            else None
        ),
        evidence_parity=(
            native.evidence == mcp.evidence
            if native.evidence is not None and mcp.evidence is not None
            else None
        ),
        wall_latency_delta_ms=_round_ms(mcp.wall_latency_ms - native.wall_latency_ms),
        # MCP boundary time includes remote domain execution; subtract the measured equivalent
        # native domain call and keep both raw terms in the ArmRecords for independent recompute.
        transport_overhead_ms=_round_ms(mcp.transport_latency_ms - native.tool_latency_ms),
        model_call_usage_parity=(native.usage.model_calls == mcp.usage.model_calls),
        tool_call_usage_parity=(native.usage.tool_calls == mcp.usage.tool_calls),
        failure_semantics_parity=native.failure == mcp.failure,
        expected_fault_semantics_match=expected_fault_match,
    )


def _rate(values: Sequence[bool]) -> RecomputableRate:
    numerator = sum(values)
    denominator = len(values)
    return RecomputableRate(
        numerator=numerator,
        denominator=denominator,
        value=round(numerator / denominator, 6) if denominator else None,
    )


def _failure_key(failure: FailureSemantics) -> str:
    return (
        f"{failure.code}|{failure.phase}|retryable={str(failure.retryable).lower()}|"
        f"outcome_unknown={str(failure.outcome_unknown).lower()}|"
        f"fallback_used={str(failure.fallback_used).lower()}"
    )


def compute_aggregate_metrics(cases: Sequence[CaseComparison]) -> AggregateMetrics:
    """Recompute aggregate rates, latencies, overhead, and failure distributions."""
    if not cases:
        raise ComparisonContractError("cannot aggregate an empty comparison")
    count = len(cases)
    open_parity = [
        item.metrics.open_errors_parity
        for item in cases
        if item.metrics.open_errors_parity is not None
    ]
    evidence_parity = [
        item.metrics.evidence_parity
        for item in cases
        if item.metrics.evidence_parity is not None
    ]
    fault_matches = [
        item.metrics.expected_fault_semantics_match
        for item in cases
        if item.metrics.expected_fault_semantics_match is not None
    ]
    native_failures = Counter(
        _failure_key(item.native.failure) for item in cases if item.native.failure is not None
    )
    mcp_failures = Counter(
        _failure_key(item.mcp.failure) for item in cases if item.mcp.failure is not None
    )
    return AggregateMetrics(
        case_count=count,
        native_task_success=_rate([item.native.task_success for item in cases]),
        mcp_task_success=_rate([item.mcp.task_success for item in cases]),
        open_errors_parity=_rate(open_parity),
        evidence_parity=_rate(evidence_parity),
        expected_fault_semantics=_rate(fault_matches),
        model_call_usage_parity=_rate(
            [item.metrics.model_call_usage_parity for item in cases]
        ),
        tool_call_usage_parity=_rate([item.metrics.tool_call_usage_parity for item in cases]),
        native_mean_wall_latency_ms=_round_ms(
            sum(item.native.wall_latency_ms for item in cases) / count
        ),
        mcp_mean_wall_latency_ms=_round_ms(
            sum(item.mcp.wall_latency_ms for item in cases) / count
        ),
        mean_wall_latency_delta_ms=_round_ms(
            sum(item.metrics.wall_latency_delta_ms for item in cases) / count
        ),
        mean_transport_overhead_ms=_round_ms(
            sum(item.metrics.transport_overhead_ms for item in cases) / count
        ),
        native_failure_semantics=dict(sorted(native_failures.items())),
        mcp_failure_semantics=dict(sorted(mcp_failures.items())),
    )


def _validate_case_contracts(
    card: CapabilityDecisionCard,
    manifest: HeldoutRunManifest,
    cases: Sequence[NativeMCPComparisonCase],
    *,
    candidate_route: str,
) -> tuple[NativeMCPComparisonCase, ...]:
    validate_heldout_run(card, manifest)
    if candidate_route not in card.candidates:
        raise ComparisonContractError("selected MCP route is not a frozen card candidate")
    by_id = {case.case_id: case for case in cases}
    if len(by_id) != len(cases):
        raise ComparisonContractError("comparison case ids must be unique")
    if set(by_id) != set(manifest.case_ids):
        raise ComparisonContractError("comparison cases must exactly match manifest.case_ids")
    ordered = tuple(by_id[case_id] for case_id in manifest.case_ids)
    expected_partitions = {
        **{case_id: "enable" for case_id in card.enable_case_ids},
        **{case_id: "disable" for case_id in card.disable_case_ids},
        **{case_id: "fault" for case_id in card.fault_case_ids},
    }
    seen_partitions: set[str] = set()
    for case in ordered:
        if case.partition != expected_partitions[case.case_id]:
            raise ComparisonContractError(
                f"case {case.case_id!r} partition drifted from the frozen decision card"
            )
        if case.candidate_route != candidate_route:
            raise ComparisonContractError(
                f"case {case.case_id!r} targets a different MCP candidate route"
            )
        if (
            case.expected_mcp_failure is not None
            and case.expected_mcp_failure.decision_key not in card.failure_semantics
        ):
            raise ComparisonContractError(
                f"case {case.case_id!r} fault has no registered card failure semantic"
            )
        seen_partitions.add(case.partition)
    if seen_partitions != {"enable", "disable", "fault"}:
        raise ComparisonContractError(
            "a native/MCP comparison run must contain enable, disable, and fault cases"
        )
    dataset_sha256 = comparison_dataset_digest(ordered)
    if dataset_sha256 != card.bindings.dataset_sha256:
        raise ComparisonContractError("comparison case dataset drifted from dataset_sha256")
    return ordered


def _run_arm(
    runner: ArmRunner,
    *,
    arm: ComparisonArm,
    case: NativeMCPComparisonCase,
    card: CapabilityDecisionCard,
    clock: Callable[[], float],
) -> ArmRecord:
    started = clock()
    try:
        observation = runner(case, card.budget)
    except Exception as exc:
        raise ComparisonContractError(
            f"{arm} runner raised an unnormalised exception for {case.case_id!r}"
        ) from exc
    wall_latency_ms = max(0.0, (clock() - started) * 1000.0)
    return _record_arm(
        arm=arm,
        observation=observation,
        wall_latency_ms=wall_latency_ms,
        case=case,
        card=card,
    )


def run_native_mcp_comparison(
    card: CapabilityDecisionCard,
    manifest: HeldoutRunManifest,
    cases: Sequence[NativeMCPComparisonCase],
    *,
    candidate_route: str,
    native_runner: ArmRunner,
    mcp_runner: ArmRunner,
    clock: Callable[[], float] = time.perf_counter,
) -> NativeMCPComparisonReport:
    """Run a frozen, paired native/MCP comparison and return a recomputable result artifact.

    The same cap is independently applied to each arm, as in a normal paired A/B evaluation.  The
    actual call counts remain measured outcomes: a failure may consume fewer calls, and parity is
    reported rather than silently padding or discarding the case.
    """
    ordered_cases = _validate_case_contracts(
        card,
        manifest,
        cases,
        candidate_route=candidate_route,
    )
    comparisons: list[CaseComparison] = []
    for case in ordered_cases:
        runners: tuple[tuple[ComparisonArm, ArmRunner], ...]
        if case.arm_order == "native_first":
            runners = (("native", native_runner), ("mcp", mcp_runner))
        else:
            runners = (("mcp", mcp_runner), ("native", native_runner))
        records: dict[ComparisonArm, ArmRecord] = {}
        for arm, runner in runners:
            records[arm] = _run_arm(
                runner,
                arm=arm,
                case=case,
                card=card,
                clock=clock,
            )
        native = records["native"]
        mcp = records["mcp"]
        metrics = compute_case_metrics(case, native, mcp)
        comparisons.append(
            CaseComparison(
                case=case,
                task_sha256=case.task_sha256,
                native=native,
                mcp=mcp,
                metrics=metrics,
            )
        )

    report = NativeMCPComparisonReport(
        decision_sha256=card.digest(),
        manifest_sha256=_canonical_digest(manifest.model_dump(mode="json")),
        manifest=manifest,
        case_dataset_sha256=comparison_dataset_digest(ordered_cases),
        policy_version=card.policy_version,
        bindings=card.bindings,
        budget=card.budget,
        native_route=card.baseline,
        mcp_route=candidate_route,
        enable_if=card.enable_if,
        disable_if=card.disable_if,
        cases=tuple(comparisons),
        aggregate=compute_aggregate_metrics(comparisons),
    )
    validate_recomputable_report(card, report)
    return report


def validate_recomputable_report(
    card: CapabilityDecisionCard, report: NativeMCPComparisonReport
) -> None:
    """Reject binding drift or any stored metric that differs from a raw-record recomputation."""
    validate_heldout_run(card, report.manifest)
    if report.decision_sha256 != card.digest():
        raise ComparisonContractError("report decision digest does not match the frozen card")
    manifest_digest = _canonical_digest(report.manifest.model_dump(mode="json"))
    if report.manifest_sha256 != manifest_digest:
        raise ComparisonContractError("report manifest digest is not recomputable")
    if report.policy_version != card.policy_version:
        raise ComparisonContractError("report policy version drifted")
    if report.enable_if != card.enable_if or report.disable_if != card.disable_if:
        raise ComparisonContractError("report enable/disable triggers drifted")
    if report.bindings != card.bindings or report.budget != card.budget:
        raise ComparisonContractError("report bindings or budget drifted")
    report_cases = tuple(item.case for item in report.cases)
    if tuple(item.case.case_id for item in report.cases) != report.manifest.case_ids:
        raise ComparisonContractError("report case order differs from its manifest")
    if comparison_dataset_digest(report_cases) != report.case_dataset_sha256:
        raise ComparisonContractError("report case dataset digest is not recomputable")
    if report.case_dataset_sha256 != card.bindings.dataset_sha256:
        raise ComparisonContractError("report case dataset is not the frozen card dataset")
    for item in report.cases:
        if item.task_sha256 != item.case.task_sha256:
            raise ComparisonContractError(f"case {item.case.case_id!r} task digest drifted")
        expected_native_success = (
            item.native.completed
            and item.native.failure is None
            and item.native.open_errors == item.case.expected.open_errors
            and item.native.evidence == item.case.expected.evidence
        )
        expected_mcp_success = (
            item.mcp.completed
            and item.mcp.failure is None
            and item.mcp.open_errors == item.case.expected.open_errors
            and item.mcp.evidence == item.case.expected.evidence
        )
        if item.native.task_success != expected_native_success:
            raise ComparisonContractError(
                f"case {item.case.case_id!r} native success is not recomputable"
            )
        if item.mcp.task_success != expected_mcp_success:
            raise ComparisonContractError(
                f"case {item.case.case_id!r} MCP success is not recomputable"
            )
        if item.metrics != compute_case_metrics(item.case, item.native, item.mcp):
            raise ComparisonContractError(
                f"case {item.case.case_id!r} metrics are not recomputable"
            )
    if report.aggregate != compute_aggregate_metrics(report.cases):
        raise ComparisonContractError("aggregate metrics are not recomputable")


__all__ = [
    "AggregateMetrics",
    "ArmObservation",
    "ArmOrder",
    "ArmRecord",
    "ArmResourceUsage",
    "AuditEvidenceSignature",
    "CaseComparison",
    "CaseMetrics",
    "ComparisonArm",
    "ComparisonContractError",
    "ComparisonPartition",
    "ComparisonTransport",
    "ExpectedAuditResult",
    "ExpectedFailureSemantics",
    "FailureSemantics",
    "MCPBoundaryEvidence",
    "NativeMCPComparisonCase",
    "NativeMCPComparisonReport",
    "RecomputableRate",
    "agent_arm_observation",
    "audit_evidence_signatures",
    "comparison_dataset_digest",
    "compute_aggregate_metrics",
    "compute_case_metrics",
    "expected_audit_result",
    "registry_with_tool_timing",
    "run_native_mcp_comparison",
    "skill_semantic_contract_digest",
    "validate_recomputable_report",
]
