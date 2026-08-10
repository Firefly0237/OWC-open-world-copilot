from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from owcopilot.evaluation.formal_artifacts import (
    default_formal_root,
    render_formal_artifact_policy_files,
    verify_formal_eval_run,
    verify_formal_eval_runs,
    verify_frozen_formal_artifacts,
)
from owcopilot.evaluation.formal_cohort import (
    FormalEvalCohort,
    parse_formal_cohort_bytes,
    render_formal_cohort,
)
from owcopilot.evaluation.formal_runner import FORMAL_SUITE_IDS, run_formal_evals
from owcopilot.evaluation.protocol import FormalEvalReport, FormalEvalTrace

SUITE_ID = "agent_trust_gold"
SUITE_VERSION = "n0.3-v1"
RUN_ID = "formal-artifact-test-run"


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _copy_formal_root(tmp_path: Path) -> Path:
    target = tmp_path / "formal"
    shutil.copytree(default_formal_root(), target)
    return target


def _trace_payload(
    *,
    case_id: str = "case-001",
    attempt: int = 1,
    status: str = "PASS",
    usage: tuple[int, int, int, float] = (1, 10, 5, 0.1),
) -> dict[str, Any]:
    calls, input_tokens, output_tokens, cost = usage
    return {
        "schema_version": "formal-eval/v1",
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "run_id": RUN_ID,
        "case_id": case_id,
        "attempt": attempt,
        "split": "dev",
        "provider": "real_model",
        "status": status,
        "status_reasons": [],
        "started_at": "2026-08-03T00:00:00Z",
        "finished_at": "2026-08-03T00:00:01Z",
        "model": {
            "provider": "test-provider",
            "full_model_id": "test-provider/model-v1",
            "configuration_sha256": "1" * 64,
        },
        "prompt_sha256": "2" * 64,
        "tool_schema_sha256": "3" * 64,
        "scorer": {
            "scorer_id": "test-scorer",
            "scorer_version": "v1",
            "sha256": "4" * 64,
        },
        "budget": {
            "model_call_cap": max(calls, 1),
            "token_cap": max(input_tokens + output_tokens, 1),
            "cost_usd_cap": max(cost, 0.1),
        },
        "usage": {
            "model_calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "latency_ms": 1.0,
        },
        "trajectory": [
            {
                "sequence": 0,
                "kind": "model_call",
                "timestamp": "2026-08-03T00:00:00Z",
                "payload": {},
            }
        ],
        "payload": {},
    }


def _write_trace(run_dir: Path, name: str, payload: dict[str, Any]) -> tuple[Path, str]:
    model = FormalEvalTrace.model_validate(payload)
    content = _json_bytes(model.model_dump(mode="json"))
    path = run_dir / "traces" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _report_payload(
    hashes: list[str],
    *,
    passed: int = 1,
    failed: int = 0,
    errored: int = 0,
    skipped: int = 0,
    harness_status: str = "PASS",
    evaluation_status: str = "NOT_AVAILABLE",
) -> dict[str, Any]:
    executed = passed + failed + errored
    total = executed + skipped
    quality_verdict = evaluation_status in {"PASS", "FAIL"}
    primary_metric = (
        "planning_task_completion_rate"
        if quality_verdict
        else "harness_control_discrimination_rate"
    )
    metric_scope = "quality" if quality_verdict else "harness"
    return {
        "schema_version": "formal-eval/v1",
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "run_id": RUN_ID,
        "split": "dev",
        "provider": "real_model",
        "generated_at": "2026-08-03T00:00:02Z",
        "harness_status": harness_status,
        "evaluation_status": evaluation_status,
        "skipped": bool(skipped),
        "status_reasons": [],
        "counts": {
            "total_cases": total,
            "executed_cases": executed,
            "passed_cases": passed,
            "failed_cases": failed,
            "errored_cases": errored,
            "skipped_cases": skipped,
        },
        "primary_metric": primary_metric,
        "metrics": [
            {
                "name": primary_metric,
                "scope": metric_scope,
                "sample_size": executed,
                "value": (passed / executed) if executed else None,
                "success_count": passed if executed else None,
                "baseline_value": 0.0 if quality_verdict and executed else None,
                "effect_size": (passed / executed) if quality_verdict and executed else None,
                "unit": "ratio",
            }
        ],
        "trace_sha256s": hashes,
    }


def _write_report(run_dir: Path, payload: dict[str, Any], *, finalize: bool = True) -> Path:
    if finalize:
        payload = FormalEvalReport.model_validate(payload).model_dump(mode="json")
    path = run_dir / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(payload))
    return path


def _valid_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    trace_path, trace_hash = _write_trace(run_dir, "case-001-attempt-1.json", _trace_payload())
    _write_report(run_dir, _report_payload([trace_hash]))
    return formal_root, run_dir, trace_path


def _valid_fixture_cohort(tmp_path: Path, *, label: str = "fixture") -> tuple[Path, Path]:
    formal_root = _copy_formal_root(tmp_path / label)
    output = tmp_path / f"{label}-cohort"
    result = run_formal_evals(
        output,
        formal_root=formal_root,
        run_id=f"{label}-cohort-v1",
        generated_at="2026-08-03T04:05:06Z",
    )
    assert result.ok, result.verification.as_dict()
    return formal_root, output


def _fixture_run_dirs(output: Path) -> tuple[Path, ...]:
    return tuple(output / suite_id for suite_id in FORMAL_SUITE_IDS)


def _rewrite_fixture_report_and_cohort(output: Path, suite_id: str) -> None:
    run_dir = output / suite_id
    trace_hashes = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((run_dir / "traces").glob("*.json"))
    ]
    report_path = run_dir / "report.json"
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload["trace_sha256s"] = trace_hashes
    report = FormalEvalReport.model_validate(report_payload)
    report_path.write_bytes(_json_bytes(report.model_dump(mode="json")))

    cohort_path = output / "cohort.json"
    cohort_payload = json.loads(cohort_path.read_text(encoding="utf-8"))
    for entry in cohort_payload["suites"]:
        if entry["suite_id"] == suite_id:
            entry["trace_sha256s"] = trace_hashes
            entry["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    cohort = FormalEvalCohort.model_validate(cohort_payload)
    cohort_path.write_bytes(render_formal_cohort(cohort))


def _codes(result: object) -> set[str]:
    return {error.code for error in result.errors}  # type: ignore[attr-defined]


def test_frozen_governance_and_executable_policy_are_byte_exact() -> None:
    result = verify_frozen_formal_artifacts()
    assert result.ok, result.as_dict()
    rendered = render_formal_artifact_policy_files()
    for name, expected in rendered.items():
        assert (default_formal_root() / "policy" / name).read_bytes() == expected


def test_one_byte_conformance_tamper_is_red(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    path = formal_root / "policy" / "conformance_vectors.json"
    path.write_bytes(
        path.read_bytes().replace(b"fixture_quality_pass_lure", b"fixture_quality_pass_lurx")
    )
    result = verify_frozen_formal_artifacts(formal_root)
    assert not result.ok
    assert "FORMAL_POLICY_BYTE_MISMATCH" in _codes(result)
    assert "ARTIFACT_REFERENCE_HASH_MISMATCH" in _codes(result)


def test_one_byte_control_fixture_tamper_is_red(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    path = formal_root / "controls" / "agent_trust_gold" / "cases.json"
    content = path.read_bytes()
    path.write_bytes(content[:-2] + (b" " if content[-2:-1] != b" " else b"\t") + content[-1:])

    result = verify_frozen_formal_artifacts(formal_root)

    assert not result.ok
    assert "FORMAL_CONTROL_BYTE_MISMATCH" in _codes(result)


def test_valid_final_run_recomputes_linkage_counts_and_usage(tmp_path: Path) -> None:
    formal_root, run_dir, _trace = _valid_run(tmp_path)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert result.ok, result.as_dict()
    assert result.real_model_usage.as_dict() == {
        "model_calls": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "tokens": 15,
        "cost_usd": 0.1,
        "attempts": 1,
    }


def test_complete_fixture_cohort_is_independently_verified(tmp_path: Path) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path)

    result = verify_formal_eval_runs(_fixture_run_dirs(output), formal_root=formal_root)

    assert result.ok, result.as_dict()
    assert parse_formal_cohort_bytes((output / "cohort.json").read_bytes())


def test_fixture_self_reported_pass_cannot_hide_two_failed_controls(tmp_path: Path) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path)
    suite_id = "agent_trust_gold"
    run_dir = output / suite_id
    for trace_path in sorted((run_dir / "traces").glob("*.json")):
        payload = json.loads(trace_path.read_text(encoding="utf-8"))
        payload["status"] = "FAIL"
        payload["status_reasons"] = ["FORGED_SELF_REPORTED_FAILURE"]
        payload["payload"]["control_result"]["harness_status"] = "FAIL"
        payload["payload"]["control_result"]["status_reasons"] = ["FORGED_SELF_REPORTED_FAILURE"]
        payload["trajectory"][0]["payload"]["actual_harness_status"] = "FAIL"
        trace = FormalEvalTrace.model_validate(payload)
        trace_path.write_bytes(_json_bytes(trace.model_dump(mode="json")))

    report_path = run_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["harness_status"] = "PASS"
    report["counts"]["passed_cases"] = 0
    report["counts"]["failed_cases"] = 2
    report["metrics"][0]["sample_size"] = 2
    report["metrics"][0]["success_count"] = 2
    report["metrics"][0]["value"] = 1.0
    report_path.write_bytes(
        _json_bytes(FormalEvalReport.model_validate(report).model_dump(mode="json"))
    )
    _rewrite_fixture_report_and_cohort(output, suite_id)

    result = verify_formal_eval_runs(_fixture_run_dirs(output), formal_root=formal_root)

    assert not result.ok
    assert {
        "RUN_FIXTURE_INDEPENDENT_RESULT_MISMATCH",
        "RUN_FIXTURE_CONTROL_RESULT_MISMATCH",
        "RUN_FIXTURE_REPORT_COUNTS_MISMATCH",
    } <= _codes(result)


def test_fixture_subset_is_explicitly_incomplete_even_with_completion_file(
    tmp_path: Path,
) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path)

    result = verify_formal_eval_runs(_fixture_run_dirs(output)[:3], formal_root=formal_root)

    assert "RUN_FIXTURE_COHORT_INCOMPLETE" in _codes(result)


def test_fixture_cohort_completion_file_is_required(tmp_path: Path) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path)
    (output / "cohort.json").unlink()

    result = verify_formal_eval_runs(_fixture_run_dirs(output), formal_root=formal_root)

    assert "RUN_FIXTURE_COHORT_MISSING_OR_ESCAPED" in _codes(result)


@pytest.mark.parametrize("target", ["report", "trace", "cohort"])
def test_fixture_cohort_hash_or_canonical_tamper_is_red(tmp_path: Path, target: str) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path, label=f"tamper-{target}")
    run_dir = output / "agent_trust_gold"
    path = {
        "report": run_dir / "report.json",
        "trace": sorted((run_dir / "traces").glob("*.json"))[0],
        "cohort": output / "cohort.json",
    }[target]
    path.write_bytes(path.read_bytes() + b" ")

    result = verify_formal_eval_runs(_fixture_run_dirs(output), formal_root=formal_root)

    expected = {
        "report": "RUN_FIXTURE_COHORT_REPORT_HASH_MISMATCH",
        "trace": "RUN_FIXTURE_COHORT_TRACE_HASH_MISMATCH",
        "cohort": "RUN_FIXTURE_COHORT_INVALID",
    }[target]
    assert expected in _codes(result)


def test_mixed_or_duplicate_fixture_cohorts_are_red(tmp_path: Path) -> None:
    formal_root, first = _valid_fixture_cohort(tmp_path, label="first")
    _second_root, second = _valid_fixture_cohort(tmp_path, label="second")
    mixed = (*_fixture_run_dirs(first)[:2], *_fixture_run_dirs(second)[2:])

    mixed_result = verify_formal_eval_runs(mixed, formal_root=formal_root)
    duplicate_result = verify_formal_eval_runs(
        (_fixture_run_dirs(first)[0],) * 4,
        formal_root=formal_root,
    )

    assert "RUN_FIXTURE_COHORT_COMMON_PARENT_MISMATCH" in _codes(mixed_result)
    assert "RUN_FIXTURE_COHORT_SUITE_SET_MISMATCH" in _codes(duplicate_result)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("run_id", "other-base.agent_trust_gold", "RUN_FIXTURE_COHORT_RUN_ID_MISMATCH"),
        (
            "generated_at",
            "2026-08-03T04:05:07Z",
            "RUN_FIXTURE_COHORT_GENERATED_AT_MISMATCH",
        ),
    ],
)
def test_fixture_cohort_rejects_one_suite_with_mixed_base_or_time(
    tmp_path: Path,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path, label=f"mixed-{field}")
    run_dir = output / "agent_trust_gold"
    report_path = run_dir / "report.json"
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload[field] = value
    for trace_path in sorted((run_dir / "traces").glob("*.json")):
        trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
        if field == "run_id":
            trace_payload["run_id"] = value
        else:
            trace_payload["started_at"] = value
            trace_payload["finished_at"] = value
            trace_payload["trajectory"][0]["timestamp"] = value
        trace = FormalEvalTrace.model_validate(trace_payload)
        trace_path.write_bytes(_json_bytes(trace.model_dump(mode="json")))
    report = FormalEvalReport.model_validate(report_payload)
    report_path.write_bytes(_json_bytes(report.model_dump(mode="json")))
    _rewrite_fixture_report_and_cohort(output, "agent_trust_gold")

    result = verify_formal_eval_runs(_fixture_run_dirs(output), formal_root=formal_root)

    assert expected_code in _codes(result)


def test_fixture_cohort_rejects_extra_parent_and_suite_entries(tmp_path: Path) -> None:
    formal_root, output = _valid_fixture_cohort(tmp_path)
    (output / "unexpected.txt").write_text("extra", encoding="utf-8")
    (output / "agent_trust_gold" / "notes.json").write_text("{}", encoding="utf-8")

    result = verify_formal_eval_runs(_fixture_run_dirs(output), formal_root=formal_root)

    assert {
        "RUN_FIXTURE_COHORT_EXTRA_ENTRIES",
        "RUN_FIXTURE_DIRECTORY_HAS_EXTRA_ENTRIES",
    } <= _codes(result)


def test_quality_verdict_is_blocked_until_sealed_prerequisites_are_frozen(
    tmp_path: Path,
) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    _trace_path, trace_hash = _write_trace(
        run_dir,
        "case-001-attempt-1.json",
        _trace_payload(),
    )
    report = _report_payload([trace_hash], evaluation_status="PASS")
    report["split"] = "sealed"
    _write_report(run_dir, report)

    result = verify_formal_eval_run(run_dir, formal_root=formal_root)

    assert not result.ok
    assert {
        "RUN_QUALITY_MANIFEST_NOT_READY",
        "RUN_QUALITY_EXPERIMENT_NOT_READY",
        "RUN_QUALITY_SEALED_DATA_NOT_FROZEN",
        "RUN_QUALITY_SCORER_NOT_FROZEN",
        "RUN_QUALITY_MANIFEST_SCORER_MISSING",
    } <= _codes(result)


def test_forged_report_trace_hash_is_rejected(tmp_path: Path) -> None:
    formal_root, run_dir, _trace = _valid_run(tmp_path)
    _write_report(run_dir, _report_payload(["f" * 64]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_TRACE_HASH_SET_MISMATCH" in _codes(result)


def test_identical_trace_bytes_are_rejected_even_if_report_set_matches(tmp_path: Path) -> None:
    formal_root, run_dir, trace_path = _valid_run(tmp_path)
    duplicate = run_dir / "traces" / "duplicate.json"
    duplicate.write_bytes(trace_path.read_bytes())
    trace_hash = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    # The protocol normalizes duplicate report hashes to INVALID; serializing that result must not
    # make the two physical artifacts unique.
    payload = _report_payload([trace_hash, trace_hash], passed=0, errored=1)
    payload["harness_status"] = "INVALID"
    payload["evaluation_status"] = "INVALID"
    payload["primary_metric"] = None
    payload["metrics"] = []
    _write_report(run_dir, payload)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_TRACE_CONTENT_HASHES_NOT_UNIQUE" in _codes(result)
    assert "RUN_REPORT_TRACE_HASHES_NOT_UNIQUE" in _codes(result)


def test_invalid_trace_cannot_support_pass_report(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    payload = _trace_payload()
    payload.pop("finished_at")
    _path, trace_hash = _write_trace(run_dir, "invalid.json", payload)
    _write_report(run_dir, _report_payload([trace_hash]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_VERDICT_CONTAINS_INVALID_TRACE" in _codes(result)
    assert "RUN_VERDICT_HAS_INVALID_TERMINAL_CASE" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "wrong-run"),
        ("suite_id", "wrong-suite"),
        ("suite_version", "wrong-version"),
        ("split", "sealed"),
        ("provider", "fixture"),
    ],
)
def test_trace_identity_must_match_report(tmp_path: Path, field: str, value: str) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    payload = _trace_payload()
    payload[field] = value
    _path, trace_hash = _write_trace(run_dir, "wrong-identity.json", payload)
    _write_report(run_dir, _report_payload([trace_hash]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_TRACE_IDENTITY_MISMATCH" in _codes(result)


def test_report_suite_must_be_preregistered(tmp_path: Path) -> None:
    formal_root, run_dir, trace_path = _valid_run(tmp_path)
    trace_hash = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    payload = _report_payload([trace_hash])
    payload["suite_id"] = "unknown-suite"
    _write_report(run_dir, payload)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    codes = _codes(result)
    assert "RUN_SUITE_UNKNOWN" in codes
    assert "RUN_TRACE_IDENTITY_MISMATCH" in codes


def test_report_provider_must_be_allowed_by_suite_manifest(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    trace = _trace_payload()
    trace["suite_id"] = "failure_isolation_matrix"
    _path, trace_hash = _write_trace(run_dir, "trace.json", trace)
    report = _report_payload([trace_hash], evaluation_status="NOT_RUN")
    report["suite_id"] = "failure_isolation_matrix"
    report["primary_metric"] = "harness_control_discrimination_rate"
    report["metrics"] = [
        {
            "name": "harness_control_discrimination_rate",
            "scope": "harness",
            "sample_size": 1,
            "value": 1.0,
            "success_count": 1,
            "baseline_value": None,
            "effect_size": None,
            "unit": "ratio",
        }
    ]
    _write_report(run_dir, report)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    codes = _codes(result)
    assert "RUN_PROVIDER_NOT_ALLOWED" in codes
    assert "RUN_MANIFEST_PROVIDER_NOT_ALLOWED" in codes


def test_report_metric_must_match_frozen_manifest(tmp_path: Path) -> None:
    formal_root, run_dir, trace_path = _valid_run(tmp_path)
    trace_hash = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    report = _report_payload([trace_hash], evaluation_status="PASS")
    report["primary_metric"] = "invented_quality_metric"
    report["metrics"][0]["name"] = "invented_quality_metric"
    _write_report(run_dir, report)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    codes = _codes(result)
    assert "RUN_METRIC_NOT_REGISTERED" in codes
    assert "RUN_QUALITY_PRIMARY_DIFFERS_FROM_MANIFEST" in codes


def test_missing_and_unreported_extra_traces_are_rejected(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    _one, hash_one = _write_trace(run_dir, "one.json", _trace_payload())
    payload = _trace_payload(case_id="case-002")
    _two, _hash_two = _write_trace(run_dir, "two.json", payload)
    _write_report(run_dir, _report_payload([hash_one], passed=2))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_TRACE_HASH_SET_MISMATCH" in _codes(result)


def test_duplicate_case_attempt_is_rejected_independently_of_hash(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    first = _trace_payload()
    _path, first_hash = _write_trace(run_dir, "first.json", first)
    second = _trace_payload()
    second["payload"] = {"distinct_bytes": True}
    _path, second_hash = _write_trace(run_dir, "second.json", second)
    _write_report(run_dir, _report_payload([first_hash, second_hash]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_CASE_ATTEMPT_DUPLICATE" in _codes(result)


def test_attempts_start_at_one_and_are_contiguous(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    payload = _trace_payload(attempt=2)
    _path, trace_hash = _write_trace(run_dir, "attempt-2.json", payload)
    _write_report(run_dir, _report_payload([trace_hash]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_CASE_ATTEMPTS_NOT_CONTIGUOUS" in _codes(result)


def test_counts_are_recomputed_from_each_cases_last_attempt(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    _path, first_hash = _write_trace(run_dir, "attempt-1.json", _trace_payload(status="PASS"))
    _path, second_hash = _write_trace(
        run_dir, "attempt-2.json", _trace_payload(attempt=2, status="FAIL")
    )
    _write_report(run_dir, _report_payload([first_hash, second_hash], passed=1))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_REPORT_COUNT_MISMATCH" in _codes(result)


def test_global_budget_ignores_per_trace_self_reported_cap(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    payload = _trace_payload(usage=(601, 0, 0, 0.0))
    _path, trace_hash = _write_trace(run_dir, "overspend.json", payload)
    _write_report(run_dir, _report_payload([trace_hash]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "GLOBAL_MODEL_CALL_BUDGET_EXCEEDED" in _codes(result)
    assert result.real_model_usage.model_calls == 601


def test_every_retry_and_error_attempt_consumes_aggregate_budget(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    _path, first_hash = _write_trace(
        run_dir,
        "retry-1.json",
        _trace_payload(status="FAIL", usage=(400, 0, 0, 1.0)),
    )
    _path, second_hash = _write_trace(
        run_dir,
        "retry-2.json",
        _trace_payload(attempt=2, status="PASS", usage=(300, 0, 0, 1.0)),
    )
    _write_report(run_dir, _report_payload([first_hash, second_hash]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert result.real_model_usage.attempts == 2
    assert result.real_model_usage.model_calls == 700
    assert "GLOBAL_MODEL_CALL_BUDGET_EXCEEDED" in _codes(result)


def test_retry_with_missing_usage_is_not_silently_omitted(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    first = _trace_payload(status="FAIL")
    first.pop("usage")
    _path, first_hash = _write_trace(run_dir, "retry-1.json", first)
    _path, second_hash = _write_trace(run_dir, "retry-2.json", _trace_payload(attempt=2))
    payload = _report_payload([first_hash, second_hash], passed=0, errored=1)
    payload["harness_status"] = "INVALID"
    payload["evaluation_status"] = "INVALID"
    _write_report(run_dir, payload)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "REAL_MODEL_ATTEMPT_USAGE_MISSING" in _codes(result)


def test_budget_is_aggregated_across_multiple_run_directories(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dirs: list[Path] = []
    for index in range(2):
        run_dir = tmp_path / f"run-{index}"
        trace = _trace_payload(usage=(400, 0, 0, 1.0))
        trace["run_id"] = f"run-{index}"
        _path, trace_hash = _write_trace(run_dir, "trace.json", trace)
        report = _report_payload([trace_hash])
        report["run_id"] = f"run-{index}"
        _write_report(run_dir, report)
        run_dirs.append(run_dir)
    result = verify_formal_eval_runs(run_dirs, formal_root=formal_root)
    assert result.real_model_usage.model_calls == 800
    assert "GLOBAL_MODEL_CALL_BUDGET_EXCEEDED" in _codes(result)


def test_policy_reference_path_traversal_is_rejected(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    policy_path = formal_root / "policy" / "artifact_verification_policy.json"
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["artifact_references"][0] = {
        "byte_count": len(outside.read_bytes()),
        "kind": "policy_conformance",
        "path": "../../outside.json",
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    policy_path.write_bytes(_json_bytes(payload))
    result = verify_frozen_formal_artifacts(formal_root)
    assert "ARTIFACT_PATH_ESCAPES_ROOT" in _codes(result)


def test_manifest_reference_path_traversal_is_rejected(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    manifest_path = formal_root / "suites" / SUITE_ID / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0] = {
        "byte_count": len(outside.read_bytes()),
        "path": "../../../outside.json",
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
        "split": None,
    }
    manifest_path.write_bytes(_json_bytes(payload))
    result = verify_frozen_formal_artifacts(formal_root)
    assert "ARTIFACT_PATH_ESCAPES_ROOT" in _codes(result)


def test_contract_dataset_reference_path_traversal_is_rejected(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    contract_path = formal_root / "suites" / SUITE_ID / "contract.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    payload["experiments"][0]["data_freeze"]["dev"]["relative_path"] = (
        "benchmarks/formal/../../outside"
    )
    contract_path.write_bytes(_json_bytes(payload))
    result = verify_frozen_formal_artifacts(formal_root)
    assert "ARTIFACT_PATH_ESCAPES_ROOT" in _codes(result)


def test_raw_report_must_serialize_model_validation_normalization(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "traces").mkdir(parents=True)
    payload = _report_payload([], passed=0)
    # Raw harness PASS becomes NOT_RUN under FormalEvalReport.model_validate. Writing the
    # pre-validation dict is therefore a dishonest final artifact.
    _write_report(run_dir, payload, finalize=False)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert "RUN_REPORT_NOT_FINALIZED_AFTER_MODEL_VALIDATION" in _codes(result)


def test_not_run_is_the_explicit_no_trace_exception(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "traces").mkdir(parents=True)
    payload = _report_payload([], passed=0, evaluation_status="NOT_RUN")
    _write_report(run_dir, payload)
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    assert result.ok, result.as_dict()


def test_pass_report_cannot_hide_missing_trace(tmp_path: Path) -> None:
    formal_root = _copy_formal_root(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "traces").mkdir(parents=True)
    _write_report(run_dir, _report_payload(["a" * 64]))
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    codes = _codes(result)
    assert "RUN_TRACE_HASH_SET_MISMATCH" in codes
    assert "RUN_NO_TRACE_ONLY_ALLOWED_FOR_NOT_RUN" in codes


def test_non_finite_json_and_duplicate_keys_fail_before_model_validation(tmp_path: Path) -> None:
    formal_root, run_dir, trace_path = _valid_run(tmp_path)
    content = trace_path.read_text(encoding="utf-8")
    content = content.replace('"latency_ms": 1.0', '"latency_ms": NaN')
    trace_path.write_text(content, encoding="utf-8")
    report_path = run_dir / "report.json"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace(
        '"run_id": "formal-artifact-test-run",',
        '"run_id": "x",\n  "run_id": "formal-artifact-test-run",',
    )
    report_path.write_text(report, encoding="utf-8")
    result = verify_formal_eval_run(run_dir, formal_root=formal_root)
    codes = _codes(result)
    assert "RUN_TRACE_INVALID_JSON" in codes
    assert "RUN_REPORT_INVALID_JSON" in codes


def test_cli_without_args_verifies_governance_and_tamper_exits_nonzero(tmp_path: Path) -> None:
    command = [sys.executable, "scripts/verify_formal_eval_artifacts.py"]
    passed = subprocess.run(command, cwd=Path.cwd(), capture_output=True, text=True, check=False)
    assert passed.returncode == 0
    assert json.loads(passed.stdout)["ok"] is True

    formal_root = _copy_formal_root(tmp_path)
    policy_path = formal_root / "policy" / "conformance_vectors.json"
    policy_path.write_bytes(policy_path.read_bytes() + b" ")
    failed = subprocess.run(
        [*command, "--formal-root", str(formal_root)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    payload = json.loads(failed.stdout)
    assert payload["ok"] is False
    assert isinstance(payload["errors"], list)
