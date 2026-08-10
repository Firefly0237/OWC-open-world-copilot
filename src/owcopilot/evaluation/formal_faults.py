"""Canonical frozen fault manifest for ``failure_isolation_matrix``.

This is a production-path test contract, not an execution result.  The real PostgreSQL,
pgvector, Redis Streams, two-tenant, and two-instance topology is explicitly future-required and
``NOT_AVAILABLE``.  Every cell therefore remains ``NOT_RUN`` until a production fault runner can
prove an exact hit, the A/B persistence boundary, bounded recovery, and observable effects.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .formal_hard_zero import FrozenHardZeroCatalog, HardZeroRole
from .protocol import EvalStatus

FAULT_MANIFEST_SCHEMA_VERSION: Final[Literal["formal-fault-manifest/v1"]] = (
    "formal-fault-manifest/v1"
)
FAULT_MANIFEST_RELATIVE_PATH: Final[Literal["failure_isolation_matrix/fault_manifest.json"]] = (
    "failure_isolation_matrix/fault_manifest.json"
)
FAULT_MANIFEST_ARTIFACT_PATH: Final[
    Literal["../../faults/failure_isolation_matrix/fault_manifest.json"]
] = "../../faults/failure_isolation_matrix/fault_manifest.json"


class FaultRecoveryClass(str, Enum):
    RECOVERABLE_INFRASTRUCTURE = "recoverable_infrastructure"
    RECOVERABLE_CONCURRENCY = "recoverable_concurrency"
    NON_RECOVERABLE_POLICY_REJECTION = "non_recoverable_policy_rejection"
    NON_RECOVERABLE_REGISTERED_TERMINAL = "non_recoverable_registered_terminal"


class FaultInjectionMechanism(str, Enum):
    REQUEST_SCOPE_ATTACK = "request_scope_attack"
    PROCESS_SIGNAL = "process_signal"
    PROCESS_EXIT = "process_exit"
    MESSAGE_REDELIVERY = "message_redelivery"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    CONCURRENT_CAS = "concurrent_cas"
    STALE_FENCING_TOKEN = "stale_fencing_token"
    MEMBERSHIP_REVOCATION = "membership_revocation"
    IDEMPOTENCY_REPLAY = "idempotency_replay"
    DURABLE_STATE_CORRUPTION = "durable_state_corruption"
    INVALID_PAYLOAD = "invalid_payload"
    EXPLICIT_CANCELLATION = "explicit_cancellation"
    RETRY_BUDGET_EXHAUSTION = "retry_budget_exhaustion"


class _FaultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        return self.__class__.model_validate(payload)


class ProductionFaultTarget(_FaultModel):
    entrypoint: str = Field(min_length=1)
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    production_path_exercised: Literal[False]
    unavailable_reasons: tuple[str, ...] = Field(min_length=1)


class ExpectedProcessTermination(_FaultModel):
    exit_code: int | None = Field(default=None, ge=0, le=255)
    signal: str | None = Field(default=None, pattern=r"^SIG[A-Z0-9]+$")

    @model_validator(mode="after")
    def require_exact_exit_or_signal(self) -> ExpectedProcessTermination:
        if (self.exit_code is None) == (self.signal is None):
            raise ValueError("FAULT_EXPECTED_TERMINATION_REQUIRES_EXACT_EXIT_OR_SIGNAL")
        return self


class FaultInjectionContract(_FaultModel):
    trigger: str = Field(min_length=1)
    mechanism: FaultInjectionMechanism
    expected_hit_count: Literal[1]
    expected_observation: str = Field(min_length=1)
    expected_process_termination: ExpectedProcessTermination | None = None
    natural_exit_is_not_run: Literal[True] | None = None
    non_exact_hit_status: Literal[EvalStatus.NOT_RUN]

    @model_validator(mode="after")
    def bind_process_termination_only_to_process_faults(self) -> FaultInjectionContract:
        process_fault = self.mechanism in {
            FaultInjectionMechanism.PROCESS_SIGNAL,
            FaultInjectionMechanism.PROCESS_EXIT,
        }
        if process_fault:
            if (
                self.expected_process_termination is None
                or self.natural_exit_is_not_run is not True
            ):
                raise ValueError("PROCESS_FAULT_REQUIRES_EXACT_TERMINATION_AND_NATURAL_EXIT_RULE")
        elif (
            self.expected_process_termination is not None
            or self.natural_exit_is_not_run is not None
        ):
            raise ValueError("NON_PROCESS_FAULT_CANNOT_DECLARE_PROCESS_TERMINATION")
        return self


class BoundaryAction(_FaultModel):
    action_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    durable_evidence_required: Literal[True]
    evidence_requirements: tuple[str, ...] = Field(min_length=1)


class FaultBoundary(_FaultModel):
    action_a: BoundaryAction
    action_a_completed_required: Literal[True]
    action_b: BoundaryAction
    action_b_not_completed_required: Literal[True]

    @model_validator(mode="after")
    def require_distinct_actions(self) -> FaultBoundary:
        if self.action_a.action_id == self.action_b.action_id:
            raise ValueError("FAULT_BOUNDARY_ACTIONS_MUST_BE_DISTINCT")
        return self


class DeadlineParameter(_FaultModel):
    name: str = Field(min_length=1)
    milliseconds: int = Field(gt=0)


class FrozenRecoveryDeadline(_FaultModel):
    fixed_duration_ms: int | None = Field(default=None, gt=0)
    formula: str | None = Field(default=None, min_length=1)
    parameters: tuple[DeadlineParameter, ...] = ()

    @model_validator(mode="after")
    def require_fixed_deadline_or_formula(self) -> FrozenRecoveryDeadline:
        if (self.fixed_duration_ms is None) == (self.formula is None):
            raise ValueError("FAULT_DEADLINE_REQUIRES_FIXED_DURATION_OR_FORMULA")
        if self.formula is None and self.parameters:
            raise ValueError("FAULT_FIXED_DEADLINE_CANNOT_HAVE_PARAMETERS")
        if self.formula is not None:
            names = [parameter.name for parameter in self.parameters]
            if not names or len(names) != len(set(names)):
                raise ValueError("FAULT_FORMULA_PARAMETERS_MUST_BE_NONEMPTY_UNIQUE")
            if any(name not in self.formula for name in names):
                raise ValueError("FAULT_FORMULA_MUST_NAME_EVERY_PARAMETER")
        return self


class RecoveryIdentityBinding(_FaultModel):
    binding_id: str = Field(min_length=1)
    left_evidence_ref: str = Field(min_length=1)
    right_evidence_ref: str = Field(min_length=1)
    comparison: Literal["exact_equal", "different", "monotonic_non_decreasing"]


class RecoveryContract(_FaultModel):
    recovery_entrypoint: str = Field(min_length=1)
    expected_terminal_or_continuing_states: tuple[str, ...] = Field(min_length=1)
    deadline: FrozenRecoveryDeadline
    terminal_failed_allowed: bool
    required_identity_bindings: tuple[RecoveryIdentityBinding, ...] = Field(min_length=1)
    required_recovery_invariants: tuple[str, ...] = (
        "The final state is recomputed from durable production truth.",
    )

    @model_validator(mode="after")
    def require_unique_identity_bindings(self) -> RecoveryContract:
        binding_ids = [binding.binding_id for binding in self.required_identity_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("RECOVERY_IDENTITY_BINDING_IDS_MUST_BE_UNIQUE")
        return self


class ObservableEffectLimit(_FaultModel):
    effect: str = Field(min_length=1)
    minimum_count: int = Field(ge=0)
    maximum_count: int = Field(ge=0)

    @model_validator(mode="after")
    def require_ordered_range(self) -> ObservableEffectLimit:
        if self.maximum_count < self.minimum_count:
            raise ValueError("FAULT_EFFECT_RANGE_INVALID")
        return self


class BenignObservableEffect(_FaultModel):
    effect: str = Field(min_length=1)
    minimum_count: int = Field(ge=0)
    maximum_count: int = Field(ge=0)

    @model_validator(mode="after")
    def require_ordered_range(self) -> BenignObservableEffect:
        if self.maximum_count < self.minimum_count:
            raise ValueError("BENIGN_EFFECT_RANGE_INVALID")
        return self


class FaultOracleContract(_FaultModel):
    oracle_requirements: tuple[str, ...] = Field(min_length=1)
    evidence_requirements: tuple[str, ...] = Field(min_length=1)


_REQUIRED_TYPED_BINDING_IDS: Final[dict[str, frozenset[str]]] = {
    "agent-kill-before-tool-001": frozenset(
        {"before_tool_run_identity", "pending_tool_intent_identity", "before_tool_budget_monotonic"}
    ),
    "agent-kill-after-tool-001": frozenset(
        {"after_tool_run_identity", "tool_evidence_identity", "after_tool_budget_monotonic"}
    ),
    "agent-kill-before-checkpoint-001": frozenset(
        {
            "before_checkpoint_run_identity",
            "observation_checkpoint_hash_identity",
            "before_checkpoint_budget_monotonic",
        }
    ),
    "agent-kill-after-checkpoint-001": frozenset(
        {
            "checkpoint_context_hash_identity",
            "checkpoint_approval_hash_identity",
            "after_checkpoint_budget_monotonic",
        }
    ),
    "agent-kill-at-hitl-pause-001": frozenset(
        {
            "hitl_pause_request_identity",
            "hitl_approval_state_hash_identity",
            "hitl_budget_monotonic",
        }
    ),
    "agent-kill-before-reflection-001": frozenset(
        {
            "reflection_feedback_hash_identity",
            "before_reflection_run_identity",
            "before_reflection_budget_monotonic",
        }
    ),
    "agent-kill-after-reflection-001": frozenset(
        {
            "reflection_next_attempt_hash_identity",
            "after_reflection_run_identity",
            "after_reflection_budget_monotonic",
        }
    ),
    "instance-a-exit-instance-b-reclaim-001": frozenset(
        {
            "source_recovery_instance_identity",
            "cross_instance_job_identity",
            "cross_instance_scope_identity",
        }
    ),
}


class AttackFaultCell(_FaultModel):
    cell_id: str = Field(min_length=1)
    hard_zero_gate_id: str = Field(min_length=1)
    hard_zero_case_id: str = Field(min_length=1)
    production_target: ProductionFaultTarget
    recovery_class: FaultRecoveryClass
    injection: FaultInjectionContract
    boundary: FaultBoundary
    recovery: RecoveryContract
    maximum_observable_effects: tuple[ObservableEffectLimit, ...] = Field(min_length=1)
    oracle: FaultOracleContract
    run_status: Literal[EvalStatus.NOT_RUN]

    @model_validator(mode="after")
    def validate_attack_cell(self) -> AttackFaultCell:
        if self.cell_id != self.hard_zero_case_id or ".attack." not in self.cell_id:
            raise ValueError("FAULT_ATTACK_CELL_ID_MUST_EQUAL_HARD_ZERO_CASE_ID")
        effect_names = [effect.effect for effect in self.maximum_observable_effects]
        if len(effect_names) != len(set(effect_names)):
            raise ValueError("FAULT_OBSERVABLE_EFFECTS_MUST_BE_UNIQUE")
        if not any(effect.minimum_count > 0 for effect in self.maximum_observable_effects):
            raise ValueError("FAULT_ATTACK_REQUIRES_POSITIVE_PROGRESS_OR_DECISION_EFFECT")
        if (
            self.recovery_class
            in {
                FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
                FaultRecoveryClass.RECOVERABLE_CONCURRENCY,
            }
            and self.recovery.terminal_failed_allowed
        ):
            raise ValueError("RECOVERABLE_FAULT_CANNOT_USE_TERMINAL_FAILED")
        suffix = self.cell_id.rsplit(".", maxsplit=1)[-1]
        expected_binding_ids = _REQUIRED_TYPED_BINDING_IDS.get(suffix)
        if expected_binding_ids is not None:
            bindings = {
                binding.binding_id: binding for binding in self.recovery.required_identity_bindings
            }
            if set(bindings) != expected_binding_ids:
                raise ValueError("FAULT_REQUIRED_TYPED_IDENTITY_BINDINGS_MISMATCH")
            for binding_id, binding in bindings.items():
                expected_comparison = (
                    "different"
                    if binding_id == "source_recovery_instance_identity"
                    else (
                        "monotonic_non_decreasing"
                        if binding_id.endswith("budget_monotonic")
                        else "exact_equal"
                    )
                )
                if binding.comparison != expected_comparison:
                    raise ValueError("FAULT_REQUIRED_IDENTITY_COMPARISON_MISMATCH")
        return self


class MatchedBenignControl(_FaultModel):
    catalog_id: str = Field(min_length=1)
    hard_zero_gate_id: str = Field(min_length=1)
    hard_zero_case_id: str = Field(min_length=1)
    production_target: ProductionFaultTarget
    catalog_hit_count: Literal[1]
    is_fault_injection: Literal[False]
    normal_path_trigger: str = Field(min_length=1)
    expected_terminal_state: str = Field(min_length=1)
    normal_path_deadline: FrozenRecoveryDeadline
    expected_observable_effects: tuple[BenignObservableEffect, ...] = Field(min_length=1)
    oracle: FaultOracleContract
    run_status: Literal[EvalStatus.NOT_RUN]

    @model_validator(mode="after")
    def validate_benign_control(self) -> MatchedBenignControl:
        if self.catalog_id != self.hard_zero_case_id or ".benign." not in self.catalog_id:
            raise ValueError("FAULT_BENIGN_CATALOG_ID_MUST_EQUAL_HARD_ZERO_CASE_ID")
        names = [effect.effect for effect in self.expected_observable_effects]
        if len(names) != len(set(names)):
            raise ValueError("BENIGN_OBSERVABLE_EFFECTS_MUST_BE_UNIQUE")
        return self


class RequiredProductionTopology(_FaultModel):
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    production_path_exercised: Literal[False]
    future_production_path_required: Literal[True]
    postgresql: Literal["real_postgresql"]
    vector_index: Literal["real_pgvector"]
    delivery_queue: Literal["real_redis_streams"]
    tenant_count: Literal[2]
    application_instance_count: Literal[2]
    topology_statement: Literal[
        "FUTURE_PRODUCTION_PATH_REQUIRES_REAL_PG_PGVECTOR_REDIS_TWO_TENANTS_TWO_INSTANCES"
    ]


class FaultExecutionRules(_FaultModel):
    rules: tuple[str, ...] = Field(min_length=1)
    synthetic_fixture_may_prove_production_recovery: Literal[False]
    fault_script_is_sealed_quality_data: Literal[False]
    uniform_fast_failure_is_valid_recovery: Literal[False]


class FaultPlanCoverageEntry(_FaultModel):
    plan_reference: Literal["§3.3", "N2.2", "N2.3", "N4.2", "N4.4"]
    requirement: str = Field(min_length=1)
    attack_cell_ids: tuple[str, ...] = Field(min_length=1)
    matched_benign_control_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_coverage_ids(self) -> FaultPlanCoverageEntry:
        if len(self.attack_cell_ids) != len(set(self.attack_cell_ids)):
            raise ValueError("FAULT_PLAN_ATTACK_IDS_MUST_BE_UNIQUE")
        if len(self.matched_benign_control_ids) != len(set(self.matched_benign_control_ids)):
            raise ValueError("FAULT_PLAN_BENIGN_IDS_MUST_BE_UNIQUE")
        return self


class FrozenFaultManifest(_FaultModel):
    schema_version: Literal["formal-fault-manifest/v1"]
    manifest_id: Literal["failure-isolation-matrix-faults-n0.3-v1"]
    suite_id: Literal["failure_isolation_matrix"]
    suite_version: Literal["n0.3-v1"]
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    production_path_exercised: Literal[False]
    run_status: Literal[EvalStatus.NOT_RUN]
    status_reasons: tuple[str, ...] = Field(min_length=1)
    required_topology: RequiredProductionTopology
    execution_rules: FaultExecutionRules
    attack_cells: tuple[AttackFaultCell, ...] = Field(min_length=1)
    matched_benign_controls: tuple[MatchedBenignControl, ...] = Field(min_length=1)
    plan_coverage: tuple[FaultPlanCoverageEntry, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_manifest(self) -> FrozenFaultManifest:
        attack_ids = [cell.cell_id for cell in self.attack_cells]
        benign_ids = [control.catalog_id for control in self.matched_benign_controls]
        if len(attack_ids) != len(set(attack_ids)):
            raise ValueError("FAULT_ATTACK_CELL_IDS_MUST_BE_UNIQUE")
        if len(benign_ids) != len(set(benign_ids)):
            raise ValueError("FAULT_BENIGN_CATALOG_IDS_MUST_BE_UNIQUE")
        if set(attack_ids) & set(benign_ids):
            raise ValueError("FAULT_ATTACK_AND_BENIGN_IDS_MUST_BE_DISJOINT")
        gate_roles: dict[str, set[str]] = {}
        for cell in self.attack_cells:
            gate_roles.setdefault(cell.hard_zero_gate_id, set()).add("attack")
        for control in self.matched_benign_controls:
            gate_roles.setdefault(control.hard_zero_gate_id, set()).add("benign")
        if any(roles != {"attack", "benign"} for roles in gate_roles.values()):
            raise ValueError("FAULT_MANIFEST_REQUIRES_PAIRED_HARD_ZERO_GATES")
        coverage_refs = [entry.plan_reference for entry in self.plan_coverage]
        if len(coverage_refs) != len(set(coverage_refs)) or set(coverage_refs) != {
            "§3.3",
            "N2.2",
            "N2.3",
            "N4.2",
            "N4.4",
        }:
            raise ValueError("FAULT_PLAN_COVERAGE_REFERENCES_MUST_BE_EXACT")
        flattened_attacks = [
            cell_id for entry in self.plan_coverage for cell_id in entry.attack_cell_ids
        ]
        flattened_benign = [
            cell_id for entry in self.plan_coverage for cell_id in entry.matched_benign_control_ids
        ]
        if len(flattened_attacks) != len(set(flattened_attacks)):
            raise ValueError("FAULT_PLAN_ATTACK_COVERAGE_MUST_BE_ONE_TO_ONE")
        if len(flattened_benign) != len(set(flattened_benign)):
            raise ValueError("FAULT_PLAN_BENIGN_COVERAGE_MUST_BE_ONE_TO_ONE")
        covered_attacks = set(flattened_attacks)
        covered_benign = set(flattened_benign)
        if covered_attacks != set(attack_ids):
            raise ValueError("FAULT_PLAN_ATTACK_COVERAGE_MUST_BE_EXACT")
        if covered_benign != set(benign_ids):
            raise ValueError("FAULT_PLAN_BENIGN_COVERAGE_MUST_BE_EXACT")
        return self


class FrozenFaultManifestMismatchError(ValueError):
    """Raised when committed fault-manifest bytes differ from the renderer."""


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _target(entrypoint: str) -> ProductionFaultTarget:
    return ProductionFaultTarget(
        entrypoint=entrypoint,
        implementation_status=EvalStatus.NOT_AVAILABLE,
        production_path_exercised=False,
        unavailable_reasons=(
            "TEAM_PROFILE_PRODUCTION_PATH_NOT_AVAILABLE",
            "PRODUCTION_FAULT_RUNNER_NOT_AVAILABLE",
        ),
    )


def _action(action_id: str, description: str) -> BoundaryAction:
    return BoundaryAction(
        action_id=action_id,
        description=description,
        durable_evidence_required=True,
        evidence_requirements=(
            "Capture the durable state before process termination or dependency recovery.",
            "Hash-bind the raw persistence record and its transaction or stream identity.",
        ),
    )


def _boundary(
    action_a_id: str,
    action_a_description: str,
    action_b_id: str,
    action_b_description: str,
) -> FaultBoundary:
    return FaultBoundary(
        action_a=_action(action_a_id, action_a_description),
        action_a_completed_required=True,
        action_b=_action(action_b_id, action_b_description),
        action_b_not_completed_required=True,
    )


def _fixed_deadline(milliseconds: int) -> FrozenRecoveryDeadline:
    return FrozenRecoveryDeadline(fixed_duration_ms=milliseconds)


def _formula_deadline(
    formula: str, parameters: tuple[tuple[str, int], ...]
) -> FrozenRecoveryDeadline:
    return FrozenRecoveryDeadline(
        formula=formula,
        parameters=tuple(
            DeadlineParameter(name=name, milliseconds=milliseconds)
            for name, milliseconds in parameters
        ),
    )


def _binding(
    binding_id: str,
    left_evidence_ref: str,
    right_evidence_ref: str,
    comparison: Literal["exact_equal", "different", "monotonic_non_decreasing"] = "exact_equal",
) -> RecoveryIdentityBinding:
    return RecoveryIdentityBinding(
        binding_id=binding_id,
        left_evidence_ref=left_evidence_ref,
        right_evidence_ref=right_evidence_ref,
        comparison=comparison,
    )


def _oracle(gate_id: str, *, benign: bool = False) -> FaultOracleContract:
    evidence_requirements = (
        (
            "Record catalog_hit_count exactly one and prove no injection marker was active.",
            "Capture the normal-path trigger, final terminal state, and elapsed time.",
            "Hash-bind the expected and actual benign observable side-effect counts.",
        )
        if benign
        else (
            (
                "Record injection_hit_count exactly one; zero, duplicate, or an expected process "
                "hook followed by natural exit is NOT_RUN."
            ),
            (
                "Capture the expected mechanism-specific observation and, only for process "
                "faults, exact termination."
            ),
            "Capture action A durable evidence, prove A completed, and prove action B did not run.",
            "Hash-bind recovery entry, final state, elapsed time, and observable-effect counts.",
        )
    )
    return FaultOracleContract(
        oracle_requirements=(
            f"Recompute hard-zero gate {gate_id} from durable state and externally visible output.",
            (
                "Check recovery state, deadline, and every minimum and maximum observable-effect "
                "bound."
            ),
        ),
        evidence_requirements=evidence_requirements,
    )


def _effect_limits(
    *items: tuple[str, int] | tuple[str, int, int],
) -> tuple[ObservableEffectLimit, ...]:
    limits: list[ObservableEffectLimit] = []
    for item in items:
        if len(item) == 2:
            name, exact_count = item
            minimum_count = exact_count
            maximum_count = exact_count
        else:
            name, minimum_count, maximum_count = item
        limits.append(
            ObservableEffectLimit(
                effect=name,
                minimum_count=minimum_count,
                maximum_count=maximum_count,
            )
        )
    return tuple(limits)


def _benign_effects(*items: tuple[str, int, int]) -> tuple[BenignObservableEffect, ...]:
    return tuple(
        BenignObservableEffect(effect=name, minimum_count=minimum, maximum_count=maximum)
        for name, minimum, maximum in items
    )


def _attack_cells() -> tuple[AttackFaultCell, ...]:
    suite = "failure_isolation_matrix"
    not_run: Literal[EvalStatus.NOT_RUN] = EvalStatus.NOT_RUN
    base = (
        AttackFaultCell(
            cell_id=f"{suite}.attack.tenant-a-to-b-read-001",
            hard_zero_gate_id="cross_tenant_read_write",
            hard_zero_case_id=f"{suite}.attack.tenant-a-to-b-read-001",
            production_target=_target("team API -> pooled PostgreSQL connection -> RLS SELECT"),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_POLICY_REJECTION,
            injection=FaultInjectionContract(
                trigger=(
                    "After Tenant A principal binding and before Tenant B row policy evaluation."
                ),
                mechanism=FaultInjectionMechanism.REQUEST_SCOPE_ATTACK,
                expected_hit_count=1,
                expected_observation=(
                    "RLS policy denies the cross-tenant SELECT and the response contains no "
                    "target bytes."
                ),
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "tenant_b_target_row_durable",
                "Tenant B target row and Tenant A principal binding are durably observable.",
                "cross_tenant_read_materialized",
                "Tenant B bytes are materialized in Tenant A response or durable response cache.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="team API authorization error mapping",
                expected_terminal_or_continuing_states=("POLICY_REJECTED",),
                deadline=_fixed_deadline(5_000),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "tenant_a_principal_scope",
                        "injection.principal_tenant_id",
                        "policy_decision.principal_tenant_id",
                    ),
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("cross_tenant_rows_returned", 0),
                ("canonical_mutations", 0),
                ("policy_rejection_decisions", 1),
            ),
            oracle=_oracle("cross_tenant_read_write"),
            run_status=not_run,
        ),
        AttackFaultCell(
            cell_id=f"{suite}.attack.tenant-a-to-b-write-001",
            hard_zero_gate_id="cross_tenant_read_write",
            hard_zero_case_id=f"{suite}.attack.tenant-a-to-b-write-001",
            production_target=_target("team API -> pooled PostgreSQL connection -> RLS mutation"),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_POLICY_REJECTION,
            injection=FaultInjectionContract(
                trigger="After Tenant A principal binding and before Tenant B row mutation policy.",
                mechanism=FaultInjectionMechanism.REQUEST_SCOPE_ATTACK,
                expected_hit_count=1,
                expected_observation=(
                    "RLS policy denies the cross-tenant mutation and the target row hash is "
                    "unchanged."
                ),
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "tenant_b_original_bytes_durable",
                "Tenant B row hash is captured from durable PostgreSQL truth.",
                "cross_tenant_write_committed",
                "A mutation under Tenant A authority is committed to the Tenant B row.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="team API authorization error mapping",
                expected_terminal_or_continuing_states=("POLICY_REJECTED",),
                deadline=_fixed_deadline(5_000),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "tenant_b_target_scope",
                        "injection.target_tenant_id",
                        "policy_decision.target_tenant_id",
                    ),
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("cross_tenant_rows_mutated", 0),
                ("tenant_b_hash_changes", 0),
                ("policy_rejection_decisions", 1),
            ),
            oracle=_oracle("cross_tenant_read_write"),
            run_status=not_run,
        ),
        AttackFaultCell(
            cell_id=f"{suite}.attack.kill-release-boundary-001",
            hard_zero_gate_id="mixed_revision_recovery",
            hard_zero_case_id=f"{suite}.attack.kill-release-boundary-001",
            production_target=_target(
                "release transaction -> revision pointer recovery reconciler"
            ),
            recovery_class=FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
            injection=FaultInjectionContract(
                trigger=(
                    "Immediately after the revision/review/outbox transaction commits while "
                    "serving remains old, before required projections and serving advance."
                ),
                mechanism=FaultInjectionMechanism.PROCESS_SIGNAL,
                expected_hit_count=1,
                expected_observation=(
                    "The child process reaches the named post-transaction/pre-projection hook "
                    "exactly once."
                ),
                expected_process_termination=ExpectedProcessTermination(signal="SIGKILL"),
                natural_exit_is_not_run=True,
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "revision_review_outbox_transaction_committed",
                (
                    "New revision bytes, review decision, append-only ledger, and outbox are "
                    "durable while serving still names the old revision."
                ),
                "required_projections_ready_and_serving_advanced",
                "Every required projection is READY and serving advances once to the new revision.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="application restart -> release reconciliation scan",
                expected_terminal_or_continuing_states=("SERVING_NEW_REVISION",),
                deadline=_formula_deadline(
                    (
                        "process_restart_ms + projection_retry_interval_ms + "
                        "reconcile_interval_ms + recovery_margin_ms"
                    ),
                    (
                        ("process_restart_ms", 5_000),
                        ("projection_retry_interval_ms", 5_000),
                        ("reconcile_interval_ms", 5_000),
                        ("recovery_margin_ms", 2_000),
                    ),
                ),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "committed_release_revision",
                        "action_a.revision_id",
                        "recovery.serving_revision_id",
                    ),
                ),
                required_recovery_invariants=(
                    "Every required projection is READY for the committed revision.",
                    "Serving advances exactly once to that revision within the deadline.",
                    "No mixed old/new revision is externally observable during recovery.",
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("mixed_revision_reads", 0),
                ("serving_pointer_advances_before_required_projections_ready", 0),
                ("serving_pointer_advances_total", 1),
                ("new_revision_rows", 1),
            ),
            oracle=_oracle("mixed_revision_recovery"),
            run_status=not_run,
        ),
        AttackFaultCell(
            cell_id=f"{suite}.attack.redeliver-outbox-001",
            hard_zero_gate_id="duplicate_observable_canonical_effect",
            hard_zero_case_id=f"{suite}.attack.redeliver-outbox-001",
            production_target=_target("PostgreSQL outbox -> Redis Streams -> idempotent consumer"),
            recovery_class=FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
            injection=FaultInjectionContract(
                trigger="After the first canonical effect is durable and before delivery ACK.",
                mechanism=FaultInjectionMechanism.MESSAGE_REDELIVERY,
                expected_hit_count=1,
                expected_observation=(
                    "The same outbox and idempotency identity is observed in two delivery events."
                ),
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "canonical_effect_committed",
                "One effect keyed by the outbox/idempotency identity is durable.",
                "stream_delivery_acknowledged",
                "The corresponding Redis Streams delivery has been acknowledged.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="pending-message reclaim -> idempotent consumer replay",
                expected_terminal_or_continuing_states=("DELIVERED", "ACKNOWLEDGED"),
                deadline=_formula_deadline(
                    "pending_reclaim_ms + reconcile_interval_ms + recovery_margin_ms",
                    (
                        ("pending_reclaim_ms", 30_000),
                        ("reconcile_interval_ms", 5_000),
                        ("recovery_margin_ms", 5_000),
                    ),
                ),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "outbox_delivery_identity",
                        "action_a.outbox_id",
                        "recovery.delivery_outbox_id",
                    ),
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("observable_canonical_effects", 1), ("durable_outbox_rows", 1)
            ),
            oracle=_oracle("duplicate_observable_canonical_effect"),
            run_status=not_run,
        ),
        AttackFaultCell(
            cell_id=f"{suite}.attack.worker-kill-after-claim-001",
            hard_zero_gate_id="durable_claim_without_unique_done",
            hard_zero_case_id=f"{suite}.attack.worker-kill-after-claim-001",
            production_target=_target("Redis Streams worker -> PostgreSQL assignment ledger"),
            recovery_class=FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
            injection=FaultInjectionContract(
                trigger="After durable claim/lease/fencing token and before result persistence.",
                mechanism=FaultInjectionMechanism.PROCESS_SIGNAL,
                expected_hit_count=1,
                expected_observation=(
                    "The child process reaches the named post-claim/pre-result hook exactly once."
                ),
                expected_process_termination=ExpectedProcessTermination(signal="SIGKILL"),
                natural_exit_is_not_run=True,
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "assignment_claim_committed",
                "Assignment CLAIMED state, lease, attempt, and fencing token are durable.",
                "assignment_result_committed",
                "Verified result, evidence, and terminal DONE state are durable.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="lease expiry -> reconciler -> fenced worker reclaim",
                expected_terminal_or_continuing_states=("DONE",),
                deadline=_formula_deadline(
                    "lease_timeout_ms + reconcile_interval_ms + recovery_margin_ms",
                    (
                        ("lease_timeout_ms", 30_000),
                        ("reconcile_interval_ms", 5_000),
                        ("recovery_margin_ms", 5_000),
                    ),
                ),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "assignment_reclaim_identity",
                        "action_a.assignment_id",
                        "recovery.assignment_id",
                    ),
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("terminal_done_results", 1),
                ("observable_canonical_effects", 1),
                ("active_fencing_tokens", 1),
            ),
            oracle=_oracle("durable_claim_without_unique_done"),
            run_status=not_run,
        ),
        AttackFaultCell(
            cell_id=f"{suite}.attack.exit-before-ack-001",
            hard_zero_gate_id="durable_claim_without_unique_done",
            hard_zero_case_id=f"{suite}.attack.exit-before-ack-001",
            production_target=_target("PostgreSQL result commit -> Redis Streams ACK boundary"),
            recovery_class=FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
            injection=FaultInjectionContract(
                trigger="After result/evidence/DONE commit and immediately before stream ACK.",
                mechanism=FaultInjectionMechanism.PROCESS_EXIT,
                expected_hit_count=1,
                expected_observation=(
                    "The child process reaches the named post-result/pre-ACK hook exactly once."
                ),
                expected_process_termination=ExpectedProcessTermination(exit_code=86),
                natural_exit_is_not_run=True,
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "assignment_done_committed",
                "Verified result, evidence, and unique terminal DONE are durable.",
                "assignment_stream_acked",
                "Redis Streams delivery is acknowledged after the durable terminal result.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="pending-message reclaim -> durable-result reconciliation",
                expected_terminal_or_continuing_states=("DONE",),
                deadline=_formula_deadline(
                    "pending_reclaim_ms + reconcile_interval_ms + recovery_margin_ms",
                    (
                        ("pending_reclaim_ms", 30_000),
                        ("reconcile_interval_ms", 5_000),
                        ("recovery_margin_ms", 5_000),
                    ),
                ),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "assignment_result_identity",
                        "action_a.assignment_id",
                        "recovery.assignment_id",
                    ),
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("terminal_done_results", 1),
                ("observable_canonical_effects", 1),
                ("stream_acknowledgements", 1),
            ),
            oracle=_oracle("durable_claim_without_unique_done"),
            run_status=not_run,
        ),
        AttackFaultCell(
            cell_id=f"{suite}.attack.projection-failure-001",
            hard_zero_gate_id="projection_not_ready_serving_advance",
            hard_zero_case_id=f"{suite}.attack.projection-failure-001",
            production_target=_target("projection worker -> pgvector -> serving-pointer gate"),
            recovery_class=FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
            injection=FaultInjectionContract(
                trigger="After authoring acceptance and before required projection READY commit.",
                mechanism=FaultInjectionMechanism.DEPENDENCY_UNAVAILABLE,
                expected_hit_count=1,
                expected_observation=(
                    "The required projection call observes the registered dependency-unavailable "
                    "response exactly once."
                ),
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                "authoring_revision_and_outbox_committed",
                "New authoring revision and projection request are durable in PostgreSQL.",
                "required_projection_ready_committed",
                "Required pgvector/graph projection READY state is durable.",
            ),
            recovery=RecoveryContract(
                recovery_entrypoint="dependency restore -> projection reconciliation worker",
                expected_terminal_or_continuing_states=("SERVING_NEW_REVISION",),
                deadline=_formula_deadline(
                    (
                        "dependency_restore_ms + projection_retry_interval_ms + "
                        "reconcile_interval_ms + recovery_margin_ms"
                    ),
                    (
                        ("dependency_restore_ms", 5_000),
                        ("projection_retry_interval_ms", 5_000),
                        ("reconcile_interval_ms", 5_000),
                        ("recovery_margin_ms", 5_000),
                    ),
                ),
                terminal_failed_allowed=False,
                required_identity_bindings=(
                    _binding(
                        "projection_revision_identity",
                        "action_a.source_revision_id",
                        "recovery.ready_revision_id",
                    ),
                ),
                required_recovery_invariants=(
                    "While the dependency fault is active, serving remains on the old revision.",
                    "After dependency restore, every required projection becomes READY.",
                    "Serving advances at most once and ends on the fully projected new revision.",
                ),
            ),
            maximum_observable_effects=_effect_limits(
                ("serving_pointer_advances_while_fault_active", 0),
                ("serving_pointer_advances_total_after_recovery", 1),
                ("mixed_revision_reads", 0),
                ("projection_retry_records", 1),
            ),
            oracle=_oracle("projection_not_ready_serving_advance"),
            run_status=not_run,
        ),
    )
    return base + _additional_attack_cells()


def _additional_attack_cells() -> tuple[AttackFaultCell, ...]:
    suite = "failure_isolation_matrix"
    not_run: Literal[EvalStatus.NOT_RUN] = EvalStatus.NOT_RUN

    def make(
        suffix: str,
        gate_id: str,
        entrypoint: str,
        mechanism: FaultInjectionMechanism,
        trigger: str,
        observation: str,
        action_a_id: str,
        action_a_description: str,
        action_b_id: str,
        action_b_description: str,
        terminal_states: tuple[str, ...],
        effects: tuple[tuple[str, int] | tuple[str, int, int], ...],
        *,
        recovery_class: FaultRecoveryClass = FaultRecoveryClass.RECOVERABLE_INFRASTRUCTURE,
        recovery_entrypoint: str = "production restart or reconciliation entrypoint",
        signal: str | None = None,
        exit_code: int | None = None,
        terminal_failed_allowed: bool = False,
        recovery_invariants: tuple[str, ...] | None = None,
        identity_bindings: tuple[RecoveryIdentityBinding, ...] | None = None,
        extra_oracle_requirements: tuple[str, ...] = (),
        extra_evidence_requirements: tuple[str, ...] = (),
    ) -> AttackFaultCell:
        termination = None
        if signal is not None:
            termination = ExpectedProcessTermination(signal=signal)
        elif exit_code is not None:
            termination = ExpectedProcessTermination(exit_code=exit_code)
        oracle = _oracle(gate_id)
        if extra_oracle_requirements or extra_evidence_requirements:
            oracle = oracle.model_copy(
                update={
                    "oracle_requirements": (oracle.oracle_requirements + extra_oracle_requirements),
                    "evidence_requirements": (
                        oracle.evidence_requirements + extra_evidence_requirements
                    ),
                }
            )
        return AttackFaultCell(
            cell_id=f"{suite}.attack.{suffix}",
            hard_zero_gate_id=gate_id,
            hard_zero_case_id=f"{suite}.attack.{suffix}",
            production_target=_target(entrypoint),
            recovery_class=recovery_class,
            injection=FaultInjectionContract(
                trigger=trigger,
                mechanism=mechanism,
                expected_hit_count=1,
                expected_observation=observation,
                expected_process_termination=termination,
                natural_exit_is_not_run=True if termination is not None else None,
                non_exact_hit_status=not_run,
            ),
            boundary=_boundary(
                action_a_id,
                action_a_description,
                action_b_id,
                action_b_description,
            ),
            recovery=RecoveryContract(
                recovery_entrypoint=recovery_entrypoint,
                expected_terminal_or_continuing_states=terminal_states,
                deadline=_fixed_deadline(40_000),
                terminal_failed_allowed=terminal_failed_allowed,
                required_identity_bindings=identity_bindings
                or (
                    _binding(
                        f"{suffix}_tenant_scope",
                        "injection.tenant_scope_id",
                        "recovery.tenant_scope_id",
                    ),
                ),
                required_recovery_invariants=recovery_invariants
                or (
                    "Recovery is recomputed from durable production truth.",
                    "The registered hard-zero effect bounds remain satisfied.",
                ),
            ),
            maximum_observable_effects=_effect_limits(*effects),
            oracle=oracle,
            run_status=not_run,
        )

    return (
        make(
            "kill-release-before-commit-001",
            "mixed_revision_recovery",
            "release transaction pre-commit hook -> restart reconciler",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After base validation and immediately before the release transaction commit.",
            "The child reaches the registered pre-commit hook exactly once and receives SIGKILL.",
            "release_base_validated",
            "The old authoring and serving heads and proposed base are durably captured.",
            "release_transaction_committed",
            "Revision, ledger, outbox, and authoring-head changes commit atomically.",
            ("OLD_REVISION_UNCHANGED",),
            (
                ("partial_release_rows", 0),
                ("authoring_pointer_advances", 0),
                ("serving_pointer_advances", 0),
                ("old_revision_recovery_confirmations", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="application restart -> release reconciliation scan",
        ),
        make(
            "same-base-concurrent-release-001",
            "concurrent_release_cas",
            "concurrent release transactions -> authoring-head CAS barrier",
            FaultInjectionMechanism.CONCURRENT_CAS,
            "After two approved writers validate the same base and before either head CAS.",
            "Both writers cross one barrier and attempt the same-base CAS exactly once.",
            "two_same_base_attempts_ready",
            "Both approved attempts and their identical base revision are durably identified.",
            "second_same_base_successor_committed",
            "A second successor from the stale base commits after the winning CAS.",
            ("ONE_COMMITTED_ONE_CONFLICT",),
            (("canonical_successors", 1), ("authoring_pointer_advances", 1)),
            recovery_class=FaultRecoveryClass.RECOVERABLE_CONCURRENCY,
            recovery_entrypoint="authoring-head compare-and-swap conflict response",
        ),
        make(
            "redis-transient-outage-001",
            "duplicate_observable_canonical_effect",
            "PostgreSQL outbox relay -> Redis Streams",
            FaultInjectionMechanism.DEPENDENCY_UNAVAILABLE,
            "After the outbox commit and before Redis XADD, make Redis unavailable once.",
            "The relay records one registered Redis-unavailable response.",
            "outbox_row_committed",
            "The outbox identity and canonical transaction are durable in PostgreSQL.",
            "stream_identity_published",
            "The outbox identity is present in Redis Streams.",
            ("DELIVERED", "ACKNOWLEDGED"),
            (("stream_identities", 1), ("observable_canonical_effects", 1)),
            recovery_entrypoint="Redis restore -> pending outbox relay scan",
        ),
        make(
            "relay-restart-before-delivery-001",
            "duplicate_observable_canonical_effect",
            "PostgreSQL outbox relay pre-delivery hook",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "Immediately before Redis delivery for one durable pending outbox row.",
            "The relay reaches the pre-delivery hook exactly once and receives SIGKILL.",
            "pending_outbox_row_committed",
            "The pending outbox row and idempotency identity are durable.",
            "stream_delivery_called",
            "The relay calls Redis delivery for that identity.",
            ("DELIVERED", "ACKNOWLEDGED"),
            (("stream_identities", 1), ("observable_canonical_effects", 1)),
            signal="SIGKILL",
            recovery_entrypoint="relay restart -> pending outbox scan",
        ),
        make(
            "relay-restart-after-delivery-before-mark-001",
            "duplicate_observable_canonical_effect",
            "Redis Streams delivery -> PostgreSQL outbox delivered marker",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After XADD returns and immediately before marking the outbox row delivered.",
            "The relay reaches the post-delivery/pre-mark hook once and receives SIGKILL.",
            "stream_identity_published",
            "The exact outbox identity is durably observable in Redis Streams.",
            "outbox_delivered_marker_committed",
            "PostgreSQL records delivery for the same outbox row.",
            ("DELIVERED", "ACKNOWLEDGED"),
            (
                ("stream_identities", 1),
                ("delivered_markers", 1),
                ("observable_canonical_effects", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="relay restart -> stream/outbox identity reconciliation",
        ),
        make(
            "projection-worker-kill-after-claim-001",
            "projection_job_without_unique_done",
            "Redis projection consumer -> PostgreSQL projection ledger",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            (
                "After claim, lease, attempt, and fence commit and before projection result "
                "persistence."
            ),
            "The projection worker reaches the post-claim hook once and receives SIGKILL.",
            "projection_claim_committed",
            "Projection CLAIMED state, lease, attempt, and fence are durable.",
            "projection_result_ready_committed",
            "Projection result, READY, and terminal DONE are durable.",
            ("DONE", "READY"),
            (("projection_results", 1), ("terminal_done_results", 1)),
            signal="SIGKILL",
            recovery_entrypoint="lease expiry -> fenced projection job reclaim",
        ),
        make(
            "projection-exit-before-ack-001",
            "projection_job_without_unique_done",
            "projection READY/DONE commit -> Redis Streams ACK",
            FaultInjectionMechanism.PROCESS_EXIT,
            "After projection result, READY, and DONE commit and immediately before ACK.",
            "The projection worker reaches the pre-ACK hook once and exits with code 86.",
            "projection_done_committed",
            "Projection result, READY, and terminal DONE are durable.",
            "projection_stream_acked",
            "The projection delivery is acknowledged.",
            ("DONE", "READY"),
            (("projection_results", 1), ("stream_acknowledgements", 1)),
            exit_code=86,
            recovery_entrypoint="pending reclaim -> durable projection result reconciliation",
        ),
        make(
            "projection-worker-kill-after-ack-001",
            "projection_job_without_unique_done",
            "projection Redis ACK post-commit hook -> worker restart",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After projection result, READY, DONE, and ACK are durable and before the next step.",
            "The projection worker reaches the post-ACK hook once and receives SIGKILL.",
            "projection_done_ready_and_ack_committed",
            "Projection result, terminal DONE/READY, and stream ACK are all durable.",
            "duplicate_projection_side_effect_committed",
            "A replayed result, READY transition, ACK, or serving effect is committed.",
            ("DONE", "READY_FOR_NEXT_DELIVERY"),
            (
                ("projection_results", 1),
                ("terminal_done_results", 1),
                ("stream_acknowledgements", 1),
                ("duplicate_projection_side_effects", 0),
                ("next_consumer_deliveries", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint=(
                "worker restart -> PostgreSQL DONE/READY query -> next consumer delivery"
            ),
            recovery_invariants=(
                "PostgreSQL durable DONE/READY remains the authoritative completed result.",
                "The restarted consumer proceeds to its next delivery without replaying effects.",
                "Exactly one projection result, terminal state, and ACK remain observable.",
            ),
            extra_oracle_requirements=(
                (
                    "Query PostgreSQL DONE/READY before and after restart and compare exact "
                    "identities."
                ),
                "Observe one subsequent normal consumer delivery without a repeated prior effect.",
            ),
            extra_evidence_requirements=(
                (
                    "Hash-bind result, terminal row, ACK identity, SIGKILL, restart, and next "
                    "delivery."
                ),
            ),
        ),
        make(
            "projection-stale-fencing-token-001",
            "projection_job_without_unique_done",
            "PostgreSQL projection result fencing CAS",
            FaultInjectionMechanism.STALE_FENCING_TOKEN,
            "After a replacement projection worker owns a newer fence, submit the old result.",
            "One late update carries the registered expired fencing token.",
            "new_projection_fence_and_result_committed",
            "The replacement token and its current result are durable.",
            "stale_projection_result_overwrite_committed",
            "The expired token overwrites the newer projection result.",
            ("DONE", "READY"),
            (("stale_result_overwrites", 0), ("projection_results", 1)),
            recovery_class=FaultRecoveryClass.RECOVERABLE_CONCURRENCY,
            recovery_entrypoint="projection result fencing compare-and-swap rejection",
        ),
        make(
            "worker-kill-before-result-001",
            "durable_claim_without_unique_done",
            "worker evidence commit -> assignment result commit",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After tool evidence is durable and immediately before result/DONE persistence.",
            "The worker reaches the post-evidence/pre-result hook once and receives SIGKILL.",
            "worker_evidence_committed",
            "Immutable evidence and any idempotent tool-effect identity are durable.",
            "assignment_result_committed",
            "Verified result and terminal DONE are durable.",
            ("DONE",),
            (("terminal_done_results", 1), ("observable_canonical_effects", 1)),
            signal="SIGKILL",
            recovery_entrypoint="lease expiry -> fenced assignment reclaim",
        ),
        make(
            "verifier-restart-before-verdict-001",
            "durable_claim_without_unique_done",
            "durable assignment result -> independent verifier verdict",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After result/evidence persistence and immediately before verdict persistence.",
            "The verifier reaches the pre-verdict hook once and receives SIGKILL.",
            "assignment_result_and_evidence_committed",
            "The assignment result and immutable evidence are durable.",
            "verifier_verdict_committed",
            "One terminal verifier verdict is durable.",
            ("DONE",),
            (("verifier_verdicts", 1), ("terminal_done_results", 1)),
            signal="SIGKILL",
            recovery_entrypoint="verifier restart -> unverified durable result scan",
        ),
        make(
            "assignment-stale-fencing-token-001",
            "durable_claim_without_unique_done",
            "PostgreSQL assignment result fencing CAS",
            FaultInjectionMechanism.STALE_FENCING_TOKEN,
            "After replacement ownership with a newer fence, submit the expired worker result.",
            "One late assignment update carries the registered expired fencing token.",
            "new_assignment_fence_and_result_committed",
            "The replacement token and current result are durable.",
            "stale_assignment_result_overwrite_committed",
            "The expired token overwrites the replacement result.",
            ("DONE",),
            (("stale_result_overwrites", 0), ("terminal_done_results", 1)),
            recovery_class=FaultRecoveryClass.RECOVERABLE_CONCURRENCY,
            recovery_entrypoint="assignment result fencing compare-and-swap rejection",
        ),
        make(
            "assignment-redelivery-001",
            "durable_claim_without_unique_done",
            "Redis assignment redelivery -> PostgreSQL assignment ledger",
            FaultInjectionMechanism.MESSAGE_REDELIVERY,
            "After assignment DONE is durable, redeliver the same stream and assignment identity.",
            "The same assignment identity is observed in a second delivery exactly once.",
            "assignment_done_committed",
            "The verified result and terminal DONE are durable.",
            "duplicate_assignment_execution_started",
            "The duplicate delivery starts a second execution.",
            ("DONE",),
            (("assignment_executions", 1), ("terminal_done_results", 1)),
            recovery_entrypoint="idempotent assignment lookup -> existing durable result",
        ),
        make(
            "agent-kill-before-tool-001",
            "durable_agent_run_resume",
            "durable AgentRun dispatcher pre-tool hook",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After plan/approval/budget/intent persistence and immediately before tool dispatch.",
            "The Agent process reaches the pre-tool hook once and receives SIGKILL.",
            "agent_run_tool_intent_committed",
            "Plan, approval, cumulative budget, and next tool intent are durable.",
            "tool_dispatch_started",
            "The registered tool dispatch starts.",
            ("DONE", "RUNNING"),
            (
                ("tool_dispatches", 1),
                ("observable_tool_effects", 1),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> pending tool-intent resume",
            identity_bindings=(
                _binding(
                    "before_tool_run_identity",
                    "action_a.agent_run_id",
                    "recovery.agent_run_id",
                ),
                _binding(
                    "pending_tool_intent_identity",
                    "action_a.tool_intent_id",
                    "recovery.dispatched_tool_intent_id",
                ),
                _binding(
                    "before_tool_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "agent-kill-after-tool-001",
            "durable_agent_run_resume",
            "tool middleware -> durable AgentRun continuation",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After tool result/evidence persistence and before run-state continuation.",
            "The Agent process reaches the post-tool hook once and receives SIGKILL.",
            "tool_result_and_evidence_committed",
            "The tool result, effect identity, and immutable evidence are durable.",
            "agent_run_continuation_committed",
            "The next AgentRun state transition is durable.",
            ("DONE", "RUNNING"),
            (
                ("observable_tool_effects", 1),
                ("run_state_continuations", 1),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> evidence-identity resume",
            identity_bindings=(
                _binding(
                    "after_tool_run_identity",
                    "action_a.agent_run_id",
                    "recovery.agent_run_id",
                ),
                _binding(
                    "tool_evidence_identity",
                    "action_a.evidence_artifact_hash",
                    "recovery.restored_evidence_artifact_hash",
                ),
                _binding(
                    "after_tool_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "agent-kill-before-checkpoint-001",
            "durable_agent_run_resume",
            "ObservationStore -> AgentRun checkpoint commit",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After observation persistence and immediately before checkpoint commit.",
            "The Agent process reaches the pre-checkpoint hook once and receives SIGKILL.",
            "observation_artifact_committed",
            "The large observation artifact and hash are durable.",
            "checkpoint_reference_committed",
            "The AgentRun checkpoint references that observation.",
            ("DONE", "RUNNING"),
            (
                ("live_observation_artifacts", 1),
                ("checkpoint_references", 1),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> orphan observation reconciliation",
            identity_bindings=(
                _binding(
                    "before_checkpoint_run_identity",
                    "action_a.agent_run_id",
                    "recovery.agent_run_id",
                ),
                _binding(
                    "observation_checkpoint_hash_identity",
                    "action_a.observation_artifact_hash",
                    "recovery.checkpoint_observation_ref_hash",
                ),
                _binding(
                    "before_checkpoint_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "agent-kill-after-checkpoint-001",
            "durable_agent_run_resume",
            "AgentRun checkpoint post-commit hook",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After checkpoint commit and immediately before the next step starts.",
            "The Agent process reaches the post-checkpoint hook once and receives SIGKILL.",
            "agent_run_checkpoint_committed",
            "Checkpoint, observation refs, approval state, and cumulative budget are durable.",
            "next_agent_step_started",
            "The next AgentRun step starts.",
            ("DONE", "RUNNING"),
            (
                ("next_step_starts", 1),
                ("duplicate_completed_effects", 0),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> checkpoint resume",
            identity_bindings=(
                _binding(
                    "checkpoint_context_hash_identity",
                    "action_a.checkpoint_context_hash",
                    "recovery.restored_context_hash",
                ),
                _binding(
                    "checkpoint_approval_hash_identity",
                    "action_a.checkpoint_approval_state_hash",
                    "recovery.restored_approval_state_hash",
                ),
                _binding(
                    "after_checkpoint_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "agent-kill-at-hitl-pause-001",
            "durable_agent_run_resume",
            "durable AgentRun HITL pause wait",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "While PAUSED_HITL is durable and before any human decision is applied.",
            "The paused Agent process receives SIGKILL at the registered wait hook once.",
            "hitl_pause_committed",
            "PAUSED_HITL, pending approval, and cumulative budget are durable.",
            "human_decision_applied",
            "A human decision is appended and applied to the run.",
            ("PAUSED_HITL",),
            (
                ("unapproved_tool_effects", 0),
                ("restored_paused_hitl_states", 1),
                ("applied_human_decisions", 0, 1),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> durable HITL pause",
            identity_bindings=(
                _binding(
                    "hitl_pause_request_identity",
                    "action_a.pending_approval_request_id",
                    "recovery.pending_approval_request_id",
                ),
                _binding(
                    "hitl_approval_state_hash_identity",
                    "action_a.approval_state_hash",
                    "recovery.restored_approval_state_hash",
                ),
                _binding(
                    "hitl_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "agent-kill-before-reflection-001",
            "durable_agent_run_resume",
            "verifier feedback -> bounded Reflection generation",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After actionable verifier feedback persistence and before Reflection generation.",
            "The Agent process reaches the pre-Reflection hook once and receives SIGKILL.",
            "actionable_verifier_feedback_committed",
            "Verifier feedback and the remaining aggregate budget are durable.",
            "reflection_artifact_committed",
            "A Reflection artifact is durable.",
            ("DONE", "RUNNING"),
            (
                ("reflection_artifacts", 1),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> pending Reflection resume",
            identity_bindings=(
                _binding(
                    "reflection_feedback_hash_identity",
                    "action_a.verifier_feedback_hash",
                    "recovery.reflection_source_feedback_hash",
                ),
                _binding(
                    "before_reflection_run_identity",
                    "action_a.agent_run_id",
                    "recovery.agent_run_id",
                ),
                _binding(
                    "before_reflection_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "agent-kill-after-reflection-001",
            "durable_agent_run_resume",
            "Reflection artifact -> next AgentRun attempt",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After Reflection persistence and before next-attempt injection.",
            "The Agent process reaches the post-Reflection hook once and receives SIGKILL.",
            "reflection_artifact_committed",
            "One Reflection identity and remaining aggregate budget are durable.",
            "next_attempt_started",
            "The next attempt starts with that Reflection.",
            ("DONE", "RUNNING"),
            (
                ("reflection_artifacts", 1),
                ("next_attempts", 1),
                ("budget_resets", 0),
                ("budget_state_monotonicity_checks", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint="AgentRun restart -> persisted Reflection injection",
            identity_bindings=(
                _binding(
                    "reflection_next_attempt_hash_identity",
                    "action_a.reflection_artifact_hash",
                    "recovery.next_attempt_injected_reflection_hash",
                ),
                _binding(
                    "after_reflection_run_identity",
                    "action_a.agent_run_id",
                    "recovery.agent_run_id",
                ),
                _binding(
                    "after_reflection_budget_monotonic",
                    "action_a.consumed_budget",
                    "recovery.consumed_budget",
                    "monotonic_non_decreasing",
                ),
            ),
            extra_oracle_requirements=(
                "Recompute pre/post budget ledgers; consumed usage cannot decrease or reset caps.",
            ),
        ),
        make(
            "sse-revoked-membership-001",
            "revoked_sse_event_leakage",
            "authorized SSE stream membership recheck",
            FaultInjectionMechanism.MEMBERSHIP_REVOCATION,
            "After durable membership revocation and before the next tenant event or heartbeat.",
            "The open SSE stream observes the registered revocation exactly once.",
            "tenant_membership_revoked",
            "Membership revocation and its version are durable.",
            "post_revocation_event_delivered",
            "A tenant event is delivered after the revocation version.",
            ("DISCONNECTED",),
            (("post_revocation_tenant_events", 0), ("revocation_disconnects", 1)),
            recovery_entrypoint="SSE authorization recheck -> bounded disconnect",
        ),
        make(
            "relay-cross-tenant-payload-001",
            "service_role_cross_tenant_access",
            "non-superuser relay service-role transaction",
            FaultInjectionMechanism.REQUEST_SCOPE_ATTACK,
            "Bind a relay payload to a tenant different from the durable outbox row.",
            "The relay role rejects the opaque cross-tenant scope exactly once.",
            "relay_target_row_durable",
            "The target tenant row and mismatched payload scope are durably identified.",
            "cross_tenant_relay_access_materialized",
            "The relay reads, mutates, or publishes target-tenant data.",
            ("POLICY_REJECTED",),
            (
                ("cross_tenant_relay_rows", 0),
                ("cross_tenant_stream_events", 0),
                ("policy_rejection_decisions", 1),
            ),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_POLICY_REJECTION,
            recovery_entrypoint="relay service-role authorization rejection",
        ),
        make(
            "worker-cross-tenant-payload-001",
            "service_role_cross_tenant_access",
            "non-superuser worker service-role transaction",
            FaultInjectionMechanism.REQUEST_SCOPE_ATTACK,
            "Bind a worker payload to a tenant different from the durable assignment row.",
            "The worker role rejects the opaque cross-tenant scope exactly once.",
            "worker_target_row_durable",
            "The target tenant row and mismatched payload scope are durably identified.",
            "cross_tenant_worker_access_materialized",
            "The worker reads, mutates, or persists target-tenant data.",
            ("POLICY_REJECTED",),
            (
                ("cross_tenant_worker_rows", 0),
                ("cross_tenant_worker_results", 0),
                ("policy_rejection_decisions", 1),
            ),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_POLICY_REJECTION,
            recovery_entrypoint="worker service-role authorization rejection",
        ),
        make(
            "publish-idempotency-replay-001",
            "publish_idempotency_replay_duplicate_effect",
            "publish API -> idempotency ledger -> canonical release transaction",
            FaultInjectionMechanism.IDEMPOTENCY_REPLAY,
            "After one publish commits, replay its exact request and idempotency key once.",
            "The publish API records one replay hit for the same key and request hash.",
            "original_publish_transaction_committed",
            (
                "One authoring revision, ledger event, outbox row, and key-to-result binding are "
                "durable."
            ),
            "replayed_publish_transaction_committed",
            "The replay commits a second authoring revision, ledger event, or outbox row.",
            ("ORIGINAL_RESULT_RETURNED",),
            (
                ("visible_authoring_revisions", 1),
                ("durable_outbox_rows", 1),
                ("durable_ledger_effects", 1),
            ),
            recovery_entrypoint="publish idempotency lookup -> original durable result",
            extra_oracle_requirements=(
                (
                    "Count publish effects by the exact idempotency key and require one shared "
                    "identity."
                ),
            ),
            extra_evidence_requirements=(
                "Hash-bind both request bytes, the shared key, and the returned result identities.",
            ),
        ),
        make(
            "vector-reconciliation-duplicate-001",
            "reconciliation_revision_corruption",
            "PostgreSQL source truth -> pgvector reconciliation worker",
            FaultInjectionMechanism.DURABLE_STATE_CORRUPTION,
            (
                "Insert one duplicate vector row for a current durable source identity, then "
                "reconcile."
            ),
            "The reconciliation scan observes the registered duplicate identity exactly once.",
            "duplicate_vector_projection_durable",
            "Two vector rows name one durable source identity and revision.",
            "vector_repair_committed",
            "The repair transaction replaces the corrupted projection from durable source truth.",
            ("REPAIRED",),
            (("vector_rows_per_source_identity", 1), ("duplicate_vector_rows", 0)),
            recovery_entrypoint="vector reconciliation scan -> same-revision repair transaction",
            recovery_invariants=(
                "Recompute vector content and identity from durable canonical source truth.",
                "The repaired row names the same source revision and is unique by source identity.",
            ),
            extra_oracle_requirements=(
                "Compare repaired vector bytes and revision identity to durable source truth.",
            ),
            extra_evidence_requirements=(
                "Hash-bind corrupted rows, source bytes, repair transaction, and final unique row.",
            ),
        ),
        make(
            "community-cross-revision-corruption-001",
            "reconciliation_revision_corruption",
            "PostgreSQL source truth -> graph/community reconciliation worker",
            FaultInjectionMechanism.DURABLE_STATE_CORRUPTION,
            "Mix one prior-revision community member into the current projection, then reconcile.",
            "The reconciliation scan observes one registered cross-revision member exactly once.",
            "cross_revision_community_projection_durable",
            "A community row or member reference names a different source revision.",
            "community_repair_committed",
            "The repair transaction rebuilds the community from one durable source revision.",
            ("REPAIRED",),
            (("cross_revision_community_refs", 0), ("community_projection_versions", 1)),
            recovery_entrypoint="community reconciliation scan -> same-revision repair transaction",
            recovery_invariants=(
                "Recompute community membership and edges from durable canonical source truth.",
                "Every repaired row, member, and edge names one identical source revision.",
            ),
            extra_oracle_requirements=(
                "Compare every repaired community identity and revision to durable source truth.",
            ),
            extra_evidence_requirements=(
                "Hash-bind corrupted rows, source revision, repair transaction, and final graph.",
            ),
        ),
        make(
            "assignment-bad-payload-001",
            "nonrecoverable_assignment_terminal_reason",
            "assignment payload validator -> PostgreSQL terminal ledger",
            FaultInjectionMechanism.INVALID_PAYLOAD,
            "Submit one durably hash-bound assignment payload that fails the frozen schema.",
            "The validator records the exact BAD_PAYLOAD reason once before worker dispatch.",
            "invalid_assignment_payload_committed",
            "The assignment identity, payload hash, and validation failure are durable.",
            "unauthorized_worker_dispatch_started",
            "A worker or tool dispatch starts for the invalid payload.",
            ("FAILED_BAD_PAYLOAD",),
            (("terminal_failed_records", 1), ("unauthorized_tool_effects", 0)),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_REGISTERED_TERMINAL,
            recovery_entrypoint="payload validator -> FAILED_BAD_PAYLOAD terminal transaction",
            terminal_failed_allowed=True,
        ),
        make(
            "assignment-policy-rejection-001",
            "nonrecoverable_assignment_terminal_reason",
            "assignment task policy -> PostgreSQL terminal ledger",
            FaultInjectionMechanism.REQUEST_SCOPE_ATTACK,
            "Submit one assignment requesting an operation forbidden by the frozen task policy.",
            "The policy gate records the exact POLICY_REJECTION reason once before dispatch.",
            "policy_rejection_decision_durable",
            "Assignment identity, policy version, and rejection reason are durable.",
            "forbidden_operation_dispatched",
            "The rejected operation reaches a worker or tool.",
            ("FAILED_POLICY_REJECTION",),
            (("terminal_failed_records", 1), ("forbidden_tool_effects", 0)),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_REGISTERED_TERMINAL,
            recovery_entrypoint="task policy -> FAILED_POLICY_REJECTION terminal transaction",
            terminal_failed_allowed=True,
        ),
        make(
            "assignment-explicit-cancellation-001",
            "nonrecoverable_assignment_terminal_reason",
            "assignment cancellation event -> PostgreSQL terminal ledger",
            FaultInjectionMechanism.EXPLICIT_CANCELLATION,
            "After a cancellation event commits, attempt the active assignment's next effect.",
            (
                "The worker observes the exact explicit cancellation event once before the next "
                "effect."
            ),
            "explicit_cancellation_event_committed",
            "Assignment identity, cancellation actor, reason, and event identity are durable.",
            "post_cancellation_effect_started",
            "A worker or tool effect starts after the cancellation event.",
            ("FAILED_EXPLICIT_CANCELLATION",),
            (("terminal_failed_records", 1), ("post_cancellation_effects", 0)),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_REGISTERED_TERMINAL,
            recovery_entrypoint=(
                "cancellation observer -> FAILED_EXPLICIT_CANCELLATION terminal transaction"
            ),
            terminal_failed_allowed=True,
        ),
        make(
            "assignment-retry-budget-exhaustion-001",
            "nonrecoverable_assignment_terminal_reason",
            "cumulative retry budget gate -> PostgreSQL terminal ledger",
            FaultInjectionMechanism.RETRY_BUDGET_EXHAUSTION,
            "After the last retry unit is durably consumed, request one additional attempt.",
            "The retry gate records exact aggregate budget exhaustion once before a new attempt.",
            "retry_budget_fully_consumed",
            "Cumulative attempts, tokens, cost, and remaining zero budget are durable.",
            "extra_retry_attempt_started",
            "An attempt starts after the preregistered aggregate budget reaches zero.",
            ("FAILED_RETRY_BUDGET_EXHAUSTED",),
            (("terminal_failed_records", 1), ("extra_retry_attempts", 0), ("budget_resets", 0)),
            recovery_class=FaultRecoveryClass.NON_RECOVERABLE_REGISTERED_TERMINAL,
            recovery_entrypoint=(
                "retry gate -> FAILED_RETRY_BUDGET_EXHAUSTED terminal transaction"
            ),
            terminal_failed_allowed=True,
        ),
        make(
            "assignment-outbox-kill-before-commit-001",
            "assignment_outbox_atomicity",
            "assignment/outbox PostgreSQL transaction pre-commit hook",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After transaction validation and immediately before assignment/outbox commit.",
            "The transaction process reaches the pre-commit hook once and receives SIGKILL.",
            "same_key_assignment_command_committed",
            "The source command, tenant scope, request hash, and idempotency key are durable.",
            "assignment_and_outbox_transaction_committed",
            "Assignment and its matching outbox row commit in one PostgreSQL transaction.",
            ("ASSIGNMENT_AND_OUTBOX_COMMITTED",),
            (
                ("assignment_rows", 1),
                ("durable_outbox_rows", 1),
                ("assignment_outbox_half_states", 0),
                ("idempotency_result_bindings", 1),
            ),
            signal="SIGKILL",
            recovery_entrypoint=(
                "restart -> same idempotency key lookup -> one atomic assignment/outbox commit"
            ),
            recovery_invariants=(
                "Immediately after termination, neither assignment nor outbox row is visible.",
                "Same-key recovery commits one matched assignment/outbox pair in one transaction.",
                "No assignment-only or outbox-only half-state is visible at any snapshot.",
            ),
            extra_oracle_requirements=(
                (
                    "Query both tables after SIGKILL and require zero rows for the transaction "
                    "identity."
                ),
                (
                    "After recovery require one pair sharing tenant, assignment, key, and "
                    "transaction IDs."
                ),
            ),
            extra_evidence_requirements=(
                (
                    "Hash-bind the pre-kill snapshot, SIGKILL, same-key replay, commit, and final "
                    "pair."
                ),
            ),
        ),
        make(
            "assignment-outbox-kill-after-commit-before-delivery-001",
            "assignment_outbox_atomicity",
            "assignment/outbox PostgreSQL commit -> Redis relay delivery",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After the atomic PG commit and immediately before the first Redis delivery call.",
            "The process reaches the post-commit/pre-delivery hook once and receives SIGKILL.",
            "assignment_and_outbox_transaction_committed",
            "One matched assignment/outbox pair and their transaction identity are durable.",
            "outbox_stream_delivery_started",
            "The relay begins Redis delivery for the committed outbox identity.",
            ("DONE",),
            (
                ("assignment_rows", 1),
                ("durable_outbox_rows", 1),
                ("stream_identities", 1),
                ("terminal_done_results", 1),
                ("duplicate_assignment_effects", 0),
            ),
            signal="SIGKILL",
            recovery_entrypoint="restart -> pending outbox relay -> worker -> unique DONE",
            recovery_invariants=(
                "The matched assignment/outbox pair remains durable after termination.",
                "Relay recovery publishes the existing outbox identity and reaches one DONE.",
                "No second assignment, outbox row, stream identity, or worker effect is created.",
            ),
            extra_oracle_requirements=(
                (
                    "Recompute pair atomicity and unique delivery/DONE from PostgreSQL and Redis "
                    "truth."
                ),
            ),
            extra_evidence_requirements=(
                "Hash-bind the PG transaction, SIGKILL, relay identity, result, and effect counts.",
            ),
        ),
        make(
            "instance-a-exit-instance-b-reclaim-001",
            "cross_instance_job_visibility",
            "instance A job create -> instance B query/SSE/reclaim/complete",
            FaultInjectionMechanism.PROCESS_SIGNAL,
            "After instance A durably creates the job and before it delivers or completes work.",
            "Source instance A reaches the post-create hook once and receives SIGKILL.",
            "instance_a_scoped_job_committed",
            "The job, tenant scope, source instance identity A, and delivery identity are durable.",
            "instance_a_job_delivery_or_completion_committed",
            "Source instance A delivers or completes the job before termination.",
            ("DONE",),
            (
                ("terminal_done_results", 1),
                ("visible_terminal_sse_events", 1),
                ("cross_scope_job_reads", 0),
            ),
            signal="SIGKILL",
            recovery_entrypoint=(
                "distinct instance B query and SSE read -> pending reclaim -> unique completion"
            ),
            recovery_invariants=(
                "Recovery instance B reads and reclaims the same durable tenant-scoped job.",
                (
                    "Source instance identity A and recovery instance identity B are present and "
                    "unequal."
                ),
                "Exactly one durable terminal result and one visible terminal transition exist.",
            ),
            identity_bindings=(
                _binding(
                    "source_recovery_instance_identity",
                    "action_a.source_instance_id",
                    "recovery.recovery_instance_id",
                    "different",
                ),
                _binding(
                    "cross_instance_job_identity",
                    "action_a.job_id",
                    "recovery.job_id",
                ),
                _binding(
                    "cross_instance_scope_identity",
                    "action_a.tenant_scope_id",
                    "recovery.tenant_scope_id",
                ),
            ),
            extra_oracle_requirements=(
                "Require source_instance_id != recovery_instance_id and identical job/scope IDs.",
            ),
            extra_evidence_requirements=(
                "Hash-bind both instance identities, query/SSE observations, reclaim, and result.",
            ),
        ),
    )


def _benign_controls() -> tuple[MatchedBenignControl, ...]:
    suite = "failure_isolation_matrix"
    specifications = (
        (
            "cross_tenant_read_write",
            "same-tenant-read-write-001",
            "team API -> pooled PostgreSQL connection -> RLS",
            "Issue one same-tenant read and one same-tenant write without an injection marker.",
            "SAME_TENANT_READ_WRITE_DONE",
            (("same_tenant_rows_read", 1, 1), ("same_tenant_rows_mutated", 1, 1)),
        ),
        (
            "mixed_revision_recovery",
            "complete-release-001",
            "release transaction -> revision pointer",
            "Complete one approved release without interruption.",
            "NEW_REVISION_VISIBLE",
            (("visible_revision_heads", 1, 1), ("new_revision_rows", 1, 1)),
        ),
        (
            "duplicate_observable_canonical_effect",
            "single-delivery-001",
            "PostgreSQL outbox -> Redis Streams -> consumer",
            "Deliver and acknowledge one outbox item exactly once.",
            "ACKNOWLEDGED",
            (("observable_canonical_effects", 1, 1), ("stream_acknowledgements", 1, 1)),
        ),
        (
            "durable_claim_without_unique_done",
            "assignment-completes-001",
            "Redis Streams worker -> PostgreSQL assignment ledger",
            "Claim, execute, persist verified result, and ACK one assignment normally.",
            "DONE",
            (("terminal_done_results", 1, 1), ("observable_canonical_effects", 1, 1)),
        ),
        (
            "projection_not_ready_serving_advance",
            "all-projections-ready-001",
            "projection worker -> pgvector -> serving-pointer gate",
            "Mark every required projection READY, then advance through the normal gate.",
            "SERVING_NEW_REVISION",
            (("serving_pointer_advances", 1, 1), ("visible_revision_heads", 1, 1)),
        ),
        (
            "concurrent_release_cas",
            "sequential-release-bases-001",
            "successive release transactions -> authoring-head CAS",
            "Commit two approved releases in current-base order without an injection marker.",
            "TWO_ORDERED_RELEASES_COMMITTED",
            (("canonical_successors", 2, 2), ("authoring_pointer_advances", 2, 2)),
        ),
        (
            "projection_job_without_unique_done",
            "projection-job-completes-001",
            "Redis projection worker -> PostgreSQL job ledger",
            "Claim, build, persist READY/DONE, and ACK one projection job normally.",
            "DONE",
            (("projection_results", 1, 1), ("stream_acknowledgements", 1, 1)),
        ),
        (
            "durable_agent_run_resume",
            "agent-tool-checkpoint-completes-001",
            "durable AgentRun tool and checkpoint path",
            "Dispatch one approved tool, persist evidence, and checkpoint normally.",
            "RUNNING",
            (("observable_tool_effects", 1, 1), ("checkpoint_references", 1, 1)),
        ),
        (
            "durable_agent_run_resume",
            "agent-hitl-resume-completes-001",
            "durable AgentRun HITL resume path",
            "Apply one valid human decision to one durable pause and resume normally.",
            "RUNNING",
            (("applied_human_decisions", 1, 1), ("run_state_continuations", 1, 1)),
        ),
        (
            "durable_agent_run_resume",
            "agent-reflection-retry-completes-001",
            "bounded Reflection retry path",
            "Persist one Reflection and enter one bounded retry without resetting budget.",
            "RUNNING",
            (("reflection_artifacts", 1, 1), ("next_attempts", 1, 1)),
        ),
        (
            "revoked_sse_event_leakage",
            "sse-authorized-stream-001",
            "authorized SSE stream",
            "Keep membership valid and deliver one in-scope event normally.",
            "CONNECTED",
            (("authorized_tenant_events", 1, 1),),
        ),
        (
            "service_role_cross_tenant_access",
            "relay-same-tenant-payload-001",
            "non-superuser relay service-role transaction",
            "Authorize and publish one same-tenant relay payload normally.",
            "DELIVERED",
            (("same_tenant_stream_events", 1, 1),),
        ),
        (
            "service_role_cross_tenant_access",
            "worker-same-tenant-payload-001",
            "non-superuser worker service-role transaction",
            "Authorize and persist one same-tenant worker result normally.",
            "DONE",
            (("same_tenant_worker_results", 1, 1),),
        ),
        (
            "publish_idempotency_replay_duplicate_effect",
            "distinct-idempotency-publishes-001",
            "publish API -> idempotency ledger -> canonical release transaction",
            "Publish two valid successive requests with distinct idempotency keys normally.",
            "TWO_DISTINCT_PUBLISHES_COMMITTED",
            (
                ("visible_authoring_revisions", 2, 2),
                ("durable_outbox_rows", 2, 2),
                ("durable_ledger_effects", 2, 2),
            ),
        ),
        (
            "reconciliation_revision_corruption",
            "reconciliation-clean-repaired-001",
            "durable source truth -> vector and community reconciliation workers",
            "Reconcile one clean and one already repaired same-revision projection normally.",
            "CURRENT_REVISION_PROJECTIONS_CLEAN",
            (
                ("duplicate_vector_rows", 0, 0),
                ("cross_revision_community_refs", 0, 0),
                ("current_revision_projection_sets", 2, 2),
            ),
        ),
        (
            "nonrecoverable_assignment_terminal_reason",
            "valid-assignment-terminal-done-001",
            "assignment worker -> verifier -> PostgreSQL terminal ledger",
            "Execute one valid authorized assignment with remaining retry budget normally.",
            "DONE",
            (("terminal_done_results", 1, 1), ("authorized_tool_effects", 1, 1)),
        ),
        (
            "cross_instance_job_visibility",
            "cross-instance-job-visible-001",
            "instance A job create -> instance B query/SSE/delivery/complete",
            "Create on healthy instance A and observe and complete on distinct instance B.",
            "DONE",
            (
                ("distinct_instance_identities", 2, 2),
                ("terminal_done_results", 1, 1),
                ("visible_terminal_sse_events", 1, 1),
            ),
        ),
        (
            "assignment_outbox_atomicity",
            "assignment-outbox-atomic-commit-001",
            "assignment/outbox PG transaction -> relay -> worker",
            "Commit one matched pair atomically, deliver once, and complete normally.",
            "DONE",
            (
                ("assignment_rows", 1, 1),
                ("durable_outbox_rows", 1, 1),
                ("assignment_outbox_half_states", 0, 0),
                ("stream_identities", 1, 1),
                ("terminal_done_results", 1, 1),
            ),
        ),
    )
    return tuple(
        MatchedBenignControl(
            catalog_id=f"{suite}.benign.{suffix}",
            hard_zero_gate_id=gate_id,
            hard_zero_case_id=f"{suite}.benign.{suffix}",
            production_target=_target(entrypoint),
            catalog_hit_count=1,
            is_fault_injection=False,
            normal_path_trigger=trigger,
            expected_terminal_state=terminal,
            normal_path_deadline=_fixed_deadline(10_000),
            expected_observable_effects=_benign_effects(*effects),
            oracle=_oracle(gate_id, benign=True),
            run_status=EvalStatus.NOT_RUN,
        )
        for gate_id, suffix, entrypoint, trigger, terminal, effects in specifications
    )


def _plan_coverage() -> tuple[FaultPlanCoverageEntry, ...]:
    suite = "failure_isolation_matrix"

    def attacks(*suffixes: str) -> tuple[str, ...]:
        return tuple(f"{suite}.attack.{suffix}" for suffix in suffixes)

    def benign(*suffixes: str) -> tuple[str, ...]:
        return tuple(f"{suite}.benign.{suffix}" for suffix in suffixes)

    return (
        FaultPlanCoverageEntry(
            plan_reference="§3.3",
            requirement=(
                "Two-tenant/two-instance isolation covers direct requests, service roles, and "
                "revoked live streams with matched usable paths."
            ),
            attack_cell_ids=attacks(
                "tenant-a-to-b-read-001",
                "tenant-a-to-b-write-001",
                "sse-revoked-membership-001",
                "relay-cross-tenant-payload-001",
                "worker-cross-tenant-payload-001",
            ),
            matched_benign_control_ids=benign(
                "same-tenant-read-write-001",
                "sse-authorized-stream-001",
                "relay-same-tenant-payload-001",
                "worker-same-tenant-payload-001",
            ),
        ),
        FaultPlanCoverageEntry(
            plan_reference="N2.2",
            requirement=(
                "Release pre/post-commit recovery, same-base authoring CAS, publish idempotency, "
                "and projection-ready serving gates have explicit physical cells."
            ),
            attack_cell_ids=attacks(
                "kill-release-before-commit-001",
                "kill-release-boundary-001",
                "same-base-concurrent-release-001",
                "projection-failure-001",
                "publish-idempotency-replay-001",
            ),
            matched_benign_control_ids=benign(
                "complete-release-001",
                "sequential-release-bases-001",
                "all-projections-ready-001",
                "distinct-idempotency-publishes-001",
            ),
        ),
        FaultPlanCoverageEntry(
            plan_reference="N2.3",
            requirement=(
                "Outbox, Redis outage, relay restart, redelivery, projection claim/result/ACK, "
                "projection fencing, reconciliation corruption, and cross-instance visibility "
                "boundaries are physical cells."
            ),
            attack_cell_ids=attacks(
                "redeliver-outbox-001",
                "redis-transient-outage-001",
                "relay-restart-before-delivery-001",
                "relay-restart-after-delivery-before-mark-001",
                "projection-worker-kill-after-claim-001",
                "projection-exit-before-ack-001",
                "projection-worker-kill-after-ack-001",
                "projection-stale-fencing-token-001",
                "vector-reconciliation-duplicate-001",
                "community-cross-revision-corruption-001",
                "instance-a-exit-instance-b-reclaim-001",
            ),
            matched_benign_control_ids=benign(
                "single-delivery-001",
                "projection-job-completes-001",
                "reconciliation-clean-repaired-001",
                "cross-instance-job-visible-001",
            ),
        ),
        FaultPlanCoverageEntry(
            plan_reference="N4.2",
            requirement=(
                "Durable AgentRun pre/post-tool, pre/post-checkpoint, HITL pause, and pre/post-"
                "Reflection kill points each have a process-signal cell."
            ),
            attack_cell_ids=attacks(
                "agent-kill-before-tool-001",
                "agent-kill-after-tool-001",
                "agent-kill-before-checkpoint-001",
                "agent-kill-after-checkpoint-001",
                "agent-kill-at-hitl-pause-001",
                "agent-kill-before-reflection-001",
                "agent-kill-after-reflection-001",
            ),
            matched_benign_control_ids=benign(
                "agent-tool-checkpoint-completes-001",
                "agent-hitl-resume-completes-001",
                "agent-reflection-retry-completes-001",
            ),
        ),
        FaultPlanCoverageEntry(
            plan_reference="N4.4",
            requirement=(
                "Assignment claim, evidence/result, persisted-result/pre-ACK, verifier restart, "
                "stale fencing, redelivery, and four precise non-recoverable terminal reasons "
                "each have a physical cell."
            ),
            attack_cell_ids=attacks(
                "worker-kill-after-claim-001",
                "worker-kill-before-result-001",
                "exit-before-ack-001",
                "verifier-restart-before-verdict-001",
                "assignment-stale-fencing-token-001",
                "assignment-redelivery-001",
                "assignment-bad-payload-001",
                "assignment-policy-rejection-001",
                "assignment-explicit-cancellation-001",
                "assignment-retry-budget-exhaustion-001",
                "assignment-outbox-kill-before-commit-001",
                "assignment-outbox-kill-after-commit-before-delivery-001",
            ),
            matched_benign_control_ids=benign(
                "assignment-completes-001",
                "valid-assignment-terminal-done-001",
                "assignment-outbox-atomic-commit-001",
            ),
        ),
    )


_FAULT_MANIFEST: Final = FrozenFaultManifest(
    schema_version=FAULT_MANIFEST_SCHEMA_VERSION,
    manifest_id="failure-isolation-matrix-faults-n0.3-v1",
    suite_id="failure_isolation_matrix",
    suite_version="n0.3-v1",
    implementation_status=EvalStatus.NOT_AVAILABLE,
    production_path_exercised=False,
    run_status=EvalStatus.NOT_RUN,
    status_reasons=(
        "TEAM_PROFILE_PRODUCTION_PATH_NOT_AVAILABLE",
        "REAL_PG_PGVECTOR_REDIS_TWO_TENANT_TWO_INSTANCE_TOPOLOGY_NOT_AVAILABLE",
        "PRODUCTION_FAULT_RUNNER_NOT_AVAILABLE",
    ),
    required_topology=RequiredProductionTopology(
        implementation_status=EvalStatus.NOT_AVAILABLE,
        production_path_exercised=False,
        future_production_path_required=True,
        postgresql="real_postgresql",
        vector_index="real_pgvector",
        delivery_queue="real_redis_streams",
        tenant_count=2,
        application_instance_count=2,
        topology_statement=(
            "FUTURE_PRODUCTION_PATH_REQUIRES_REAL_PG_PGVECTOR_REDIS_TWO_TENANTS_TWO_INSTANCES"
        ),
    ),
    execution_rules=FaultExecutionRules(
        rules=(
            (
                "Every attack requires injection_hit_count exactly one and the registered "
                "mechanism-specific observation."
            ),
            (
                "Zero or multiple hits make every attack NOT_RUN; natural exit invalidates only "
                "a process-fault cell that expected forced termination."
            ),
            "Action A must have durable evidence and be complete while action B is proven not run.",
            (
                "Recoverable infrastructure faults must reach the registered bounded state "
                "without businessless terminal FAILED."
            ),
            (
                "Only the preregistered bad-payload, policy-rejection, explicit-cancellation, "
                "and retry-budget-exhaustion reasons may terminate FAILED; each reason must be "
                "exact and unique."
            ),
            (
                "Matched benign controls use catalog_hit_count one and never carry an injection "
                "marker."
            ),
            (
                "No fixture or unexecuted catalog establishes production reliability or a "
                "quality result."
            ),
        ),
        synthetic_fixture_may_prove_production_recovery=False,
        fault_script_is_sealed_quality_data=False,
        uniform_fast_failure_is_valid_recovery=False,
    ),
    attack_cells=_attack_cells(),
    matched_benign_controls=_benign_controls(),
    plan_coverage=_plan_coverage(),
)


def get_frozen_fault_manifest() -> FrozenFaultManifest:
    return _FAULT_MANIFEST.model_copy(deep=True)


def render_fault_manifest() -> bytes:
    return _json_bytes(_FAULT_MANIFEST.model_dump(mode="json"))


def render_frozen_fault_files() -> dict[str, bytes]:
    return {FAULT_MANIFEST_RELATIVE_PATH: render_fault_manifest()}


def fault_manifest_reference() -> tuple[str, str, int]:
    content = render_fault_manifest()
    return FAULT_MANIFEST_ARTIFACT_PATH, hashlib.sha256(content).hexdigest(), len(content)


def validate_fault_manifest_against_hard_zero_gates(
    manifest: FrozenFaultManifest,
    gates: Sequence[Mapping[str, object]],
) -> None:
    attack_by_gate: dict[str, set[str]] = {}
    benign_by_gate: dict[str, set[str]] = {}
    for cell in manifest.attack_cells:
        attack_by_gate.setdefault(cell.hard_zero_gate_id, set()).add(cell.hard_zero_case_id)
    for control in manifest.matched_benign_controls:
        benign_by_gate.setdefault(control.hard_zero_gate_id, set()).add(control.hard_zero_case_id)
    registry_gate_ids: set[str] = set()
    for gate in gates:
        gate_id = gate.get("gate_id")
        attacks = gate.get("attack_case_ids")
        benign = gate.get("benign_control_case_ids")
        min_attacks = gate.get("minimum_attack_denominator")
        min_benign = gate.get("minimum_benign_denominator")
        if (
            not isinstance(gate_id, str)
            or not isinstance(attacks, Sequence)
            or not isinstance(benign, Sequence)
        ):
            raise ValueError("FAULT_HARD_ZERO_GATE_SHAPE_INVALID")
        attack_ids = tuple(item for item in attacks if isinstance(item, str))
        benign_ids = tuple(item for item in benign if isinstance(item, str))
        if len(attack_ids) != len(attacks) or len(benign_ids) != len(benign):
            raise ValueError("FAULT_HARD_ZERO_CASE_ID_SHAPE_INVALID")
        if not attack_ids or not benign_ids:
            raise ValueError("FAULT_HARD_ZERO_ZERO_DENOMINATOR")
        if min_attacks != len(attack_ids) or min_benign != len(benign_ids):
            raise ValueError("FAULT_HARD_ZERO_DENOMINATOR_NOT_EXACT")
        registry_gate_ids.add(gate_id)
        if attack_by_gate.get(gate_id) != set(attack_ids):
            raise ValueError(f"FAULT_ATTACK_CASE_SET_MISMATCH:{gate_id}")
        if benign_by_gate.get(gate_id) != set(benign_ids):
            raise ValueError(f"FAULT_BENIGN_CASE_SET_MISMATCH:{gate_id}")
    if registry_gate_ids != set(attack_by_gate) or registry_gate_ids != set(benign_by_gate):
        raise ValueError("FAULT_HARD_ZERO_GATE_SET_MISMATCH")


def validate_fault_manifest_against_hard_zero_catalog(
    manifest: FrozenFaultManifest, catalog: FrozenHardZeroCatalog
) -> None:
    if catalog.suite_id != manifest.suite_id:
        raise ValueError("FAULT_HARD_ZERO_CATALOG_SUITE_MISMATCH")
    expected_attacks = {case.case_id for case in catalog.cases if case.role is HardZeroRole.ATTACK}
    expected_benign = {
        case.case_id for case in catalog.cases if case.role is HardZeroRole.MATCHED_BENIGN
    }
    actual_attacks = {cell.hard_zero_case_id for cell in manifest.attack_cells}
    actual_benign = {control.hard_zero_case_id for control in manifest.matched_benign_controls}
    if actual_attacks != expected_attacks:
        raise ValueError("FAULT_ATTACK_CASES_DIFFER_FROM_PHYSICAL_HARD_ZERO_CATALOG")
    if actual_benign != expected_benign:
        raise ValueError("FAULT_BENIGN_CASES_DIFFER_FROM_PHYSICAL_HARD_ZERO_CATALOG")


def verify_frozen_fault_manifest(directory: str | Path) -> None:
    root = Path(directory)
    path = root / FAULT_MANIFEST_RELATIVE_PATH
    expected = render_fault_manifest()
    if not path.exists():
        raise FrozenFaultManifestMismatchError(f"{FAULT_MANIFEST_RELATIVE_PATH}: missing")
    actual = path.read_bytes()
    if actual != expected:
        raise FrozenFaultManifestMismatchError(f"{FAULT_MANIFEST_RELATIVE_PATH}: byte mismatch")
    FrozenFaultManifest.model_validate_json(actual)


__all__ = [
    "FAULT_MANIFEST_ARTIFACT_PATH",
    "FAULT_MANIFEST_RELATIVE_PATH",
    "FAULT_MANIFEST_SCHEMA_VERSION",
    "AttackFaultCell",
    "BenignObservableEffect",
    "BoundaryAction",
    "DeadlineParameter",
    "ExpectedProcessTermination",
    "FaultBoundary",
    "FaultExecutionRules",
    "FaultInjectionContract",
    "FaultInjectionMechanism",
    "FaultOracleContract",
    "FaultPlanCoverageEntry",
    "FaultRecoveryClass",
    "FrozenFaultManifest",
    "FrozenFaultManifestMismatchError",
    "FrozenRecoveryDeadline",
    "MatchedBenignControl",
    "ObservableEffectLimit",
    "ProductionFaultTarget",
    "RecoveryContract",
    "RecoveryIdentityBinding",
    "RequiredProductionTopology",
    "fault_manifest_reference",
    "get_frozen_fault_manifest",
    "render_fault_manifest",
    "render_frozen_fault_files",
    "validate_fault_manifest_against_hard_zero_catalog",
    "validate_fault_manifest_against_hard_zero_gates",
    "verify_frozen_fault_manifest",
]
