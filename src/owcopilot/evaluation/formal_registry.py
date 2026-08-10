"""Frozen registry for the four formal evaluation suites.

This module is governance, not an evaluation runner.  It preregisters metrics, baselines,
anti-degeneracy controls, hard-zero denominators, and dev/sealed separation.  Capabilities and
datasets that do not exist are represented as ``NOT_AVAILABLE`` or
``BLOCKED_EXTERNAL_DATA``; no synthetic fixture is allowed to stand in for a quality result.
"""

from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .formal_faults import (
    FAULT_MANIFEST_ARTIFACT_PATH,
    fault_manifest_reference,
    get_frozen_fault_manifest,
    validate_fault_manifest_against_hard_zero_catalog,
    validate_fault_manifest_against_hard_zero_gates,
)
from .formal_hard_zero import (
    get_frozen_hard_zero_catalogs,
    hard_zero_catalog_reference,
    validate_hard_zero_catalogs_against_gates,
)
from .protocol import (
    ArtifactReference,
    EvalProvider,
    EvalSplit,
    EvalStatus,
    FormalEvalManifest,
    MetricDefinition,
    MetricScope,
)

FORMAL_SUITE_CONTRACT_VERSION: Final[Literal["formal-suite-contract/v1"]] = (
    "formal-suite-contract/v1"
)
FORMAL_SUITE_VERSION: Final[Literal["n0.3-v1"]] = "n0.3-v1"
GLOBAL_REAL_MODEL_BUDGET_SCHEMA_VERSION: Final[Literal["formal-global-real-model-budget/v1"]] = (
    "formal-global-real-model-budget/v1"
)
GLOBAL_REAL_MODEL_BUDGET_ID: Final[Literal["n0.3-global-real-model-budget-v1"]] = (
    "n0.3-global-real-model-budget-v1"
)
GLOBAL_REAL_MODEL_BUDGET_ARTIFACT_PATH: Final[Literal["../../global_real_model_budget.json"]] = (
    "../../global_real_model_budget.json"
)


class SuiteId(str, Enum):
    GAME_PLANNER_VERTICAL_E2E = "game_planner_vertical_e2e"
    AGENT_TRUST_GOLD = "agent_trust_gold"
    FAILURE_ISOLATION_MATRIX = "failure_isolation_matrix"
    SEMANTIC_SHADOW_EVAL = "semantic_shadow_eval"


class EvaluationDimension(str, Enum):
    TRIAGE = "triage"
    GROUNDED_DRAFT = "grounded_draft"
    IMPACT = "impact"
    PLANNING = "planning"
    REFLECTION = "reflection"
    MEMORY = "memory"
    MULTI_AGENT = "multi_agent"
    TOOL_USE = "tool_use"
    RAG = "rag"
    PERSONA = "persona"
    SEMANTIC_REVIEW = "semantic_review"
    FAILURE_ISOLATION = "failure_isolation"


class ControlKind(str, Enum):
    MATCHED_GOOD = "matched_good"
    FROZEN_MUTANT = "frozen_mutant"


class ExperimentArmRole(str, Enum):
    CANDIDATE = "candidate"
    BASELINE = "baseline"
    CONTROL = "control"


class DatasetOrigin(str, Enum):
    AUTHORED_DEV = "authored_dev"
    INDEPENDENT_SEALED = "independent_sealed"
    RENAMED_DEV = "renamed_dev"


class BudgetAllocationId(str, Enum):
    TRIAGE_GROUNDED_DRAFT = "triage_grounded_draft"
    EVIDENCE_VERIFIER = "evidence_verifier"
    MEMORY = "memory"
    REFLECTION = "reflection"
    MULTI_AGENT = "multi_agent"
    MCTS = "mcts"


class _RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BudgetAllocation(_RegistryModel):
    allocation_id: BudgetAllocationId
    priority_rank: int | None = Field(default=None, ge=1)
    model_call_cap: int = Field(ge=0)
    token_cap: int = Field(ge=0)
    cost_usd_cap: float = Field(ge=0)


class BudgetReserve(_RegistryModel):
    model_call_cap: int = Field(ge=0)
    token_cap: int = Field(ge=0)
    cost_usd_cap: float = Field(ge=0)


class GlobalRealModelBudget(_RegistryModel):
    """One safety ceiling for every formal real-model experiment, not spend authority."""

    schema_version: Literal["formal-global-real-model-budget/v1"]
    budget_id: Literal["n0.3-global-real-model-budget-v1"]
    scope_id: Literal["n0.3_to_m5_formal_real_model_program"]
    currency: Literal["USD"]
    model_call_cap: int = Field(gt=0)
    token_cap: int = Field(gt=0)
    cost_usd_cap: float = Field(gt=0)
    charged_components: tuple[str, ...] = Field(min_length=1)
    token_definition: Literal["input_plus_output_across_all_charged_components"]
    failed_errored_and_retried_usage_counted: Literal[True]
    allocations: tuple[BudgetAllocation, ...] = Field(min_length=1)
    unallocated_reserve: BudgetReserve
    spend_authorized: Literal[False]
    authorization_note: Literal["CAP_IS_A_SAFETY_CEILING_NOT_SPEND_AUTHORIZATION"]

    @model_validator(mode="after")
    def validate_global_budget(self) -> GlobalRealModelBudget:
        required_components = {
            "planner",
            "orchestrator",
            "worker",
            "verifier",
            "reflection",
            "compression",
            "retry",
            "retriever",
            "generator",
            "semantic_judge",
        }
        if len(self.charged_components) != len(set(self.charged_components)):
            raise ValueError("GLOBAL_BUDGET_COMPONENTS_MUST_BE_UNIQUE")
        if set(self.charged_components) != required_components:
            raise ValueError("GLOBAL_BUDGET_MISSING_OR_UNKNOWN_CHARGED_COMPONENT")

        by_id = {allocation.allocation_id: allocation for allocation in self.allocations}
        if len(by_id) != len(self.allocations) or set(by_id) != set(BudgetAllocationId):
            raise ValueError("GLOBAL_BUDGET_ALLOCATION_COVERAGE_MISMATCH")
        expected_priorities = {
            BudgetAllocationId.TRIAGE_GROUNDED_DRAFT: 1,
            BudgetAllocationId.EVIDENCE_VERIFIER: 2,
            BudgetAllocationId.MEMORY: 3,
            BudgetAllocationId.REFLECTION: 4,
            BudgetAllocationId.MULTI_AGENT: 5,
            BudgetAllocationId.MCTS: None,
        }
        for allocation_id, priority in expected_priorities.items():
            allocation = by_id[allocation_id]
            if allocation.priority_rank != priority:
                raise ValueError("GLOBAL_BUDGET_PRIORITY_ORDER_MISMATCH")
            caps = (
                allocation.model_call_cap,
                allocation.token_cap,
                allocation.cost_usd_cap,
            )
            if allocation_id is BudgetAllocationId.MCTS:
                if caps != (0, 0, 0.0):
                    raise ValueError("MCTS_MUST_HAVE_ZERO_CORE_REAL_MODEL_BUDGET")
            elif any(cap <= 0 for cap in caps):
                raise ValueError("CORE_REAL_MODEL_ALLOCATION_MUST_BE_POSITIVE")

        allocated_calls = sum(item.model_call_cap for item in self.allocations)
        allocated_tokens = sum(item.token_cap for item in self.allocations)
        allocated_cost = sum(item.cost_usd_cap for item in self.allocations)
        if allocated_calls > self.model_call_cap:
            raise ValueError("GLOBAL_MODEL_CALL_BUDGET_OVERALLOCATED")
        if allocated_tokens > self.token_cap:
            raise ValueError("GLOBAL_TOKEN_BUDGET_OVERALLOCATED")
        if allocated_cost > self.cost_usd_cap:
            raise ValueError("GLOBAL_COST_BUDGET_OVERALLOCATED")
        if allocated_calls + self.unallocated_reserve.model_call_cap != self.model_call_cap:
            raise ValueError("GLOBAL_MODEL_CALL_RESERVE_MISMATCH")
        if allocated_tokens + self.unallocated_reserve.token_cap != self.token_cap:
            raise ValueError("GLOBAL_TOKEN_RESERVE_MISMATCH")
        if not math.isclose(
            allocated_cost + self.unallocated_reserve.cost_usd_cap,
            self.cost_usd_cap,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("GLOBAL_COST_RESERVE_MISMATCH")
        return self


class GlobalBudgetArtifactReference(_RegistryModel):
    budget_id: Literal["n0.3-global-real-model-budget-v1"]
    path: Literal["../../global_real_model_budget.json"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0)


class HardZeroCatalogArtifactReference(_RegistryModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0)


class FaultManifestArtifactReference(_RegistryModel):
    path: Literal["../../faults/failure_isolation_matrix/fault_manifest.json"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0)


class BaselineContract(_RegistryModel):
    baseline_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    paired_by_case: Literal[True]
    scorer_identical_between_arms: Literal[True]
    aggregate_budget_rule: Literal[
        "same_model_calls_and_input_plus_output_token_cap", "not_applicable_harness"
    ]
    frozen_before_sealed: Literal[True]


class ExperimentArmContract(_RegistryModel):
    arm_id: str = Field(min_length=1)
    role: ExperimentArmRole
    description: str = Field(min_length=1)


class PairedBudgetContract(_RegistryModel):
    allocation_id: BudgetAllocationId | None
    model_calls_allowed: bool
    charged_components: tuple[str, ...]
    same_model_id_between_arms: Literal[True]
    same_model_call_cap_between_arms: Literal[True]
    same_aggregate_input_output_token_cap_between_arms: Literal[True]
    same_cases_between_arms: Literal[True]
    same_scorer_between_arms: Literal[True]
    failed_errored_and_retried_usage_counted: Literal[True]
    actual_tokens_cost_and_latency_reported_per_arm: Literal[True]

    @model_validator(mode="after")
    def validate_model_accounting(self) -> PairedBudgetContract:
        if self.model_calls_allowed:
            if self.allocation_id is None or not self.charged_components:
                raise ValueError("MODEL_ARMS_REQUIRE_ALLOCATION_AND_CHARGED_COMPONENTS")
        elif self.allocation_id is not None or self.charged_components:
            raise ValueError("NO_MODEL_ARM_CANNOT_DECLARE_MODEL_ALLOCATION")
        if len(self.charged_components) != len(set(self.charged_components)):
            raise ValueError("ARM_CHARGED_COMPONENTS_MUST_BE_UNIQUE")
        return self


class ScorerContract(_RegistryModel):
    scorer_id: str = Field(min_length=1)
    scorer_version: str = Field(min_length=1)
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    sha256: None = None
    changes_retire_opened_sealed_version: Literal[True]
    byte_identical_trace_recalculation_only: Literal[True]


class ProviderEvidencePolicy(_RegistryModel):
    fixture_max_scope: Literal[MetricScope.HARNESS]
    scripted_max_scope: Literal[MetricScope.HARNESS]
    quality_provider: Literal[EvalProvider.REAL_MODEL]
    fixture_quality_effect_allowed: Literal[False]
    scripted_gold_f1_is_external_quality_metric: Literal[False]


class DatasetSlot(_RegistryModel):
    split: EvalSplit
    dataset_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    origin: DatasetOrigin
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: EvalStatus
    status_reasons: tuple[str, ...] = Field(min_length=1)


class DataFreezeContract(_RegistryModel):
    dev: DatasetSlot
    sealed: DatasetSlot
    physical_separation_required: Literal[True]
    dev_may_be_promoted_to_sealed: Literal[False]
    world_entity_plot_cluster_split_required: Literal[True]
    near_duplicate_check_required: Literal[True]
    sealed_outputs_unopened: Literal[True]

    @model_validator(mode="after")
    def reject_dev_renamed_as_sealed(self) -> DataFreezeContract:
        if self.dev.split is not EvalSplit.DEV or self.sealed.split is not EvalSplit.SEALED:
            raise ValueError("DATASET_SPLIT_LABEL_MISMATCH")
        if self.sealed.origin is not DatasetOrigin.INDEPENDENT_SEALED:
            raise ValueError("SEALED_CANNOT_BE_RENAMED_DEV")
        if self.dev.dataset_id == self.sealed.dataset_id:
            raise ValueError("DEV_AND_SEALED_DATASET_IDS_MUST_DIFFER")
        if Path(self.dev.relative_path).as_posix() == Path(self.sealed.relative_path).as_posix():
            raise ValueError("DEV_AND_SEALED_PATHS_MUST_DIFFER")
        if (
            self.dev.content_sha256 is not None
            and self.sealed.content_sha256 is not None
            and self.dev.content_sha256 == self.sealed.content_sha256
        ):
            raise ValueError("DEV_BYTES_CANNOT_BE_RELABELED_AS_SEALED")
        return self


class PositiveCoverageGate(_RegistryModel):
    gate_id: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    case_ids: tuple[str, ...] = Field(min_length=1)
    minimum_denominator: int = Field(ge=1)
    minimum_rate: float = Field(gt=0, le=1)
    unmet_status: Literal[EvalStatus.NOT_RUN]

    @model_validator(mode="after")
    def require_frozen_nonzero_denominator(self) -> PositiveCoverageGate:
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("POSITIVE_CONTROL_CASE_IDS_MUST_BE_UNIQUE")
        if len(self.case_ids) < self.minimum_denominator:
            raise ValueError("POSITIVE_CONTROL_DENOMINATOR_EXCEEDS_FROZEN_CASES")
        return self


class HardZeroGate(_RegistryModel):
    gate_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    violation_metric: str = Field(min_length=1)
    benign_false_reject_metric: str = Field(min_length=1)
    attack_case_ids: tuple[str, ...] = Field(min_length=1)
    benign_control_case_ids: tuple[str, ...] = Field(min_length=1)
    minimum_attack_denominator: int = Field(ge=1)
    minimum_benign_denominator: int = Field(ge=1)
    maximum_violations: Literal[0]
    maximum_benign_false_rejects: Literal[0]
    zero_denominator_status: Literal[EvalStatus.NOT_RUN]

    @model_validator(mode="after")
    def require_attack_and_benign_denominators(self) -> HardZeroGate:
        attacks = set(self.attack_case_ids)
        benign = set(self.benign_control_case_ids)
        if len(attacks) != len(self.attack_case_ids) or len(benign) != len(
            self.benign_control_case_ids
        ):
            raise ValueError("HARD_ZERO_CASE_IDS_MUST_BE_UNIQUE")
        if attacks & benign:
            raise ValueError("HARD_ZERO_ATTACK_AND_BENIGN_CASES_MUST_BE_DISJOINT")
        if len(attacks) < self.minimum_attack_denominator:
            raise ValueError("HARD_ZERO_ATTACK_DENOMINATOR_EXCEEDS_FROZEN_CASES")
        if len(benign) < self.minimum_benign_denominator:
            raise ValueError("HARD_ZERO_BENIGN_DENOMINATOR_EXCEEDS_FROZEN_CASES")
        return self


class ExternalDataBlocker(_RegistryModel):
    blocker_id: Literal[
        "REPRESENTATIVE_SEALED_CORPUS_UNAVAILABLE",
        "SECOND_ANNOTATOR_UNAVAILABLE",
        "DISAGREEMENT_ADJUDICATION_UNAVAILABLE",
    ]
    status: Literal[EvalStatus.BLOCKED_EXTERNAL_DATA]
    description: str = Field(min_length=1)


class FormalControl(_RegistryModel):
    control_id: str = Field(min_length=1)
    kind: ControlKind
    expected_harness_status: EvalStatus
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    production_path_required: Literal[True]

    @model_validator(mode="after")
    def validate_expected_result(self) -> FormalControl:
        expected = {
            ControlKind.MATCHED_GOOD: EvalStatus.PASS,
            ControlKind.FROZEN_MUTANT: EvalStatus.FAIL,
        }[self.kind]
        if self.expected_harness_status is not expected:
            raise ValueError("CONTROL_EXPECTED_STATUS_MISMATCH")
        return self


class ExperimentContract(_RegistryModel):
    experiment_id: str = Field(min_length=1)
    dimension: EvaluationDimension
    description: str = Field(min_length=1)
    status: EvalStatus
    status_reasons: tuple[str, ...] = Field(min_length=1)
    primary_metric: MetricDefinition
    diagnostic_metrics: tuple[MetricDefinition, ...]
    baseline: BaselineContract
    preregistered_arms: tuple[ExperimentArmContract, ...] = Field(min_length=2)
    paired_budget: PairedBudgetContract
    scorer: ScorerContract
    data_freeze: DataFreezeContract | None
    positive_coverage_gates: tuple[PositiveCoverageGate, ...] = Field(min_length=1)
    external_data_blockers: tuple[ExternalDataBlocker, ...] = ()

    @model_validator(mode="after")
    def validate_metric_and_blocker_contract(self) -> ExperimentContract:
        if not self.primary_metric.primary:
            raise ValueError("EXPERIMENT_PRIMARY_METRIC_MUST_BE_MARKED_PRIMARY")
        if any(metric.primary for metric in self.diagnostic_metrics):
            raise ValueError("EXPERIMENT_MUST_HAVE_EXACTLY_ONE_PRIMARY_METRIC")
        metric_names = [
            self.primary_metric.name,
            *(metric.name for metric in self.diagnostic_metrics),
        ]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("EXPERIMENT_METRIC_NAMES_MUST_BE_UNIQUE")
        for gate in self.positive_coverage_gates:
            if gate.metric_name not in metric_names:
                raise ValueError("POSITIVE_GATE_MUST_REFERENCE_REGISTERED_METRIC")

        arm_ids = [arm.arm_id for arm in self.preregistered_arms]
        if len(arm_ids) != len(set(arm_ids)):
            raise ValueError("EXPERIMENT_ARM_IDS_MUST_BE_UNIQUE")
        if sum(arm.role is ExperimentArmRole.CANDIDATE for arm in self.preregistered_arms) != 1:
            raise ValueError("EXPERIMENT_REQUIRES_EXACTLY_ONE_CANDIDATE_ARM")
        baseline_arms = [
            arm for arm in self.preregistered_arms if arm.role is ExperimentArmRole.BASELINE
        ]
        if len(baseline_arms) != 1 or baseline_arms[0].arm_id != self.baseline.baseline_id:
            raise ValueError("EXPERIMENT_BASELINE_ARM_MUST_MATCH_BASELINE_CONTRACT")
        if self.dimension is EvaluationDimension.REFLECTION:
            expected = {
                "single_run_no_retry_v1",
                "ordinary_retry_same_feedback_v1",
                "reflection_retry_v1",
            }
            if set(arm_ids) != expected:
                raise ValueError("REFLECTION_REQUIRES_FROZEN_THREE_ARM_COMPARISON")
        if self.dimension is EvaluationDimension.RAG:
            expected = {
                "bm25_only_v1",
                "vector_only_v1",
                "graph_expand_only_v1",
                "bm25_vector_graph_fusion_v1",
            }
            if set(arm_ids) != expected:
                raise ValueError("RAG_REQUIRES_FROZEN_BM25_VECTOR_GRAPH_FUSION_ARMS")

        subjective = {
            EvaluationDimension.GROUNDED_DRAFT,
            EvaluationDimension.PERSONA,
            EvaluationDimension.SEMANTIC_REVIEW,
        }
        required_blockers = {
            "REPRESENTATIVE_SEALED_CORPUS_UNAVAILABLE",
            "SECOND_ANNOTATOR_UNAVAILABLE",
            "DISAGREEMENT_ADJUDICATION_UNAVAILABLE",
        }
        actual_blockers = {blocker.blocker_id for blocker in self.external_data_blockers}
        if self.dimension is EvaluationDimension.FAILURE_ISOLATION:
            if self.data_freeze is not None:
                raise ValueError("FAILURE_MATRIX_CANNOT_BE_MODELED_AS_DEV_SEALED_DATASET")
            if self.paired_budget.model_calls_allowed:
                raise ValueError("FAILURE_MATRIX_CANNOT_ENABLE_MODEL_CALLS")
        elif self.data_freeze is None:
            raise ValueError("NON_FAILURE_EXPERIMENT_REQUIRES_DATA_FREEZE_CONTRACT")
        elif not self.paired_budget.model_calls_allowed:
            raise ValueError("QUALITY_EXPERIMENT_REQUIRES_MODEL_BUDGET_ACCOUNTING")
        if self.dimension in subjective:
            if self.status is not EvalStatus.BLOCKED_EXTERNAL_DATA:
                raise ValueError("SUBJECTIVE_SEALED_QUALITY_MUST_BE_BLOCKED_EXTERNAL_DATA")
            if actual_blockers != required_blockers:
                raise ValueError("MISSING_EXTERNAL_BLOCKERS_FOR_SUBJECTIVE_SEALED_QUALITY")
            if (
                self.data_freeze is None
                or self.data_freeze.sealed.status is not EvalStatus.BLOCKED_EXTERNAL_DATA
            ):
                raise ValueError("SUBJECTIVE_SEALED_DATASET_MUST_BE_BLOCKED_EXTERNAL_DATA")
        elif actual_blockers:
            raise ValueError("EXTERNAL_BLOCKERS_ONLY_APPLY_TO_SUBJECTIVE_SEALED_QUALITY")
        return self


_REQUIRED_ANTI_DEGENERACY_GATES: dict[EvaluationDimension, str] = {
    EvaluationDimension.TRIAGE: "triage_non_abstain_coverage",
    EvaluationDimension.GROUNDED_DRAFT: "grounded_draft_non_refusal_coverage",
    EvaluationDimension.MEMORY: "memory_delivered_positive_coverage",
    EvaluationDimension.MULTI_AGENT: "multi_agent_fanout_join_coverage",
    EvaluationDimension.RAG: "rag_answerable_coverage",
    EvaluationDimension.PERSONA: "persona_non_refusal_coverage",
    EvaluationDimension.SEMANTIC_REVIEW: "semantic_candidate_recall_coverage",
}


class FormalSuiteContract(_RegistryModel):
    schema_version: Literal["formal-suite-contract/v1"]
    suite_id: SuiteId
    suite_version: Literal["n0.3-v1"]
    description: str = Field(min_length=1)
    manifest_status: Literal[EvalStatus.NOT_AVAILABLE]
    manifest_status_reasons: tuple[str, ...] = Field(min_length=1)
    allowed_providers: tuple[EvalProvider, ...] = Field(min_length=1)
    provider_evidence_policy: ProviderEvidencePolicy
    global_real_model_budget: GlobalBudgetArtifactReference
    hard_zero_catalog: HardZeroCatalogArtifactReference
    fault_manifest: FaultManifestArtifactReference | None = None
    suite_primary_metric: str = Field(min_length=1)
    experiments: tuple[ExperimentContract, ...] = Field(min_length=1)
    controls: tuple[FormalControl, ...] = Field(min_length=2)
    hard_zero_gates: tuple[HardZeroGate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suite_contract(self) -> FormalSuiteContract:
        dimensions = [experiment.dimension for experiment in self.experiments]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("SUITE_EXPERIMENT_DIMENSIONS_MUST_BE_UNIQUE")
        metric_names: list[str] = []
        for experiment in self.experiments:
            metric_names.append(experiment.primary_metric.name)
            metric_names.extend(metric.name for metric in experiment.diagnostic_metrics)
            required_gate = _REQUIRED_ANTI_DEGENERACY_GATES.get(experiment.dimension)
            actual_gates = {gate.gate_id for gate in experiment.positive_coverage_gates}
            if required_gate is not None and required_gate not in actual_gates:
                raise ValueError(f"MISSING_ANTI_DEGENERACY_GATE:{required_gate}")
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("SUITE_METRIC_NAMES_MUST_BE_UNIQUE")
        if self.suite_primary_metric not in {
            experiment.primary_metric.name for experiment in self.experiments
        }:
            raise ValueError("SUITE_PRIMARY_MUST_REFERENCE_EXPERIMENT_PRIMARY")

        kinds = {control.kind for control in self.controls}
        if kinds != {ControlKind.MATCHED_GOOD, ControlKind.FROZEN_MUTANT}:
            raise ValueError("SUITE_REQUIRES_MATCHED_GOOD_AND_FROZEN_MUTANT")
        control_ids = [control.control_id for control in self.controls]
        if len(control_ids) != len(set(control_ids)):
            raise ValueError("SUITE_CONTROL_IDS_MUST_BE_UNIQUE")
        hard_zero_ids = [gate.gate_id for gate in self.hard_zero_gates]
        if len(hard_zero_ids) != len(set(hard_zero_ids)):
            raise ValueError("SUITE_HARD_ZERO_GATE_IDS_MUST_BE_UNIQUE")
        expected_hard_zero = HardZeroCatalogArtifactReference(
            path=hard_zero_catalog_reference(self.suite_id.value)[0],
            sha256=hard_zero_catalog_reference(self.suite_id.value)[1],
            byte_count=hard_zero_catalog_reference(self.suite_id.value)[2],
        )
        if self.hard_zero_catalog != expected_hard_zero:
            raise ValueError("SUITE_HARD_ZERO_CATALOG_REFERENCE_MISMATCH")
        is_failure = self.suite_id is SuiteId.FAILURE_ISOLATION_MATRIX
        if is_failure:
            _path, fault_sha256, fault_byte_count = fault_manifest_reference()
            expected_fault = FaultManifestArtifactReference(
                path=FAULT_MANIFEST_ARTIFACT_PATH,
                sha256=fault_sha256,
                byte_count=fault_byte_count,
            )
            if self.fault_manifest != expected_fault:
                raise ValueError("FAILURE_SUITE_FAULT_MANIFEST_REFERENCE_MISMATCH")
            if any(experiment.data_freeze is not None for experiment in self.experiments):
                raise ValueError("FAILURE_SUITE_CANNOT_HAVE_NARRATIVE_DATA_FREEZE")
        elif self.fault_manifest is not None:
            raise ValueError("NON_FAILURE_SUITE_CANNOT_REFERENCE_FAULT_MANIFEST")
        return self


class FrozenSuiteMismatchError(ValueError):
    """Raised when committed suite artifacts differ from the Python registry."""


class SuiteRegistryError(ValueError):
    """Raised when a manifest violates its preregistered suite contract."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _metric(
    name: str,
    description: str,
    unit: str = "ratio",
    *,
    scope: MetricScope = MetricScope.QUALITY,
    primary: bool = False,
) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        scope=scope,
        description=description,
        unit=unit,
        primary=primary,
    )


def _coverage(
    gate_id: str,
    metric_name: str,
    case_ids: tuple[str, ...],
    *,
    minimum_rate: float,
) -> PositiveCoverageGate:
    return PositiveCoverageGate(
        gate_id=gate_id,
        metric_name=metric_name,
        case_ids=case_ids,
        minimum_denominator=len(case_ids),
        minimum_rate=minimum_rate,
        unmet_status=EvalStatus.NOT_RUN,
    )


def _baseline(baseline_id: str, description: str, *, harness: bool = False) -> BaselineContract:
    return BaselineContract(
        baseline_id=baseline_id,
        description=description,
        paired_by_case=True,
        scorer_identical_between_arms=True,
        aggregate_budget_rule=(
            "not_applicable_harness"
            if harness
            else "same_model_calls_and_input_plus_output_token_cap"
        ),
        frozen_before_sealed=True,
    )


def _data_freeze(
    suite_id: SuiteId, dimension: EvaluationDimension, *, subjective: bool = False
) -> DataFreezeContract:
    stem = f"{suite_id.value}.{dimension.value}.v1"
    sealed_status = EvalStatus.BLOCKED_EXTERNAL_DATA if subjective else EvalStatus.NOT_AVAILABLE
    sealed_reason = (
        "REPRESENTATIVE_SEALED_CORPUS_AND_SECOND_ANNOTATOR_UNAVAILABLE"
        if subjective
        else "SEALED_DATASET_NOT_YET_FROZEN"
    )
    return DataFreezeContract(
        dev=DatasetSlot(
            split=EvalSplit.DEV,
            dataset_id=f"{stem}.dev",
            relative_path=f"benchmarks/formal/data/{suite_id.value}/{dimension.value}/dev",
            origin=DatasetOrigin.AUTHORED_DEV,
            status=EvalStatus.NOT_AVAILABLE,
            status_reasons=("DEV_DATASET_NOT_YET_FROZEN",),
        ),
        sealed=DatasetSlot(
            split=EvalSplit.SEALED,
            dataset_id=f"{stem}.sealed",
            relative_path=f"benchmarks/formal/data/{suite_id.value}/{dimension.value}/sealed",
            origin=DatasetOrigin.INDEPENDENT_SEALED,
            status=sealed_status,
            status_reasons=(sealed_reason,),
        ),
        physical_separation_required=True,
        dev_may_be_promoted_to_sealed=False,
        world_entity_plot_cluster_split_required=True,
        near_duplicate_check_required=True,
        sealed_outputs_unopened=True,
    )


def _external_blockers() -> tuple[ExternalDataBlocker, ...]:
    return (
        ExternalDataBlocker(
            blocker_id="REPRESENTATIVE_SEALED_CORPUS_UNAVAILABLE",
            status=EvalStatus.BLOCKED_EXTERNAL_DATA,
            description="No representative independently held sealed narrative corpus is present.",
        ),
        ExternalDataBlocker(
            blocker_id="SECOND_ANNOTATOR_UNAVAILABLE",
            status=EvalStatus.BLOCKED_EXTERNAL_DATA,
            description="No independent second human annotator has been secured.",
        ),
        ExternalDataBlocker(
            blocker_id="DISAGREEMENT_ADJUDICATION_UNAVAILABLE",
            status=EvalStatus.BLOCKED_EXTERNAL_DATA,
            description=(
                "No independent disagreement adjudicator or completed protocol is available."
            ),
        ),
    )


def _arm(arm_id: str, role: ExperimentArmRole, description: str) -> ExperimentArmContract:
    return ExperimentArmContract(arm_id=arm_id, role=role, description=description)


_DIMENSION_BUDGETS: Final[dict[EvaluationDimension, tuple[BudgetAllocationId, tuple[str, ...]]]] = {
    EvaluationDimension.TRIAGE: (BudgetAllocationId.TRIAGE_GROUNDED_DRAFT, ("generator",)),
    EvaluationDimension.GROUNDED_DRAFT: (
        BudgetAllocationId.TRIAGE_GROUNDED_DRAFT,
        ("retriever", "generator", "verifier"),
    ),
    EvaluationDimension.IMPACT: (BudgetAllocationId.EVIDENCE_VERIFIER, ("retriever",)),
    EvaluationDimension.PLANNING: (
        BudgetAllocationId.EVIDENCE_VERIFIER,
        ("planner", "worker", "verifier"),
    ),
    EvaluationDimension.REFLECTION: (
        BudgetAllocationId.REFLECTION,
        ("worker", "verifier", "reflection", "compression", "retry"),
    ),
    EvaluationDimension.MEMORY: (
        BudgetAllocationId.MEMORY,
        ("retriever", "worker", "verifier"),
    ),
    EvaluationDimension.MULTI_AGENT: (
        BudgetAllocationId.MULTI_AGENT,
        ("orchestrator", "worker", "verifier", "compression", "retry"),
    ),
    EvaluationDimension.TOOL_USE: (
        BudgetAllocationId.EVIDENCE_VERIFIER,
        ("worker", "verifier"),
    ),
    EvaluationDimension.RAG: (
        BudgetAllocationId.EVIDENCE_VERIFIER,
        ("retriever", "generator", "verifier"),
    ),
    EvaluationDimension.PERSONA: (
        BudgetAllocationId.EVIDENCE_VERIFIER,
        ("generator", "verifier"),
    ),
    EvaluationDimension.SEMANTIC_REVIEW: (
        BudgetAllocationId.EVIDENCE_VERIFIER,
        ("retriever", "semantic_judge"),
    ),
}


def _paired_budget(dimension: EvaluationDimension) -> PairedBudgetContract:
    allocation = _DIMENSION_BUDGETS.get(dimension)
    return PairedBudgetContract(
        allocation_id=None if allocation is None else allocation[0],
        model_calls_allowed=allocation is not None,
        charged_components=() if allocation is None else allocation[1],
        same_model_id_between_arms=True,
        same_model_call_cap_between_arms=True,
        same_aggregate_input_output_token_cap_between_arms=True,
        same_cases_between_arms=True,
        same_scorer_between_arms=True,
        failed_errored_and_retried_usage_counted=True,
        actual_tokens_cost_and_latency_reported_per_arm=True,
    )


def _default_arms(
    dimension: EvaluationDimension, baseline: BaselineContract
) -> tuple[ExperimentArmContract, ...]:
    return (
        _arm(
            f"{dimension.value}_candidate_v1",
            ExperimentArmRole.CANDIDATE,
            f"Preregistered {dimension.value} candidate on the production path.",
        ),
        _arm(baseline.baseline_id, ExperimentArmRole.BASELINE, baseline.description),
    )


def _experiment(
    *,
    suite_id: SuiteId,
    dimension: EvaluationDimension,
    description: str,
    primary: MetricDefinition,
    diagnostics: tuple[MetricDefinition, ...],
    baseline: BaselineContract,
    coverage: tuple[PositiveCoverageGate, ...],
    subjective: bool = False,
    arms: tuple[ExperimentArmContract, ...] | None = None,
) -> ExperimentContract:
    return ExperimentContract(
        experiment_id=f"{suite_id.value}.{dimension.value}.v1",
        dimension=dimension,
        description=description,
        status=(EvalStatus.BLOCKED_EXTERNAL_DATA if subjective else EvalStatus.NOT_AVAILABLE),
        status_reasons=(
            (
                "REPRESENTATIVE_SEALED_CORPUS_SECOND_ANNOTATOR_AND_ADJUDICATION_UNAVAILABLE"
                if subjective
                else "FORMAL_PRODUCTION_PATH_OR_DATASET_NOT_AVAILABLE"
            ),
        ),
        primary_metric=primary,
        diagnostic_metrics=diagnostics,
        baseline=baseline,
        preregistered_arms=arms or _default_arms(dimension, baseline),
        paired_budget=_paired_budget(dimension),
        scorer=ScorerContract(
            scorer_id=f"{suite_id.value}.{dimension.value}",
            scorer_version="preregistered-v1",
            implementation_status=EvalStatus.NOT_AVAILABLE,
            changes_retire_opened_sealed_version=True,
            byte_identical_trace_recalculation_only=True,
        ),
        data_freeze=(
            None
            if dimension is EvaluationDimension.FAILURE_ISOLATION
            else _data_freeze(suite_id, dimension, subjective=subjective)
        ),
        positive_coverage_gates=coverage,
        external_data_blockers=_external_blockers() if subjective else (),
    )


def _hard_zero(
    suite_id: SuiteId,
    gate_id: str,
    description: str,
    attack_case_ids: tuple[str, ...],
    benign_case_ids: tuple[str, ...],
) -> HardZeroGate:
    return HardZeroGate(
        gate_id=gate_id,
        description=description,
        violation_metric=f"{gate_id}_violation_count",
        benign_false_reject_metric=f"{gate_id}_benign_false_reject_count",
        attack_case_ids=tuple(f"{suite_id.value}.{case_id}" for case_id in attack_case_ids),
        benign_control_case_ids=tuple(f"{suite_id.value}.{case_id}" for case_id in benign_case_ids),
        minimum_attack_denominator=len(attack_case_ids),
        minimum_benign_denominator=len(benign_case_ids),
        maximum_violations=0,
        maximum_benign_false_rejects=0,
        zero_denominator_status=EvalStatus.NOT_RUN,
    )


def _controls(suite_id: SuiteId) -> tuple[FormalControl, ...]:
    return (
        FormalControl(
            control_id=f"{suite_id.value}.matched_good.v1",
            kind=ControlKind.MATCHED_GOOD,
            expected_harness_status=EvalStatus.PASS,
            implementation_status=EvalStatus.NOT_AVAILABLE,
            production_path_required=True,
        ),
        FormalControl(
            control_id=f"{suite_id.value}.frozen_mutant.v1",
            kind=ControlKind.FROZEN_MUTANT,
            expected_harness_status=EvalStatus.FAIL,
            implementation_status=EvalStatus.NOT_AVAILABLE,
            production_path_required=True,
        ),
    )


_PROVIDER_POLICY = ProviderEvidencePolicy(
    fixture_max_scope=MetricScope.HARNESS,
    scripted_max_scope=MetricScope.HARNESS,
    quality_provider=EvalProvider.REAL_MODEL,
    fixture_quality_effect_allowed=False,
    scripted_gold_f1_is_external_quality_metric=False,
)

_GLOBAL_REAL_MODEL_BUDGET = GlobalRealModelBudget(
    schema_version=GLOBAL_REAL_MODEL_BUDGET_SCHEMA_VERSION,
    budget_id=GLOBAL_REAL_MODEL_BUDGET_ID,
    scope_id="n0.3_to_m5_formal_real_model_program",
    currency="USD",
    model_call_cap=600,
    token_cap=6_000_000,
    cost_usd_cap=60.0,
    charged_components=(
        "planner",
        "orchestrator",
        "worker",
        "verifier",
        "reflection",
        "compression",
        "retry",
        "retriever",
        "generator",
        "semantic_judge",
    ),
    token_definition="input_plus_output_across_all_charged_components",
    failed_errored_and_retried_usage_counted=True,
    allocations=(
        BudgetAllocation(
            allocation_id=BudgetAllocationId.TRIAGE_GROUNDED_DRAFT,
            priority_rank=1,
            model_call_cap=160,
            token_cap=1_600_000,
            cost_usd_cap=16.0,
        ),
        BudgetAllocation(
            allocation_id=BudgetAllocationId.EVIDENCE_VERIFIER,
            priority_rank=2,
            model_call_cap=120,
            token_cap=1_200_000,
            cost_usd_cap=12.0,
        ),
        BudgetAllocation(
            allocation_id=BudgetAllocationId.MEMORY,
            priority_rank=3,
            model_call_cap=80,
            token_cap=800_000,
            cost_usd_cap=8.0,
        ),
        BudgetAllocation(
            allocation_id=BudgetAllocationId.REFLECTION,
            priority_rank=4,
            model_call_cap=80,
            token_cap=800_000,
            cost_usd_cap=8.0,
        ),
        BudgetAllocation(
            allocation_id=BudgetAllocationId.MULTI_AGENT,
            priority_rank=5,
            model_call_cap=100,
            token_cap=1_000_000,
            cost_usd_cap=10.0,
        ),
        BudgetAllocation(
            allocation_id=BudgetAllocationId.MCTS,
            priority_rank=None,
            model_call_cap=0,
            token_cap=0,
            cost_usd_cap=0.0,
        ),
    ),
    unallocated_reserve=BudgetReserve(
        model_call_cap=60,
        token_cap=600_000,
        cost_usd_cap=6.0,
    ),
    spend_authorized=False,
    authorization_note="CAP_IS_A_SAFETY_CEILING_NOT_SPEND_AUTHORIZATION",
)


def render_global_real_model_budget() -> bytes:
    """Render the sole aggregate real-model safety ceiling for all formal suites."""

    return _json_bytes(_GLOBAL_REAL_MODEL_BUDGET.model_dump(mode="json"))


def _global_budget_reference() -> GlobalBudgetArtifactReference:
    content = render_global_real_model_budget()
    return GlobalBudgetArtifactReference(
        budget_id=GLOBAL_REAL_MODEL_BUDGET_ID,
        path=GLOBAL_REAL_MODEL_BUDGET_ARTIFACT_PATH,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )


def _hard_zero_catalog_reference(suite_id: SuiteId) -> HardZeroCatalogArtifactReference:
    path, sha256, byte_count = hard_zero_catalog_reference(suite_id.value)
    return HardZeroCatalogArtifactReference(path=path, sha256=sha256, byte_count=byte_count)


def _fault_manifest_reference() -> FaultManifestArtifactReference:
    _path, sha256, byte_count = fault_manifest_reference()
    return FaultManifestArtifactReference(
        path=FAULT_MANIFEST_ARTIFACT_PATH,
        sha256=sha256,
        byte_count=byte_count,
    )


def _game_planner_contract() -> FormalSuiteContract:
    suite = SuiteId.GAME_PLANNER_VERTICAL_E2E
    triage = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.TRIAGE,
        description="Classify canon candidate, reference, mixed, and unknown with abstention.",
        primary=_metric(
            "triage_content_role_macro_f1",
            "Macro-F1 across every preregistered content_role; no role may be omitted.",
            primary=True,
        ),
        diagnostics=(
            _metric("triage_selective_accuracy", "Accuracy on non-abstained segments."),
            _metric("triage_coverage", "Fraction of eligible segments not abstained."),
            _metric("triage_risk_coverage_auc", "Area under the selective risk-coverage curve."),
            _metric(
                "triage_expected_calibration_error", "Frozen-bin confidence calibration error."
            ),
            _metric("triage_locator_fidelity", "Exact source-locator match rate."),
        ),
        baseline=_baseline(
            "triage_rules_abstain_v1",
            "Frozen deterministic parser/rules baseline with the same abstention labels.",
        ),
        coverage=(
            _coverage(
                "triage_non_abstain_coverage",
                "triage_coverage",
                (
                    "triage.canon_candidate.positive-001",
                    "triage.reference.positive-001",
                    "triage.mixed.positive-001",
                    "triage.unknown.positive-001",
                ),
                minimum_rate=0.5,
            ),
        ),
    )
    grounded = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.GROUNDED_DRAFT,
        description="Compare grounded narrative drafting under blinded human review.",
        primary=_metric(
            "grounded_draft_human_accept_rate",
            "Blinded human accept rate; revise and reject remain in the denominator.",
            primary=True,
        ),
        diagnostics=(
            _metric("grounded_draft_structural_validity_rate", "Schema-valid draft rate."),
            _metric("grounded_draft_canon_conflict_rate", "Per-draft canonical conflict rate."),
            _metric(
                "grounded_draft_leakage_rate", "Per-draft unauthorized inspiration leakage rate."
            ),
            _metric("grounded_draft_human_revise_rate", "Blinded human revise rate."),
            _metric("grounded_draft_human_reject_rate", "Blinded human reject rate."),
            _metric(
                "grounded_draft_relative_modification_burden",
                "Paired human edit burden relative to the frozen baseline.",
                "normalized_edit_burden",
            ),
            _metric("grounded_draft_output_coverage", "Fraction producing a reviewable draft."),
        ),
        baseline=_baseline(
            "single_pass_grounded_draft_v1",
            "Single-pass grounded draft without optimizer/reflection, under equal "
            "aggregate budget.",
        ),
        coverage=(
            _coverage(
                "grounded_draft_non_refusal_coverage",
                "grounded_draft_output_coverage",
                ("draft.positive-001", "draft.positive-002"),
                minimum_rate=0.5,
            ),
        ),
        subjective=True,
    )
    impact = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.IMPACT,
        description="Locate expected impacted references after an existing object changes.",
        primary=_metric(
            "impact_expected_refs_f1",
            "F1 over preregistered expected impacted reference identities.",
            primary=True,
        ),
        diagnostics=(
            _metric("impact_expected_refs_precision", "Precision over expected impacted refs."),
            _metric("impact_expected_refs_recall", "Recall over expected impacted refs."),
            _metric(
                "impact_query_result_coverage", "Fraction of changed objects yielding candidates."
            ),
        ),
        baseline=_baseline(
            "direct_reference_index_v1",
            "Frozen direct-reference-only impact lookup on the same changed objects.",
        ),
        coverage=(
            _coverage(
                "impact_result_coverage",
                "impact_query_result_coverage",
                ("impact.positive-001", "impact.positive-002"),
                minimum_rate=0.5,
            ),
        ),
    )
    rag = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.RAG,
        description="Compare BM25, vector, and graph retrieval on answerable grounded QA.",
        primary=_metric(
            "rag_answerable_question_success_rate",
            "Cited correct-answer rate over all preregistered answerable questions.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "rag_answerable_coverage", "Fraction of answerable questions receiving an answer."
            ),
            _metric("rag_citation_fidelity", "Exact supporting-locator citation rate."),
            _metric(
                "rag_unanswerable_abstain_rate", "Correct abstention rate on unanswerable cases."
            ),
            _metric("rag_retrieval_recall_at_k", "Gold evidence recall at frozen K."),
        ),
        baseline=_baseline(
            "bm25_only_v1",
            "BM25-only retrieval using the same generation model, cases, scorer, and budget caps.",
        ),
        coverage=(
            _coverage(
                "rag_answerable_coverage",
                "rag_answerable_coverage",
                ("rag.answerable.positive-001", "rag.answerable.positive-002"),
                minimum_rate=1.0,
            ),
        ),
        arms=(
            _arm(
                "bm25_only_v1",
                ExperimentArmRole.BASELINE,
                "BM25-only retrieval with the frozen generator, cases, scorer, and budget.",
            ),
            _arm(
                "vector_only_v1",
                ExperimentArmRole.CONTROL,
                "Vector-only retrieval with the frozen generator, cases, scorer, and budget.",
            ),
            _arm(
                "graph_expand_only_v1",
                ExperimentArmRole.CONTROL,
                "Graph-expansion-only retrieval with the same scorer and paired budget.",
            ),
            _arm(
                "bm25_vector_graph_fusion_v1",
                ExperimentArmRole.CANDIDATE,
                "Frozen BM25, vector, and graph fusion candidate under the paired budget.",
            ),
        ),
    )
    hard_zero = (
        _hard_zero(
            suite,
            "third_party_provenance_promotion",
            "Third-party material cannot become own/human/approved; legitimate own input "
            "remains usable.",
            ("attack.third-party-promotion-001",),
            ("benign.human-own-intake-001",),
        ),
        _hard_zero(
            suite,
            "reference_used_as_canonical_fact",
            "Reference material cannot support canonical QA; canonical evidence remains "
            "answerable.",
            ("attack.reference-as-canon-001",),
            ("benign.canonical-answer-001",),
        ),
        _hard_zero(
            suite,
            "unapproved_release_visibility",
            (
                "An unapproved ChangeSet violates the gate if it creates a visible canonical "
                "revision, advances authoring, OR advances serving; an approved revision must "
                "create exactly one canonical successor, advance authoring once, reach READY for "
                "every required projection, and advance serving exactly once."
            ),
            ("attack.unapproved-serving-001",),
            ("benign.approved-serving-001",),
        ),
        _hard_zero(
            suite,
            "unchecked_leakage_acceptance",
            "Drafts with leakage not_checked cannot be accepted; checked clean drafts "
            "remain usable.",
            ("attack.leakage-not-checked-001",),
            ("benign.leakage-checked-clean-001",),
        ),
        _hard_zero(
            suite,
            "serving_before_projection_ready",
            "Serving cannot advance before required projections are ready.",
            ("attack.projection-not-ready-001",),
            ("benign.projection-ready-001",),
        ),
    )
    return FormalSuiteContract(
        schema_version=FORMAL_SUITE_CONTRACT_VERSION,
        suite_id=suite,
        suite_version=FORMAL_SUITE_VERSION,
        description="Vertical game-planner journey from intake through grounded update and impact.",
        manifest_status=EvalStatus.NOT_AVAILABLE,
        manifest_status_reasons=(
            "FORMAL_PRODUCTION_PATH_NOT_IMPLEMENTED",
            "PRODUCTION_QUALITY_RUNNER_NOT_AVAILABLE",
            "SUBJECTIVE_GROUNDED_DRAFT_BLOCKED_EXTERNAL_DATA",
        ),
        allowed_providers=(EvalProvider.FIXTURE, EvalProvider.SCRIPTED, EvalProvider.REAL_MODEL),
        provider_evidence_policy=_PROVIDER_POLICY,
        global_real_model_budget=_global_budget_reference(),
        hard_zero_catalog=_hard_zero_catalog_reference(suite),
        suite_primary_metric="triage_content_role_macro_f1",
        experiments=(triage, grounded, impact, rag),
        controls=_controls(suite),
        hard_zero_gates=hard_zero,
    )


def _agent_trust_contract() -> FormalSuiteContract:
    suite = SuiteId.AGENT_TRUST_GOLD
    planning = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.PLANNING,
        description="Compare bounded ReAct with thin plan-and-execute on staged tasks.",
        primary=_metric(
            "planning_task_completion_rate",
            "Verifier-accepted task completion over every paired task.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "planning_valid_plan_rate", "Plans satisfying dependency and budget contracts."
            ),
            _metric(
                "planning_replan_success_rate",
                "Successful bounded replans after actionable failure.",
            ),
        ),
        baseline=_baseline("bounded_react_v1", "Bounded ReAct without a PlanArtifact."),
        coverage=(
            _coverage(
                "planning_positive_task_coverage",
                "planning_valid_plan_rate",
                ("planning.positive-001", "planning.positive-002"),
                minimum_rate=0.5,
            ),
        ),
    )
    reflection = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.REFLECTION,
        description=(
            "Compare reflection retry with ordinary retry under identical feedback and budget."
        ),
        primary=_metric(
            "reflection_retry_task_completion_rate",
            "Verifier-accepted completion rate for reflection retry versus ordinary retry.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "reflection_actionable_retry_coverage", "Eligible failures receiving one retry."
            ),
            _metric(
                "reflection_plan_change_rate",
                "Retries where reflection changes plan or tool choice.",
            ),
        ),
        baseline=_baseline(
            "ordinary_retry_same_feedback_v1",
            "Ordinary retry receiving the same verifier feedback and aggregate budget.",
        ),
        coverage=(
            _coverage(
                "reflection_actionable_retry_coverage",
                "reflection_actionable_retry_coverage",
                ("reflection.actionable.positive-001",),
                minimum_rate=1.0,
            ),
        ),
        arms=(
            _arm(
                "single_run_no_retry_v1",
                ExperimentArmRole.CONTROL,
                "One run with no retry, under the frozen per-case budget accounting.",
            ),
            _arm(
                "ordinary_retry_same_feedback_v1",
                ExperimentArmRole.BASELINE,
                "Ordinary retry receiving the same verifier feedback and aggregate budget.",
            ),
            _arm(
                "reflection_retry_v1",
                ExperimentArmRole.CANDIDATE,
                "One bounded Reflection retry using the same feedback, scorer, and budget.",
            ),
        ),
    )
    memory = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.MEMORY,
        description="Compare validated memory on/off without cross-run contamination.",
        primary=_metric(
            "memory_on_task_completion_rate",
            "Verifier-accepted completion with validated memory versus memory off.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "memory_delivered_positive_rate", "Validated useful memory delivered to prompt."
            ),
            _metric("memory_invalid_prompt_entry_rate", "Invalid memory entering a prompt."),
        ),
        baseline=_baseline("memory_off_v1", "Memory disabled in an isolated run namespace."),
        coverage=(
            _coverage(
                "memory_delivered_positive_coverage",
                "memory_delivered_positive_rate",
                ("memory.validated.positive-001",),
                minimum_rate=1.0,
            ),
        ),
        arms=(
            _arm(
                "memory_off_v1",
                ExperimentArmRole.BASELINE,
                "Memory disabled in an isolated run namespace.",
            ),
            _arm(
                "direct_evidence_context_v1",
                ExperimentArmRole.CONTROL,
                "Direct validated evidence context without durable memory retrieval.",
            ),
            _arm(
                "validated_memory_v1",
                ExperimentArmRole.CANDIDATE,
                "Validated, scoped, unexpired, current-revision memory retrieval.",
            ),
        ),
    )
    multi_agent = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.MULTI_AGENT,
        description="Compare fixed orchestrator-worker-verifier fan-out with a single agent.",
        primary=_metric(
            "multi_agent_task_completion_rate",
            "Unique verifier-accepted completion rate under equal aggregate budget.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "multi_agent_fanout_join_rate", "Decomposable tasks completing fan-out and join."
            ),
            _metric(
                "multi_agent_unique_result_rate", "Assignments with exactly one visible result."
            ),
        ),
        baseline=_baseline(
            "single_agent_equal_budget_v1",
            "Single agent with the same model, tools, model-call cap, and aggregate token cap.",
        ),
        coverage=(
            _coverage(
                "multi_agent_fanout_join_coverage",
                "multi_agent_fanout_join_rate",
                ("multi-agent.decomposable.positive-001",),
                minimum_rate=1.0,
            ),
        ),
    )
    tool_use = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.TOOL_USE,
        description="Evaluate production-path tool selection, arguments, effects, and evidence.",
        primary=_metric(
            "tool_use_task_completion_rate",
            "Verifier-accepted task completion on tool-requiring tasks.",
            primary=True,
        ),
        diagnostics=(
            _metric("tool_selection_precision", "Precision of selected production tools."),
            _metric("tool_selection_recall", "Recall of required production tools."),
            _metric("tool_argument_correctness_rate", "Exact rubric-valid tool argument rate."),
            _metric("tool_evidence_coverage", "Required effects covered by trusted evidence."),
            _metric("tool_completed_action_coverage", "Tool tasks completing a valid action."),
        ),
        baseline=_baseline(
            "direct_bounded_react_tool_use_v1",
            "Bounded ReAct using the same tools without the new task-policy controls.",
        ),
        coverage=(
            _coverage(
                "tool_use_positive_action_coverage",
                "tool_completed_action_coverage",
                ("tool-use.positive-001", "tool-use.positive-002"),
                minimum_rate=0.5,
            ),
        ),
    )
    persona = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.PERSONA,
        description="Blinded review of character consistency and identity/knowledge boundaries.",
        primary=_metric(
            "persona_blinded_accept_rate",
            "Blinded independent accept rate over representative Persona tasks.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "persona_consistency_rate", "Statements consistent with frozen character facts."
            ),
            _metric(
                "persona_knowledge_boundary_violation_rate", "Knowledge-boundary violation rate."
            ),
            _metric("persona_drift_rate", "Unjustified cross-scene Persona drift rate."),
            _metric("persona_output_coverage", "Fraction producing a reviewable response."),
        ),
        baseline=_baseline(
            "single_pass_without_persona_rubric_v1",
            "Single-pass grounded generation without the Persona review rubric.",
        ),
        coverage=(
            _coverage(
                "persona_non_refusal_coverage",
                "persona_output_coverage",
                ("persona.positive-001", "persona.positive-002"),
                minimum_rate=0.5,
            ),
        ),
        subjective=True,
    )
    hard_zero = (
        _hard_zero(
            suite,
            "forged_worker_evidence_accepted",
            "Forged worker evidence must be rejected; two trusted evidence classes remain "
            "accepted.",
            ("attack.forged-evidence-001",),
            ("benign.tool-evidence-001", "benign.artifact-evidence-001"),
        ),
        _hard_zero(
            suite,
            "invalid_memory_enters_prompt",
            "Unverified, revoked, expired, stale, or cross-scope memory cannot enter a prompt.",
            (
                "attack.memory-unverified-001",
                "attack.memory-revoked-001",
                "attack.memory-cross-scope-001",
                "attack.memory-expired-001",
                "attack.memory-stale-revision-001",
            ),
            ("benign.memory-validated-001",),
        ),
        _hard_zero(
            suite,
            "attempt_budget_reset",
            "A new attempt cannot reset consumed budget; a within-budget retry remains usable.",
            ("attack.attempt-budget-reset-001",),
            ("benign.retry-budget-preserved-001",),
        ),
        _hard_zero(
            suite,
            "duplicate_visible_assignment_result",
            (
                "Redelivery cannot create two visible results; a decomposable task completes one "
                "fan-out/join and a simple nondecomposable task completes with one single agent."
            ),
            ("attack.assignment-redelivery-001",),
            ("benign.fanout-join-001", "benign.single-agent-simple-task-001"),
        ),
    )
    return FormalSuiteContract(
        schema_version=FORMAL_SUITE_CONTRACT_VERSION,
        suite_id=suite,
        suite_version=FORMAL_SUITE_VERSION,
        description=(
            "Planning, tools, evidence, reflection, memory, Persona, and fixed collaboration."
        ),
        manifest_status=EvalStatus.NOT_AVAILABLE,
        manifest_status_reasons=(
            "FORMAL_AGENT_TRUST_PATH_NOT_IMPLEMENTED",
            "PRODUCTION_QUALITY_RUNNER_NOT_AVAILABLE",
            "SUBJECTIVE_PERSONA_BLOCKED_EXTERNAL_DATA",
        ),
        allowed_providers=(EvalProvider.FIXTURE, EvalProvider.SCRIPTED, EvalProvider.REAL_MODEL),
        provider_evidence_policy=_PROVIDER_POLICY,
        global_real_model_budget=_global_budget_reference(),
        hard_zero_catalog=_hard_zero_catalog_reference(suite),
        suite_primary_metric="planning_task_completion_rate",
        experiments=(planning, reflection, memory, multi_agent, tool_use, persona),
        controls=_controls(suite),
        hard_zero_gates=hard_zero,
    )


def _failure_isolation_contract() -> FormalSuiteContract:
    suite = SuiteId.FAILURE_ISOLATION_MATRIX
    fault_manifest = get_frozen_fault_manifest()
    all_attack_ids = tuple(cell.cell_id for cell in fault_manifest.attack_cells)
    all_benign_ids = tuple(control.catalog_id for control in fault_manifest.matched_benign_controls)
    failure = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.FAILURE_ISOLATION,
        description="Exercise frozen persistence boundaries, fault hits, recovery, and isolation.",
        primary=_metric(
            "failure_invariant_preservation_rate",
            "Fraction of executed fault cells preserving every preregistered invariant.",
            scope=MetricScope.HARNESS,
            primary=True,
        ),
        diagnostics=(
            _metric(
                "failure_injection_hit_rate",
                "Fault cells with injection_hit_count exactly one and boundary A/B evidence.",
                scope=MetricScope.HARNESS,
            ),
            _metric(
                "failure_benign_catalog_hit_rate",
                "Matched benign cells with catalog_hit_count exactly one and no injection marker.",
                scope=MetricScope.HARNESS,
            ),
            _metric(
                "recoverable_fault_unique_done_rate",
                "Recoverable durable claims reaching one DONE result within the frozen bound.",
                scope=MetricScope.HARNESS,
            ),
        ),
        baseline=_baseline(
            "frozen_fault_mutant_v1",
            "Known-bad implementation that misses one persistence/isolation invariant.",
            harness=True,
        ),
        coverage=(
            _coverage(
                "failure_injection_hit_coverage",
                "failure_injection_hit_rate",
                all_attack_ids,
                minimum_rate=1.0,
            ),
            _coverage(
                "failure_benign_catalog_hit_coverage",
                "failure_benign_catalog_hit_rate",
                all_benign_ids,
                minimum_rate=1.0,
            ),
        ),
    )
    hard_zero = (
        _hard_zero(
            suite,
            "cross_tenant_read_write",
            "Cross-tenant reads/writes are blocked while same-tenant operations work.",
            ("attack.tenant-a-to-b-read-001", "attack.tenant-a-to-b-write-001"),
            ("benign.same-tenant-read-write-001",),
        ),
        _hard_zero(
            suite,
            "mixed_revision_recovery",
            "Recovery never exposes a mixed old/new revision; either consistent revision works.",
            ("attack.kill-release-before-commit-001", "attack.kill-release-boundary-001"),
            ("benign.complete-release-001",),
        ),
        _hard_zero(
            suite,
            "concurrent_release_cas",
            (
                "Two approved same-base releases produce exactly one canonical successor and one "
                "authoring-head advance; ordered current-base releases both remain usable."
            ),
            ("attack.same-base-concurrent-release-001",),
            ("benign.sequential-release-bases-001",),
        ),
        _hard_zero(
            suite,
            "duplicate_observable_canonical_effect",
            (
                "Redelivery, Redis outage, and either relay restart boundary converge to at most "
                "one observable canonical effect."
            ),
            (
                "attack.redeliver-outbox-001",
                "attack.redis-transient-outage-001",
                "attack.relay-restart-before-delivery-001",
                "attack.relay-restart-after-delivery-before-mark-001",
            ),
            ("benign.single-delivery-001",),
        ),
        _hard_zero(
            suite,
            "projection_job_without_unique_done",
            (
                "Projection claim/result/ACK restarts and stale fencing converge to exactly one "
                "current DONE/READY projection result."
            ),
            (
                "attack.projection-worker-kill-after-claim-001",
                "attack.projection-exit-before-ack-001",
                "attack.projection-worker-kill-after-ack-001",
                "attack.projection-stale-fencing-token-001",
            ),
            ("benign.projection-job-completes-001",),
        ),
        _hard_zero(
            suite,
            "durable_claim_without_unique_done",
            "A recoverable fault after durable claim must converge to one DONE result in time.",
            (
                "attack.worker-kill-after-claim-001",
                "attack.worker-kill-before-result-001",
                "attack.exit-before-ack-001",
                "attack.verifier-restart-before-verdict-001",
                "attack.assignment-stale-fencing-token-001",
                "attack.assignment-redelivery-001",
            ),
            ("benign.assignment-completes-001",),
        ),
        _hard_zero(
            suite,
            "durable_agent_run_resume",
            (
                "Every registered AgentRun kill boundary resumes from durable plan, evidence, "
                "checkpoint, HITL, Reflection, and cumulative-budget truth without duplicate "
                "effects."
            ),
            (
                "attack.agent-kill-before-tool-001",
                "attack.agent-kill-after-tool-001",
                "attack.agent-kill-before-checkpoint-001",
                "attack.agent-kill-after-checkpoint-001",
                "attack.agent-kill-at-hitl-pause-001",
                "attack.agent-kill-before-reflection-001",
                "attack.agent-kill-after-reflection-001",
            ),
            (
                "benign.agent-tool-checkpoint-completes-001",
                "benign.agent-hitl-resume-completes-001",
                "benign.agent-reflection-retry-completes-001",
            ),
        ),
        _hard_zero(
            suite,
            "revoked_sse_event_leakage",
            "Revoked membership yields zero later tenant events while an authorized stream works.",
            ("attack.sse-revoked-membership-001",),
            ("benign.sse-authorized-stream-001",),
        ),
        _hard_zero(
            suite,
            "service_role_cross_tenant_access",
            (
                "Non-superuser relay and worker roles reject opaque cross-tenant payload scope "
                "while both matched same-tenant service paths work."
            ),
            (
                "attack.relay-cross-tenant-payload-001",
                "attack.worker-cross-tenant-payload-001",
            ),
            (
                "benign.relay-same-tenant-payload-001",
                "benign.worker-same-tenant-payload-001",
            ),
        ),
        _hard_zero(
            suite,
            "publish_idempotency_replay_duplicate_effect",
            (
                "A same-key publish replay returns the original result and cannot create a "
                "second visible authoring revision, outbox row, or ledger effect; distinct keys "
                "on successive bases remain usable."
            ),
            ("attack.publish-idempotency-replay-001",),
            ("benign.distinct-idempotency-publishes-001",),
        ),
        _hard_zero(
            suite,
            "reconciliation_revision_corruption",
            (
                "Vector duplicates and cross-revision community corruption are recomputed from "
                "durable source truth into one unique same-revision projection state."
            ),
            (
                "attack.vector-reconciliation-duplicate-001",
                "attack.community-cross-revision-corruption-001",
            ),
            ("benign.reconciliation-clean-repaired-001",),
        ),
        _hard_zero(
            suite,
            "nonrecoverable_assignment_terminal_reason",
            (
                "Bad payload, policy rejection, explicit cancellation, and retry-budget "
                "exhaustion each yield exactly one FAILED terminal with its precise reason and "
                "zero unauthorized side effects; a valid assignment reaches DONE."
            ),
            (
                "attack.assignment-bad-payload-001",
                "attack.assignment-policy-rejection-001",
                "attack.assignment-explicit-cancellation-001",
                "attack.assignment-retry-budget-exhaustion-001",
            ),
            ("benign.valid-assignment-terminal-done-001",),
        ),
        _hard_zero(
            suite,
            "cross_instance_job_visibility",
            (
                "After source instance A durably creates a scoped job and exits, distinct "
                "recovery instance B must query, observe, reclaim, and uniquely complete that "
                "same job; the healthy two-instance path remains visible."
            ),
            ("attack.instance-a-exit-instance-b-reclaim-001",),
            ("benign.cross-instance-job-visible-001",),
        ),
        _hard_zero(
            suite,
            "assignment_outbox_atomicity",
            (
                "Assignment and matching outbox row commit in one PostgreSQL transaction: a "
                "pre-commit kill exposes neither before same-key recovery commits one pair, and "
                "a post-commit/pre-delivery kill relays the durable pair to one DONE result."
            ),
            (
                "attack.assignment-outbox-kill-before-commit-001",
                "attack.assignment-outbox-kill-after-commit-before-delivery-001",
            ),
            ("benign.assignment-outbox-atomic-commit-001",),
        ),
        _hard_zero(
            suite,
            "projection_not_ready_serving_advance",
            "Serving pointer cannot advance before all required projections are READY.",
            ("attack.projection-failure-001",),
            ("benign.all-projections-ready-001",),
        ),
    )
    return FormalSuiteContract(
        schema_version=FORMAL_SUITE_CONTRACT_VERSION,
        suite_id=suite,
        suite_version=FORMAL_SUITE_VERSION,
        description="Two-tenant/two-instance fault, redelivery, fencing, and recovery matrix.",
        manifest_status=EvalStatus.NOT_AVAILABLE,
        manifest_status_reasons=(
            "TEAM_PROFILE_AND_FAULT_TARGETS_NOT_IMPLEMENTED",
            "PRODUCTION_FAULT_RUNNER_NOT_AVAILABLE",
        ),
        allowed_providers=(EvalProvider.FIXTURE, EvalProvider.SCRIPTED),
        provider_evidence_policy=_PROVIDER_POLICY,
        global_real_model_budget=_global_budget_reference(),
        hard_zero_catalog=_hard_zero_catalog_reference(suite),
        fault_manifest=_fault_manifest_reference(),
        suite_primary_metric="failure_invariant_preservation_rate",
        experiments=(failure,),
        controls=_controls(suite),
        hard_zero_gates=hard_zero,
    )


def _semantic_shadow_contract() -> FormalSuiteContract:
    suite = SuiteId.SEMANTIC_SHADOW_EVAL
    semantic = _experiment(
        suite_id=suite,
        dimension=EvaluationDimension.SEMANTIC_REVIEW,
        description="Candidate retrieval and blinded judging for frozen narrative defect types.",
        primary=_metric(
            "semantic_end_to_end_recall",
            "Recall from eligible defect to correctly attributed defect_type and evidence refs.",
            primary=True,
        ),
        diagnostics=(
            _metric(
                "semantic_candidate_recall_at_k",
                "Eligible defects recalled among top-K candidates.",
            ),
            _metric("semantic_judge_precision", "Precision of judged defects among candidates."),
            _metric("semantic_judge_recall", "Recall of judged defects among candidates."),
            _metric("semantic_evidence_locator_precision", "Exact supporting-locator precision."),
            _metric(
                "semantic_abstain_rate",
                "Rate of explicit abstention when evidence is insufficient.",
            ),
            _metric(
                "semantic_false_positives_per_1000_statements",
                "False positives per 1,000 representative negative statements.",
                "count_per_1000",
            ),
            _metric("semantic_cost_per_1000_statements", "Model cost per 1,000 statements.", "usd"),
        ),
        baseline=_baseline(
            "deterministic_canary_and_structural_review_v1",
            "Existing deterministic canaries plus structural review, without semantic judgement.",
        ),
        coverage=(
            _coverage(
                "semantic_candidate_recall_coverage",
                "semantic_candidate_recall_at_k",
                (
                    "semantic.identity-knowledge.positive-001",
                    "semantic.cross-segment-state.positive-001",
                    "semantic.persona-drift.positive-001",
                ),
                minimum_rate=1.0 / 3.0,
            ),
        ),
        subjective=True,
    )
    hard_zero = (
        _hard_zero(
            suite,
            "semantic_clean_world_false_positive",
            "Clean worlds, hard negatives, and justified growth remain unflagged while "
            "defects are found.",
            (
                "attack.clean-world-false-positive-001",
                "attack.hard-negative-false-positive-001",
                "attack.justified-growth-false-positive-001",
            ),
            ("benign.true-defect-detected-001",),
        ),
    )
    return FormalSuiteContract(
        schema_version=FORMAL_SUITE_CONTRACT_VERSION,
        suite_id=suite,
        suite_version=FORMAL_SUITE_VERSION,
        description=(
            "Semantic shadow recall, judgement, abstention, and exact evidence attribution."
        ),
        manifest_status=EvalStatus.NOT_AVAILABLE,
        manifest_status_reasons=(
            "SEMANTIC_SHADOW_PRODUCTION_PATH_NOT_IMPLEMENTED",
            "PRODUCTION_QUALITY_RUNNER_NOT_AVAILABLE",
            "REPRESENTATIVE_SEALED_CORPUS_SECOND_ANNOTATOR_AND_ADJUDICATION_UNAVAILABLE",
        ),
        allowed_providers=(EvalProvider.FIXTURE, EvalProvider.SCRIPTED, EvalProvider.REAL_MODEL),
        provider_evidence_policy=_PROVIDER_POLICY,
        global_real_model_budget=_global_budget_reference(),
        hard_zero_catalog=_hard_zero_catalog_reference(suite),
        suite_primary_metric="semantic_end_to_end_recall",
        experiments=(semantic,),
        controls=_controls(suite),
        hard_zero_gates=hard_zero,
    )


_SUITE_CONTRACTS = (
    _game_planner_contract(),
    _agent_trust_contract(),
    _failure_isolation_contract(),
    _semantic_shadow_contract(),
)

EXPECTED_EVALUATION_DIMENSIONS = frozenset(EvaluationDimension)


def _validate_complete_registry(contracts: tuple[FormalSuiteContract, ...]) -> None:
    suite_ids = [contract.suite_id for contract in contracts]
    if set(suite_ids) != set(SuiteId) or len(suite_ids) != len(set(suite_ids)):
        raise SuiteRegistryError("FORMAL_REGISTRY_MUST_CONTAIN_EXACTLY_FOUR_UNIQUE_SUITES")
    dimensions = [
        experiment.dimension for contract in contracts for experiment in contract.experiments
    ]
    if set(dimensions) != EXPECTED_EVALUATION_DIMENSIONS or len(dimensions) != len(set(dimensions)):
        raise SuiteRegistryError("FORMAL_REGISTRY_DIMENSION_COVERAGE_MISMATCH")
    expected_budget = _global_budget_reference()
    if any(contract.global_real_model_budget != expected_budget for contract in contracts):
        raise SuiteRegistryError("FORMAL_SUITES_MUST_BIND_THE_ONE_GLOBAL_REAL_MODEL_BUDGET")
    allowed_components = set(_GLOBAL_REAL_MODEL_BUDGET.charged_components)
    if any(
        not set(experiment.paired_budget.charged_components) <= allowed_components
        for contract in contracts
        for experiment in contract.experiments
    ):
        raise SuiteRegistryError("EXPERIMENT_USES_UNKNOWN_GLOBAL_BUDGET_COMPONENT")
    gates_by_suite = {
        contract.suite_id.value: tuple(
            gate.model_dump(mode="json") for gate in contract.hard_zero_gates
        )
        for contract in contracts
    }
    try:
        hard_zero_catalogs = get_frozen_hard_zero_catalogs()
        validate_hard_zero_catalogs_against_gates(hard_zero_catalogs, gates_by_suite)
        failure_contract = next(
            contract
            for contract in contracts
            if contract.suite_id is SuiteId.FAILURE_ISOLATION_MATRIX
        )
        fault_manifest = get_frozen_fault_manifest()
        validate_fault_manifest_against_hard_zero_gates(
            fault_manifest,
            gates_by_suite[SuiteId.FAILURE_ISOLATION_MATRIX.value],
        )
        validate_fault_manifest_against_hard_zero_catalog(
            fault_manifest,
            hard_zero_catalogs[SuiteId.FAILURE_ISOLATION_MATRIX.value],
        )
        failure_experiment = failure_contract.experiments[0]
        coverage_by_id = {gate.gate_id: gate for gate in failure_experiment.positive_coverage_gates}
        attack_coverage = coverage_by_id.get("failure_injection_hit_coverage")
        benign_coverage = coverage_by_id.get("failure_benign_catalog_hit_coverage")
        expected_attacks = {cell.cell_id for cell in fault_manifest.attack_cells}
        expected_benign = {control.catalog_id for control in fault_manifest.matched_benign_controls}
        if (
            attack_coverage is None
            or set(attack_coverage.case_ids) != expected_attacks
            or attack_coverage.minimum_denominator != len(expected_attacks)
        ):
            raise ValueError("FAILURE_ATTACK_POSITIVE_COVERAGE_MUST_BIND_ALL_FAULT_CELLS")
        if (
            benign_coverage is None
            or set(benign_coverage.case_ids) != expected_benign
            or benign_coverage.minimum_denominator != len(expected_benign)
        ):
            raise ValueError("FAILURE_BENIGN_POSITIVE_COVERAGE_MUST_BIND_ALL_CATALOG_CELLS")
        if failure_contract.fault_manifest != _fault_manifest_reference():
            raise ValueError("FAILURE_FAULT_MANIFEST_REFERENCE_MISMATCH")
    except ValueError as exc:
        raise SuiteRegistryError(str(exc)) from exc


_validate_complete_registry(_SUITE_CONTRACTS)
_CONTRACT_BY_ID = {contract.suite_id.value: contract for contract in _SUITE_CONTRACTS}


def get_formal_suite_registry() -> dict[str, FormalSuiteContract]:
    """Return defensive copies of every preregistered suite contract."""

    return {
        suite_id: contract.model_copy(deep=True) for suite_id, contract in _CONTRACT_BY_ID.items()
    }


def get_global_real_model_budget() -> GlobalRealModelBudget:
    """Return a defensive copy of the sole aggregate real-model safety ceiling."""

    return _GLOBAL_REAL_MODEL_BUDGET.model_copy(deep=True)


def render_suite_contracts() -> dict[str, bytes]:
    """Render deterministic suite contract sidecars from the Python source of truth."""

    return {
        suite_id: _json_bytes(contract.model_dump(mode="json"))
        for suite_id, contract in _CONTRACT_BY_ID.items()
    }


def _manifest_metrics(contract: FormalSuiteContract) -> list[MetricDefinition]:
    metrics: list[MetricDefinition] = []
    for experiment in contract.experiments:
        for metric in (experiment.primary_metric, *experiment.diagnostic_metrics):
            payload = metric.model_dump()
            payload["primary"] = metric.name == contract.suite_primary_metric
            metrics.append(MetricDefinition.model_validate(payload))
    metrics.append(
        _metric(
            "harness_control_discrimination_rate",
            "Matched-good PASS and frozen-mutant FAIL decisions over all frozen controls.",
            scope=MetricScope.HARNESS,
        )
    )
    for gate in contract.hard_zero_gates:
        metrics.extend(
            (
                _metric(
                    gate.violation_metric,
                    f"Hard-zero attack-side violations for {gate.gate_id}.",
                    "count",
                    scope=MetricScope.HARNESS,
                ),
                _metric(
                    gate.benign_false_reject_metric,
                    f"Hard-zero matched-benign false rejects for {gate.gate_id}.",
                    "count",
                    scope=MetricScope.HARNESS,
                ),
            )
        )
    names = [metric.name for metric in metrics]
    if len(names) != len(set(names)):
        raise SuiteRegistryError(f"MANIFEST_METRIC_NAMES_MUST_BE_UNIQUE:{contract.suite_id.value}")
    if sum(metric.primary for metric in metrics) != 1:
        raise SuiteRegistryError(
            f"MANIFEST_MUST_HAVE_EXACTLY_ONE_PRIMARY:{contract.suite_id.value}"
        )
    return metrics


def _build_manifest(contract: FormalSuiteContract, contract_bytes: bytes) -> FormalEvalManifest:
    artifacts = [
        ArtifactReference(
            path="contract.json",
            sha256=hashlib.sha256(contract_bytes).hexdigest(),
            byte_count=len(contract_bytes),
        ),
        ArtifactReference(
            path=contract.global_real_model_budget.path,
            sha256=contract.global_real_model_budget.sha256,
            byte_count=contract.global_real_model_budget.byte_count,
        ),
        ArtifactReference(
            path=contract.hard_zero_catalog.path,
            sha256=contract.hard_zero_catalog.sha256,
            byte_count=contract.hard_zero_catalog.byte_count,
        ),
    ]
    if contract.fault_manifest is not None:
        artifacts.append(
            ArtifactReference(
                path=contract.fault_manifest.path,
                sha256=contract.fault_manifest.sha256,
                byte_count=contract.fault_manifest.byte_count,
            )
        )
    return FormalEvalManifest(
        schema_version="formal-eval/v1",
        suite_id=contract.suite_id.value,
        suite_version=contract.suite_version,
        description=contract.description,
        status=contract.manifest_status,
        status_reasons=contract.manifest_status_reasons,
        trace_schema_version="formal-eval/v1",
        report_schema_version="formal-eval/v1",
        scorer=None,
        allowed_providers=contract.allowed_providers,
        artifacts=tuple(artifacts),
        metrics=tuple(_manifest_metrics(contract)),
    )


def render_suite_manifests() -> dict[str, bytes]:
    """Render deterministic manifests that hash-bind each full contract sidecar."""

    contract_bytes = render_suite_contracts()
    return {
        suite_id: _json_bytes(
            _build_manifest(_CONTRACT_BY_ID[suite_id], content).model_dump(mode="json")
        )
        for suite_id, content in contract_bytes.items()
    }


def render_frozen_suite_files() -> dict[str, bytes]:
    """Return all relative suite artifact paths and their canonical bytes."""

    contracts = render_suite_contracts()
    manifests = render_suite_manifests()
    files: dict[str, bytes] = {}
    for suite_id in sorted(_CONTRACT_BY_ID):
        files[f"{suite_id}/contract.json"] = contracts[suite_id]
        files[f"{suite_id}/manifest.json"] = manifests[suite_id]
    return files


def render_frozen_formal_files() -> dict[str, bytes]:
    """Return canonical paths rooted at ``benchmarks/formal`` including the global budget."""

    files = {"global_real_model_budget.json": render_global_real_model_budget()}
    files.update(
        {
            f"suites/{relative_path}": content
            for relative_path, content in render_frozen_suite_files().items()
        }
    )
    return files


def validate_manifest_against_registry(manifest: FormalEvalManifest) -> None:
    """Reject a parsed manifest that diverges from its preregistered suite contract."""

    contract = _CONTRACT_BY_ID.get(manifest.suite_id)
    if contract is None:
        raise SuiteRegistryError(f"UNKNOWN_FORMAL_SUITE:{manifest.suite_id}")
    primary = [metric.name for metric in manifest.metrics if metric.primary]
    if len(primary) != 1:
        raise SuiteRegistryError("MANIFEST_MUST_HAVE_EXACTLY_ONE_PRIMARY_METRIC")
    if primary[0] != contract.suite_primary_metric:
        raise SuiteRegistryError("MANIFEST_PRIMARY_METRIC_DIFFERS_FROM_PREREGISTRATION")
    expected_contract = render_suite_contracts()[manifest.suite_id]
    expected = _build_manifest(contract, expected_contract)
    if manifest.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise SuiteRegistryError("MANIFEST_DIFFERS_FROM_PREREGISTERED_CONTRACT")


def verify_frozen_suite_manifests(directory: str | Path) -> None:
    """Raise if any committed suite manifest or contract differs by one byte."""

    root = Path(directory)
    mismatches: list[str] = []
    global_budget_path = root.parent / "global_real_model_budget.json"
    expected_global_budget = render_global_real_model_budget()
    if not global_budget_path.exists():
        mismatches.append("../global_real_model_budget.json: missing")
    elif global_budget_path.read_bytes() != expected_global_budget:
        mismatches.append("../global_real_model_budget.json: byte mismatch")
    for relative_path, expected in render_frozen_suite_files().items():
        path = root / relative_path
        if not path.exists():
            mismatches.append(f"{relative_path}: missing")
        elif path.read_bytes() != expected:
            mismatches.append(f"{relative_path}: byte mismatch")
    if mismatches:
        raise FrozenSuiteMismatchError("; ".join(mismatches))

    for suite_id in sorted(_CONTRACT_BY_ID):
        manifest = FormalEvalManifest.model_validate_json(
            (root / suite_id / "manifest.json").read_bytes()
        )
        validate_manifest_against_registry(manifest)


__all__ = [
    "EXPECTED_EVALUATION_DIMENSIONS",
    "FORMAL_SUITE_CONTRACT_VERSION",
    "FORMAL_SUITE_VERSION",
    "GLOBAL_REAL_MODEL_BUDGET_ARTIFACT_PATH",
    "GLOBAL_REAL_MODEL_BUDGET_ID",
    "GLOBAL_REAL_MODEL_BUDGET_SCHEMA_VERSION",
    "BaselineContract",
    "BudgetAllocation",
    "BudgetAllocationId",
    "BudgetReserve",
    "ControlKind",
    "DataFreezeContract",
    "DatasetOrigin",
    "DatasetSlot",
    "EvaluationDimension",
    "ExperimentArmContract",
    "ExperimentArmRole",
    "ExperimentContract",
    "ExternalDataBlocker",
    "FaultManifestArtifactReference",
    "FormalControl",
    "FormalSuiteContract",
    "FrozenSuiteMismatchError",
    "GlobalBudgetArtifactReference",
    "GlobalRealModelBudget",
    "HardZeroCatalogArtifactReference",
    "HardZeroGate",
    "PositiveCoverageGate",
    "PairedBudgetContract",
    "ProviderEvidencePolicy",
    "ScorerContract",
    "SuiteId",
    "SuiteRegistryError",
    "get_formal_suite_registry",
    "get_global_real_model_budget",
    "render_frozen_formal_files",
    "render_frozen_suite_files",
    "render_global_real_model_budget",
    "render_suite_contracts",
    "render_suite_manifests",
    "validate_manifest_against_registry",
    "verify_frozen_suite_manifests",
]
