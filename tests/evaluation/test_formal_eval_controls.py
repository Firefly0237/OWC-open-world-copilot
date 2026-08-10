"""Conformance tests for the frozen harness-only formal evaluator controls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from owcopilot.evaluation.formal_controls import (
    ControlImplementation,
    ControlObservation,
    DeclaredControlKind,
    FormalControlFixture,
    FrozenControlMismatchError,
    evaluate_control_implementation,
    get_frozen_control_fixtures,
    render_control_fixtures,
    verify_frozen_control_fixtures,
)
from owcopilot.evaluation.protocol import EvalProvider, EvalStatus, MetricScope

_CONTROL_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "formal" / "controls"
_SUITES = (
    "game_planner_vertical_e2e",
    "agent_trust_gold",
    "failure_isolation_matrix",
    "semantic_shadow_eval",
)
_PAYLOAD_HASHES = {
    "agent_trust_gold": "3b82e90c3907d889d3871189a187d1268c02ea0a1cd2f0f9b220bff76d05bae0",
    "failure_isolation_matrix": "79faef892c1feb2ffd60d8c779991bc14e2c69ce070ed8974c230c938c48b09b",
    "game_planner_vertical_e2e": "41e873bb4150de77bb24bc031f5dd47d5693ef244bf648619b8560c8b6d44483",
    "semantic_shadow_eval": "c3e242629e478d32b221ca6db32c093359c0ef91975225119d6e7795bf33c6e7",
}


def _fixture(suite_id: str) -> FormalControlFixture:
    return get_frozen_control_fixtures()[suite_id]


def _implementation(
    fixture: FormalControlFixture, kind: DeclaredControlKind
) -> ControlImplementation:
    return next(item for item in fixture.implementations if item.declared_kind is kind)


def _replace_implementation(
    fixture: FormalControlFixture, replacement: ControlImplementation
) -> FormalControlFixture:
    return fixture.model_copy(
        update={
            "implementations": tuple(
                replacement if item.implementation_id == replacement.implementation_id else item
                for item in fixture.implementations
            )
        }
    )


@pytest.mark.parametrize("suite_id", _SUITES)
def test_same_fixture_makes_good_pass_and_known_bad_mutant_fail(suite_id: str) -> None:
    fixture = _fixture(suite_id)
    assert fixture.provider is EvalProvider.FIXTURE
    assert fixture.metric_scope is MetricScope.HARNESS
    assert fixture.data_partition == "harness_control"
    assert fixture.production_path_exercised is False
    assert fixture.real_model_used is False
    assert fixture.quality_effects_allowed is False
    assert {case.role.value for case in fixture.cases} == {"attack", "benign"}

    case_ids = {case.case_id for case in fixture.cases}
    for implementation in fixture.implementations:
        assert {item.case_id for item in implementation.observations} == case_ids
        result = evaluate_control_implementation(fixture, implementation.implementation_id)
        assert result.harness_status is implementation.expected_harness_status
        assert result.quality_evaluation_status is EvalStatus.NOT_RUN
        assert result.quality_effect_metrics == ()

    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    mutant = _implementation(fixture, DeclaredControlKind.FROZEN_MUTANT)
    assert evaluate_control_implementation(fixture, good.implementation_id).harness_status is (
        EvalStatus.PASS
    )
    assert evaluate_control_implementation(fixture, mutant.implementation_id).harness_status is (
        EvalStatus.FAIL
    )


@pytest.mark.parametrize("suite_id", _SUITES)
def test_declared_labels_expected_statuses_and_descriptive_ids_do_not_drive_result(
    suite_id: str,
) -> None:
    fixture = _fixture(suite_id)
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD).model_copy(
        update={
            "implementation_id": f"{suite_id}.opaque.a",
            "declared_kind": DeclaredControlKind.FROZEN_MUTANT,
            "expected_harness_status": EvalStatus.FAIL,
        }
    )
    mutant = _implementation(fixture, DeclaredControlKind.FROZEN_MUTANT).model_copy(
        update={
            "implementation_id": f"{suite_id}.opaque.b",
            "declared_kind": DeclaredControlKind.MATCHED_GOOD,
            "expected_harness_status": EvalStatus.PASS,
        }
    )
    relabeled = fixture.model_copy(update={"implementations": (good, mutant)})

    assert evaluate_control_implementation(relabeled, good.implementation_id).harness_status is (
        EvalStatus.PASS
    )
    assert evaluate_control_implementation(relabeled, mutant.implementation_id).harness_status is (
        EvalStatus.FAIL
    )


def test_fixture_collections_are_tuples_not_mutable_lists() -> None:
    for fixture in get_frozen_control_fixtures().values():
        assert isinstance(fixture.cases, tuple)
        assert isinstance(fixture.implementations, tuple)
        assert all(isinstance(item.observations, tuple) for item in fixture.implementations)
        assert not hasattr(fixture.cases, "append")
        assert not hasattr(fixture.implementations, "append")


def test_defensive_copy_prevents_nested_mutation_from_poisoning_frozen_source() -> None:
    first = _fixture("agent_trust_gold")
    good = _implementation(first, DeclaredControlKind.MATCHED_GOOD)
    good.observations[0].outcome["verifier_accepted"] = True

    fresh = _fixture("agent_trust_gold")
    fresh_good = _implementation(fresh, DeclaredControlKind.MATCHED_GOOD)
    assert fresh_good.observations[0].outcome["verifier_accepted"] is False
    assert evaluate_control_implementation(fresh, fresh_good.implementation_id).harness_status is (
        EvalStatus.PASS
    )


def test_vertical_control_uses_real_signed_intake_contract_vocabulary() -> None:
    fixture = _fixture("game_planner_vertical_e2e")
    attack = next(case for case in fixture.cases if case.role.value == "attack")
    benign = next(case for case in fixture.cases if case.role.value == "benign")
    assert attack.stimulus["signed_contract_provenance"] == "licensed"
    assert attack.stimulus["client_provenance_claim"] == "own_ip"
    assert benign.stimulus["signed_contract_provenance"] == "licensed"
    assert "reference" not in json.dumps(fixture.model_dump(mode="json"))


def test_frozen_control_bytes_and_embedded_payload_hashes_match() -> None:
    verify_frozen_control_fixtures(_CONTROL_DIR)
    rendered = render_control_fixtures()
    assert set(rendered) == {f"{suite_id}/cases.json" for suite_id in _SUITES}
    for suite_id, fixture in get_frozen_control_fixtures().items():
        relative_path = f"{suite_id}/cases.json"
        assert (_CONTROL_DIR / relative_path).read_bytes() == rendered[relative_path]
        payload = fixture.model_dump(mode="json")
        supplied_hash = payload.pop("payload_sha256")
        payload_bytes = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        assert supplied_hash == hashlib.sha256(payload_bytes).hexdigest()
        assert supplied_hash == _PAYLOAD_HASHES[suite_id]


def test_one_byte_fixture_tamper_is_rejected(tmp_path: Path) -> None:
    for relative_path, content in render_control_fixtures().items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    target = tmp_path / "agent_trust_gold" / "cases.json"
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(FrozenControlMismatchError, match="byte mismatch"):
        verify_frozen_control_fixtures(tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_partition", "dev"),
        ("provider", "real_model"),
        ("production_path_exercised", True),
        ("real_model_used", True),
        ("quality_effects_allowed", True),
    ],
)
def test_fixture_cannot_claim_dev_sealed_production_real_model_or_quality(
    field: str, value: object
) -> None:
    payload = _fixture("agent_trust_gold").model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        FormalControlFixture.model_validate(payload)


def test_empty_case_set_is_not_run() -> None:
    fixture = _fixture("agent_trust_gold")
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    empty = fixture.model_copy(update={"cases": ()})
    result = evaluate_control_implementation(empty, good.implementation_id)
    assert result.harness_status is EvalStatus.NOT_RUN
    assert result.status_reasons == ("NO_CONTROL_CASES",)


def test_duplicate_case_id_is_invalid() -> None:
    fixture = _fixture("agent_trust_gold")
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    duplicate = fixture.cases[1].model_copy(update={"case_id": fixture.cases[0].case_id})
    changed = fixture.model_copy(update={"cases": (fixture.cases[0], duplicate)})
    result = evaluate_control_implementation(changed, good.implementation_id)
    assert result.harness_status is EvalStatus.INVALID
    assert result.status_reasons == ("DUPLICATE_CONTROL_CASE_ID",)


@pytest.mark.parametrize("kept_role", ["attack", "benign"])
def test_zero_attack_or_benign_denominator_is_not_run(kept_role: str) -> None:
    fixture = _fixture("agent_trust_gold")
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    cases = tuple(case for case in fixture.cases if case.role.value == kept_role)
    case_ids = {case.case_id for case in cases}
    observations = tuple(item for item in good.observations if item.case_id in case_ids)
    changed_good = good.model_copy(update={"observations": observations})
    changed = _replace_implementation(fixture.model_copy(update={"cases": cases}), changed_good)
    result = evaluate_control_implementation(changed, good.implementation_id)
    assert result.harness_status is EvalStatus.NOT_RUN
    assert result.status_reasons == ("ZERO_ATTACK_OR_BENIGN_DENOMINATOR",)


def test_duplicate_or_missing_observation_is_invalid() -> None:
    fixture = _fixture("agent_trust_gold")
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    duplicate = good.model_copy(update={"observations": (good.observations[0],) * 2})
    duplicate_fixture = _replace_implementation(fixture, duplicate)
    assert (
        evaluate_control_implementation(duplicate_fixture, good.implementation_id).harness_status
        is EvalStatus.INVALID
    )

    missing = good.model_copy(update={"observations": (good.observations[0],)})
    missing_fixture = _replace_implementation(fixture, missing)
    assert (
        evaluate_control_implementation(missing_fixture, good.implementation_id).harness_status
        is EvalStatus.INVALID
    )


def test_unknown_implementation_is_not_available() -> None:
    result = evaluate_control_implementation(_fixture("agent_trust_gold"), "missing-control")
    assert result.harness_status is EvalStatus.NOT_AVAILABLE
    assert result.status_reasons == ("CONTROL_IMPLEMENTATION_NOT_FOUND",)


def _replace_failure_attack_outcome(**patch: object) -> tuple[FormalControlFixture, str]:
    fixture = _fixture("failure_isolation_matrix")
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    observations: list[ControlObservation] = []
    for observation in good.observations:
        if observation.case_id.endswith("attack.001"):
            outcome = dict(observation.outcome)
            outcome.update(patch)
            observation = ControlObservation(case_id=observation.case_id, outcome=outcome)
        observations.append(observation)
    changed_good = good.model_copy(update={"observations": tuple(observations)})
    return _replace_implementation(fixture, changed_good), good.implementation_id


def test_unhit_fault_reported_honestly_is_not_run() -> None:
    fixture, implementation_id = _replace_failure_attack_outcome(
        injection_hit_count=0,
        boundary_a_completed=False,
        boundary_b_executed=False,
        reported_harness_status="NOT_RUN",
    )
    result = evaluate_control_implementation(fixture, implementation_id)
    assert result.harness_status is EvalStatus.NOT_RUN
    assert any("FAULT_NOT_PROVEN" in reason for reason in result.status_reasons)


def test_fault_hit_more_than_once_is_invalid() -> None:
    fixture, implementation_id = _replace_failure_attack_outcome(injection_hit_count=2)
    result = evaluate_control_implementation(fixture, implementation_id)
    assert result.harness_status is EvalStatus.INVALID
    assert any("FAULT_INJECTION_HIT_MORE_THAN_ONCE" in reason for reason in result.status_reasons)


def test_false_pass_on_zero_denominators_and_unhit_fault_is_failed() -> None:
    fixture = _fixture("failure_isolation_matrix")
    mutant = _implementation(fixture, DeclaredControlKind.FROZEN_MUTANT)
    result = evaluate_control_implementation(fixture, mutant.implementation_id)
    assert result.harness_status is EvalStatus.FAIL
    assert any("ZERO_OR_UNHIT_FAULT_FALSE_PASS" in reason for reason in result.status_reasons)


def test_zero_semantic_candidates_are_not_clean() -> None:
    fixture = _fixture("semantic_shadow_eval")
    good = _implementation(fixture, DeclaredControlKind.MATCHED_GOOD)
    mutant = _implementation(fixture, DeclaredControlKind.FROZEN_MUTANT)
    assert evaluate_control_implementation(fixture, good.implementation_id).harness_status is (
        EvalStatus.PASS
    )
    mutant_result = evaluate_control_implementation(fixture, mutant.implementation_id)
    assert mutant_result.harness_status is EvalStatus.FAIL
    assert any(
        "ZERO_CANDIDATES_MISREPORTED_AS_CLEAN" in reason for reason in mutant_result.status_reasons
    )
