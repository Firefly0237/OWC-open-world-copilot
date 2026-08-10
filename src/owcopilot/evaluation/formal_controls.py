"""Frozen deterministic controls for the formal-evaluator harness.

These controls test whether an evaluator can distinguish a matched-good observation from a
known-bad observation on the same synthetic fixture.  They exercise harness semantics only: no
production path, sealed/dev data, real model, or quality-effect metric is represented here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .protocol import EvalProvider, EvalStatus, MetricScope

CONTROL_FIXTURE_SCHEMA_VERSION = "formal-control-fixture/v1"
CONTROL_RESULT_SCHEMA_VERSION = "formal-control-result/v1"

ControlSuiteId: TypeAlias = Literal[
    "game_planner_vertical_e2e",
    "agent_trust_gold",
    "failure_isolation_matrix",
    "semantic_shadow_eval",
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ControlCaseRole(str, Enum):
    ATTACK = "attack"
    BENIGN = "benign"


class DeclaredControlKind(str, Enum):
    """Fixture metadata only; the evaluator deliberately never branches on this value."""

    MATCHED_GOOD = "matched_good"
    FROZEN_MUTANT = "frozen_mutant"


class _ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _payload_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_json_bytes(dict(payload))).hexdigest()


class ControlCase(_ControlModel):
    case_id: str = Field(min_length=1)
    role: ControlCaseRole
    stimulus: dict[str, JsonValue]


class ControlObservation(_ControlModel):
    case_id: str = Field(min_length=1)
    outcome: dict[str, JsonValue]


class ControlImplementation(_ControlModel):
    implementation_id: str = Field(min_length=1)
    declared_kind: DeclaredControlKind
    expected_harness_status: EvalStatus
    observations: tuple[ControlObservation, ...]


class FormalControlFixture(_ControlModel):
    schema_version: Literal["formal-control-fixture/v1"]
    suite_id: ControlSuiteId
    fixture_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    provider: Literal[EvalProvider.FIXTURE]
    metric_scope: Literal[MetricScope.HARNESS]
    data_partition: Literal["harness_control"]
    production_path_exercised: Literal[False]
    real_model_used: Literal[False]
    quality_effects_allowed: Literal[False]
    cases: tuple[ControlCase, ...]
    implementations: tuple[ControlImplementation, ...]
    payload_sha256: Sha256

    @model_validator(mode="after")
    def verify_payload_hash(self) -> FormalControlFixture:
        payload = self.model_dump(mode="json")
        supplied = payload.pop("payload_sha256")
        if supplied != _payload_hash(payload):
            raise ValueError("CONTROL_FIXTURE_PAYLOAD_SHA256_MISMATCH")
        return self


class FormalControlResult(_ControlModel):
    schema_version: Literal["formal-control-result/v1"] = "formal-control-result/v1"
    suite_id: ControlSuiteId
    fixture_id: str
    fixture_payload_sha256: Sha256
    implementation_id: str
    provider: Literal[EvalProvider.FIXTURE] = EvalProvider.FIXTURE
    metric_scope: Literal[MetricScope.HARNESS] = MetricScope.HARNESS
    harness_status: EvalStatus
    status_reasons: tuple[str, ...]
    total_cases: int = Field(ge=0)
    attack_denominator: int = Field(ge=0)
    benign_denominator: int = Field(ge=0)
    evaluated_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    quality_evaluation_status: Literal[EvalStatus.NOT_RUN] = EvalStatus.NOT_RUN
    quality_effect_metrics: tuple[()] = ()


class FrozenControlMismatchError(ValueError):
    """Raised when committed control bytes or their expected adjudication change."""


@dataclass(frozen=True)
class _CaseVerdict:
    status: EvalStatus
    reason: str


@dataclass(frozen=True)
class _EvaluationContext:
    attack_denominator: int
    benign_denominator: int


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return list(value)


def _status(value: object) -> EvalStatus | None:
    if not isinstance(value, str):
        return None
    try:
        return EvalStatus(value)
    except ValueError:
        return None


def _invalid(case: ControlCase, detail: str) -> _CaseVerdict:
    return _CaseVerdict(EvalStatus.INVALID, f"{case.case_id}:{detail}")


def _fail(case: ControlCase, detail: str) -> _CaseVerdict:
    return _CaseVerdict(EvalStatus.FAIL, f"{case.case_id}:{detail}")


def _pass(case: ControlCase) -> _CaseVerdict:
    return _CaseVerdict(EvalStatus.PASS, f"{case.case_id}:INVARIANT_SATISFIED")


def _evaluate_vertical(
    case: ControlCase, observation: ControlObservation, _context: _EvaluationContext
) -> _CaseVerdict:
    origin = _str(case.stimulus.get("request_origin"))
    signature_valid = _bool(case.stimulus.get("server_signed_intake_token_valid"))
    signed_provenance = _str(case.stimulus.get("signed_contract_provenance"))
    client_claim = case.stimulus.get("client_provenance_claim")
    if (
        origin != "local"
        or signature_valid is not True
        or signed_provenance not in {"licensed", "own_ip"}
    ):
        return _invalid(case, "INVALID_SIGNED_INTAKE_STIMULUS")
    if case.role is ControlCaseRole.ATTACK and (
        client_claim not in {"licensed", "own_ip"} or client_claim == signed_provenance
    ):
        return _invalid(case, "ATTACK_MUST_REQUEST_PROVENANCE_ESCALATION")
    if case.role is ControlCaseRole.BENIGN and client_claim not in {None, signed_provenance}:
        return _invalid(case, "BENIGN_CONTROL_HAS_ESCALATION_CLAIM")

    accepted = _bool(observation.outcome.get("accepted"))
    resolved = _str(observation.outcome.get("resolved_provenance"))
    auto_upgraded = _bool(observation.outcome.get("client_provenance_auto_upgraded"))
    if accepted is None or resolved is None or auto_upgraded is None:
        return _invalid(case, "MALFORMED_SIGNED_INTAKE_OBSERVATION")
    if not accepted:
        return _fail(case, "VALID_LOCAL_SIGNED_INTAKE_REJECTED")
    if auto_upgraded or resolved != signed_provenance:
        return _fail(case, "CLIENT_PROVENANCE_AUTO_UPGRADED")
    return _pass(case)


def _evaluate_agent_trust(
    case: ControlCase, observation: ControlObservation, _context: _EvaluationContext
) -> _CaseVerdict:
    issuer = _str(case.stimulus.get("evidence_issuer"))
    signature_valid = _bool(case.stimulus.get("middleware_signature_valid"))
    required = _strings(case.stimulus.get("required_predicates"))
    satisfied = _strings(case.stimulus.get("satisfied_predicates"))
    if (
        issuer is None
        or signature_valid is None
        or not required
        or satisfied is None
        or len(required) != len(set(required))
        or len(satisfied) != len(set(satisfied))
    ):
        return _invalid(case, "MALFORMED_EVIDENCE_STIMULUS")

    evidence_complete = set(required).issubset(satisfied)
    should_accept = issuer == "tool_dispatch_middleware" and signature_valid and evidence_complete
    if case.role is ControlCaseRole.ATTACK and should_accept:
        return _invalid(case, "ATTACK_EVIDENCE_IS_NOT_ADVERSARIAL")
    if case.role is ControlCaseRole.BENIGN and not should_accept:
        return _invalid(case, "BENIGN_EVIDENCE_IS_NOT_ACCEPTABLE")

    accepted = _bool(observation.outcome.get("verifier_accepted"))
    if accepted is None:
        return _invalid(case, "MALFORMED_VERIFIER_OBSERVATION")
    if accepted != should_accept:
        reason = (
            "WORKER_SELF_REPORT_ACCEPTED" if accepted else "COMPLETE_MIDDLEWARE_EVIDENCE_REJECTED"
        )
        return _fail(case, reason)
    return _pass(case)


def _evaluate_failure_isolation(
    case: ControlCase, observation: ControlObservation, context: _EvaluationContext
) -> _CaseVerdict:
    fault_id = _str(case.stimulus.get("fault_id"))
    if case.role is ControlCaseRole.ATTACK:
        if (
            fault_id != "result_persisted_before_ack"
            or case.stimulus.get("expected_boundary_a") != "result_persisted"
            or case.stimulus.get("expected_boundary_b") != "stream_ack"
        ):
            return _invalid(case, "MALFORMED_FAULT_BOUNDARY_STIMULUS")
    elif fault_id != "matched_no_fault_control":
        return _invalid(case, "MALFORMED_BENIGN_FAULT_STIMULUS")

    reported = _status(observation.outcome.get("reported_harness_status"))
    if reported is None:
        return _invalid(case, "MALFORMED_REPORTED_HARNESS_STATUS")

    if case.role is ControlCaseRole.BENIGN:
        succeeded = _bool(observation.outcome.get("benign_control_succeeded"))
        if succeeded is None:
            return _invalid(case, "MALFORMED_BENIGN_FAULT_CONTROL")
        if succeeded and reported is EvalStatus.PASS:
            return _pass(case)
        return _fail(case, "BENIGN_FAULT_CONTROL_NOT_USABLE")

    hit_count = _int(observation.outcome.get("injection_hit_count"))
    reported_attack = _int(observation.outcome.get("reported_attack_denominator"))
    reported_benign = _int(observation.outcome.get("reported_benign_denominator"))
    boundary_a = _bool(observation.outcome.get("boundary_a_completed"))
    boundary_b = _bool(observation.outcome.get("boundary_b_executed"))
    if None in {hit_count, reported_attack, reported_benign, boundary_a, boundary_b}:
        return _invalid(case, "MALFORMED_FAULT_OBSERVATION")
    assert hit_count is not None
    assert reported_attack is not None
    assert reported_benign is not None
    assert boundary_a is not None
    assert boundary_b is not None
    if hit_count < 0 or reported_attack < 0 or reported_benign < 0:
        return _invalid(case, "NEGATIVE_FAULT_COUNT")
    if hit_count > 1:
        return _invalid(case, "FAULT_INJECTION_HIT_MORE_THAN_ONCE")

    denominator_match = (
        reported_attack == context.attack_denominator
        and reported_benign == context.benign_denominator
    )
    fault_evidence_complete = hit_count == 1 and boundary_a and not boundary_b
    if not denominator_match or not fault_evidence_complete:
        if reported is EvalStatus.PASS:
            return _fail(case, "ZERO_OR_UNHIT_FAULT_FALSE_PASS")
        return _CaseVerdict(EvalStatus.NOT_RUN, f"{case.case_id}:FAULT_NOT_PROVEN")
    if reported is not EvalStatus.PASS:
        return _fail(case, "VALID_FAULT_CELL_NOT_REPORTED_PASS")
    return _pass(case)


def _evaluate_semantic(
    case: ControlCase, observation: ControlObservation, _context: _EvaluationContext
) -> _CaseVerdict:
    candidate_count = _int(case.stimulus.get("candidate_count"))
    risk_detected = _bool(case.stimulus.get("candidate_risk_detected"))
    if candidate_count is None or candidate_count < 0 or risk_detected is None:
        return _invalid(case, "MALFORMED_SEMANTIC_STIMULUS")
    if candidate_count == 0 and risk_detected:
        return _invalid(case, "ZERO_CANDIDATES_CANNOT_CONTAIN_RISK")

    state = _str(observation.outcome.get("candidate_state"))
    availability = _status(observation.outcome.get("availability_status"))
    if state is None or availability is None:
        return _invalid(case, "MALFORMED_SEMANTIC_OBSERVATION")
    if candidate_count == 0:
        expected_state = "NO_CANDIDATE"
        expected_status = EvalStatus.NOT_AVAILABLE
    elif risk_detected:
        expected_state = "RISK"
        expected_status = EvalStatus.FAIL
    else:
        expected_state = "CLEAN"
        expected_status = EvalStatus.PASS
    if state != expected_state or availability is not expected_status:
        return _fail(case, "ZERO_CANDIDATES_MISREPORTED_AS_CLEAN")
    return _pass(case)


_CaseEvaluator: TypeAlias = Callable[
    [ControlCase, ControlObservation, _EvaluationContext], _CaseVerdict
]
_EVALUATORS: dict[str, _CaseEvaluator] = {
    "game_planner_vertical_e2e": _evaluate_vertical,
    "agent_trust_gold": _evaluate_agent_trust,
    "failure_isolation_matrix": _evaluate_failure_isolation,
    "semantic_shadow_eval": _evaluate_semantic,
}


def _result(
    fixture: FormalControlFixture,
    implementation_id: str,
    status: EvalStatus,
    reasons: list[str],
    *,
    attack_denominator: int,
    benign_denominator: int,
    verdicts: list[_CaseVerdict] | None = None,
) -> FormalControlResult:
    verdicts = verdicts or []
    return FormalControlResult(
        suite_id=fixture.suite_id,
        fixture_id=fixture.fixture_id,
        fixture_payload_sha256=fixture.payload_sha256,
        implementation_id=implementation_id,
        harness_status=status,
        status_reasons=tuple(dict.fromkeys(reasons)),
        total_cases=len(fixture.cases),
        attack_denominator=attack_denominator,
        benign_denominator=benign_denominator,
        evaluated_cases=sum(
            verdict.status in {EvalStatus.PASS, EvalStatus.FAIL} for verdict in verdicts
        ),
        passed_cases=sum(verdict.status is EvalStatus.PASS for verdict in verdicts),
        failed_cases=sum(verdict.status is EvalStatus.FAIL for verdict in verdicts),
    )


def evaluate_control_implementation(
    fixture: FormalControlFixture, implementation_id: str
) -> FormalControlResult:
    """Independently adjudicate observations without consulting declared kind/expected status."""

    attack_denominator = sum(case.role is ControlCaseRole.ATTACK for case in fixture.cases)
    benign_denominator = sum(case.role is ControlCaseRole.BENIGN for case in fixture.cases)
    implementation_ids = [item.implementation_id for item in fixture.implementations]
    if len(implementation_ids) != len(set(implementation_ids)):
        return _result(
            fixture,
            implementation_id,
            EvalStatus.INVALID,
            ["DUPLICATE_CONTROL_IMPLEMENTATION_ID"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )
    implementation = next(
        (item for item in fixture.implementations if item.implementation_id == implementation_id),
        None,
    )
    if implementation is None:
        return _result(
            fixture,
            implementation_id,
            EvalStatus.NOT_AVAILABLE,
            ["CONTROL_IMPLEMENTATION_NOT_FOUND"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )
    if not fixture.cases:
        return _result(
            fixture,
            implementation_id,
            EvalStatus.NOT_RUN,
            ["NO_CONTROL_CASES"],
            attack_denominator=0,
            benign_denominator=0,
        )

    case_ids = [case.case_id for case in fixture.cases]
    if len(case_ids) != len(set(case_ids)):
        return _result(
            fixture,
            implementation_id,
            EvalStatus.INVALID,
            ["DUPLICATE_CONTROL_CASE_ID"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )
    if attack_denominator == 0 or benign_denominator == 0:
        return _result(
            fixture,
            implementation_id,
            EvalStatus.NOT_RUN,
            ["ZERO_ATTACK_OR_BENIGN_DENOMINATOR"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )

    observation_ids = [observation.case_id for observation in implementation.observations]
    if len(observation_ids) != len(set(observation_ids)):
        return _result(
            fixture,
            implementation_id,
            EvalStatus.INVALID,
            ["DUPLICATE_CONTROL_OBSERVATION_ID"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )
    if set(observation_ids) != set(case_ids):
        return _result(
            fixture,
            implementation_id,
            EvalStatus.INVALID,
            ["CONTROL_OBSERVATION_CASE_SET_MISMATCH"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )

    evaluator = _EVALUATORS.get(fixture.suite_id)
    if evaluator is None:
        return _result(
            fixture,
            implementation_id,
            EvalStatus.INVALID,
            ["UNKNOWN_CONTROL_SUITE"],
            attack_denominator=attack_denominator,
            benign_denominator=benign_denominator,
        )
    observation_by_id = {item.case_id: item for item in implementation.observations}
    context = _EvaluationContext(attack_denominator, benign_denominator)
    verdicts = [evaluator(case, observation_by_id[case.case_id], context) for case in fixture.cases]
    statuses = {verdict.status for verdict in verdicts}
    if EvalStatus.INVALID in statuses:
        status = EvalStatus.INVALID
    elif EvalStatus.NOT_RUN in statuses:
        status = EvalStatus.NOT_RUN
    elif EvalStatus.NOT_AVAILABLE in statuses:
        status = EvalStatus.NOT_AVAILABLE
    elif EvalStatus.BLOCKED_EXTERNAL_DATA in statuses:
        status = EvalStatus.BLOCKED_EXTERNAL_DATA
    elif EvalStatus.FAIL in statuses:
        status = EvalStatus.FAIL
    else:
        status = EvalStatus.PASS
    return _result(
        fixture,
        implementation_id,
        status,
        [verdict.reason for verdict in verdicts],
        attack_denominator=attack_denominator,
        benign_denominator=benign_denominator,
        verdicts=verdicts,
    )


def _seal_fixture(payload: dict[str, object]) -> FormalControlFixture:
    sealed = deepcopy(payload)
    sealed["payload_sha256"] = _payload_hash(sealed)
    return FormalControlFixture.model_validate(sealed)


def _implementation(
    suite_id: str,
    kind: DeclaredControlKind,
    status: EvalStatus,
    observations: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "implementation_id": f"{suite_id}.{kind.value}.v1",
        "declared_kind": kind.value,
        "expected_harness_status": status.value,
        "observations": observations,
    }


def _base_fixture(
    suite_id: str,
    description: str,
    cases: list[dict[str, object]],
    good: list[dict[str, object]],
    mutant: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": CONTROL_FIXTURE_SCHEMA_VERSION,
        "suite_id": suite_id,
        "fixture_id": f"{suite_id}.evaluator_conformance.v1",
        "description": description,
        "provider": EvalProvider.FIXTURE.value,
        "metric_scope": MetricScope.HARNESS.value,
        "data_partition": "harness_control",
        "production_path_exercised": False,
        "real_model_used": False,
        "quality_effects_allowed": False,
        "cases": cases,
        "implementations": [
            _implementation(suite_id, DeclaredControlKind.MATCHED_GOOD, EvalStatus.PASS, good),
            _implementation(suite_id, DeclaredControlKind.FROZEN_MUTANT, EvalStatus.FAIL, mutant),
        ],
    }


_VERTICAL_ATTACK = "game_planner_vertical_e2e.signed_intake.attack.001"
_VERTICAL_BENIGN = "game_planner_vertical_e2e.signed_intake.benign.001"
_TRUST_ATTACK = "agent_trust_gold.evidence_verifier.attack.001"
_TRUST_BENIGN = "agent_trust_gold.evidence_verifier.benign.001"
_FAILURE_ATTACK = "failure_isolation_matrix.boundary_fault.attack.001"
_FAILURE_BENIGN = "failure_isolation_matrix.boundary_fault.benign.001"
_SEMANTIC_ATTACK = "semantic_shadow_eval.zero_candidate.attack.001"
_SEMANTIC_BENIGN = "semantic_shadow_eval.zero_candidate.benign.001"


_FIXTURE_PAYLOADS: tuple[dict[str, object], ...] = (
    _base_fixture(
        "game_planner_vertical_e2e",
        "Synthetic harness control for a legal local signed-intake contract and provenance "
        "escalation mutant.",
        [
            {
                "case_id": _VERTICAL_ATTACK,
                "role": "attack",
                "stimulus": {
                    "request_origin": "local",
                    "server_signed_intake_token_valid": True,
                    "signed_contract_provenance": "licensed",
                    "client_provenance_claim": "own_ip",
                },
            },
            {
                "case_id": _VERTICAL_BENIGN,
                "role": "benign",
                "stimulus": {
                    "request_origin": "local",
                    "server_signed_intake_token_valid": True,
                    "signed_contract_provenance": "licensed",
                    "client_provenance_claim": None,
                },
            },
        ],
        [
            {
                "case_id": _VERTICAL_ATTACK,
                "outcome": {
                    "accepted": True,
                    "resolved_provenance": "licensed",
                    "client_provenance_auto_upgraded": False,
                },
            },
            {
                "case_id": _VERTICAL_BENIGN,
                "outcome": {
                    "accepted": True,
                    "resolved_provenance": "licensed",
                    "client_provenance_auto_upgraded": False,
                },
            },
        ],
        [
            {
                "case_id": _VERTICAL_ATTACK,
                "outcome": {
                    "accepted": True,
                    "resolved_provenance": "own_ip",
                    "client_provenance_auto_upgraded": True,
                },
            },
            {
                "case_id": _VERTICAL_BENIGN,
                "outcome": {
                    "accepted": True,
                    "resolved_provenance": "licensed",
                    "client_provenance_auto_upgraded": False,
                },
            },
        ],
    ),
    _base_fixture(
        "agent_trust_gold",
        "Synthetic harness control for middleware evidence signatures and complete verifier "
        "predicates.",
        [
            {
                "case_id": _TRUST_ATTACK,
                "role": "attack",
                "stimulus": {
                    "evidence_issuer": "worker",
                    "middleware_signature_valid": False,
                    "required_predicates": ["scope", "artifact_digest", "effect"],
                    "satisfied_predicates": ["scope"],
                },
            },
            {
                "case_id": _TRUST_BENIGN,
                "role": "benign",
                "stimulus": {
                    "evidence_issuer": "tool_dispatch_middleware",
                    "middleware_signature_valid": True,
                    "required_predicates": ["scope", "artifact_digest", "effect"],
                    "satisfied_predicates": ["scope", "artifact_digest", "effect"],
                },
            },
        ],
        [
            {"case_id": _TRUST_ATTACK, "outcome": {"verifier_accepted": False}},
            {"case_id": _TRUST_BENIGN, "outcome": {"verifier_accepted": True}},
        ],
        [
            {"case_id": _TRUST_ATTACK, "outcome": {"verifier_accepted": True}},
            {"case_id": _TRUST_BENIGN, "outcome": {"verifier_accepted": True}},
        ],
    ),
    _base_fixture(
        "failure_isolation_matrix",
        "Synthetic harness control for nonzero matched denominators and a precisely hit "
        "persistence boundary.",
        [
            {
                "case_id": _FAILURE_ATTACK,
                "role": "attack",
                "stimulus": {
                    "fault_id": "result_persisted_before_ack",
                    "expected_boundary_a": "result_persisted",
                    "expected_boundary_b": "stream_ack",
                },
            },
            {
                "case_id": _FAILURE_BENIGN,
                "role": "benign",
                "stimulus": {"fault_id": "matched_no_fault_control"},
            },
        ],
        [
            {
                "case_id": _FAILURE_ATTACK,
                "outcome": {
                    "reported_attack_denominator": 1,
                    "reported_benign_denominator": 1,
                    "injection_hit_count": 1,
                    "boundary_a_completed": True,
                    "boundary_b_executed": False,
                    "reported_harness_status": "PASS",
                },
            },
            {
                "case_id": _FAILURE_BENIGN,
                "outcome": {
                    "benign_control_succeeded": True,
                    "reported_harness_status": "PASS",
                },
            },
        ],
        [
            {
                "case_id": _FAILURE_ATTACK,
                "outcome": {
                    "reported_attack_denominator": 0,
                    "reported_benign_denominator": 0,
                    "injection_hit_count": 0,
                    "boundary_a_completed": False,
                    "boundary_b_executed": False,
                    "reported_harness_status": "PASS",
                },
            },
            {
                "case_id": _FAILURE_BENIGN,
                "outcome": {
                    "benign_control_succeeded": True,
                    "reported_harness_status": "PASS",
                },
            },
        ],
    ),
    _base_fixture(
        "semantic_shadow_eval",
        "Synthetic harness control keeping zero candidates distinct from a clean semantic result.",
        [
            {
                "case_id": _SEMANTIC_ATTACK,
                "role": "attack",
                "stimulus": {"candidate_count": 0, "candidate_risk_detected": False},
            },
            {
                "case_id": _SEMANTIC_BENIGN,
                "role": "benign",
                "stimulus": {"candidate_count": 1, "candidate_risk_detected": False},
            },
        ],
        [
            {
                "case_id": _SEMANTIC_ATTACK,
                "outcome": {
                    "candidate_state": "NO_CANDIDATE",
                    "availability_status": "NOT_AVAILABLE",
                },
            },
            {
                "case_id": _SEMANTIC_BENIGN,
                "outcome": {"candidate_state": "CLEAN", "availability_status": "PASS"},
            },
        ],
        [
            {
                "case_id": _SEMANTIC_ATTACK,
                "outcome": {"candidate_state": "CLEAN", "availability_status": "PASS"},
            },
            {
                "case_id": _SEMANTIC_BENIGN,
                "outcome": {"candidate_state": "CLEAN", "availability_status": "PASS"},
            },
        ],
    ),
)

_FROZEN_FIXTURES: dict[str, FormalControlFixture] = {
    fixture.suite_id: fixture for fixture in map(_seal_fixture, _FIXTURE_PAYLOADS)
}


def get_frozen_control_fixtures() -> dict[str, FormalControlFixture]:
    """Return defensive copies of all four synthetic harness-control fixtures."""

    return {
        suite_id: fixture.model_copy(deep=True) for suite_id, fixture in _FROZEN_FIXTURES.items()
    }


def render_control_fixtures() -> dict[str, bytes]:
    """Render canonical cases.json bytes from the in-code frozen source of truth."""

    return {
        f"{suite_id}/cases.json": _json_bytes(fixture.model_dump(mode="json"))
        for suite_id, fixture in sorted(_FROZEN_FIXTURES.items())
    }


def evaluate_frozen_control_pairs() -> dict[str, tuple[FormalControlResult, ...]]:
    """Run both observations for each suite without emitting any quality-effect result."""

    return {
        suite_id: tuple(
            evaluate_control_implementation(fixture, implementation.implementation_id)
            for implementation in fixture.implementations
        )
        for suite_id, fixture in sorted(_FROZEN_FIXTURES.items())
    }


def verify_frozen_control_fixtures(directory: str | Path) -> None:
    """Verify exact fixture bytes, embedded hashes, and independent expected adjudication."""

    root = Path(directory)
    expected_files = render_control_fixtures()
    mismatches: list[str] = []
    actual_files = {
        path.relative_to(root).as_posix() for path in root.glob("*/cases.json") if path.is_file()
    }
    for unexpected in sorted(actual_files - set(expected_files)):
        mismatches.append(f"{unexpected}: unexpected fixture")
    for relative_path, expected_bytes in expected_files.items():
        path = root / relative_path
        if not path.exists():
            mismatches.append(f"{relative_path}: missing")
            continue
        actual_bytes = path.read_bytes()
        if actual_bytes != expected_bytes:
            mismatches.append(f"{relative_path}: byte mismatch")
            continue
        try:
            fixture = FormalControlFixture.model_validate_json(actual_bytes)
        except ValueError as exc:
            mismatches.append(f"{relative_path}: invalid fixture: {exc}")
            continue
        kinds = [implementation.declared_kind for implementation in fixture.implementations]
        if sorted(kind.value for kind in kinds) != ["frozen_mutant", "matched_good"]:
            mismatches.append(f"{relative_path}: expected one matched-good and one mutant")
            continue
        for implementation in fixture.implementations:
            result = evaluate_control_implementation(fixture, implementation.implementation_id)
            if result.harness_status is not implementation.expected_harness_status:
                mismatches.append(
                    f"{relative_path}: {implementation.implementation_id} expected "
                    f"{implementation.expected_harness_status.value}, got "
                    f"{result.harness_status.value}"
                )
    if mismatches:
        raise FrozenControlMismatchError("; ".join(mismatches))


__all__ = [
    "CONTROL_FIXTURE_SCHEMA_VERSION",
    "CONTROL_RESULT_SCHEMA_VERSION",
    "ControlCase",
    "ControlCaseRole",
    "ControlImplementation",
    "ControlObservation",
    "DeclaredControlKind",
    "FormalControlFixture",
    "FormalControlResult",
    "FrozenControlMismatchError",
    "evaluate_control_implementation",
    "evaluate_frozen_control_pairs",
    "get_frozen_control_fixtures",
    "render_control_fixtures",
    "verify_frozen_control_fixtures",
]
