"""Frozen fault-manifest and physical hard-zero catalog governance tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from owcopilot.evaluation.formal_artifacts import (
    default_formal_root,
    verify_frozen_formal_artifacts,
)
from owcopilot.evaluation.formal_faults import (
    FAULT_MANIFEST_ARTIFACT_PATH,
    FaultInjectionContract,
    FaultInjectionMechanism,
    FrozenFaultManifest,
    FrozenFaultManifestMismatchError,
    get_frozen_fault_manifest,
    render_fault_manifest,
    render_frozen_fault_files,
    validate_fault_manifest_against_hard_zero_catalog,
    validate_fault_manifest_against_hard_zero_gates,
    verify_frozen_fault_manifest,
)
from owcopilot.evaluation.formal_hard_zero import (
    FrozenHardZeroCatalog,
    FrozenHardZeroMismatchError,
    HardZeroRole,
    get_frozen_hard_zero_catalogs,
    render_hard_zero_catalogs,
    validate_hard_zero_catalogs_against_gates,
    verify_frozen_hard_zero_catalogs,
)
from owcopilot.evaluation.formal_registry import (
    EvaluationDimension,
    SuiteId,
    get_formal_suite_registry,
    render_suite_manifests,
)
from owcopilot.evaluation.protocol import EvalStatus, FormalEvalManifest

_FORMAL_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "formal"


def _gates_by_suite() -> dict[str, tuple[dict[str, object], ...]]:
    return {
        suite_id: tuple(gate.model_dump(mode="json") for gate in contract.hard_zero_gates)
        for suite_id, contract in get_formal_suite_registry().items()
    }


def _codes(result: object) -> set[str]:
    return {error.code for error in result.errors}  # type: ignore[attr-defined]


def test_every_registry_hard_zero_name_has_one_physical_unexecuted_case() -> None:
    catalogs = get_frozen_hard_zero_catalogs()
    gates = _gates_by_suite()
    validate_hard_zero_catalogs_against_gates(catalogs, gates)

    assert len(catalogs) == 4
    assert sum(len(catalog.cases) for catalog in catalogs.values()) == 86
    assert sum(len(suite_gates) for suite_gates in gates.values()) == 25
    for catalog in catalogs.values():
        assert catalog.implementation_status is EvalStatus.NOT_AVAILABLE
        assert catalog.production_path_exercised is False
        assert catalog.run_status is EvalStatus.NOT_RUN
        for case in catalog.cases:
            assert case.stimulus.setup
            assert case.stimulus.trigger
            assert case.stimulus.production_entrypoint
            assert case.expected_outcome.decision
            assert case.expected_outcome.observable_effect_constraint
            assert case.oracle_requirements
            assert case.evidence_requirements
            assert case.implementation_status is EvalStatus.NOT_AVAILABLE
            assert case.production_path_exercised is False
            assert case.run_status is EvalStatus.NOT_RUN


def test_fault_manifest_has_exact_40_attack_and_18_independent_benign_cells() -> None:
    manifest = get_frozen_fault_manifest()
    assert manifest.implementation_status is EvalStatus.NOT_AVAILABLE
    assert manifest.production_path_exercised is False
    assert manifest.run_status is EvalStatus.NOT_RUN
    assert len(manifest.attack_cells) == 40
    assert len(manifest.matched_benign_controls) == 18

    topology = manifest.required_topology
    assert topology.implementation_status is EvalStatus.NOT_AVAILABLE
    assert topology.production_path_exercised is False
    assert topology.future_production_path_required is True
    assert topology.postgresql == "real_postgresql"
    assert topology.vector_index == "real_pgvector"
    assert topology.delivery_queue == "real_redis_streams"
    assert (topology.tenant_count, topology.application_instance_count) == (2, 2)
    assert manifest.execution_rules.synthetic_fixture_may_prove_production_recovery is False
    assert manifest.execution_rules.fault_script_is_sealed_quality_data is False
    assert manifest.execution_rules.uniform_fast_failure_is_valid_recovery is False

    for cell in manifest.attack_cells:
        assert cell.production_target.implementation_status is EvalStatus.NOT_AVAILABLE
        assert cell.production_target.production_path_exercised is False
        assert cell.injection.expected_hit_count == 1
        assert cell.injection.expected_observation
        assert cell.injection.non_exact_hit_status is EvalStatus.NOT_RUN
        assert cell.boundary.action_a.durable_evidence_required is True
        assert cell.boundary.action_a_completed_required is True
        assert cell.boundary.action_b_not_completed_required is True
        assert cell.boundary.action_a.evidence_requirements
        assert cell.recovery.recovery_entrypoint
        assert cell.recovery.expected_terminal_or_continuing_states
        assert cell.recovery.deadline.fixed_duration_ms or cell.recovery.deadline.formula
        assert cell.maximum_observable_effects
        assert cell.oracle.oracle_requirements
        assert cell.oracle.evidence_requirements
        assert cell.run_status is EvalStatus.NOT_RUN

    for control in manifest.matched_benign_controls:
        assert control.catalog_hit_count == 1
        assert control.is_fault_injection is False
        assert control.normal_path_trigger
        assert control.expected_terminal_state
        assert control.normal_path_deadline.fixed_duration_ms
        assert control.expected_observable_effects
        assert control.run_status is EvalStatus.NOT_RUN


def test_fault_manifest_binds_every_required_plan_cell_and_matched_benign_control() -> None:
    manifest = get_frozen_fault_manifest()
    attack_ids = {cell.cell_id for cell in manifest.attack_cells}
    benign_ids = {control.catalog_id for control in manifest.matched_benign_controls}
    expected_suffixes = {
        "tenant-a-to-b-read-001",
        "tenant-a-to-b-write-001",
        "kill-release-before-commit-001",
        "kill-release-boundary-001",
        "same-base-concurrent-release-001",
        "redeliver-outbox-001",
        "redis-transient-outage-001",
        "relay-restart-before-delivery-001",
        "relay-restart-after-delivery-before-mark-001",
        "projection-worker-kill-after-claim-001",
        "projection-exit-before-ack-001",
        "projection-worker-kill-after-ack-001",
        "projection-stale-fencing-token-001",
        "worker-kill-after-claim-001",
        "worker-kill-before-result-001",
        "exit-before-ack-001",
        "verifier-restart-before-verdict-001",
        "assignment-stale-fencing-token-001",
        "assignment-redelivery-001",
        "agent-kill-before-tool-001",
        "agent-kill-after-tool-001",
        "agent-kill-before-checkpoint-001",
        "agent-kill-after-checkpoint-001",
        "agent-kill-at-hitl-pause-001",
        "agent-kill-before-reflection-001",
        "agent-kill-after-reflection-001",
        "sse-revoked-membership-001",
        "relay-cross-tenant-payload-001",
        "worker-cross-tenant-payload-001",
        "projection-failure-001",
        "publish-idempotency-replay-001",
        "vector-reconciliation-duplicate-001",
        "community-cross-revision-corruption-001",
        "assignment-bad-payload-001",
        "assignment-policy-rejection-001",
        "assignment-explicit-cancellation-001",
        "assignment-retry-budget-exhaustion-001",
        "instance-a-exit-instance-b-reclaim-001",
        "assignment-outbox-kill-before-commit-001",
        "assignment-outbox-kill-after-commit-before-delivery-001",
    }
    assert attack_ids == {
        f"failure_isolation_matrix.attack.{suffix}" for suffix in expected_suffixes
    }
    assert {entry.plan_reference for entry in manifest.plan_coverage} == {
        "§3.3",
        "N2.2",
        "N2.3",
        "N4.2",
        "N4.4",
    }
    assert {
        cell_id for entry in manifest.plan_coverage for cell_id in entry.attack_cell_ids
    } == attack_ids
    assert {
        control_id
        for entry in manifest.plan_coverage
        for control_id in entry.matched_benign_control_ids
    } == benign_ids


def test_plan_coverage_is_one_to_one_and_cannot_duplicate_or_omit_cells() -> None:
    manifest = get_frozen_fault_manifest()

    duplicate_attack = manifest.model_dump(mode="json")
    n22 = next(
        item for item in duplicate_attack["plan_coverage"] if item["plan_reference"] == "N2.2"
    )
    n23 = next(
        item for item in duplicate_attack["plan_coverage"] if item["plan_reference"] == "N2.3"
    )
    n22["attack_cell_ids"].append(n23["attack_cell_ids"][0])
    with pytest.raises(ValidationError, match="FAULT_PLAN_ATTACK_COVERAGE_MUST_BE_ONE_TO_ONE"):
        FrozenFaultManifest.model_validate(duplicate_attack)

    duplicate_benign = manifest.model_dump(mode="json")
    n22 = next(
        item for item in duplicate_benign["plan_coverage"] if item["plan_reference"] == "N2.2"
    )
    n23 = next(
        item for item in duplicate_benign["plan_coverage"] if item["plan_reference"] == "N2.3"
    )
    n22["matched_benign_control_ids"].append(n23["matched_benign_control_ids"][0])
    with pytest.raises(ValidationError, match="FAULT_PLAN_BENIGN_COVERAGE_MUST_BE_ONE_TO_ONE"):
        FrozenFaultManifest.model_validate(duplicate_benign)

    missing_attack = manifest.model_dump(mode="json")
    missing_attack["plan_coverage"][0]["attack_cell_ids"].pop()
    with pytest.raises(ValidationError, match="FAULT_PLAN_ATTACK_COVERAGE_MUST_BE_EXACT"):
        FrozenFaultManifest.model_validate(missing_attack)

    missing_benign = manifest.model_dump(mode="json")
    missing_benign["plan_coverage"][0]["matched_benign_control_ids"].pop()
    with pytest.raises(ValidationError, match="FAULT_PLAN_BENIGN_COVERAGE_MUST_BE_EXACT"):
        FrozenFaultManifest.model_validate(missing_benign)


def test_every_attack_freezes_nonzero_progress_and_explicit_effect_ranges() -> None:
    manifest = get_frozen_fault_manifest()
    for cell in manifest.attack_cells:
        assert any(effect.minimum_count > 0 for effect in cell.maximum_observable_effects)
        assert all(
            0 <= effect.minimum_count <= effect.maximum_count
            for effect in cell.maximum_observable_effects
        )
        all_zero_observation = {effect.effect: 0 for effect in cell.maximum_observable_effects}
        satisfies_ranges = all(
            effect.minimum_count <= all_zero_observation[effect.effect] <= effect.maximum_count
            for effect in cell.maximum_observable_effects
        )
        assert satisfies_ranges is False

    rendered = json.loads(render_fault_manifest())
    assert all(
        "minimum_count" in effect and "maximum_count" in effect
        for cell in rendered["attack_cells"]
        for effect in cell["maximum_observable_effects"]
    )

    missing_minimum = manifest.model_dump(mode="json")
    missing_minimum["attack_cells"][0]["maximum_observable_effects"][0].pop("minimum_count")
    with pytest.raises(ValidationError, match="Field required"):
        FrozenFaultManifest.model_validate(missing_minimum)

    invalid_range = manifest.model_dump(mode="json")
    effect = invalid_range["attack_cells"][0]["maximum_observable_effects"][0]
    effect["minimum_count"] = effect["maximum_count"] + 1
    with pytest.raises(ValidationError, match="FAULT_EFFECT_RANGE_INVALID"):
        FrozenFaultManifest.model_validate(invalid_range)


def test_n42_recovery_bindings_and_budget_monotonicity_are_machine_checked() -> None:
    manifest = get_frozen_fault_manifest()
    agent_cells = [
        cell
        for cell in manifest.attack_cells
        if cell.hard_zero_gate_id == "durable_agent_run_resume"
    ]
    assert len(agent_cells) == 7
    for cell in agent_cells:
        effects = {
            effect.effect: (effect.minimum_count, effect.maximum_count)
            for effect in cell.maximum_observable_effects
        }
        assert effects["budget_resets"] == (0, 0)
        assert effects["budget_state_monotonicity_checks"] == (1, 1)
        assert len(cell.recovery.required_identity_bindings) == 3
        assert any(
            binding.comparison == "monotonic_non_decreasing"
            for binding in cell.recovery.required_identity_bindings
        )

    by_id = {cell.cell_id: cell for cell in agent_cells}
    assert {
        binding.binding_id
        for binding in by_id[
            "failure_isolation_matrix.attack.agent-kill-before-checkpoint-001"
        ].recovery.required_identity_bindings
    } >= {"observation_checkpoint_hash_identity"}
    assert {
        binding.binding_id
        for binding in by_id[
            "failure_isolation_matrix.attack.agent-kill-after-checkpoint-001"
        ].recovery.required_identity_bindings
    } >= {"checkpoint_context_hash_identity", "checkpoint_approval_hash_identity"}
    assert {
        binding.binding_id
        for binding in by_id[
            "failure_isolation_matrix.attack.agent-kill-after-reflection-001"
        ].recovery.required_identity_bindings
    } >= {"reflection_next_attempt_hash_identity"}

    missing_binding = manifest.model_dump(mode="json")
    target = next(
        cell
        for cell in missing_binding["attack_cells"]
        if cell["cell_id"].endswith("agent-kill-before-checkpoint-001")
    )
    target["recovery"]["required_identity_bindings"].pop()
    with pytest.raises(ValidationError, match="FAULT_REQUIRED_TYPED_IDENTITY_BINDINGS_MISMATCH"):
        FrozenFaultManifest.model_validate(missing_binding)

    tampered_binding = manifest.model_dump(mode="json")
    target = next(
        cell
        for cell in tampered_binding["attack_cells"]
        if cell["cell_id"].endswith("instance-a-exit-instance-b-reclaim-001")
    )
    source_recovery = next(
        binding
        for binding in target["recovery"]["required_identity_bindings"]
        if binding["binding_id"] == "source_recovery_instance_identity"
    )
    source_recovery["comparison"] = "exact_equal"
    with pytest.raises(ValidationError, match="FAULT_REQUIRED_IDENTITY_COMPARISON_MISMATCH"):
        FrozenFaultManifest.model_validate(tampered_binding)


def test_idempotency_reconciliation_terminal_and_cross_instance_cells_are_distinct() -> None:
    manifest = get_frozen_fault_manifest()
    by_id = {cell.cell_id: cell for cell in manifest.attack_cells}

    replay = by_id["failure_isolation_matrix.attack.publish-idempotency-replay-001"]
    assert replay.injection.mechanism is FaultInjectionMechanism.IDEMPOTENCY_REPLAY
    assert {
        effect.effect: effect.maximum_count for effect in replay.maximum_observable_effects
    } == {
        "visible_authoring_revisions": 1,
        "durable_outbox_rows": 1,
        "durable_ledger_effects": 1,
    }

    for case_id in (
        "failure_isolation_matrix.attack.vector-reconciliation-duplicate-001",
        "failure_isolation_matrix.attack.community-cross-revision-corruption-001",
    ):
        reconciliation = by_id[case_id]
        assert (
            reconciliation.injection.mechanism is FaultInjectionMechanism.DURABLE_STATE_CORRUPTION
        )
        assert any(
            "durable canonical source truth" in invariant
            for invariant in reconciliation.recovery.required_recovery_invariants
        )
        assert any(
            "durable source truth" in requirement
            for requirement in reconciliation.oracle.oracle_requirements
        )

    terminal_states = {
        "assignment-bad-payload-001": "FAILED_BAD_PAYLOAD",
        "assignment-policy-rejection-001": "FAILED_POLICY_REJECTION",
        "assignment-explicit-cancellation-001": "FAILED_EXPLICIT_CANCELLATION",
        "assignment-retry-budget-exhaustion-001": "FAILED_RETRY_BUDGET_EXHAUSTED",
    }
    for suffix, state in terminal_states.items():
        terminal = by_id[f"failure_isolation_matrix.attack.{suffix}"]
        assert terminal.recovery.expected_terminal_or_continuing_states == (state,)
        assert terminal.recovery.terminal_failed_allowed is True
        effects = {
            effect.effect: effect.maximum_count for effect in terminal.maximum_observable_effects
        }
        assert effects["terminal_failed_records"] == 1
        assert any(
            count == 0 for name, count in effects.items() if name != "terminal_failed_records"
        )

    cross_instance = by_id["failure_isolation_matrix.attack.instance-a-exit-instance-b-reclaim-001"]
    assert cross_instance.injection.mechanism is FaultInjectionMechanism.PROCESS_SIGNAL
    assert any(
        "identity A" in invariant and "identity B" in invariant
        for invariant in cross_instance.recovery.required_recovery_invariants
    )
    assert any(
        "source_instance_id != recovery_instance_id" in requirement
        for requirement in cross_instance.oracle.oracle_requirements
    )

    benign_ids = {control.catalog_id for control in manifest.matched_benign_controls}
    assert {
        "failure_isolation_matrix.benign.distinct-idempotency-publishes-001",
        "failure_isolation_matrix.benign.reconciliation-clean-repaired-001",
        "failure_isolation_matrix.benign.valid-assignment-terminal-done-001",
        "failure_isolation_matrix.benign.cross-instance-job-visible-001",
        "failure_isolation_matrix.benign.assignment-outbox-atomic-commit-001",
    } <= benign_ids


def test_projection_post_ack_and_assignment_outbox_atomicity_boundaries_are_physical() -> None:
    manifest = get_frozen_fault_manifest()
    by_id = {cell.cell_id: cell for cell in manifest.attack_cells}

    post_ack = by_id["failure_isolation_matrix.attack.projection-worker-kill-after-ack-001"]
    assert post_ack.injection.mechanism is FaultInjectionMechanism.PROCESS_SIGNAL
    assert post_ack.boundary.action_a.action_id == "projection_done_ready_and_ack_committed"
    assert post_ack.boundary.action_b.action_id == "duplicate_projection_side_effect_committed"
    post_ack_effects = {
        effect.effect: (effect.minimum_count, effect.maximum_count)
        for effect in post_ack.maximum_observable_effects
    }
    assert post_ack_effects["projection_results"] == (1, 1)
    assert post_ack_effects["stream_acknowledgements"] == (1, 1)
    assert post_ack_effects["duplicate_projection_side_effects"] == (0, 0)
    assert post_ack_effects["next_consumer_deliveries"] == (1, 1)
    assert "PostgreSQL DONE/READY query" in post_ack.recovery.recovery_entrypoint

    pre_commit = by_id["failure_isolation_matrix.attack.assignment-outbox-kill-before-commit-001"]
    assert pre_commit.boundary.action_a.action_id == "same_key_assignment_command_committed"
    assert pre_commit.boundary.action_b.action_id == "assignment_and_outbox_transaction_committed"
    assert any(
        "neither assignment nor outbox row is visible" in invariant
        for invariant in pre_commit.recovery.required_recovery_invariants
    )
    pre_effects = {
        effect.effect: (effect.minimum_count, effect.maximum_count)
        for effect in pre_commit.maximum_observable_effects
    }
    assert pre_effects["assignment_rows"] == (1, 1)
    assert pre_effects["durable_outbox_rows"] == (1, 1)
    assert pre_effects["assignment_outbox_half_states"] == (0, 0)

    post_commit = by_id[
        "failure_isolation_matrix.attack.assignment-outbox-kill-after-commit-before-delivery-001"
    ]
    assert post_commit.boundary.action_a.action_id == "assignment_and_outbox_transaction_committed"
    assert post_commit.boundary.action_b.action_id == "outbox_stream_delivery_started"
    assert post_commit.recovery.expected_terminal_or_continuing_states == ("DONE",)
    post_effects = {
        effect.effect: (effect.minimum_count, effect.maximum_count)
        for effect in post_commit.maximum_observable_effects
    }
    assert post_effects["assignment_rows"] == (1, 1)
    assert post_effects["durable_outbox_rows"] == (1, 1)
    assert post_effects["stream_identities"] == (1, 1)
    assert post_effects["terminal_done_results"] == (1, 1)
    assert post_effects["duplicate_assignment_effects"] == (0, 0)


def test_process_termination_is_only_legal_for_process_fault_mechanisms() -> None:
    manifest = get_frozen_fault_manifest()
    process_mechanisms = {
        FaultInjectionMechanism.PROCESS_SIGNAL,
        FaultInjectionMechanism.PROCESS_EXIT,
    }
    for cell in manifest.attack_cells:
        if cell.injection.mechanism in process_mechanisms:
            assert cell.injection.expected_process_termination is not None
            assert cell.injection.natural_exit_is_not_run is True
        else:
            assert cell.injection.expected_process_termination is None
            assert cell.injection.natural_exit_is_not_run is None

    non_process = next(
        cell.injection
        for cell in manifest.attack_cells
        if cell.injection.mechanism is FaultInjectionMechanism.REQUEST_SCOPE_ATTACK
    ).model_dump(mode="json")
    non_process["expected_process_termination"] = {"exit_code": 0, "signal": None}
    with pytest.raises(
        ValidationError, match="NON_PROCESS_FAULT_CANNOT_DECLARE_PROCESS_TERMINATION"
    ):
        FaultInjectionContract.model_validate(non_process)

    process = next(
        cell.injection
        for cell in manifest.attack_cells
        if cell.injection.mechanism is FaultInjectionMechanism.PROCESS_SIGNAL
    ).model_dump(mode="json")
    process["expected_process_termination"] = None
    with pytest.raises(
        ValidationError, match="PROCESS_FAULT_REQUIRES_EXACT_TERMINATION_AND_NATURAL_EXIT_RULE"
    ):
        FaultInjectionContract.model_validate(process)


def test_projection_and_release_recovery_freeze_eventual_ready_serving_bounds() -> None:
    manifest = get_frozen_fault_manifest()
    by_id = {cell.cell_id: cell for cell in manifest.attack_cells}
    projection = by_id["failure_isolation_matrix.attack.projection-failure-001"]
    release = by_id["failure_isolation_matrix.attack.kill-release-boundary-001"]

    assert projection.recovery.expected_terminal_or_continuing_states == ("SERVING_NEW_REVISION",)
    assert projection.recovery.deadline.formula == (
        "dependency_restore_ms + projection_retry_interval_ms + reconcile_interval_ms + "
        "recovery_margin_ms"
    )
    projection_effects = {
        effect.effect: effect.maximum_count for effect in projection.maximum_observable_effects
    }
    assert projection_effects["serving_pointer_advances_while_fault_active"] == 0
    assert projection_effects["serving_pointer_advances_total_after_recovery"] == 1
    assert any(
        "every required projection becomes READY" in item
        for item in projection.recovery.required_recovery_invariants
    )

    assert release.boundary.action_a.action_id == "revision_review_outbox_transaction_committed"
    assert release.boundary.action_b.action_id == (
        "required_projections_ready_and_serving_advanced"
    )
    assert release.recovery.expected_terminal_or_continuing_states == ("SERVING_NEW_REVISION",)
    release_effects = {
        effect.effect: effect.maximum_count for effect in release.maximum_observable_effects
    }
    assert release_effects["serving_pointer_advances_before_required_projections_ready"] == 0
    assert release_effects["serving_pointer_advances_total"] == 1


def test_fault_manifest_exactly_cross_links_failure_hard_zero_cases_and_denominators() -> None:
    manifest = get_frozen_fault_manifest()
    catalogs = get_frozen_hard_zero_catalogs()
    gates = _gates_by_suite()[SuiteId.FAILURE_ISOLATION_MATRIX.value]
    validate_fault_manifest_against_hard_zero_gates(manifest, gates)
    validate_fault_manifest_against_hard_zero_catalog(
        manifest, catalogs[SuiteId.FAILURE_ISOLATION_MATRIX.value]
    )

    failure_cases = catalogs[SuiteId.FAILURE_ISOLATION_MATRIX.value].cases
    assert {case.case_id for case in failure_cases if case.role is HardZeroRole.ATTACK} == {
        cell.cell_id for cell in manifest.attack_cells
    }
    assert {case.case_id for case in failure_cases if case.role is HardZeroRole.MATCHED_BENIGN} == {
        control.catalog_id for control in manifest.matched_benign_controls
    }


def test_failure_experiment_uses_fault_manifest_not_narrative_dev_sealed_slots() -> None:
    registry = get_formal_suite_registry()
    failure = registry[SuiteId.FAILURE_ISOLATION_MATRIX.value]
    assert len(failure.experiments) == 1
    assert failure.experiments[0].dimension is EvaluationDimension.FAILURE_ISOLATION
    assert failure.experiments[0].data_freeze is None
    assert failure.fault_manifest is not None
    assert failure.fault_manifest.path == FAULT_MANIFEST_ARTIFACT_PATH
    for suite_id, contract in registry.items():
        if suite_id == SuiteId.FAILURE_ISOLATION_MATRIX.value:
            continue
        assert contract.fault_manifest is None
        assert all(experiment.data_freeze is not None for experiment in contract.experiments)


def test_all_manifests_hash_pin_physical_hard_zero_and_failure_hash_pins_faults() -> None:
    hard_zero_bytes = render_hard_zero_catalogs()
    fault_bytes = render_fault_manifest()
    registry = get_formal_suite_registry()
    for suite_id, raw in render_suite_manifests().items():
        manifest = FormalEvalManifest.model_validate_json(raw)
        refs = {artifact.path: artifact for artifact in manifest.artifacts}
        hard_path = f"../../hard_zero/{suite_id}/cases.json"
        hard_content = hard_zero_bytes[f"{suite_id}/cases.json"]
        assert refs[hard_path].sha256 == hashlib.sha256(hard_content).hexdigest()
        assert refs[hard_path].byte_count == len(hard_content)
        if suite_id == SuiteId.FAILURE_ISOLATION_MATRIX.value:
            assert (
                refs[FAULT_MANIFEST_ARTIFACT_PATH].sha256 == hashlib.sha256(fault_bytes).hexdigest()
            )
            assert refs[FAULT_MANIFEST_ARTIFACT_PATH].byte_count == len(fault_bytes)
        assert "FORMAL_RUNNER_NOT_IMPLEMENTED" not in registry[suite_id].manifest_status_reasons


def test_canonical_fault_and_hard_zero_bytes_match_committed_files() -> None:
    assert render_fault_manifest() == render_fault_manifest()
    assert render_frozen_fault_files() == render_frozen_fault_files()
    assert render_hard_zero_catalogs() == render_hard_zero_catalogs()
    verify_frozen_fault_manifest(_FORMAL_ROOT / "faults")
    verify_frozen_hard_zero_catalogs(_FORMAL_ROOT / "hard_zero")


def test_one_byte_fault_and_hard_zero_tamper_is_detected(tmp_path: Path) -> None:
    fault_root = tmp_path / "faults"
    fault_path = fault_root / "failure_isolation_matrix" / "fault_manifest.json"
    fault_path.parent.mkdir(parents=True)
    fault_path.write_bytes(render_fault_manifest() + b" ")
    with pytest.raises(FrozenFaultManifestMismatchError, match="byte mismatch"):
        verify_frozen_fault_manifest(fault_root)

    hard_root = tmp_path / "hard_zero"
    for relative, content in render_hard_zero_catalogs().items():
        path = hard_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    target = hard_root / "agent_trust_gold" / "cases.json"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(FrozenHardZeroMismatchError, match="byte mismatch"):
        verify_frozen_hard_zero_catalogs(hard_root)


def test_missing_extra_and_zero_denominator_are_red() -> None:
    catalogs = get_frozen_hard_zero_catalogs()
    gates = _gates_by_suite()

    missing_payload = catalogs["semantic_shadow_eval"].model_dump(mode="json")
    missing_payload["cases"].pop()
    with pytest.raises(ValidationError, match="HARD_ZERO_GATE_REQUIRES_ATTACK_AND_MATCHED_BENIGN"):
        FrozenHardZeroCatalog.model_validate(missing_payload)

    extra_payload = catalogs["game_planner_vertical_e2e"].model_dump(mode="json")
    extra = dict(extra_payload["cases"][0])
    extra["case_id"] = "game_planner_vertical_e2e.attack.unregistered-extra-001"
    extra_payload["cases"].append(extra)
    extra_catalog = FrozenHardZeroCatalog.model_validate(extra_payload)
    catalogs["game_planner_vertical_e2e"] = extra_catalog
    with pytest.raises(ValueError, match="HARD_ZERO_ATTACK_CASE_SET_MISMATCH"):
        validate_hard_zero_catalogs_against_gates(catalogs, gates)

    zero_gates = _gates_by_suite()
    zero_gates["failure_isolation_matrix"][0]["minimum_attack_denominator"] = 0
    with pytest.raises(ValueError, match="FAULT_HARD_ZERO_DENOMINATOR_NOT_EXACT"):
        validate_fault_manifest_against_hard_zero_gates(
            get_frozen_fault_manifest(), zero_gates["failure_isolation_matrix"]
        )


def test_fault_manifest_cannot_drop_or_add_cells() -> None:
    payload = get_frozen_fault_manifest().model_dump(mode="json")
    payload["attack_cells"].pop()
    with pytest.raises(ValidationError):
        FrozenFaultManifest.model_validate(payload)

    payload = get_frozen_fault_manifest().model_dump(mode="json")
    extra = dict(payload["attack_cells"][0])
    extra["cell_id"] = "failure_isolation_matrix.attack.extra-001"
    extra["hard_zero_case_id"] = extra["cell_id"]
    payload["attack_cells"].append(extra)
    with pytest.raises(ValidationError):
        FrozenFaultManifest.model_validate(payload)


def test_governance_verifier_checks_missing_and_tampered_fault_catalogs(tmp_path: Path) -> None:
    formal_root = tmp_path / "formal"
    shutil.copytree(default_formal_root(), formal_root)
    fault_path = formal_root / "faults" / "failure_isolation_matrix" / "fault_manifest.json"
    fault_path.write_bytes(fault_path.read_bytes() + b" ")
    result = verify_frozen_formal_artifacts(formal_root)
    assert not result.ok
    assert "FAULT_MANIFEST_BYTE_MISMATCH" in _codes(result)
    assert "ARTIFACT_REFERENCE_HASH_MISMATCH" in _codes(result)

    shutil.rmtree(formal_root)
    shutil.copytree(default_formal_root(), formal_root)
    missing = formal_root / "hard_zero" / "semantic_shadow_eval" / "cases.json"
    missing.unlink()
    result = verify_frozen_formal_artifacts(formal_root)
    assert not result.ok
    assert "HARD_ZERO_CATALOG_MISSING" in _codes(result)


def test_policy_hash_binds_every_hard_zero_catalog_and_fault_manifest() -> None:
    policy_path = _FORMAL_ROOT / "policy" / "artifact_verification_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    refs = {reference["path"]: reference for reference in policy["artifact_references"]}
    for relative, content in render_hard_zero_catalogs().items():
        path = f"../hard_zero/{relative}"
        assert refs[path]["sha256"] == hashlib.sha256(content).hexdigest()
        assert refs[path]["byte_count"] == len(content)
        assert refs[path]["kind"] == "hard_zero_case_catalog"
    fault_content = render_fault_manifest()
    fault_path = "../faults/failure_isolation_matrix/fault_manifest.json"
    assert refs[fault_path]["sha256"] == hashlib.sha256(fault_content).hexdigest()
    assert refs[fault_path]["byte_count"] == len(fault_content)
    assert refs[fault_path]["kind"] == "fault_manifest"
