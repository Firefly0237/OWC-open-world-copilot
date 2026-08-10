"""Frozen invariants for the N0.3 formal-evaluation protocol."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from owcopilot.evaluation.protocol import (
    EvalProvider,
    EvalStatus,
    FormalEvalReport,
    FormalEvalTrace,
    FrozenSchemaMismatchError,
    render_protocol_schemas,
    verify_frozen_schemas,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "formal" / "schemas"


def _counts(
    *,
    total: int,
    executed: int,
    passed: int = 0,
    failed: int = 0,
    errored: int = 0,
    skipped: int = 0,
) -> dict[str, int]:
    return {
        "total_cases": total,
        "executed_cases": executed,
        "passed_cases": passed,
        "failed_cases": failed,
        "errored_cases": errored,
        "skipped_cases": skipped,
    }


def _metric(*, scope: str = "quality", sample_size: int = 1) -> dict[str, object]:
    return {
        "name": "primary_score",
        "scope": scope,
        "sample_size": sample_size,
        "value": 1.0 if sample_size else None,
        "unit": "ratio",
    }


def _report(**overrides: object) -> FormalEvalReport:
    payload: dict[str, object] = {
        "schema_version": "formal-eval/v1",
        "suite_id": "agent_trust_gold",
        "suite_version": "v1",
        "run_id": "run-1",
        "split": "dev",
        "provider": "real_model",
        "generated_at": "2026-08-03T12:00:00Z",
        "harness_status": "PASS",
        "evaluation_status": "PASS",
        "counts": _counts(total=1, executed=1, passed=1),
        "primary_metric": "primary_score",
        "metrics": [_metric()],
        "trace_sha256s": [_SHA_A],
    }
    payload.update(overrides)
    return FormalEvalReport.model_validate(payload)


def _real_trace_payload() -> dict[str, object]:
    return {
        "schema_version": "formal-eval/v1",
        "suite_id": "agent_trust_gold",
        "suite_version": "v1",
        "run_id": "run-1",
        "case_id": "case-1",
        "attempt": 1,
        "split": "dev",
        "provider": "real_model",
        "status": "PASS",
        "started_at": "2026-08-03T12:00:00Z",
        "finished_at": "2026-08-03T12:00:01Z",
        "model": {
            "provider": "example-provider",
            "full_model_id": "example/model-2026-08-01",
            "configuration_sha256": _SHA_A,
        },
        "prompt_sha256": _SHA_B,
        "tool_schema_sha256": _SHA_C,
        "scorer": {"scorer_id": "trust", "scorer_version": "v1", "sha256": _SHA_D},
        "budget": {"model_call_cap": 3, "token_cap": 1000, "cost_usd_cap": 1.0},
        "usage": {
            "model_calls": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cost_usd": 0.1,
            "latency_ms": 500,
        },
        "trajectory": [{"sequence": 0, "kind": "model", "payload": {"text": "ok"}}],
    }


def test_eval_status_is_exactly_the_preregistered_six_state_vocabulary() -> None:
    assert [status.value for status in EvalStatus] == [
        "PASS",
        "FAIL",
        "NOT_RUN",
        "NOT_AVAILABLE",
        "INVALID",
        "BLOCKED_EXTERNAL_DATA",
    ]


def test_report_keeps_harness_and_quality_status_separate() -> None:
    report = _report()
    assert report.harness_status is EvalStatus.PASS
    assert report.evaluation_status is EvalStatus.PASS
    assert "passed" not in FormalEvalReport.model_fields


def test_zero_over_zero_is_not_run_never_pass() -> None:
    report = _report(
        counts=_counts(total=0, executed=0),
        metrics=[_metric(sample_size=0)],
        trace_sha256s=[],
    )
    assert report.harness_status is EvalStatus.NOT_RUN
    assert report.evaluation_status is EvalStatus.NOT_RUN


def test_skipped_report_is_not_run_never_pass() -> None:
    report = _report(
        skipped=True,
        counts=_counts(total=1, executed=0, skipped=1),
        metrics=[],
        primary_metric=None,
        trace_sha256s=[],
    )
    assert report.harness_status is EvalStatus.NOT_RUN
    assert report.evaluation_status is EvalStatus.NOT_RUN


def test_all_errored_report_is_invalid_never_pass() -> None:
    report = _report(
        counts=_counts(total=2, executed=2, errored=2),
        metrics=[_metric(sample_size=2)],
    )
    assert report.harness_status is EvalStatus.INVALID
    assert report.evaluation_status is EvalStatus.INVALID
    assert "ALL_TRIALS_ERRORED" in report.status_reasons


@pytest.mark.parametrize("provider", [EvalProvider.FIXTURE, EvalProvider.SCRIPTED])
def test_non_real_provider_can_pass_harness_but_not_quality_evaluation(
    provider: EvalProvider,
) -> None:
    report = _report(
        provider=provider,
        metrics=[_metric(scope="harness")],
        trace_sha256s=[],
    )
    assert report.harness_status is EvalStatus.PASS
    assert report.evaluation_status is EvalStatus.NOT_RUN
    assert "NON_REAL_PROVIDER_HAS_NO_QUALITY_EFFECT" in report.status_reasons


@pytest.mark.parametrize("provider", [EvalProvider.FIXTURE, EvalProvider.SCRIPTED])
def test_non_real_provider_cannot_serialize_quality_effect_metrics(
    provider: EvalProvider,
) -> None:
    with pytest.raises(ValidationError, match="cannot emit quality-effect metrics"):
        _report(provider=provider, trace_sha256s=[])


def test_harness_metric_cannot_smuggle_baseline_or_effect_size() -> None:
    metric = {**_metric(scope="harness"), "baseline_value": 0.5, "effect_size": 0.5}
    with pytest.raises(ValidationError, match="harness metrics cannot carry"):
        _report(provider="fixture", metrics=[metric], trace_sha256s=[])


@pytest.mark.parametrize(
    ("missing_field", "reason"),
    [
        ("started_at", "MISSING_REAL_MODEL_STARTED_AT"),
        ("finished_at", "MISSING_REAL_MODEL_FINISHED_AT"),
        ("model", "MISSING_REAL_MODEL_MODEL"),
        ("prompt_sha256", "MISSING_REAL_MODEL_PROMPT_SHA256"),
        ("tool_schema_sha256", "MISSING_REAL_MODEL_TOOL_SCHEMA_SHA256"),
        ("scorer", "MISSING_REAL_MODEL_SCORER"),
        ("budget", "MISSING_REAL_MODEL_BUDGET"),
        ("usage", "MISSING_REAL_MODEL_USAGE"),
        ("trajectory", "MISSING_REAL_MODEL_TRAJECTORY"),
    ],
)
def test_real_model_trace_missing_provenance_is_structured_invalid(
    missing_field: str, reason: str
) -> None:
    payload = _real_trace_payload()
    payload.pop(missing_field)
    trace = FormalEvalTrace.model_validate(payload)
    assert trace.status is EvalStatus.INVALID
    assert reason in trace.status_reasons


def test_complete_real_model_trace_can_remain_pass() -> None:
    trace = FormalEvalTrace.model_validate(_real_trace_payload())
    assert trace.status is EvalStatus.PASS
    assert trace.status_reasons == ()


def test_real_model_trace_with_reversed_timestamps_is_invalid() -> None:
    payload = _real_trace_payload()
    payload["finished_at"] = "2026-08-03T11:59:59Z"
    trace = FormalEvalTrace.model_validate(payload)
    assert trace.status is EvalStatus.INVALID
    assert "FINISHED_BEFORE_STARTED" in trace.status_reasons


def test_trace_sequence_must_be_contiguous_unique_and_ordered() -> None:
    payload = _real_trace_payload()
    payload["trajectory"] = [
        {"sequence": 1, "kind": "model", "payload": {}},
        {"sequence": 1, "kind": "tool", "payload": {}},
        {"sequence": 0, "kind": "model", "payload": {}},
    ]
    trace = FormalEvalTrace.model_validate(payload)
    assert trace.status is EvalStatus.INVALID
    assert "INVALID_TRACE_SEQUENCE" in trace.status_reasons


def test_zero_call_real_model_verdict_is_invalid() -> None:
    payload = _real_trace_payload()
    payload["budget"] = {"model_call_cap": 0, "token_cap": 0, "cost_usd_cap": 0}
    payload["usage"] = {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0,
        "latency_ms": 1,
    }
    trace = FormalEvalTrace.model_validate(payload)
    assert trace.status is EvalStatus.INVALID
    assert "ZERO_REAL_MODEL_CALLS" in trace.status_reasons


@pytest.mark.parametrize(
    ("usage_patch", "reason"),
    [
        ({"model_calls": 4}, "MODEL_CALL_BUDGET_EXCEEDED"),
        ({"input_tokens": 900, "output_tokens": 200}, "TOKEN_BUDGET_EXCEEDED"),
        ({"cost_usd": 1.1}, "COST_BUDGET_EXCEEDED"),
    ],
)
def test_real_model_budget_overrun_is_invalid(usage_patch: dict[str, object], reason: str) -> None:
    payload = _real_trace_payload()
    usage = dict(payload["usage"])  # type: ignore[arg-type]
    usage.update(usage_patch)
    payload["usage"] = usage
    trace = FormalEvalTrace.model_validate(payload)
    assert trace.status is EvalStatus.INVALID
    assert reason in trace.status_reasons


def test_real_model_quality_pass_without_trace_hashes_is_invalid() -> None:
    report = _report(trace_sha256s=[])
    assert report.evaluation_status is EvalStatus.INVALID
    assert "MISSING_TRACE_HASHES" in report.status_reasons


def test_report_and_nested_protocol_values_are_immutable() -> None:
    report = _report()
    with pytest.raises(ValidationError, match="frozen"):
        report.evaluation_status = EvalStatus.FAIL
    with pytest.raises(ValidationError, match="frozen"):
        report.counts.executed_cases = 0
    assert isinstance(report.trace_sha256s, tuple)
    assert isinstance(report.metrics, tuple)


def test_model_copy_update_revalidates_instead_of_smuggling_fixture_quality_pass() -> None:
    fixture = _report(
        provider="fixture",
        metrics=[_metric(scope="harness")],
        trace_sha256s=[],
    )
    copied = fixture.model_copy(update={"evaluation_status": EvalStatus.PASS})
    assert copied.evaluation_status is EvalStatus.NOT_RUN


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_budget_usage_and_metric_values_are_rejected(nonfinite: float) -> None:
    trace = _real_trace_payload()
    trace["budget"] = {"model_call_cap": 1, "token_cap": 1, "cost_usd_cap": nonfinite}
    with pytest.raises(ValidationError):
        FormalEvalTrace.model_validate(trace)
    with pytest.raises(ValidationError):
        _report(metrics=[{**_metric(), "value": nonfinite}])


def test_counts_require_complete_case_accounting() -> None:
    with pytest.raises(ValidationError, match="must equal executed_cases"):
        _report(counts=_counts(total=10, executed=10, passed=1))
    with pytest.raises(ValidationError, match="must equal total_cases"):
        _report(counts=_counts(total=100, executed=1, passed=1))


def test_errored_trials_cannot_be_removed_from_primary_denominator() -> None:
    report = _report(
        counts=_counts(total=10, executed=10, passed=1, errored=9),
        metrics=[_metric(sample_size=1)],
    )
    assert report.evaluation_status is EvalStatus.INVALID
    assert "PRIMARY_CASE_DENOMINATOR_MISMATCH" in report.status_reasons
    assert "INSUFFICIENT_TRACE_HASHES" in report.status_reasons


def test_duplicate_or_insufficient_trace_hashes_make_quality_verdict_invalid() -> None:
    duplicate = _report(
        counts=_counts(total=2, executed=2, passed=2),
        metrics=[_metric(sample_size=2)],
        trace_sha256s=[_SHA_A, _SHA_A],
    )
    assert duplicate.evaluation_status is EvalStatus.INVALID
    assert "DUPLICATE_TRACE_HASHES" in duplicate.status_reasons

    insufficient = _report(
        counts=_counts(total=2, executed=2, passed=2),
        metrics=[_metric(sample_size=2)],
        trace_sha256s=[_SHA_A],
    )
    assert insufficient.evaluation_status is EvalStatus.INVALID
    assert "INSUFFICIENT_TRACE_HASHES" in insufficient.status_reasons


def test_zero_sample_diagnostic_cannot_carry_a_result() -> None:
    with pytest.raises(ValidationError, match="zero-sample metric cannot carry a result"):
        _report(
            metrics=[
                _metric(),
                {
                    "name": "diagnostic",
                    "scope": "quality",
                    "sample_size": 0,
                    "value": 1.0,
                    "unit": "ratio",
                },
            ]
        )


@pytest.mark.parametrize(
    ("harness_status", "expected_evaluation_status"),
    [
        (EvalStatus.INVALID, EvalStatus.INVALID),
        (EvalStatus.FAIL, EvalStatus.INVALID),
        (EvalStatus.NOT_RUN, EvalStatus.NOT_RUN),
        (EvalStatus.NOT_AVAILABLE, EvalStatus.NOT_AVAILABLE),
        (EvalStatus.BLOCKED_EXTERNAL_DATA, EvalStatus.BLOCKED_EXTERNAL_DATA),
    ],
)
@pytest.mark.parametrize("requested_quality_status", [EvalStatus.PASS, EvalStatus.FAIL])
def test_nonpassing_harness_cannot_emit_quality_pass_or_fail(
    harness_status: EvalStatus,
    expected_evaluation_status: EvalStatus,
    requested_quality_status: EvalStatus,
) -> None:
    report = _report(
        harness_status=harness_status,
        evaluation_status=requested_quality_status,
    )
    assert report.evaluation_status is expected_evaluation_status
    assert f"HARNESS_STATUS_{harness_status.value}" in report.status_reasons


def test_generated_schema_contains_separate_statuses_and_no_boolean_passed() -> None:
    report_schema = json.loads(render_protocol_schemas()["report.schema.json"])
    properties = report_schema["properties"]
    assert {"harness_status", "evaluation_status"} <= set(properties)
    assert "passed" not in properties


def test_committed_schema_bytes_match_python_source_of_truth() -> None:
    verify_frozen_schemas(_SCHEMA_DIR)


def test_one_byte_schema_tamper_is_detected(tmp_path: Path) -> None:
    for filename, content in render_protocol_schemas().items():
        (tmp_path / filename).write_bytes(content)
    report_path = tmp_path / "report.schema.json"
    report_path.write_bytes(report_path.read_bytes() + b" ")

    with pytest.raises(FrozenSchemaMismatchError, match="report.schema.json: byte mismatch"):
        verify_frozen_schemas(tmp_path)
