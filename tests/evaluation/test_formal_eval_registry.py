"""Preregistration controls for the N0.3 formal suite registry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from owcopilot.evaluation.formal_faults import get_frozen_fault_manifest
from owcopilot.evaluation.formal_registry import (
    EXPECTED_EVALUATION_DIMENSIONS,
    BudgetAllocationId,
    ControlKind,
    EvaluationDimension,
    ExperimentArmRole,
    FormalSuiteContract,
    FrozenSuiteMismatchError,
    GlobalRealModelBudget,
    SuiteId,
    SuiteRegistryError,
    get_formal_suite_registry,
    get_global_real_model_budget,
    render_frozen_formal_files,
    render_frozen_suite_files,
    render_global_real_model_budget,
    render_suite_contracts,
    render_suite_manifests,
    validate_manifest_against_registry,
    verify_frozen_suite_manifests,
)
from owcopilot.evaluation.protocol import (
    EvalProvider,
    EvalStatus,
    FormalEvalManifest,
    MetricScope,
)

_FORMAL_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "formal"
_SUITE_DIR = _FORMAL_DIR / "suites"


def _experiment_payload(suite_id: str, dimension: str) -> tuple[dict[str, object], int]:
    payload = get_formal_suite_registry()[suite_id].model_dump(mode="json")
    experiments = payload["experiments"]
    assert isinstance(experiments, list)
    for index, experiment in enumerate(experiments):
        assert isinstance(experiment, dict)
        if experiment["dimension"] == dimension:
            return payload, index
    raise AssertionError(f"missing {suite_id}/{dimension}")


def test_registry_has_exactly_four_suites_and_every_preregistered_dimension_once() -> None:
    registry = get_formal_suite_registry()
    assert set(registry) == {suite.value for suite in SuiteId}

    dimensions = [
        experiment.dimension
        for contract in registry.values()
        for experiment in contract.experiments
    ]
    assert set(dimensions) == EXPECTED_EVALUATION_DIMENSIONS
    assert len(dimensions) == len(set(dimensions))


def test_one_global_budget_freezes_finite_caps_priority_allocations_and_reserve() -> None:
    budget = get_global_real_model_budget()
    assert budget.budget_id == "n0.3-global-real-model-budget-v1"
    assert budget.currency == "USD"
    assert budget.model_call_cap == 600
    assert budget.token_cap == 6_000_000
    assert budget.cost_usd_cap == 60.0
    assert budget.spend_authorized is False
    assert budget.authorization_note == "CAP_IS_A_SAFETY_CEILING_NOT_SPEND_AUTHORIZATION"
    assert set(budget.charged_components) == {
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
    assert budget.failed_errored_and_retried_usage_counted is True

    by_id = {allocation.allocation_id: allocation for allocation in budget.allocations}
    assert [
        by_id[allocation_id].priority_rank
        for allocation_id in (
            BudgetAllocationId.TRIAGE_GROUNDED_DRAFT,
            BudgetAllocationId.EVIDENCE_VERIFIER,
            BudgetAllocationId.MEMORY,
            BudgetAllocationId.REFLECTION,
            BudgetAllocationId.MULTI_AGENT,
        )
    ] == [1, 2, 3, 4, 5]
    mcts = by_id[BudgetAllocationId.MCTS]
    assert (mcts.model_call_cap, mcts.token_cap, mcts.cost_usd_cap) == (0, 0, 0.0)

    assert sum(item.model_call_cap for item in budget.allocations) == 540
    assert sum(item.token_cap for item in budget.allocations) == 5_400_000
    assert sum(item.cost_usd_cap for item in budget.allocations) == 54.0
    assert budget.unallocated_reserve.model_call_cap == 60
    assert budget.unallocated_reserve.token_cap == 600_000
    assert budget.unallocated_reserve.cost_usd_cap == 6.0


def test_all_suites_bind_the_same_global_budget_id_hash_and_bytes() -> None:
    rendered = render_global_real_model_budget()
    expected = (
        "n0.3-global-real-model-budget-v1",
        hashlib.sha256(rendered).hexdigest(),
        len(rendered),
    )
    references = {
        (
            contract.global_real_model_budget.budget_id,
            contract.global_real_model_budget.sha256,
            contract.global_real_model_budget.byte_count,
        )
        for contract in get_formal_suite_registry().values()
    }
    assert references == {expected}
    assert {
        contract.global_real_model_budget.path for contract in get_formal_suite_registry().values()
    } == {"../../global_real_model_budget.json"}


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda payload: payload.update(cost_usd_cap=float("inf")),
            "Input should be a finite number",
        ),
        (
            lambda payload: payload["charged_components"].remove("retry"),
            "GLOBAL_BUDGET_MISSING_OR_UNKNOWN_CHARGED_COMPONENT",
        ),
        (
            lambda payload: payload["allocations"][0].update(model_call_cap=700),
            "GLOBAL_MODEL_CALL_BUDGET_OVERALLOCATED",
        ),
        (
            lambda payload: next(
                item for item in payload["allocations"] if item["allocation_id"] == "mcts"
            ).update(model_call_cap=1),
            "MCTS_MUST_HAVE_ZERO_CORE_REAL_MODEL_BUDGET",
        ),
    ],
)
def test_global_budget_tamper_is_stably_rejected(mutation: object, error: str) -> None:
    payload = get_global_real_model_budget().model_dump(mode="json")
    assert callable(mutation)
    mutation(payload)

    with pytest.raises(ValidationError, match=error):
        GlobalRealModelBudget.model_validate(payload)


def test_every_experiment_has_one_primary_metric_baseline_and_positive_denominator() -> None:
    for contract in get_formal_suite_registry().values():
        for experiment in contract.experiments:
            assert experiment.primary_metric.primary is True
            assert all(metric.primary is False for metric in experiment.diagnostic_metrics)
            assert experiment.baseline.baseline_id
            assert experiment.baseline.paired_by_case is True
            assert experiment.baseline.scorer_identical_between_arms is True
            assert experiment.positive_coverage_gates
            for gate in experiment.positive_coverage_gates:
                assert gate.minimum_denominator > 0
                assert len(gate.case_ids) >= gate.minimum_denominator
                assert gate.minimum_rate > 0
                assert gate.unmet_status is EvalStatus.NOT_RUN


def test_every_experiment_preregisters_candidate_baseline_and_exact_paired_budget() -> None:
    global_components = set(get_global_real_model_budget().charged_components)
    for contract in get_formal_suite_registry().values():
        for experiment in contract.experiments:
            assert len(experiment.preregistered_arms) >= 2
            candidates = [
                arm
                for arm in experiment.preregistered_arms
                if arm.role is ExperimentArmRole.CANDIDATE
            ]
            baselines = [
                arm
                for arm in experiment.preregistered_arms
                if arm.role is ExperimentArmRole.BASELINE
            ]
            assert len(candidates) == 1
            assert [arm.arm_id for arm in baselines] == [experiment.baseline.baseline_id]
            budget = experiment.paired_budget
            assert budget.same_model_id_between_arms is True
            assert budget.same_model_call_cap_between_arms is True
            assert budget.same_aggregate_input_output_token_cap_between_arms is True
            assert budget.same_cases_between_arms is True
            assert budget.same_scorer_between_arms is True
            assert budget.failed_errored_and_retried_usage_counted is True
            assert budget.actual_tokens_cost_and_latency_reported_per_arm is True
            assert set(budget.charged_components) <= global_components
            if experiment.dimension is EvaluationDimension.FAILURE_ISOLATION:
                assert budget.model_calls_allowed is False
                assert budget.allocation_id is None
                assert budget.charged_components == ()
            else:
                assert budget.model_calls_allowed is True
                assert budget.allocation_id is not None
                assert budget.charged_components


def test_reflection_and_rag_freeze_the_required_exact_arms() -> None:
    experiments = {
        experiment.dimension: experiment
        for contract in get_formal_suite_registry().values()
        for experiment in contract.experiments
    }
    reflection = experiments[EvaluationDimension.REFLECTION]
    assert {arm.arm_id: arm.role for arm in reflection.preregistered_arms} == {
        "single_run_no_retry_v1": ExperimentArmRole.CONTROL,
        "ordinary_retry_same_feedback_v1": ExperimentArmRole.BASELINE,
        "reflection_retry_v1": ExperimentArmRole.CANDIDATE,
    }
    assert {"reflection", "compression", "retry"} <= set(
        reflection.paired_budget.charged_components
    )

    rag = experiments[EvaluationDimension.RAG]
    assert {arm.arm_id: arm.role for arm in rag.preregistered_arms} == {
        "bm25_only_v1": ExperimentArmRole.BASELINE,
        "vector_only_v1": ExperimentArmRole.CONTROL,
        "graph_expand_only_v1": ExperimentArmRole.CONTROL,
        "bm25_vector_graph_fusion_v1": ExperimentArmRole.CANDIDATE,
    }
    assert rag.paired_budget.same_cases_between_arms is True
    assert rag.paired_budget.same_scorer_between_arms is True


def test_required_metrics_are_preregistered_with_unique_primary_choices() -> None:
    registry = get_formal_suite_registry()
    experiments = {
        experiment.dimension: experiment
        for contract in registry.values()
        for experiment in contract.experiments
    }

    triage = experiments[EvaluationDimension.TRIAGE]
    assert triage.primary_metric.name == "triage_content_role_macro_f1"
    assert {
        "triage_selective_accuracy",
        "triage_coverage",
        "triage_risk_coverage_auc",
        "triage_expected_calibration_error",
        "triage_locator_fidelity",
    } <= {metric.name for metric in triage.diagnostic_metrics}

    draft = experiments[EvaluationDimension.GROUNDED_DRAFT]
    assert draft.primary_metric.name == "grounded_draft_human_accept_rate"
    assert {
        "grounded_draft_structural_validity_rate",
        "grounded_draft_canon_conflict_rate",
        "grounded_draft_leakage_rate",
        "grounded_draft_human_revise_rate",
        "grounded_draft_human_reject_rate",
        "grounded_draft_relative_modification_burden",
    } <= {metric.name for metric in draft.diagnostic_metrics}

    impact = experiments[EvaluationDimension.IMPACT]
    assert impact.primary_metric.name == "impact_expected_refs_f1"
    assert {"impact_expected_refs_precision", "impact_expected_refs_recall"} <= {
        metric.name for metric in impact.diagnostic_metrics
    }

    semantic = experiments[EvaluationDimension.SEMANTIC_REVIEW]
    assert semantic.primary_metric.name == "semantic_end_to_end_recall"
    assert {
        "semantic_candidate_recall_at_k",
        "semantic_judge_precision",
        "semantic_judge_recall",
        "semantic_evidence_locator_precision",
        "semantic_abstain_rate",
        "semantic_false_positives_per_1000_statements",
        "semantic_cost_per_1000_statements",
    } <= {metric.name for metric in semantic.diagnostic_metrics}


def test_baselines_freeze_the_required_equal_budget_comparisons() -> None:
    experiments = {
        experiment.dimension: experiment
        for contract in get_formal_suite_registry().values()
        for experiment in contract.experiments
    }
    assert experiments[EvaluationDimension.PLANNING].baseline.baseline_id == "bounded_react_v1"
    assert (
        experiments[EvaluationDimension.REFLECTION].baseline.baseline_id
        == "ordinary_retry_same_feedback_v1"
    )
    assert experiments[EvaluationDimension.MEMORY].baseline.baseline_id == "memory_off_v1"
    assert (
        experiments[EvaluationDimension.MULTI_AGENT].baseline.baseline_id
        == "single_agent_equal_budget_v1"
    )
    assert experiments[EvaluationDimension.RAG].baseline.baseline_id == "bm25_only_v1"
    for dimension, experiment in experiments.items():
        expected = (
            "not_applicable_harness"
            if dimension is EvaluationDimension.FAILURE_ISOLATION
            else "same_model_calls_and_input_plus_output_token_cap"
        )
        assert experiment.baseline.aggregate_budget_rule == expected


def test_fixture_and_scripted_providers_are_harness_only_not_quality_effect() -> None:
    for contract in get_formal_suite_registry().values():
        policy = contract.provider_evidence_policy
        assert policy.fixture_max_scope is MetricScope.HARNESS
        assert policy.scripted_max_scope is MetricScope.HARNESS
        assert policy.quality_provider is EvalProvider.REAL_MODEL
        assert policy.fixture_quality_effect_allowed is False
        assert policy.scripted_gold_f1_is_external_quality_metric is False


def test_subjective_quality_is_blocked_on_real_external_inputs_not_demo_data() -> None:
    subjective = {
        EvaluationDimension.GROUNDED_DRAFT,
        EvaluationDimension.PERSONA,
        EvaluationDimension.SEMANTIC_REVIEW,
    }
    for contract in get_formal_suite_registry().values():
        for experiment in contract.experiments:
            if experiment.dimension in subjective:
                assert experiment.status is EvalStatus.BLOCKED_EXTERNAL_DATA
                assert experiment.data_freeze is not None
                assert experiment.data_freeze.sealed.status is EvalStatus.BLOCKED_EXTERNAL_DATA
                assert {blocker.blocker_id for blocker in experiment.external_data_blockers} == {
                    "REPRESENTATIVE_SEALED_CORPUS_UNAVAILABLE",
                    "SECOND_ANNOTATOR_UNAVAILABLE",
                    "DISAGREEMENT_ADJUDICATION_UNAVAILABLE",
                }
            else:
                assert experiment.status is EvalStatus.NOT_AVAILABLE
                assert not experiment.external_data_blockers


def test_dev_and_sealed_are_physically_distinct_and_unfrozen_hashes_are_honest() -> None:
    for contract in get_formal_suite_registry().values():
        for experiment in contract.experiments:
            freeze = experiment.data_freeze
            if experiment.dimension is EvaluationDimension.FAILURE_ISOLATION:
                assert freeze is None
                continue
            assert freeze is not None
            assert freeze.dev.dataset_id != freeze.sealed.dataset_id
            assert freeze.dev.relative_path != freeze.sealed.relative_path
            assert freeze.dev.content_sha256 is None
            assert freeze.sealed.content_sha256 is None
            assert freeze.dev_may_be_promoted_to_sealed is False
            assert freeze.sealed_outputs_unopened is True


def test_every_suite_preregisters_good_mutant_and_nonzero_hard_zero_controls() -> None:
    for contract in get_formal_suite_registry().values():
        assert {control.kind for control in contract.controls} == {
            ControlKind.MATCHED_GOOD,
            ControlKind.FROZEN_MUTANT,
        }
        for gate in contract.hard_zero_gates:
            assert gate.attack_case_ids
            assert gate.benign_control_case_ids
            assert gate.minimum_attack_denominator > 0
            assert gate.minimum_benign_denominator > 0
            assert gate.maximum_violations == 0
            assert gate.maximum_benign_false_rejects == 0
            assert gate.zero_denominator_status is EvalStatus.NOT_RUN


def test_release_memory_and_multi_agent_hard_zero_gates_bind_exact_physical_cases() -> None:
    registry = get_formal_suite_registry()
    game_gates = {
        gate.gate_id: gate
        for gate in registry[SuiteId.GAME_PLANNER_VERTICAL_E2E.value].hard_zero_gates
    }
    release = game_gates["unapproved_release_visibility"]
    assert "visible canonical revision" in release.description
    assert "advances authoring" in release.description
    assert "OR advances serving" in release.description
    assert "exactly one canonical successor" in release.description
    assert "READY for every required projection" in release.description
    assert "advance serving exactly once" in release.description

    agent_gates = {
        gate.gate_id: gate for gate in registry[SuiteId.AGENT_TRUST_GOLD.value].hard_zero_gates
    }
    memory = agent_gates["invalid_memory_enters_prompt"]
    assert set(memory.attack_case_ids) == {
        "agent_trust_gold.attack.memory-unverified-001",
        "agent_trust_gold.attack.memory-revoked-001",
        "agent_trust_gold.attack.memory-cross-scope-001",
        "agent_trust_gold.attack.memory-expired-001",
        "agent_trust_gold.attack.memory-stale-revision-001",
    }
    assert memory.minimum_attack_denominator == 5

    multi_agent = agent_gates["duplicate_visible_assignment_result"]
    assert set(multi_agent.benign_control_case_ids) == {
        "agent_trust_gold.benign.fanout-join-001",
        "agent_trust_gold.benign.single-agent-simple-task-001",
    }
    assert multi_agent.minimum_benign_denominator == 2


def test_failure_positive_coverage_exactly_binds_all_attack_and_benign_cells() -> None:
    failure_contract = get_formal_suite_registry()[SuiteId.FAILURE_ISOLATION_MATRIX.value]
    assert len(failure_contract.hard_zero_gates) == 15
    experiment = failure_contract.experiments[0]
    coverage = {gate.gate_id: gate for gate in experiment.positive_coverage_gates}
    manifest = get_frozen_fault_manifest()
    attack_ids = {cell.cell_id for cell in manifest.attack_cells}
    benign_ids = {control.catalog_id for control in manifest.matched_benign_controls}
    attack_gate = coverage["failure_injection_hit_coverage"]
    benign_gate = coverage["failure_benign_catalog_hit_coverage"]
    assert set(attack_gate.case_ids) == attack_ids
    assert attack_gate.minimum_denominator == len(attack_ids) == 40
    assert attack_gate.minimum_rate == 1.0
    assert set(benign_gate.case_ids) == benign_ids
    assert benign_gate.minimum_denominator == len(benign_ids) == 18
    assert benign_gate.minimum_rate == 1.0


def test_unimplemented_suites_have_honest_formal_not_available_manifests() -> None:
    contracts = render_suite_contracts()
    budget = render_global_real_model_budget()
    for suite_id, raw_manifest in render_suite_manifests().items():
        manifest = FormalEvalManifest.model_validate_json(raw_manifest)
        assert manifest.suite_id == suite_id
        assert manifest.status is EvalStatus.NOT_AVAILABLE
        assert manifest.scorer is None
        assert sum(metric.primary for metric in manifest.metrics) == 1
        assert [metric.name for metric in manifest.metrics if metric.primary] == [
            get_formal_suite_registry()[suite_id].suite_primary_metric
        ]
        expected_artifact_count = 4 if suite_id == SuiteId.FAILURE_ISOLATION_MATRIX.value else 3
        assert len(manifest.artifacts) == expected_artifact_count
        artifact_by_path = {artifact.path: artifact for artifact in manifest.artifacts}
        contract_artifact = artifact_by_path["contract.json"]
        assert contract_artifact.sha256 == hashlib.sha256(contracts[suite_id]).hexdigest()
        assert contract_artifact.byte_count == len(contracts[suite_id])
        budget_artifact = artifact_by_path["../../global_real_model_budget.json"]
        assert budget_artifact.sha256 == hashlib.sha256(budget).hexdigest()
        assert budget_artifact.byte_count == len(budget)
        hard_zero = get_formal_suite_registry()[suite_id].hard_zero_catalog
        hard_zero_artifact = artifact_by_path[hard_zero.path]
        assert hard_zero_artifact.sha256 == hard_zero.sha256
        assert hard_zero_artifact.byte_count == hard_zero.byte_count
        fault = get_formal_suite_registry()[suite_id].fault_manifest
        if suite_id == SuiteId.FAILURE_ISOLATION_MATRIX.value:
            assert fault is not None
            assert artifact_by_path[fault.path].sha256 == fault.sha256
            assert artifact_by_path[fault.path].byte_count == fault.byte_count
        else:
            assert fault is None
        validate_manifest_against_registry(manifest)


def test_frozen_suite_files_are_deterministic_and_match_committed_bytes() -> None:
    assert render_global_real_model_budget() == render_global_real_model_budget()
    assert (_FORMAL_DIR / "global_real_model_budget.json").read_bytes() == (
        render_global_real_model_budget()
    )
    assert render_frozen_suite_files() == render_frozen_suite_files()
    assert render_frozen_formal_files() == render_frozen_formal_files()
    verify_frozen_suite_manifests(_SUITE_DIR)


@pytest.mark.parametrize("target", ["manifest.json", "contract.json"])
def test_one_byte_suite_artifact_tamper_is_detected(tmp_path: Path, target: str) -> None:
    formal_root = tmp_path / "formal"
    suite_root = formal_root / "suites"
    for relative_path, content in render_frozen_suite_files().items():
        path = suite_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (formal_root / "global_real_model_budget.json").write_bytes(render_global_real_model_budget())
    tampered = suite_root / SuiteId.AGENT_TRUST_GOLD.value / target
    tampered.write_bytes(tampered.read_bytes() + b" ")

    with pytest.raises(FrozenSuiteMismatchError, match=f"{target}: byte mismatch"):
        verify_frozen_suite_manifests(suite_root)


def test_one_byte_global_budget_tamper_is_detected(tmp_path: Path) -> None:
    formal_root = tmp_path / "formal"
    for relative_path, content in render_frozen_formal_files().items():
        path = formal_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    budget_path = formal_root / "global_real_model_budget.json"
    budget_path.write_bytes(budget_path.read_bytes() + b" ")

    with pytest.raises(
        FrozenSuiteMismatchError,
        match=r"\.\./global_real_model_budget\.json: byte mismatch",
    ):
        verify_frozen_suite_manifests(formal_root / "suites")


def test_manifest_with_multiple_primary_metrics_is_rejected() -> None:
    raw = render_suite_manifests()[SuiteId.AGENT_TRUST_GOLD.value]
    payload = json.loads(raw)
    secondary = next(metric for metric in payload["metrics"] if not metric["primary"])
    secondary["primary"] = True
    manifest = FormalEvalManifest.model_validate(payload)

    with pytest.raises(SuiteRegistryError, match="MANIFEST_MUST_HAVE_EXACTLY_ONE_PRIMARY_METRIC"):
        validate_manifest_against_registry(manifest)


@pytest.mark.parametrize(
    ("suite_id", "dimension", "required_gate"),
    [
        (
            SuiteId.GAME_PLANNER_VERTICAL_E2E.value,
            EvaluationDimension.TRIAGE.value,
            "triage_non_abstain_coverage",
        ),
        (
            SuiteId.GAME_PLANNER_VERTICAL_E2E.value,
            EvaluationDimension.GROUNDED_DRAFT.value,
            "grounded_draft_non_refusal_coverage",
        ),
        (
            SuiteId.GAME_PLANNER_VERTICAL_E2E.value,
            EvaluationDimension.RAG.value,
            "rag_answerable_coverage",
        ),
        (
            SuiteId.AGENT_TRUST_GOLD.value,
            EvaluationDimension.MEMORY.value,
            "memory_delivered_positive_coverage",
        ),
        (
            SuiteId.SEMANTIC_SHADOW_EVAL.value,
            EvaluationDimension.SEMANTIC_REVIEW.value,
            "semantic_candidate_recall_coverage",
        ),
    ],
)
def test_all_abstain_refusal_or_no_recall_contract_cannot_drop_coverage_gate(
    suite_id: str, dimension: str, required_gate: str
) -> None:
    payload, index = _experiment_payload(suite_id, dimension)
    experiments = payload["experiments"]
    assert isinstance(experiments, list)
    experiment = experiments[index]
    assert isinstance(experiment, dict)
    gates = experiment["positive_coverage_gates"]
    assert isinstance(gates, list) and gates
    gates[0]["gate_id"] = f"tampered-{required_gate}"

    with pytest.raises(ValidationError, match=f"MISSING_ANTI_DEGENERACY_GATE:{required_gate}"):
        FormalSuiteContract.model_validate(payload)


def test_dev_dataset_cannot_be_renamed_as_sealed() -> None:
    payload, index = _experiment_payload(
        SuiteId.GAME_PLANNER_VERTICAL_E2E.value, EvaluationDimension.TRIAGE.value
    )
    experiments = payload["experiments"]
    assert isinstance(experiments, list)
    experiment = experiments[index]
    assert isinstance(experiment, dict)
    freeze = experiment["data_freeze"]
    assert isinstance(freeze, dict)
    sealed = freeze["sealed"]
    assert isinstance(sealed, dict)
    sealed["origin"] = "renamed_dev"

    with pytest.raises(ValidationError, match="SEALED_CANNOT_BE_RENAMED_DEV"):
        FormalSuiteContract.model_validate(payload)


def test_subjective_sealed_contract_cannot_hide_missing_external_blocker() -> None:
    payload, index = _experiment_payload(
        SuiteId.AGENT_TRUST_GOLD.value, EvaluationDimension.PERSONA.value
    )
    experiments = payload["experiments"]
    assert isinstance(experiments, list)
    experiment = experiments[index]
    assert isinstance(experiment, dict)
    blockers = experiment["external_data_blockers"]
    assert isinstance(blockers, list)
    blockers.pop()

    with pytest.raises(
        ValidationError, match="MISSING_EXTERNAL_BLOCKERS_FOR_SUBJECTIVE_SEALED_QUALITY"
    ):
        FormalSuiteContract.model_validate(payload)


def test_contract_sidecar_json_is_canonical_and_contains_no_pass_claim() -> None:
    for raw in render_suite_contracts().values():
        payload = json.loads(raw)
        canonical = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        assert canonical == raw.decode()
        assert '"status": "PASS"' not in raw.decode()
