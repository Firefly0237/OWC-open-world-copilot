"""Byte-level verification for frozen formal-evaluation governance and run artifacts.

This module is deliberately not a runner or scorer.  It treats committed policy, schemas,
contracts, manifests, budgets, reports, and traces as hostile bytes and reconstructs every
Pydantic value from a fresh ``json.loads`` result.  JSON Schema is useful documentation, but it is
not trusted as a substitute for the protocol models' honesty validators.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Final, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from .formal_cohort import (
    FORMAL_COHORT_SUITE_IDS,
    FormalCohortError,
    compute_formal_governance_sha256,
    parse_formal_cohort_bytes,
)
from .formal_controls import (
    DeclaredControlKind,
    FormalControlFixture,
    FormalControlResult,
    evaluate_control_implementation,
    render_control_fixtures,
)
from .formal_faults import (
    FrozenFaultManifest,
    get_frozen_fault_manifest,
    render_frozen_fault_files,
    validate_fault_manifest_against_hard_zero_catalog,
    validate_fault_manifest_against_hard_zero_gates,
)
from .formal_hard_zero import (
    FrozenHardZeroCatalog,
    get_frozen_hard_zero_catalogs,
    render_hard_zero_catalogs,
    validate_hard_zero_catalogs_against_gates,
)
from .formal_registry import (
    FormalSuiteContract,
    GlobalRealModelBudget,
    get_formal_suite_registry,
    render_frozen_formal_files,
    render_global_real_model_budget,
    validate_manifest_against_registry,
)
from .protocol import (
    EvalProvider,
    EvalSplit,
    EvalStatus,
    EvaluationCounts,
    FormalEvalManifest,
    FormalEvalReport,
    FormalEvalTrace,
    MetricResult,
    MetricScope,
    render_protocol_schemas,
)

FORMAL_ARTIFACT_POLICY_VERSION: Final[Literal["formal-artifact-verification-policy/v1"]] = (
    "formal-artifact-verification-policy/v1"
)
POLICY_CONFORMANCE_VERSION: Final[Literal["formal-artifact-policy-conformance/v1"]] = (
    "formal-artifact-policy-conformance/v1"
)

_PASS_FAIL = frozenset({EvalStatus.PASS, EvalStatus.FAIL})
_HARNESS_METRIC_NAME: Final = "harness_control_discrimination_rate"
_STATUS_FIELDS: dict[str, tuple[str, ...]] = {
    "report": ("harness_status", "evaluation_status"),
    "trace": ("status",),
}
_MODEL_BY_ARTIFACT: dict[str, type[BaseModel]] = {
    "report": FormalEvalReport,
    "trace": FormalEvalTrace,
}
_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    """One stable, machine-readable verification failure."""

    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class AggregateUsage:
    """Usage reconstructed from every real-model attempt, including errors and retries."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int | float]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "attempts": self.attempts,
        }


@dataclass(frozen=True, slots=True)
class FormalArtifactVerification:
    """Structured verifier result used by tests and the offline CLI."""

    scope: str
    checked_files: tuple[str, ...]
    errors: tuple[VerificationIssue, ...]
    real_model_usage: AggregateUsage = AggregateUsage()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "ok": self.ok,
            "checked_file_count": len(self.checked_files),
            "checked_files": list(self.checked_files),
            "real_model_usage": self.real_model_usage.as_dict(),
            "errors": [error.as_dict() for error in self.errors],
        }


class _Collector:
    def __init__(self) -> None:
        self.errors: list[VerificationIssue] = []
        self.checked_files: set[str] = set()

    def check(self, path: Path) -> None:
        self.checked_files.add(str(path))

    def add(self, code: str, path: str | Path, message: str) -> None:
        self.errors.append(VerificationIssue(code=code, path=str(path), message=message))


class _DuplicateKeyError(ValueError):
    pass


def default_formal_root() -> Path:
    """Return the repository's committed ``benchmarks/formal`` directory."""

    return Path(__file__).resolve().parents[3] / "benchmarks" / "formal"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(raw: bytes) -> Any:
    """Parse UTF-8 JSON while rejecting duplicate keys and NaN/Infinity extensions."""

    text = raw.decode("utf-8", errors="strict")
    return json.loads(
        text,
        object_pairs_hook=_duplicate_rejecting_object,
        parse_constant=_reject_non_finite_constant,
    )


def _read_json(path: Path, collector: _Collector, *, code: str) -> Any | None:
    collector.check(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        collector.add(f"{code}_READ_ERROR", path, str(exc))
        return None
    try:
        return _strict_json_loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
        collector.add(f"{code}_INVALID_JSON", path, str(exc))
        return None


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_ref(path: str, content: bytes, kind: str) -> dict[str, object]:
    return {
        "byte_count": len(content),
        "kind": kind,
        "path": path,
        "sha256": _sha256(content),
    }


def _quality_metric(*, sample_size: int = 1, value: float = 1.0) -> dict[str, object]:
    return {
        "name": "quality_primary",
        "scope": "quality",
        "sample_size": sample_size,
        "value": value,
        "success_count": sample_size,
        "baseline_value": 0.0,
        "effect_size": value,
        "unit": "ratio",
    }


def _base_report(*, cases: int = 1) -> dict[str, object]:
    return {
        "schema_version": "formal-eval/v1",
        "suite_id": "conformance_suite",
        "suite_version": "n0.3-v1",
        "run_id": "conformance-run",
        "split": "dev",
        "provider": "real_model",
        "generated_at": "2026-08-03T00:00:00Z",
        "harness_status": "PASS",
        "evaluation_status": "PASS",
        "skipped": False,
        "status_reasons": [],
        "counts": {
            "total_cases": cases,
            "executed_cases": cases,
            "passed_cases": cases,
            "failed_cases": 0,
            "errored_cases": 0,
            "skipped_cases": 0,
        },
        "primary_metric": "quality_primary",
        "metrics": [_quality_metric(sample_size=cases)],
        "trace_sha256s": [f"{index + 1:064x}" for index in range(cases)],
    }


def _base_trace() -> dict[str, object]:
    return {
        "schema_version": "formal-eval/v1",
        "suite_id": "conformance_suite",
        "suite_version": "n0.3-v1",
        "run_id": "conformance-run",
        "case_id": "case-001",
        "attempt": 1,
        "split": "dev",
        "provider": "real_model",
        "status": "PASS",
        "status_reasons": [],
        "started_at": "2026-08-03T00:00:00Z",
        "finished_at": "2026-08-03T00:00:01Z",
        "model": {
            "provider": "offline-conformance",
            "full_model_id": "offline-conformance/model-v1",
            "configuration_sha256": "1" * 64,
        },
        "prompt_sha256": "2" * 64,
        "tool_schema_sha256": "3" * 64,
        "scorer": {
            "scorer_id": "offline-conformance",
            "scorer_version": "v1",
            "sha256": "4" * 64,
        },
        "budget": {"model_call_cap": 1, "token_cap": 100, "cost_usd_cap": 1.0},
        "usage": {
            "model_calls": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_usd": 0.1,
            "latency_ms": 100.0,
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


def _policy_conformance_vectors() -> tuple[dict[str, object], ...]:
    """Return deterministic adversarial inputs for the executable policy contract."""

    fixture_lure = _base_report()
    fixture_lure["provider"] = "fixture"

    harness_non_pass = _base_report()
    harness_non_pass["harness_status"] = "FAIL"

    zero_cases = _base_report(cases=0)
    zero_cases["primary_metric"] = None
    zero_cases["metrics"] = []

    all_error = _base_report()
    all_error["counts"] = {
        "total_cases": 1,
        "executed_cases": 1,
        "passed_cases": 0,
        "failed_cases": 0,
        "errored_cases": 1,
        "skipped_cases": 0,
    }

    missing_model = _base_trace()
    missing_model.pop("model")

    missing_finished = _base_trace()
    missing_finished.pop("finished_at")

    zero_call = _base_trace()
    assert isinstance(zero_call["usage"], dict)
    zero_call["usage"]["model_calls"] = 0

    unordered = _base_trace()
    unordered["trajectory"] = [
        {"sequence": 1, "kind": "second", "timestamp": None, "payload": {}},
        {"sequence": 0, "kind": "first", "timestamp": None, "payload": {}},
    ]

    nan_metric = _base_report()

    missing_denominator = _base_report()
    assert isinstance(missing_denominator["metrics"], list)
    missing_denominator["metrics"][0].pop("sample_size")

    duplicate_hash = _base_report(cases=2)
    duplicate_hash["trace_sha256s"] = ["a" * 64, "a" * 64]

    insufficient_hash = _base_report(cases=2)
    insufficient_hash["trace_sha256s"] = ["a" * 64]

    return (
        {
            "artifact": "report",
            "expected": {"validation": "REJECT"},
            "id": "fixture_quality_pass_lure",
            "payload": fixture_lure,
        },
        {
            "artifact": "report",
            "expected": {
                "evaluation_status": "INVALID",
                "harness_status": "FAIL",
                "required_reasons": ["HARNESS_STATUS_FAIL"],
                "validation": "ACCEPT",
            },
            "id": "non_pass_harness_cannot_carry_quality_verdict",
            "payload": harness_non_pass,
        },
        {
            "artifact": "report",
            "expected": {
                "evaluation_status": "NOT_RUN",
                "harness_status": "NOT_RUN",
                "required_reasons": ["NO_EXECUTED_CASES"],
                "validation": "ACCEPT",
            },
            "id": "zero_over_zero_is_not_run",
            "payload": zero_cases,
        },
        {
            "artifact": "report",
            "expected": {
                "evaluation_status": "INVALID",
                "harness_status": "INVALID",
                "required_reasons": ["ALL_TRIALS_ERRORED"],
                "validation": "ACCEPT",
            },
            "id": "all_error_is_invalid",
            "payload": all_error,
        },
        {
            "artifact": "trace",
            "expected": {
                "required_reasons": ["MISSING_REAL_MODEL_MODEL"],
                "status": "INVALID",
                "validation": "ACCEPT",
            },
            "id": "real_model_missing_model_metadata",
            "payload": missing_model,
        },
        {
            "artifact": "trace",
            "expected": {
                "required_reasons": ["MISSING_REAL_MODEL_FINISHED_AT"],
                "status": "INVALID",
                "validation": "ACCEPT",
            },
            "id": "real_model_missing_finished_at",
            "payload": missing_finished,
        },
        {
            "artifact": "trace",
            "expected": {
                "required_reasons": ["ZERO_REAL_MODEL_CALLS"],
                "status": "INVALID",
                "validation": "ACCEPT",
            },
            "id": "real_model_zero_call_pass_lure",
            "payload": zero_call,
        },
        {
            "artifact": "trace",
            "expected": {
                "required_reasons": ["INVALID_TRACE_SEQUENCE"],
                "status": "INVALID",
                "validation": "ACCEPT",
            },
            "id": "unordered_trajectory_sequence",
            "payload": unordered,
        },
        {
            "artifact": "report",
            "expected": {"validation": "REJECT"},
            "id": "non_finite_metric_value",
            "mutation": {"path": ["metrics", 0, "value"], "value_kind": "nan"},
            "payload": nan_metric,
        },
        {
            "artifact": "report",
            "expected": {"validation": "REJECT"},
            "id": "missing_primary_denominator",
            "payload": missing_denominator,
        },
        {
            "artifact": "report",
            "expected": {
                "evaluation_status": "INVALID",
                "harness_status": "PASS",
                "required_reasons": ["DUPLICATE_TRACE_HASHES"],
                "validation": "ACCEPT",
            },
            "id": "duplicate_trace_hashes",
            "payload": duplicate_hash,
        },
        {
            "artifact": "report",
            "expected": {
                "evaluation_status": "INVALID",
                "harness_status": "PASS",
                "required_reasons": ["INSUFFICIENT_TRACE_HASHES"],
                "validation": "ACCEPT",
            },
            "id": "insufficient_trace_hashes",
            "payload": insufficient_hash,
        },
    )


def render_policy_conformance_vectors() -> bytes:
    """Render the executable adversarial corpus from the Python source of truth."""

    return _json_bytes(
        {
            "schema_validation_alone_is_sufficient": False,
            "schema_version": POLICY_CONFORMANCE_VERSION,
            "validation_pipeline": ["json.loads", "pydantic.model_validate"],
            "vectors": _policy_conformance_vectors(),
        }
    )


def _governance_bytes() -> dict[str, bytes]:
    files = {
        "../global_real_model_budget.json": render_global_real_model_budget(),
        **{f"../schemas/{name}": content for name, content in render_protocol_schemas().items()},
        **{f"../{name}": content for name, content in render_frozen_formal_files().items()},
        **{f"../controls/{name}": content for name, content in render_control_fixtures().items()},
        **{
            f"../hard_zero/{name}": content for name, content in render_hard_zero_catalogs().items()
        },
        **{f"../faults/{name}": content for name, content in render_frozen_fault_files().items()},
    }
    # ``render_frozen_formal_files`` includes the budget too; the dictionary intentionally
    # collapses it to one path so there remains exactly one aggregate budget artifact.
    return files


def render_formal_artifact_policy_files() -> dict[str, bytes]:
    """Render every committed file under ``benchmarks/formal/policy``."""

    conformance = render_policy_conformance_vectors()
    references = [_artifact_ref("conformance_vectors.json", conformance, "policy_conformance")]
    for path, content in sorted(_governance_bytes().items()):
        kind = "global_budget"
        if "/schemas/" in path:
            kind = "protocol_schema"
        elif "/controls/" in path:
            kind = "formal_control"
        elif "/hard_zero/" in path:
            kind = "hard_zero_case_catalog"
        elif "/faults/" in path:
            kind = "fault_manifest"
        elif "/suites/" in path and path.endswith("/contract.json"):
            kind = "suite_contract"
        elif "/suites/" in path and path.endswith("/manifest.json"):
            kind = "suite_manifest"
        references.append(_artifact_ref(path, content, kind))

    policy = {
        "artifact_references": references,
        "budget_accounting": {
            "aggregate_source": "all_real_model_trace_attempts",
            "count_error_and_retry_attempts": True,
            "self_reported_trace_budget_is_global_authority": False,
            "token_definition": "input_tokens_plus_output_tokens",
            "unique_global_budget_path": "../global_real_model_budget.json",
        },
        "final_json_validation": {
            "duplicate_keys_allowed": False,
            "non_finite_numbers_allowed": False,
            "pipeline": ["utf8_decode", "json.loads", "pydantic.model_validate"],
            "schema_only_validation_allowed": False,
            "status_normalization_must_be_serialized": True,
        },
        "run_layout": {
            "no_trace_exception": (
                "both report statuses NOT_RUN, executed_cases=0, and trace_sha256s empty"
            ),
            "report": "report.json",
            "trace_glob": "traces/*.json",
        },
        "run_invariants": [
            "actual trace SHA256 set equals the report set and both are unique",
            "suite/version/run/split/provider agree across report and every trace",
            "report metrics and any quality primary match the frozen suite manifest",
            (
                "quality PASS or FAIL requires sealed split and PASS-ready frozen manifest, "
                "experiment, dataset, and scorer provenance"
            ),
            "case_id plus attempt is unique and attempts start at one without gaps",
            "counts are recomputed from each distinct case's final attempt",
            (
                "fixture CANARY traces are independently adjudicated from frozen control bytes; "
                "trace and report claims cannot substitute for control evaluation"
            ),
            (
                "any fixture CANARY publication requires one canonical completion cohort binding "
                "exactly the four suites, one base run, timestamp, governance tree, reports, "
                "and traces"
            ),
            "PASS or FAIL cannot omit traces or contain skipped, unavailable, or INVALID attempts",
            "all real-model attempts, including errors and retries, consume the one global budget",
        ],
        "schema_version": FORMAL_ARTIFACT_POLICY_VERSION,
    }
    return {
        "artifact_verification_policy.json": _json_bytes(policy),
        "conformance_vectors.json": conformance,
    }


def _apply_vector_mutation(payload: Any, mutation: Mapping[str, Any]) -> None:
    path = mutation.get("path")
    if not isinstance(path, list) or not path:
        raise ValueError("mutation path must be a non-empty list")
    target = payload
    for component in path[:-1]:
        target = target[component]
    value_kind = mutation.get("value_kind")
    if value_kind != "nan":
        raise ValueError(f"unknown mutation value_kind: {value_kind!r}")
    target[path[-1]] = math.nan


def _verify_conformance_vectors(payload: Any, collector: _Collector, path: Path) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("vectors"), list):
        collector.add("POLICY_CONFORMANCE_SHAPE_INVALID", path, "vectors must be a list")
        return
    if payload.get("schema_validation_alone_is_sufficient") is not False:
        collector.add(
            "POLICY_SCHEMA_ONLY_BYPASS",
            path,
            "schema validation alone must be explicitly insufficient",
        )
    ids: list[str] = []
    for index, vector in enumerate(payload["vectors"]):
        label = f"{path}#vectors[{index}]"
        if not isinstance(vector, dict):
            collector.add("POLICY_VECTOR_SHAPE_INVALID", label, "vector must be an object")
            continue
        vector_id = vector.get("id")
        if not isinstance(vector_id, str):
            collector.add("POLICY_VECTOR_SHAPE_INVALID", label, "id must be a string")
            continue
        ids.append(vector_id)
        artifact_value = vector.get("artifact")
        artifact = artifact_value if isinstance(artifact_value, str) else ""
        model = _MODEL_BY_ARTIFACT.get(artifact)
        if model is None:
            collector.add("POLICY_VECTOR_UNKNOWN_ARTIFACT", label, repr(artifact_value))
            continue
        expected = vector.get("expected")
        if not isinstance(expected, dict) or expected.get("validation") not in {
            "ACCEPT",
            "REJECT",
        }:
            collector.add("POLICY_VECTOR_SHAPE_INVALID", label, "invalid expectation")
            continue
        candidate = copy.deepcopy(vector.get("payload"))
        mutation = vector.get("mutation")
        if mutation is not None:
            if not isinstance(mutation, dict):
                collector.add("POLICY_VECTOR_SHAPE_INVALID", label, "mutation must be an object")
                continue
            try:
                _apply_vector_mutation(candidate, mutation)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                collector.add("POLICY_VECTOR_MUTATION_INVALID", label, str(exc))
                continue

        try:
            # This call is the executable policy boundary.  Validating the JSON Schema alone would
            # miss status normalization performed by the protocol models.
            validated = model.model_validate(candidate)
        except (ValidationError, ValueError, TypeError) as exc:
            if expected["validation"] != "REJECT":
                collector.add("POLICY_VECTOR_UNEXPECTED_REJECTION", label, str(exc))
            continue
        if expected["validation"] != "ACCEPT":
            collector.add(
                "POLICY_VECTOR_UNEXPECTED_ACCEPTANCE",
                label,
                "model_validate accepted a vector that must be rejected",
            )
            continue
        for field in _STATUS_FIELDS[artifact]:
            wanted = expected.get(field)
            if wanted is None:
                continue
            actual = getattr(validated, field)
            actual_value = actual.value if isinstance(actual, EvalStatus) else actual
            if actual_value != wanted:
                collector.add(
                    "POLICY_VECTOR_STATUS_MISMATCH",
                    label,
                    f"{field}: expected {wanted!r}, got {actual_value!r}",
                )
        reasons = set(getattr(validated, "status_reasons", ()))
        required_reasons = expected.get("required_reasons", [])
        if not isinstance(required_reasons, list) or not set(required_reasons) <= reasons:
            collector.add(
                "POLICY_VECTOR_REASON_MISMATCH",
                label,
                f"required={required_reasons!r}, actual={sorted(reasons)!r}",
            )
    if len(ids) != len(set(ids)):
        collector.add("POLICY_VECTOR_IDS_NOT_UNIQUE", path, "vector ids must be unique")


def _is_absolute_any_platform(value: str) -> bool:
    windows_path = PureWindowsPath(value)
    return (
        Path(value).is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
    )


def _contained_path(
    *,
    base: Path,
    relative: str,
    containment_root: Path,
    collector: _Collector,
    label: str,
    must_exist: bool,
) -> Path | None:
    if not relative or _is_absolute_any_platform(relative):
        collector.add("ARTIFACT_PATH_NOT_RELATIVE", label, relative)
        return None
    root = containment_root.resolve()
    try:
        candidate = (base / relative).resolve(strict=must_exist)
    except OSError as exc:
        collector.add("ARTIFACT_PATH_RESOLUTION_FAILED", label, str(exc))
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        collector.add(
            "ARTIFACT_PATH_ESCAPES_ROOT",
            label,
            f"{relative!r} resolves outside {root}",
        )
        return None
    return candidate


def _verify_reference(
    reference: Mapping[str, Any],
    *,
    base: Path,
    root: Path,
    collector: _Collector,
    label: str,
) -> None:
    relative = reference.get("path")
    expected_hash = reference.get("sha256")
    expected_bytes = reference.get("byte_count")
    if not isinstance(relative, str):
        collector.add("ARTIFACT_REFERENCE_INVALID", label, "path must be a string")
        return
    candidate = _contained_path(
        base=base,
        relative=relative,
        containment_root=root,
        collector=collector,
        label=label,
        must_exist=True,
    )
    if candidate is None:
        return
    collector.check(candidate)
    if not candidate.is_file():
        collector.add("ARTIFACT_REFERENCE_NOT_FILE", label, str(candidate))
        return
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        collector.add("ARTIFACT_REFERENCE_READ_ERROR", label, str(exc))
        return
    if expected_hash != _sha256(content):
        collector.add("ARTIFACT_REFERENCE_HASH_MISMATCH", label, relative)
    if expected_bytes != len(content):
        collector.add("ARTIFACT_REFERENCE_BYTE_COUNT_MISMATCH", label, relative)


def _verify_exact_file(path: Path, expected: bytes, collector: _Collector, *, kind: str) -> None:
    collector.check(path)
    try:
        actual = path.read_bytes()
    except OSError as exc:
        collector.add(f"{kind}_MISSING", path, str(exc))
        return
    if actual != expected:
        collector.add(f"{kind}_BYTE_MISMATCH", path, "committed bytes differ from renderer")


def _walk_external_schema_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/"):
            yield ref
        for nested in value.values():
            yield from _walk_external_schema_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_external_schema_refs(nested)


def _verify_contract_paths(
    contract: FormalSuiteContract,
    *,
    contract_path: Path,
    formal_root: Path,
    collector: _Collector,
) -> None:
    budget_ref = contract.global_real_model_budget.model_dump(mode="json")
    _verify_reference(
        budget_ref,
        base=contract_path.parent,
        root=formal_root,
        collector=collector,
        label=f"{contract_path}#global_real_model_budget",
    )
    _verify_reference(
        contract.hard_zero_catalog.model_dump(mode="json"),
        base=contract_path.parent,
        root=formal_root,
        collector=collector,
        label=f"{contract_path}#hard_zero_catalog",
    )
    if contract.fault_manifest is not None:
        _verify_reference(
            contract.fault_manifest.model_dump(mode="json"),
            base=contract_path.parent,
            root=formal_root,
            collector=collector,
            label=f"{contract_path}#fault_manifest",
        )
    for experiment in contract.experiments:
        if experiment.data_freeze is None:
            continue
        for slot_name, slot in (
            ("dev", experiment.data_freeze.dev),
            ("sealed", experiment.data_freeze.sealed),
        ):
            label = f"{contract_path}#{experiment.experiment_id}.{slot_name}"
            logical_path = Path(slot.relative_path).as_posix()
            formal_prefix = "benchmarks/formal/"
            if not logical_path.startswith(formal_prefix):
                collector.add(
                    "FORMAL_DATASET_PATH_OUTSIDE_NAMESPACE",
                    label,
                    slot.relative_path,
                )
                continue
            candidate = _contained_path(
                # Dataset paths are repository-logical names.  Stripping the frozen namespace
                # keeps verification relocatable for an independently copied formal root while
                # preserving the same containment boundary.
                base=formal_root,
                relative=logical_path.removeprefix(formal_prefix),
                containment_root=formal_root,
                collector=collector,
                label=label,
                must_exist=False,
            )
            if candidate is None:
                continue
            if slot.content_sha256 is not None:
                if not candidate.is_file():
                    collector.add("FROZEN_DATASET_MISSING", label, str(candidate))
                elif _sha256(candidate.read_bytes()) != slot.content_sha256:
                    collector.add("FROZEN_DATASET_HASH_MISMATCH", label, str(candidate))


def verify_frozen_formal_artifacts(
    formal_root: str | Path | None = None,
) -> FormalArtifactVerification:
    """Verify all frozen schemas, registry files, policy vectors, hashes, and paths."""

    root = Path(formal_root) if formal_root is not None else default_formal_root()
    collector = _Collector()
    if not root.is_dir():
        collector.add("FORMAL_ROOT_MISSING", root, "formal root is not a directory")
        return FormalArtifactVerification(
            scope="frozen_governance",
            checked_files=(),
            errors=tuple(collector.errors),
        )
    root = root.resolve()

    expected_files: dict[Path, tuple[bytes, str]] = {}
    for filename, content in render_protocol_schemas().items():
        expected_files[root / "schemas" / filename] = (content, "PROTOCOL_SCHEMA")
    for relative, content in render_frozen_formal_files().items():
        expected_files[root / relative] = (content, "FORMAL_REGISTRY")
    for relative, content in render_control_fixtures().items():
        expected_files[root / "controls" / relative] = (content, "FORMAL_CONTROL")
    for relative, content in render_hard_zero_catalogs().items():
        expected_files[root / "hard_zero" / relative] = (content, "HARD_ZERO_CATALOG")
    for relative, content in render_frozen_fault_files().items():
        expected_files[root / "faults" / relative] = (content, "FAULT_MANIFEST")
    for relative, content in render_formal_artifact_policy_files().items():
        expected_files[root / "policy" / relative] = (content, "FORMAL_POLICY")
    for path, (content, kind) in expected_files.items():
        _verify_exact_file(path, content, collector, kind=kind)

    budget_path = root / "global_real_model_budget.json"
    budget_payload = _read_json(budget_path, collector, code="GLOBAL_BUDGET")
    if budget_payload is not None:
        try:
            budget = GlobalRealModelBudget.model_validate(budget_payload)
        except ValidationError as exc:
            collector.add("GLOBAL_BUDGET_MODEL_INVALID", budget_path, str(exc))
        else:
            if _json_bytes(budget.model_dump(mode="json")) != render_global_real_model_budget():
                collector.add(
                    "GLOBAL_BUDGET_RENDER_MISMATCH",
                    budget_path,
                    "validated budget differs from the unique registry renderer",
                )

    for schema_name in render_protocol_schemas():
        schema_path = root / "schemas" / schema_name
        schema_payload = _read_json(schema_path, collector, code="PROTOCOL_SCHEMA")
        if schema_payload is not None:
            for external_ref in _walk_external_schema_refs(schema_payload):
                collector.add(
                    "PROTOCOL_SCHEMA_EXTERNAL_REFERENCE",
                    schema_path,
                    f"external $ref is forbidden: {external_ref}",
                )

    registry = get_formal_suite_registry()
    for suite_id, expected_contract in registry.items():
        suite_dir = root / "suites" / suite_id
        contract_path = suite_dir / "contract.json"
        manifest_path = suite_dir / "manifest.json"
        contract_payload = _read_json(contract_path, collector, code="SUITE_CONTRACT")
        contract: FormalSuiteContract | None = None
        if contract_payload is not None:
            try:
                contract = FormalSuiteContract.model_validate(contract_payload)
            except ValidationError as exc:
                collector.add("SUITE_CONTRACT_MODEL_INVALID", contract_path, str(exc))
            else:
                if contract.model_dump(mode="json") != expected_contract.model_dump(mode="json"):
                    collector.add(
                        "SUITE_CONTRACT_REGISTRY_MISMATCH",
                        contract_path,
                        "contract differs from preregistration",
                    )
                _verify_contract_paths(
                    contract,
                    contract_path=contract_path,
                    formal_root=root,
                    collector=collector,
                )

        manifest_payload = _read_json(manifest_path, collector, code="SUITE_MANIFEST")
        if manifest_payload is not None:
            try:
                manifest = FormalEvalManifest.model_validate(manifest_payload)
            except ValidationError as exc:
                collector.add("SUITE_MANIFEST_MODEL_INVALID", manifest_path, str(exc))
            else:
                try:
                    validate_manifest_against_registry(manifest)
                except ValueError as exc:
                    collector.add("SUITE_MANIFEST_MODEL_INVALID", manifest_path, str(exc))
                # Reference integrity is checked even when preregistration comparison failed;
                # otherwise a forged path could hide behind the earlier registry error.
                for index, reference in enumerate(manifest.artifacts):
                    _verify_reference(
                        reference.model_dump(mode="json"),
                        base=suite_dir,
                        root=root,
                        collector=collector,
                        label=f"{manifest_path}#artifacts[{index}]",
                    )

    parsed_hard_zero: dict[str, FrozenHardZeroCatalog] = {}
    expected_hard_zero = get_frozen_hard_zero_catalogs()
    for suite_id, expected_catalog in expected_hard_zero.items():
        catalog_path = root / "hard_zero" / suite_id / "cases.json"
        payload = _read_json(catalog_path, collector, code="HARD_ZERO_CATALOG")
        if payload is None:
            continue
        try:
            catalog = FrozenHardZeroCatalog.model_validate(payload)
        except ValidationError as exc:
            collector.add("HARD_ZERO_CATALOG_MODEL_INVALID", catalog_path, str(exc))
            continue
        parsed_hard_zero[suite_id] = catalog
        if catalog.model_dump(mode="json") != expected_catalog.model_dump(mode="json"):
            collector.add(
                "HARD_ZERO_CATALOG_CANONICAL_MISMATCH",
                catalog_path,
                "catalog differs from the canonical Python renderer",
            )

    gates_by_suite = {
        suite_id: tuple(gate.model_dump(mode="json") for gate in contract.hard_zero_gates)
        for suite_id, contract in registry.items()
    }
    if set(parsed_hard_zero) == set(registry):
        try:
            validate_hard_zero_catalogs_against_gates(parsed_hard_zero, gates_by_suite)
        except ValueError as exc:
            collector.add("HARD_ZERO_REGISTRY_CROSSCHECK_FAILED", root / "hard_zero", str(exc))

    fault_path = root / "faults" / "failure_isolation_matrix" / "fault_manifest.json"
    fault_payload = _read_json(fault_path, collector, code="FAULT_MANIFEST")
    if fault_payload is not None:
        try:
            fault_manifest = FrozenFaultManifest.model_validate(fault_payload)
        except ValidationError as exc:
            collector.add("FAULT_MANIFEST_MODEL_INVALID", fault_path, str(exc))
        else:
            if fault_manifest.model_dump(mode="json") != get_frozen_fault_manifest().model_dump(
                mode="json"
            ):
                collector.add(
                    "FAULT_MANIFEST_CANONICAL_MISMATCH",
                    fault_path,
                    "fault manifest differs from the canonical Python renderer",
                )
            try:
                validate_fault_manifest_against_hard_zero_gates(
                    fault_manifest,
                    gates_by_suite["failure_isolation_matrix"],
                )
                failure_catalog = parsed_hard_zero.get("failure_isolation_matrix")
                if failure_catalog is None:
                    raise ValueError("FAILURE_HARD_ZERO_CATALOG_UNAVAILABLE")
                validate_fault_manifest_against_hard_zero_catalog(fault_manifest, failure_catalog)
            except ValueError as exc:
                collector.add("FAULT_MANIFEST_HARD_ZERO_CROSSCHECK_FAILED", fault_path, str(exc))

    policy_path = root / "policy" / "artifact_verification_policy.json"
    policy_payload = _read_json(policy_path, collector, code="FORMAL_POLICY")
    if isinstance(policy_payload, dict):
        references = policy_payload.get("artifact_references")
        if not isinstance(references, list):
            collector.add(
                "FORMAL_POLICY_REFERENCES_INVALID",
                policy_path,
                "artifact_references must be a list",
            )
        else:
            paths: list[str] = []
            for index, reference in enumerate(references):
                label = f"{policy_path}#artifact_references[{index}]"
                if not isinstance(reference, dict):
                    collector.add("ARTIFACT_REFERENCE_INVALID", label, "must be an object")
                    continue
                if isinstance(reference.get("path"), str):
                    paths.append(reference["path"])
                _verify_reference(
                    reference,
                    base=policy_path.parent,
                    root=root,
                    collector=collector,
                    label=label,
                )
            if len(paths) != len(set(paths)):
                collector.add(
                    "FORMAL_POLICY_REFERENCE_PATHS_NOT_UNIQUE",
                    policy_path,
                    "reference paths must be unique",
                )

    conformance_path = root / "policy" / "conformance_vectors.json"
    conformance_payload = _read_json(conformance_path, collector, code="POLICY_CONFORMANCE")
    if conformance_payload is not None:
        _verify_conformance_vectors(conformance_payload, collector, conformance_path)

    return FormalArtifactVerification(
        scope="frozen_governance",
        checked_files=tuple(sorted(collector.checked_files)),
        errors=tuple(collector.errors),
    )


def _raw_status_is_finalized(
    raw: Mapping[str, Any], model: FormalEvalReport | FormalEvalTrace
) -> bool:
    fields = (
        ("harness_status", "evaluation_status")
        if isinstance(model, FormalEvalReport)
        else ("status",)
    )
    for field in fields:
        actual = getattr(model, field)
        value = actual.value if isinstance(actual, EvalStatus) else actual
        if raw.get(field) != value:
            return False
    raw_reasons = raw.get("status_reasons", [])
    return isinstance(raw_reasons, list) and tuple(raw_reasons) == model.status_reasons


def _safe_run_file(path: Path, root: Path, collector: _Collector, *, label: str) -> Path | None:
    if _is_link_like(path):
        collector.add("RUN_ARTIFACT_SYMLINK_FORBIDDEN", path, label)
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        collector.add("RUN_ARTIFACT_PATH_ESCAPE", path, str(exc))
        return None
    if not resolved.is_file():
        collector.add("RUN_ARTIFACT_NOT_FILE", path, label)
        return None
    return resolved


def _validate_budget_file(formal_root: Path, collector: _Collector) -> GlobalRealModelBudget | None:
    path = formal_root / "global_real_model_budget.json"
    payload = _read_json(path, collector, code="GLOBAL_BUDGET")
    if payload is None:
        return None
    try:
        return GlobalRealModelBudget.model_validate(payload)
    except ValidationError as exc:
        collector.add("GLOBAL_BUDGET_MODEL_INVALID", path, str(exc))
        return None


@dataclass(frozen=True, slots=True)
class _FixtureGovernance:
    manifest: FormalEvalManifest
    contract: FormalSuiteContract
    fixture: FormalControlFixture
    input_artifacts: dict[str, dict[str, str | int]]


def _is_link_like(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_check and junction_check())


def _load_governance_model(
    formal_root: Path,
    relative: Path,
    model_type: type[_ModelT],
    collector: _Collector,
) -> tuple[_ModelT, bytes] | None:
    path = formal_root / relative
    if _is_link_like(path):
        collector.add("RUN_FIXTURE_GOVERNANCE_LINK_FORBIDDEN", path, relative.as_posix())
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(formal_root)
    except (OSError, ValueError) as exc:
        collector.add("RUN_FIXTURE_GOVERNANCE_PATH_INVALID", path, str(exc))
        return None
    if not resolved.is_file():
        collector.add("RUN_FIXTURE_GOVERNANCE_NOT_FILE", path, relative.as_posix())
        return None
    collector.check(resolved)
    try:
        content = resolved.read_bytes()
        payload = _strict_json_loads(content)
    except OSError as exc:
        collector.add("RUN_FIXTURE_GOVERNANCE_READ_ERROR", resolved, str(exc))
        return None
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
        collector.add("RUN_FIXTURE_GOVERNANCE_INVALID_JSON", resolved, str(exc))
        return None
    try:
        return model_type.model_validate(payload), content
    except ValidationError as exc:
        collector.add("RUN_FIXTURE_GOVERNANCE_MODEL_INVALID", resolved, str(exc))
        return None


def _load_fixture_governance(
    formal_root: Path,
    suite_id: str,
    collector: _Collector,
) -> _FixtureGovernance | None:
    relatives = {
        "manifest": Path("suites", suite_id, "manifest.json"),
        "contract": Path("suites", suite_id, "contract.json"),
        "control": Path("controls", suite_id, "cases.json"),
    }
    manifest_item = _load_governance_model(
        formal_root, relatives["manifest"], FormalEvalManifest, collector
    )
    contract_item = _load_governance_model(
        formal_root, relatives["contract"], FormalSuiteContract, collector
    )
    fixture_item = _load_governance_model(
        formal_root, relatives["control"], FormalControlFixture, collector
    )
    if manifest_item is None or contract_item is None or fixture_item is None:
        return None
    manifest, manifest_bytes = manifest_item
    contract, contract_bytes = contract_item
    fixture, fixture_bytes = fixture_item
    if (
        manifest.suite_id != suite_id
        or contract.suite_id.value != suite_id
        or fixture.suite_id != suite_id
    ):
        collector.add(
            "RUN_FIXTURE_GOVERNANCE_SUITE_MISMATCH",
            formal_root / "suites" / suite_id,
            "manifest, contract, and control fixture must match the report suite",
        )
        return None
    contents = {
        "manifest": manifest_bytes,
        "contract": contract_bytes,
        "control": fixture_bytes,
    }
    return _FixtureGovernance(
        manifest=manifest,
        contract=contract,
        fixture=fixture,
        input_artifacts={
            name: {
                "path": relatives[name].as_posix(),
                "sha256": _sha256(content),
                "byte_count": len(content),
            }
            for name, content in sorted(contents.items())
        },
    )


def _fixture_quality_status(contract: FormalSuiteContract) -> EvalStatus:
    blocker_ids = {
        blocker.blocker_id
        for experiment in contract.experiments
        for blocker in experiment.external_data_blockers
    }
    return EvalStatus.BLOCKED_EXTERNAL_DATA if blocker_ids else EvalStatus.NOT_RUN


def _fixture_report_reasons(
    contract: FormalSuiteContract,
    *,
    harness_ok: bool,
) -> tuple[str, ...]:
    blocker_ids = tuple(
        sorted(
            {
                blocker.blocker_id
                for experiment in contract.experiments
                for blocker in experiment.external_data_blockers
            }
        )
    )
    quality_reasons = (
        tuple(f"BLOCKED_EXTERNAL_DATA:{blocker_id}" for blocker_id in blocker_ids)
        if blocker_ids
        else ("HARNESS_ONLY_FIXTURE_HAS_NO_QUALITY_EVALUATION",)
    )
    return (
        "HARNESS_ONLY_FIXTURE_CONTROL",
        "NON_REAL_PROVIDER_HAS_NO_QUALITY_EFFECT",
        "PRODUCTION_PATH_NOT_EXERCISED",
        *quality_reasons,
        (
            "MATCHED_GOOD_AND_MUTANT_DISCRIMINATED"
            if harness_ok
            else "CONTROL_DISCRIMINATION_FAILED"
        ),
    )


def _verify_fixture_harness_run(
    *,
    root: Path,
    formal_root: Path,
    report_path: Path,
    report: FormalEvalReport,
    traces: Sequence[tuple[FormalEvalTrace, Path]],
    actual_hashes: Sequence[str],
    collector: _Collector,
) -> None:
    if report.split is not EvalSplit.CANARY:
        collector.add(
            "RUN_FIXTURE_REQUIRES_CANARY_SPLIT",
            report_path,
            f"expected canary, got {report.split.value}",
        )
    if report.suite_id not in FORMAL_COHORT_SUITE_IDS:
        collector.add(
            "RUN_FIXTURE_SUITE_NOT_OFFICIAL",
            report_path,
            report.suite_id,
        )
        return
    governance = _load_fixture_governance(formal_root, report.suite_id, collector)
    if governance is None:
        return
    manifest = governance.manifest
    contract = governance.contract
    fixture = governance.fixture
    if report.suite_version != manifest.suite_version or (
        manifest.suite_version != contract.suite_version
    ):
        collector.add(
            "RUN_FIXTURE_GOVERNANCE_VERSION_MISMATCH",
            report_path,
            (
                f"report={report.suite_version}, manifest={manifest.suite_version}, "
                f"contract={contract.suite_version}"
            ),
        )
    if fixture.provider is not EvalProvider.FIXTURE or (
        fixture.metric_scope is not MetricScope.HARNESS
    ):
        collector.add(
            "RUN_FIXTURE_GOVERNANCE_MODE_INVALID",
            formal_root / "controls" / report.suite_id / "cases.json",
            "fixture provider/harness scope is not frozen correctly",
        )

    implementations = {item.implementation_id: item for item in fixture.implementations}
    contract_controls = {item.control_id: item for item in contract.controls}
    expected_case_ids = set(implementations)
    if not implementations:
        collector.add(
            "RUN_FIXTURE_IMPLEMENTATION_SET_EMPTY",
            formal_root / "controls" / report.suite_id / "cases.json",
            "fixture cohort requires the frozen matched-good and mutant implementations",
        )
        return
    if expected_case_ids != set(contract_controls):
        collector.add(
            "RUN_FIXTURE_CONTROL_SET_DIFFERS_FROM_CONTRACT",
            report_path,
            f"fixture={sorted(expected_case_ids)!r}, contract={sorted(contract_controls)!r}",
        )
    else:
        for case_id, implementation in implementations.items():
            control = contract_controls[case_id]
            if (
                implementation.declared_kind.value != control.kind.value
                or implementation.expected_harness_status is not control.expected_harness_status
            ):
                collector.add(
                    "RUN_FIXTURE_CONTROL_METADATA_MISMATCH",
                    report_path,
                    case_id,
                )

    traces_by_case: dict[str, list[tuple[FormalEvalTrace, Path]]] = defaultdict(list)
    for trace, path in traces:
        traces_by_case[trace.case_id].append((trace, path))
    if set(traces_by_case) != expected_case_ids or any(
        len(items) != 1 for items in traces_by_case.values()
    ):
        collector.add(
            "RUN_FIXTURE_IMPLEMENTATION_SET_MISMATCH",
            root / "traces",
            (
                f"expected exactly one trace for {sorted(expected_case_ids)!r}, "
                f"got {sorted((key, len(value)) for key, value in traces_by_case.items())!r}"
            ),
        )

    results: dict[str, FormalControlResult] = {}
    for case_id, implementation in implementations.items():
        result = evaluate_control_implementation(fixture, case_id)
        results[case_id] = result
        items = traces_by_case.get(case_id, [])
        if len(items) != 1:
            continue
        trace, trace_path = items[0]
        if trace.attempt != 1:
            collector.add(
                "RUN_FIXTURE_ATTEMPT_MUST_EQUAL_ONE",
                trace_path,
                f"{case_id}: {trace.attempt}",
            )
        if (
            trace.status is not result.harness_status
            or trace.status_reasons != result.status_reasons
        ):
            collector.add(
                "RUN_FIXTURE_INDEPENDENT_RESULT_MISMATCH",
                trace_path,
                (
                    f"expected status/reasons={result.harness_status.value}/"
                    f"{result.status_reasons!r}, got {trace.status.value}/{trace.status_reasons!r}"
                ),
            )
        raw_control_result = trace.payload.get("control_result")
        try:
            declared_result = FormalControlResult.model_validate(raw_control_result)
        except ValidationError as exc:
            collector.add("RUN_FIXTURE_CONTROL_RESULT_INVALID", trace_path, str(exc))
        else:
            if declared_result.model_dump(mode="json") != result.model_dump(mode="json"):
                collector.add(
                    "RUN_FIXTURE_CONTROL_RESULT_MISMATCH",
                    trace_path,
                    "payload.control_result differs from independent adjudication",
                )

        expected_payload = {
            "artifact_kind": "synthetic_evaluator_harness_control",
            "control_result": result.model_dump(mode="json"),
            "data_partition": "harness_control",
            "declared_kind": implementation.declared_kind.value,
            "fixture_id": fixture.fixture_id,
            "fixture_payload_sha256": fixture.payload_sha256,
            "input_artifacts": governance.input_artifacts,
            "production_path_exercised": False,
            "quality_effects_emitted": False,
            "real_model_used": False,
        }
        if trace.payload != expected_payload:
            collector.add(
                "RUN_FIXTURE_PAYLOAD_MISMATCH",
                trace_path,
                "trace payload differs from frozen fixture, input digests, flags, or result",
            )
        expected_event_payload = {
            "implementation_id": case_id,
            "expected_harness_status": implementation.expected_harness_status.value,
            "actual_harness_status": result.harness_status.value,
        }
        trajectory_matches = len(trace.trajectory) == 1 and (
            trace.trajectory[0].sequence == 0
            and trace.trajectory[0].kind == "deterministic_control_adjudication"
            and trace.trajectory[0].timestamp == report.generated_at
            and trace.trajectory[0].payload == expected_event_payload
        )
        if not trajectory_matches:
            collector.add(
                "RUN_FIXTURE_TRAJECTORY_MISMATCH",
                trace_path,
                "trajectory differs from independently reconstructed control event",
            )
        if trace.started_at != report.generated_at or trace.finished_at != report.generated_at:
            collector.add(
                "RUN_FIXTURE_TIMESTAMP_MISMATCH",
                trace_path,
                "fixture trace timestamps must equal report.generated_at",
            )
        provenance_values = (
            trace.model,
            trace.prompt_sha256,
            trace.tool_schema_sha256,
            trace.scorer,
            trace.budget,
            trace.usage,
        )
        if any(value is not None for value in provenance_values):
            collector.add(
                "RUN_FIXTURE_REAL_MODEL_PROVENANCE_FORBIDDEN",
                trace_path,
                "fixture controls cannot carry real-model provenance or usage",
            )

    actual_by_kind = {
        implementation.declared_kind: results[case_id].harness_status
        for case_id, implementation in implementations.items()
    }
    harness_ok = actual_by_kind == {
        DeclaredControlKind.MATCHED_GOOD: EvalStatus.PASS,
        DeclaredControlKind.FROZEN_MUTANT: EvalStatus.FAIL,
    }
    correct_count = sum(
        results[case_id].harness_status is implementation.expected_harness_status
        for case_id, implementation in implementations.items()
    )
    statuses = [results[case_id].harness_status for case_id in implementations]
    passed = sum(status is EvalStatus.PASS for status in statuses)
    failed = sum(status is EvalStatus.FAIL for status in statuses)
    expected_counts = EvaluationCounts(
        total_cases=len(implementations),
        executed_cases=len(implementations),
        passed_cases=passed,
        failed_cases=failed,
        errored_cases=len(implementations) - passed - failed,
        skipped_cases=0,
    )
    if report.counts != expected_counts:
        collector.add(
            "RUN_FIXTURE_REPORT_COUNTS_MISMATCH",
            report_path,
            (
                f"expected {expected_counts.model_dump(mode='json')!r}, "
                f"got {report.counts.model_dump(mode='json')!r}"
            ),
        )
    expected_metric = MetricResult(
        name=_HARNESS_METRIC_NAME,
        scope=MetricScope.HARNESS,
        sample_size=len(implementations),
        value=correct_count / len(implementations),
        success_count=correct_count,
        unit="ratio",
    )
    if report.primary_metric != _HARNESS_METRIC_NAME or report.metrics != (expected_metric,):
        collector.add(
            "RUN_FIXTURE_REPORT_METRIC_MISMATCH",
            report_path,
            "harness discrimination sample, successes, value, scope, or primary metric differs",
        )
    if any(metric.scope is MetricScope.QUALITY for metric in report.metrics):
        collector.add(
            "RUN_FIXTURE_QUALITY_METRIC_FORBIDDEN",
            report_path,
            "fixture controls cannot emit quality-effect metrics",
        )
    expected_harness_status = EvalStatus.PASS if harness_ok else EvalStatus.FAIL
    if report.harness_status is not expected_harness_status:
        collector.add(
            "RUN_FIXTURE_REPORT_HARNESS_STATUS_MISMATCH",
            report_path,
            f"expected {expected_harness_status.value}, got {report.harness_status.value}",
        )
    expected_quality_status = _fixture_quality_status(contract)
    if report.evaluation_status is not expected_quality_status:
        collector.add(
            "RUN_FIXTURE_REPORT_EVALUATION_STATUS_MISMATCH",
            report_path,
            f"expected {expected_quality_status.value}, got {report.evaluation_status.value}",
        )
    expected_reasons = _fixture_report_reasons(contract, harness_ok=harness_ok)
    if report.status_reasons != expected_reasons:
        collector.add(
            "RUN_FIXTURE_REPORT_REASONS_MISMATCH",
            report_path,
            f"expected {expected_reasons!r}, got {report.status_reasons!r}",
        )
    if report.skipped:
        collector.add("RUN_FIXTURE_REPORT_CANNOT_SKIP", report_path, "skipped must be false")
    if tuple(report.trace_sha256s) != tuple(actual_hashes):
        collector.add(
            "RUN_FIXTURE_TRACE_HASH_ORDER_MISMATCH",
            report_path,
            "official fixture report hashes must match trace filename order exactly",
        )


@dataclass(frozen=True, slots=True)
class _RunAudit:
    run_dir: Path | None
    report: FormalEvalReport | None
    report_sha256: str | None
    trace_sha256s: tuple[str, ...]
    usage_rows: tuple[tuple[int, int, int, float], ...]


def _verify_run_only(run_dir: Path, formal_root: Path, collector: _Collector) -> _RunAudit:
    if _is_link_like(run_dir):
        collector.add(
            "RUN_DIRECTORY_LINK_FORBIDDEN",
            run_dir,
            "run path cannot be a symlink or junction",
        )
    try:
        root = run_dir.resolve(strict=True)
    except OSError as exc:
        collector.add("RUN_DIRECTORY_MISSING", run_dir, str(exc))
        return _RunAudit(
            run_dir=None,
            report=None,
            report_sha256=None,
            trace_sha256s=(),
            usage_rows=(),
        )
    if not root.is_dir():
        collector.add("RUN_DIRECTORY_INVALID", root, "run path is not a directory")
        return _RunAudit(
            run_dir=None,
            report=None,
            report_sha256=None,
            trace_sha256s=(),
            usage_rows=(),
        )

    report_path = _safe_run_file(root / "report.json", root, collector, label="report")
    raw_report: Any | None = None
    report: FormalEvalReport | None = None
    report_sha256: str | None = None
    if report_path is not None:
        collector.check(report_path)
        try:
            report_bytes = report_path.read_bytes()
        except OSError as exc:
            collector.add("RUN_REPORT_READ_ERROR", report_path, str(exc))
        else:
            report_sha256 = _sha256(report_bytes)
            try:
                raw_report = _strict_json_loads(report_bytes)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                _DuplicateKeyError,
                ValueError,
            ) as exc:
                collector.add("RUN_REPORT_INVALID_JSON", report_path, str(exc))
            if raw_report is not None:
                try:
                    # Never trust a caller-provided model or schema-only success: reconstruct from
                    # the exact final report bytes via json.loads + model_validate.
                    report = FormalEvalReport.model_validate(raw_report)
                except ValidationError as exc:
                    collector.add("RUN_REPORT_MODEL_INVALID", report_path, str(exc))
                else:
                    if not isinstance(raw_report, dict) or not _raw_status_is_finalized(
                        raw_report, report
                    ):
                        collector.add(
                            "RUN_REPORT_NOT_FINALIZED_AFTER_MODEL_VALIDATION",
                            report_path,
                            "serialized status/reasons differ from model_validate result",
                        )

    traces_dir = root / "traces"
    trace_paths: list[Path] = []
    if _is_link_like(traces_dir):
        collector.add("RUN_TRACES_DIRECTORY_SYMLINK_FORBIDDEN", traces_dir, "traces")
    elif not traces_dir.is_dir():
        collector.add("RUN_TRACES_DIRECTORY_MISSING", traces_dir, "traces directory is required")
    else:
        try:
            traces_root = traces_dir.resolve(strict=True)
            traces_root.relative_to(root)
        except (OSError, ValueError) as exc:
            collector.add("RUN_TRACES_DIRECTORY_ESCAPE", traces_dir, str(exc))
        else:
            for candidate in sorted(traces_dir.iterdir(), key=lambda item: item.name):
                if candidate.is_dir() or candidate.suffix.lower() != ".json":
                    collector.add(
                        "RUN_TRACES_UNEXPECTED_ENTRY",
                        candidate,
                        "traces directory may contain only direct JSON files",
                    )
                    continue
                safe = _safe_run_file(candidate, root, collector, label="trace")
                if safe is not None:
                    trace_paths.append(safe)

    actual_hashes: list[str] = []
    traces: list[tuple[FormalEvalTrace, Path]] = []
    usage_rows: list[tuple[int, int, int, float]] = []
    for path in trace_paths:
        collector.check(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            collector.add("RUN_TRACE_READ_ERROR", path, str(exc))
            continue
        actual_hashes.append(_sha256(raw))
        try:
            payload = _strict_json_loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
            collector.add("RUN_TRACE_INVALID_JSON", path, str(exc))
            continue
        try:
            trace = FormalEvalTrace.model_validate(payload)
        except ValidationError as exc:
            collector.add("RUN_TRACE_MODEL_INVALID", path, str(exc))
            continue
        if not isinstance(payload, dict) or not _raw_status_is_finalized(payload, trace):
            collector.add(
                "RUN_TRACE_NOT_FINALIZED_AFTER_MODEL_VALIDATION",
                path,
                "serialized status/reasons differ from model_validate result",
            )
        traces.append((trace, path))
        if trace.provider is EvalProvider.REAL_MODEL:
            if trace.usage is None:
                collector.add(
                    "REAL_MODEL_ATTEMPT_USAGE_MISSING",
                    path,
                    "every error/retry attempt must expose usage for global accounting",
                )
            else:
                row = (
                    trace.usage.model_calls,
                    trace.usage.input_tokens,
                    trace.usage.output_tokens,
                    trace.usage.cost_usd,
                )
                if not math.isfinite(row[3]):
                    collector.add("REAL_MODEL_USAGE_NON_FINITE", path, repr(row[3]))
                else:
                    usage_rows.append(row)

    if len(actual_hashes) != len(set(actual_hashes)):
        collector.add(
            "RUN_TRACE_CONTENT_HASHES_NOT_UNIQUE",
            traces_dir,
            "two trace files have identical bytes/SHA256",
        )

    if report is None:
        return _RunAudit(
            run_dir=root,
            report=None,
            report_sha256=report_sha256,
            trace_sha256s=tuple(actual_hashes),
            usage_rows=tuple(usage_rows),
        )

    report_hashes = list(report.trace_sha256s)
    if len(report_hashes) != len(set(report_hashes)):
        collector.add(
            "RUN_REPORT_TRACE_HASHES_NOT_UNIQUE",
            report_path or root,
            "report trace_sha256s must be unique",
        )
    missing_hashes = sorted(set(report_hashes) - set(actual_hashes))
    extra_hashes = sorted(set(actual_hashes) - set(report_hashes))
    if missing_hashes or extra_hashes or len(report_hashes) != len(actual_hashes):
        collector.add(
            "RUN_TRACE_HASH_SET_MISMATCH",
            report_path or root,
            f"missing_actual={missing_hashes}, unreported_actual={extra_hashes}",
        )

    registry = get_formal_suite_registry()
    contract = registry.get(report.suite_id)
    if contract is None:
        collector.add("RUN_SUITE_UNKNOWN", report_path or root, report.suite_id)
    else:
        if report.suite_version != contract.suite_version:
            collector.add(
                "RUN_SUITE_VERSION_MISMATCH",
                report_path or root,
                f"expected {contract.suite_version}, got {report.suite_version}",
            )
        if report.provider not in contract.allowed_providers:
            collector.add(
                "RUN_PROVIDER_NOT_ALLOWED",
                report_path or root,
                report.provider.value,
            )
        manifest_path = formal_root / "suites" / report.suite_id / "manifest.json"
        manifest_payload = _read_json(manifest_path, collector, code="RUN_MANIFEST")
        if manifest_payload is not None:
            try:
                manifest = FormalEvalManifest.model_validate(manifest_payload)
            except ValidationError as exc:
                collector.add("RUN_MANIFEST_MODEL_INVALID", manifest_path, str(exc))
            else:
                if report.suite_version != manifest.suite_version:
                    collector.add(
                        "RUN_MANIFEST_VERSION_MISMATCH",
                        report_path or root,
                        report.suite_version,
                    )
                if report.provider not in manifest.allowed_providers:
                    collector.add(
                        "RUN_MANIFEST_PROVIDER_NOT_ALLOWED",
                        report_path or root,
                        report.provider.value,
                    )
                definitions = {metric.name: metric for metric in manifest.metrics}
                for metric in report.metrics:
                    definition = definitions.get(metric.name)
                    if definition is None:
                        collector.add(
                            "RUN_METRIC_NOT_REGISTERED",
                            report_path or root,
                            metric.name,
                        )
                    elif metric.scope != definition.scope or metric.unit != definition.unit:
                        collector.add(
                            "RUN_METRIC_CONTRACT_MISMATCH",
                            report_path or root,
                            (
                                f"{metric.name}: expected scope={definition.scope.value}, "
                                f"unit={definition.unit!r}"
                            ),
                        )
                if report.evaluation_status in _PASS_FAIL:
                    registered_primary = [
                        metric.name for metric in manifest.metrics if metric.primary
                    ]
                    if registered_primary != [report.primary_metric]:
                        collector.add(
                            "RUN_QUALITY_PRIMARY_DIFFERS_FROM_MANIFEST",
                            report_path or root,
                            (
                                f"registered={registered_primary!r}, "
                                f"reported={report.primary_metric!r}"
                            ),
                        )
                    if report.split is not EvalSplit.SEALED:
                        collector.add(
                            "RUN_QUALITY_REQUIRES_SEALED_SPLIT",
                            report_path or root,
                            report.split.value,
                        )
                    if manifest.status is not EvalStatus.PASS:
                        collector.add(
                            "RUN_QUALITY_MANIFEST_NOT_READY",
                            manifest_path,
                            manifest.status.value,
                        )

                    experiment = next(
                        (
                            candidate
                            for candidate in contract.experiments
                            if candidate.primary_metric.name == report.primary_metric
                        ),
                        None,
                    )
                    if experiment is None:
                        collector.add(
                            "RUN_QUALITY_EXPERIMENT_NOT_REGISTERED",
                            report_path or root,
                            str(report.primary_metric),
                        )
                    else:
                        if experiment.status is not EvalStatus.PASS:
                            collector.add(
                                "RUN_QUALITY_EXPERIMENT_NOT_READY",
                                report_path or root,
                                f"{experiment.experiment_id}:{experiment.status.value}",
                            )
                        sealed = (
                            experiment.data_freeze.sealed
                            if experiment.data_freeze is not None
                            else None
                        )
                        if sealed is None:
                            collector.add(
                                "RUN_QUALITY_DATA_CONTRACT_NOT_APPLICABLE",
                                report_path or root,
                                f"{experiment.experiment_id}: fault matrices are harness-only",
                            )
                        elif sealed.status is not EvalStatus.PASS or sealed.content_sha256 is None:
                            collector.add(
                                "RUN_QUALITY_SEALED_DATA_NOT_FROZEN",
                                report_path or root,
                                f"{sealed.dataset_id}:{sealed.status.value}",
                            )
                        scorer = experiment.scorer
                        if (
                            scorer.implementation_status is not EvalStatus.PASS
                            or scorer.sha256 is None
                        ):
                            collector.add(
                                "RUN_QUALITY_SCORER_NOT_FROZEN",
                                report_path or root,
                                f"{scorer.scorer_id}:{scorer.implementation_status.value}",
                            )

                    if manifest.scorer is None:
                        collector.add(
                            "RUN_QUALITY_MANIFEST_SCORER_MISSING",
                            manifest_path,
                            "quality verdict requires a frozen scorer reference",
                        )
                    else:
                        expected_scorer = manifest.scorer.model_dump(mode="json")
                        for trace, trace_path in traces:
                            actual_scorer = (
                                trace.scorer.model_dump(mode="json")
                                if trace.scorer is not None
                                else None
                            )
                            if actual_scorer != expected_scorer:
                                collector.add(
                                    "RUN_QUALITY_TRACE_SCORER_MISMATCH",
                                    trace_path,
                                    "trace scorer differs from the frozen manifest scorer",
                                )

    attempts_by_case: dict[str, list[FormalEvalTrace]] = defaultdict(list)
    seen_attempts: set[tuple[str, int]] = set()
    for trace, path in traces:
        identity = {
            "suite_id": trace.suite_id,
            "suite_version": trace.suite_version,
            "run_id": trace.run_id,
            "split": trace.split,
            "provider": trace.provider,
        }
        expected_identity = {
            "suite_id": report.suite_id,
            "suite_version": report.suite_version,
            "run_id": report.run_id,
            "split": report.split,
            "provider": report.provider,
        }
        differing = [
            field for field, value in identity.items() if value != expected_identity[field]
        ]
        if differing:
            collector.add(
                "RUN_TRACE_IDENTITY_MISMATCH",
                path,
                f"fields differ from report: {differing}",
            )
        key = (trace.case_id, trace.attempt)
        if key in seen_attempts:
            collector.add(
                "RUN_CASE_ATTEMPT_DUPLICATE",
                path,
                f"duplicate case_id/attempt: {key!r}",
            )
        seen_attempts.add(key)
        attempts_by_case[trace.case_id].append(trace)

    terminal_statuses: list[EvalStatus] = []
    for case_id, case_attempts in attempts_by_case.items():
        attempts = sorted(trace.attempt for trace in case_attempts)
        expected_attempts = list(range(1, attempts[-1] + 1))
        if attempts != expected_attempts:
            collector.add(
                "RUN_CASE_ATTEMPTS_NOT_CONTIGUOUS",
                traces_dir / case_id,
                f"expected {expected_attempts}, got {attempts}",
            )
        terminal = max(case_attempts, key=lambda trace: trace.attempt)
        terminal_statuses.append(terminal.status)

    passed = sum(status is EvalStatus.PASS for status in terminal_statuses)
    failed = sum(status is EvalStatus.FAIL for status in terminal_statuses)
    errored = len(terminal_statuses) - passed - failed
    recomputed = {
        "executed_cases": len(attempts_by_case),
        "passed_cases": passed,
        "failed_cases": failed,
        "errored_cases": errored,
    }
    for field, expected in recomputed.items():
        actual = getattr(report.counts, field)
        if actual != expected:
            collector.add(
                "RUN_REPORT_COUNT_MISMATCH",
                report_path or root,
                f"{field}: expected {expected}, got {actual}",
            )

    if not traces:
        no_trace_not_run = (
            report.harness_status is EvalStatus.NOT_RUN
            and report.evaluation_status is EvalStatus.NOT_RUN
            and report.counts.executed_cases == 0
            and not report.trace_sha256s
        )
        if not no_trace_not_run:
            collector.add(
                "RUN_NO_TRACE_ONLY_ALLOWED_FOR_NOT_RUN",
                report_path or root,
                "both statuses must be NOT_RUN with zero executed cases and hashes",
            )

    has_verdict = report.harness_status in _PASS_FAIL or report.evaluation_status in _PASS_FAIL
    if has_verdict:
        if report.counts.skipped_cases or report.skipped:
            collector.add(
                "RUN_VERDICT_HAS_SKIPPED_CASES",
                report_path or root,
                "PASS/FAIL reports cannot skip cases",
            )
        non_verdict_terminals = [
            status.value for status in terminal_statuses if status not in _PASS_FAIL
        ]
        if non_verdict_terminals:
            collector.add(
                "RUN_VERDICT_HAS_INVALID_TERMINAL_CASE",
                report_path or root,
                repr(non_verdict_terminals),
            )
        if any(trace.status is EvalStatus.INVALID for trace, _path in traces):
            collector.add(
                "RUN_VERDICT_CONTAINS_INVALID_TRACE",
                report_path or root,
                "INVALID attempts cannot support a PASS/FAIL report",
            )
        non_executed_attempts = [
            trace.status.value
            for trace, _path in traces
            if trace.status
            in {
                EvalStatus.NOT_RUN,
                EvalStatus.NOT_AVAILABLE,
                EvalStatus.BLOCKED_EXTERNAL_DATA,
            }
        ]
        if non_executed_attempts:
            collector.add(
                "RUN_VERDICT_CONTAINS_NON_EXECUTED_TRACE",
                report_path or root,
                repr(non_executed_attempts),
            )

    if report.provider is EvalProvider.FIXTURE and report_path is not None:
        try:
            unexpected = sorted(
                entry.name
                for entry in root.iterdir()
                if entry.name not in {"report.json", "traces"}
            )
        except OSError as exc:
            collector.add("RUN_FIXTURE_DIRECTORY_READ_ERROR", root, str(exc))
        else:
            if unexpected:
                collector.add(
                    "RUN_FIXTURE_DIRECTORY_HAS_EXTRA_ENTRIES",
                    root,
                    repr(unexpected),
                )
        _verify_fixture_harness_run(
            root=root,
            formal_root=formal_root,
            report_path=report_path,
            report=report,
            traces=traces,
            actual_hashes=actual_hashes,
            collector=collector,
        )

    return _RunAudit(
        run_dir=root,
        report=report,
        report_sha256=report_sha256,
        trace_sha256s=tuple(actual_hashes),
        usage_rows=tuple(usage_rows),
    )


def _sum_usage(rows: Sequence[tuple[int, int, int, float]]) -> AggregateUsage:
    return AggregateUsage(
        model_calls=sum(row[0] for row in rows),
        input_tokens=sum(row[1] for row in rows),
        output_tokens=sum(row[2] for row in rows),
        cost_usd=math.fsum(row[3] for row in rows),
        attempts=len(rows),
    )


def _verify_fixture_cohort(
    audits: Sequence[_RunAudit],
    formal_root: Path,
    collector: _Collector,
) -> None:
    fixture_audits = [
        audit
        for audit in audits
        if audit.report is not None and audit.report.provider is EvalProvider.FIXTURE
    ]
    if not fixture_audits:
        return
    if len(audits) != len(FORMAL_COHORT_SUITE_IDS) or len(fixture_audits) != len(
        FORMAL_COHORT_SUITE_IDS
    ):
        collector.add(
            "RUN_FIXTURE_COHORT_INCOMPLETE",
            "fixture_cohort",
            (
                f"expected exactly {len(FORMAL_COHORT_SUITE_IDS)} fixture directories, "
                f"got inputs={len(audits)}, valid_fixture_reports={len(fixture_audits)}"
            ),
        )

    audits_by_suite: dict[str, list[_RunAudit]] = defaultdict(list)
    for audit in fixture_audits:
        assert audit.report is not None
        audits_by_suite[audit.report.suite_id].append(audit)
    actual_suite_ids = set(audits_by_suite)
    expected_suite_ids = set(FORMAL_COHORT_SUITE_IDS)
    duplicate_suites = sorted(
        suite_id for suite_id, suite_audits in audits_by_suite.items() if len(suite_audits) != 1
    )
    if actual_suite_ids != expected_suite_ids or duplicate_suites:
        collector.add(
            "RUN_FIXTURE_COHORT_SUITE_SET_MISMATCH",
            "fixture_cohort",
            (
                f"expected={sorted(expected_suite_ids)!r}, actual={sorted(actual_suite_ids)!r}, "
                f"duplicates={duplicate_suites!r}"
            ),
        )
        return

    ordered_audits = tuple(audits_by_suite[suite_id][0] for suite_id in FORMAL_COHORT_SUITE_IDS)
    run_roots = tuple(audit.run_dir for audit in ordered_audits)
    if any(root is None for root in run_roots):
        collector.add(
            "RUN_FIXTURE_COHORT_DIRECTORY_INVALID",
            "fixture_cohort",
            "every fixture suite directory must resolve",
        )
        return
    resolved_roots = tuple(root for root in run_roots if root is not None)
    parents = {root.parent for root in resolved_roots}
    if len(parents) != 1:
        collector.add(
            "RUN_FIXTURE_COHORT_COMMON_PARENT_MISMATCH",
            "fixture_cohort",
            repr(sorted(str(parent) for parent in parents)),
        )
        return
    parent = next(iter(parents))
    for suite_id, run_root in zip(FORMAL_COHORT_SUITE_IDS, resolved_roots, strict=True):
        if run_root != parent / suite_id:
            collector.add(
                "RUN_FIXTURE_COHORT_DIRECTORY_NAME_MISMATCH",
                run_root,
                f"expected {parent / suite_id}",
            )

    if _is_link_like(parent):
        collector.add(
            "RUN_FIXTURE_COHORT_PARENT_LINK_FORBIDDEN",
            parent,
            "cohort output root cannot be a symlink or junction",
        )
        return
    try:
        unexpected = sorted(
            entry.name
            for entry in parent.iterdir()
            if entry.name not in {*FORMAL_COHORT_SUITE_IDS, "cohort.json"}
        )
    except OSError as exc:
        collector.add("RUN_FIXTURE_COHORT_PARENT_READ_ERROR", parent, str(exc))
        return
    if unexpected:
        collector.add(
            "RUN_FIXTURE_COHORT_EXTRA_ENTRIES",
            parent,
            repr(unexpected),
        )

    cohort_path = parent / "cohort.json"
    if _is_link_like(cohort_path):
        collector.add(
            "RUN_FIXTURE_COHORT_LINK_FORBIDDEN",
            cohort_path,
            "cohort.json cannot be a symlink or junction",
        )
        return
    try:
        resolved_cohort = cohort_path.resolve(strict=True)
        resolved_cohort.relative_to(parent)
    except (OSError, ValueError) as exc:
        collector.add("RUN_FIXTURE_COHORT_MISSING_OR_ESCAPED", cohort_path, str(exc))
        return
    if not resolved_cohort.is_file():
        collector.add(
            "RUN_FIXTURE_COHORT_NOT_FILE",
            resolved_cohort,
            "cohort completion marker must be a regular file",
        )
        return
    collector.check(resolved_cohort)
    try:
        cohort_bytes = resolved_cohort.read_bytes()
        cohort = parse_formal_cohort_bytes(cohort_bytes)
    except OSError as exc:
        collector.add("RUN_FIXTURE_COHORT_READ_ERROR", resolved_cohort, str(exc))
        return
    except FormalCohortError as exc:
        collector.add("RUN_FIXTURE_COHORT_INVALID", resolved_cohort, str(exc))
        return

    try:
        governance_sha256 = compute_formal_governance_sha256(formal_root)
    except FormalCohortError as exc:
        collector.add("RUN_FIXTURE_COHORT_GOVERNANCE_HASH_ERROR", formal_root, str(exc))
    else:
        if cohort.governance_sha256 != governance_sha256:
            collector.add(
                "RUN_FIXTURE_COHORT_GOVERNANCE_HASH_MISMATCH",
                resolved_cohort,
                f"expected {governance_sha256}, got {cohort.governance_sha256}",
            )

    entries: dict[str, Any] = {entry.suite_id: entry for entry in cohort.suites}
    for suite_id, audit in zip(FORMAL_COHORT_SUITE_IDS, ordered_audits, strict=True):
        report = audit.report
        assert report is not None
        entry = entries[suite_id]
        expected_run_id = f"{cohort.base_run_id}.{suite_id}"
        if report.run_id != expected_run_id or entry.run_id != report.run_id:
            collector.add(
                "RUN_FIXTURE_COHORT_RUN_ID_MISMATCH",
                audit.run_dir or parent,
                (
                    f"expected={expected_run_id!r}, report={report.run_id!r}, "
                    f"cohort={entry.run_id!r}"
                ),
            )
        if report.generated_at != cohort.generated_at:
            collector.add(
                "RUN_FIXTURE_COHORT_GENERATED_AT_MISMATCH",
                audit.run_dir or parent,
                (
                    f"report={report.generated_at.isoformat()}, "
                    f"cohort={cohort.generated_at.isoformat()}"
                ),
            )
        if audit.report_sha256 is None or entry.report_sha256 != audit.report_sha256:
            collector.add(
                "RUN_FIXTURE_COHORT_REPORT_HASH_MISMATCH",
                audit.run_dir or parent,
                f"cohort={entry.report_sha256}, actual={audit.report_sha256}",
            )
        if tuple(entry.trace_sha256s) != audit.trace_sha256s:
            collector.add(
                "RUN_FIXTURE_COHORT_TRACE_HASH_MISMATCH",
                audit.run_dir or parent,
                (f"cohort={entry.trace_sha256s!r}, actual={audit.trace_sha256s!r}"),
            )


def _verify_global_usage(
    usage: AggregateUsage,
    budget: GlobalRealModelBudget | None,
    collector: _Collector,
    *,
    label: str,
) -> None:
    if budget is None:
        collector.add(
            "GLOBAL_BUDGET_UNAVAILABLE_FOR_ACCOUNTING",
            label,
            "cannot verify aggregate real-model usage",
        )
        return
    if usage.model_calls > budget.model_call_cap:
        collector.add(
            "GLOBAL_MODEL_CALL_BUDGET_EXCEEDED",
            label,
            f"{usage.model_calls} > {budget.model_call_cap}",
        )
    if usage.tokens > budget.token_cap:
        collector.add(
            "GLOBAL_TOKEN_BUDGET_EXCEEDED",
            label,
            f"{usage.tokens} > {budget.token_cap}",
        )
    if not math.isfinite(usage.cost_usd):
        collector.add("GLOBAL_COST_USAGE_NON_FINITE", label, repr(usage.cost_usd))
    elif usage.cost_usd > budget.cost_usd_cap:
        collector.add(
            "GLOBAL_COST_BUDGET_EXCEEDED",
            label,
            f"{usage.cost_usd} > {budget.cost_usd_cap}",
        )


def verify_formal_eval_runs(
    run_dirs: Sequence[str | Path],
    *,
    formal_root: str | Path | None = None,
) -> FormalArtifactVerification:
    """Verify governance plus one or more runs under one aggregate real-model budget."""

    governance = verify_frozen_formal_artifacts(formal_root)
    root = Path(formal_root) if formal_root is not None else default_formal_root()
    root = root.resolve()
    collector = _Collector()
    collector.errors.extend(governance.errors)
    collector.checked_files.update(governance.checked_files)
    if not run_dirs:
        return FormalArtifactVerification(
            scope="frozen_governance",
            checked_files=tuple(sorted(collector.checked_files)),
            errors=tuple(collector.errors),
        )

    rows: list[tuple[int, int, int, float]] = []
    audits: list[_RunAudit] = []
    seen_run_ids: set[str] = set()
    for run_dir in run_dirs:
        run_path = Path(run_dir)
        audit = _verify_run_only(run_path, root, collector)
        audits.append(audit)
        rows.extend(audit.usage_rows)
        if audit.report is not None:
            if audit.report.run_id in seen_run_ids:
                collector.add(
                    "RUN_ID_DUPLICATED_ACROSS_DIRECTORIES",
                    run_path,
                    audit.report.run_id,
                )
            seen_run_ids.add(audit.report.run_id)

    _verify_fixture_cohort(audits, root, collector)
    usage = _sum_usage(rows)
    budget = _validate_budget_file(root, collector)
    _verify_global_usage(usage, budget, collector, label="aggregate_real_model_usage")
    return FormalArtifactVerification(
        scope="formal_eval_runs",
        checked_files=tuple(sorted(collector.checked_files)),
        errors=tuple(collector.errors),
        real_model_usage=usage,
    )


def verify_formal_eval_run(
    run_dir: str | Path,
    *,
    formal_root: str | Path | None = None,
) -> FormalArtifactVerification:
    """Verify one formal evaluation run and all governance it relies on."""

    return verify_formal_eval_runs((run_dir,), formal_root=formal_root)


__all__ = [
    "FORMAL_ARTIFACT_POLICY_VERSION",
    "POLICY_CONFORMANCE_VERSION",
    "AggregateUsage",
    "FormalArtifactVerification",
    "VerificationIssue",
    "default_formal_root",
    "render_formal_artifact_policy_files",
    "render_policy_conformance_vectors",
    "verify_formal_eval_run",
    "verify_formal_eval_runs",
    "verify_frozen_formal_artifacts",
]
