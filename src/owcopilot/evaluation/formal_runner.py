"""Deterministic unified runner for the four frozen formal harness controls.

The runner is intentionally fixture-only.  It reads and hash-binds the committed suite manifest,
contract, and evaluator-control bytes, emits one protocol trace per matched-good/mutant
implementation, and writes the protocol report/trace layout consumed by ``formal_artifacts``.
It never invokes a model or emits a quality-effect metric.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Protocol, cast

from pydantic import BaseModel, ValidationError

from owcopilot.storage.atomic_file import atomic_write_text

from .formal_artifacts import (
    FormalArtifactVerification,
    default_formal_root,
    verify_formal_eval_runs,
    verify_frozen_formal_artifacts,
)
from .formal_cohort import (
    FORMAL_COHORT_SCHEMA_VERSION,
    FORMAL_COHORT_SUITE_IDS,
    FormalCohortError,
    FormalCohortSuiteEntry,
    FormalEvalCohort,
    compute_formal_governance_sha256,
    parse_formal_cohort_bytes,
    render_formal_cohort,
    sha256_bytes,
)
from .formal_controls import (
    DeclaredControlKind,
    FormalControlFixture,
    FrozenControlMismatchError,
    evaluate_control_implementation,
    verify_frozen_control_fixtures,
)
from .formal_registry import FormalSuiteContract
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
    TraceEvent,
)

DEFAULT_FORMAL_RUN_ID: Final = "formal-fixture-conformance-v1"
DEFAULT_FORMAL_GENERATED_AT: Final = "2026-08-03T00:00:00Z"
HARNESS_METRIC_NAME: Final = "harness_control_discrimination_rate"
FORMAL_SUITE_IDS: Final = FORMAL_COHORT_SUITE_IDS
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_VERDICT_STATUSES = frozenset({EvalStatus.PASS, EvalStatus.FAIL})
_PUBLICATION_THREAD_LOCKS_GUARD = threading.Lock()
_PUBLICATION_THREAD_LOCKS: dict[str, threading.Lock] = {}


class _WindowsLockingAPI(Protocol):
    """Typed boundary for the Windows-only byte-range locking API."""

    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, byte_count: int, /) -> None: ...


def _windows_locking_api() -> _WindowsLockingAPI:
    """Load ``msvcrt`` only on the Windows execution path."""

    return cast(_WindowsLockingAPI, import_module("msvcrt"))


class FormalRunnerError(RuntimeError):
    """Base class for deterministic runner failures."""


class FormalRunnerInputError(FormalRunnerError):
    """Frozen governance/control input is missing, changed, or internally inconsistent."""


class FormalRunnerPathError(FormalRunnerError):
    """An output path is unsafe or collides with the frozen governance tree."""


class FormalRunnerCollisionError(FormalRunnerPathError):
    """Existing output is not byte-identical to the requested cohort."""


class FormalRunnerWriteError(FormalRunnerError):
    """An atomic artifact write failed."""


class FormalRunnerVerifierError(FormalRunnerError):
    """The verifier adapter itself could not audit the emitted layout."""


@dataclass(frozen=True, slots=True)
class InputArtifactDigest:
    path: str
    sha256: str
    byte_count: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class _OfficialSuiteInputs:
    manifest: FormalEvalManifest
    contract: FormalSuiteContract
    fixture: FormalControlFixture
    digests: dict[str, InputArtifactDigest]


@dataclass(frozen=True, slots=True)
class _RenderedTrace:
    filename: str
    model: FormalEvalTrace
    content: bytes


@dataclass(frozen=True, slots=True)
class _SuiteBundle:
    suite_id: str
    run_id: str
    harness_ok: bool
    report: FormalEvalReport
    report_bytes: bytes
    traces: tuple[_RenderedTrace, ...]


@dataclass(frozen=True, slots=True)
class _OutputArtifact:
    suite_id: str
    path: Path
    content: bytes


@dataclass(frozen=True, slots=True)
class _OutputPlan:
    root: Path
    run_dirs: dict[str, Path]
    suite_artifacts: tuple[_OutputArtifact, ...]
    missing_suite_artifacts: tuple[_OutputArtifact, ...]
    cohort_path: Path
    cohort_content: bytes
    cohort_missing: bool


@dataclass(frozen=True, slots=True)
class FormalSuiteRunResult:
    suite_id: str
    run_id: str
    run_dir: Path
    harness_status: EvalStatus
    evaluation_status: EvalStatus
    trace_count: int
    harness_ok: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "suite_id": self.suite_id,
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "harness_status": self.harness_status.value,
            "evaluation_status": self.evaluation_status.value,
            "trace_count": self.trace_count,
            "harness_ok": self.harness_ok,
        }


@dataclass(frozen=True, slots=True)
class FormalRunnerResult:
    output_root: Path
    suites: tuple[FormalSuiteRunResult, ...]
    verification: FormalArtifactVerification

    @property
    def ok(self) -> bool:
        return all(suite.harness_ok for suite in self.suites) and self.verification.ok

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "output_root": str(self.output_root),
            "suites": [suite.as_dict() for suite in self.suites],
            "verification": self.verification.as_dict(),
        }


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _model_bytes(model: BaseModel) -> bytes:
    return _json_bytes(model.model_dump(mode="json"))


def _sha256(content: bytes) -> str:
    return sha256_bytes(content)


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise FormalRunnerInputError("run_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    return run_id


def _parse_generated_at(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise FormalRunnerInputError(f"generated_at is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FormalRunnerInputError("generated_at must include an explicit UTC offset")
    return parsed


def _verification_summary(verification: FormalArtifactVerification) -> str:
    return "; ".join(f"{issue.code}:{issue.path}" for issue in verification.errors)


def _resolve_formal_root(value: str | Path | None) -> Path:
    candidate = Path(value) if value is not None else default_formal_root()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FormalRunnerInputError(f"formal root is unavailable: {candidate}: {exc}") from exc
    if not resolved.is_dir():
        raise FormalRunnerInputError(f"formal root is not a directory: {resolved}")
    return resolved


def _official_file(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FormalRunnerInputError(
            f"formal input escapes or is missing: {candidate}: {exc}"
        ) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise FormalRunnerInputError(
            f"formal input must be a regular non-symlink file: {candidate}"
        )
    return resolved


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FormalRunnerInputError(f"cannot read frozen input {path}: {exc}") from exc


def _digest(root: Path, path: Path, content: bytes) -> InputArtifactDigest:
    return InputArtifactDigest(
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        byte_count=len(content),
    )


def _load_suite_inputs(formal_root: Path, suite_id: str) -> _OfficialSuiteInputs:
    manifest_path = _official_file(formal_root, "suites", suite_id, "manifest.json")
    contract_path = _official_file(formal_root, "suites", suite_id, "contract.json")
    control_path = _official_file(formal_root, "controls", suite_id, "cases.json")
    manifest_bytes = _read_bytes(manifest_path)
    contract_bytes = _read_bytes(contract_path)
    control_bytes = _read_bytes(control_path)
    try:
        manifest = FormalEvalManifest.model_validate_json(manifest_bytes)
        contract = FormalSuiteContract.model_validate_json(contract_bytes)
        fixture = FormalControlFixture.model_validate_json(control_bytes)
    except ValidationError as exc:
        raise FormalRunnerInputError(f"{suite_id}: frozen input model invalid: {exc}") from exc

    if manifest.suite_id != suite_id or contract.suite_id.value != suite_id:
        raise FormalRunnerInputError(f"{suite_id}: manifest/contract suite identity mismatch")
    if fixture.suite_id != suite_id:
        raise FormalRunnerInputError(f"{suite_id}: control fixture suite identity mismatch")
    if manifest.suite_version != contract.suite_version:
        raise FormalRunnerInputError(f"{suite_id}: manifest/contract version mismatch")
    if EvalProvider.FIXTURE not in manifest.allowed_providers:
        raise FormalRunnerInputError(f"{suite_id}: manifest does not allow fixture provider")
    if EvalProvider.FIXTURE not in contract.allowed_providers:
        raise FormalRunnerInputError(f"{suite_id}: contract does not allow fixture provider")

    harness_metrics = {
        metric.name for metric in manifest.metrics if metric.scope is MetricScope.HARNESS
    }
    if HARNESS_METRIC_NAME not in harness_metrics:
        raise FormalRunnerInputError(f"{suite_id}: manifest lacks {HARNESS_METRIC_NAME}")

    contract_controls = {
        control.control_id: (control.kind.value, control.expected_harness_status)
        for control in contract.controls
    }
    fixture_controls = {
        implementation.implementation_id: (
            implementation.declared_kind.value,
            implementation.expected_harness_status,
        )
        for implementation in fixture.implementations
    }
    if fixture_controls != contract_controls:
        raise FormalRunnerInputError(f"{suite_id}: control fixture differs from suite contract")

    return _OfficialSuiteInputs(
        manifest=manifest,
        contract=contract,
        fixture=fixture,
        digests={
            "manifest": _digest(formal_root, manifest_path, manifest_bytes),
            "contract": _digest(formal_root, contract_path, contract_bytes),
            "control": _digest(formal_root, control_path, control_bytes),
        },
    )


def _quality_status(contract: FormalSuiteContract) -> tuple[EvalStatus, tuple[str, ...]]:
    blocker_ids = tuple(
        sorted(
            {
                blocker.blocker_id
                for experiment in contract.experiments
                for blocker in experiment.external_data_blockers
            }
        )
    )
    if blocker_ids:
        return EvalStatus.BLOCKED_EXTERNAL_DATA, blocker_ids
    return EvalStatus.NOT_RUN, ("HARNESS_ONLY_FIXTURE_HAS_NO_QUALITY_EVALUATION",)


def _finalized_model_bytes(model: BaseModel, model_type: type[BaseModel]) -> bytes:
    content = _model_bytes(model)
    try:
        rebuilt = model_type.model_validate_json(content)
    except ValidationError as exc:  # pragma: no cover - construction bug, retained as hard gate
        raise FormalRunnerInputError(f"rendered {model_type.__name__} is invalid: {exc}") from exc
    if rebuilt.model_dump(mode="json") != model.model_dump(mode="json"):
        raise FormalRunnerInputError(f"rendered {model_type.__name__} is not finalized")
    return content


def _build_suite_bundle(
    inputs: _OfficialSuiteInputs,
    *,
    base_run_id: str,
    generated_at: datetime,
) -> _SuiteBundle:
    fixture = inputs.fixture
    suite_id = fixture.suite_id
    suite_run_id = f"{base_run_id}.{suite_id}"
    evaluated: list[tuple[Any, Any]] = []
    for implementation in fixture.implementations:
        result = evaluate_control_implementation(fixture, implementation.implementation_id)
        if result.harness_status not in _VERDICT_STATUSES:
            raise FormalRunnerInputError(
                f"{suite_id}:{implementation.implementation_id}: non-verdict control result "
                f"{result.harness_status.value}"
            )
        evaluated.append((implementation, result))

    actual_by_kind = {
        implementation.declared_kind: result.harness_status for implementation, result in evaluated
    }
    harness_ok = actual_by_kind == {
        DeclaredControlKind.MATCHED_GOOD: EvalStatus.PASS,
        DeclaredControlKind.FROZEN_MUTANT: EvalStatus.FAIL,
    }
    correct_count = sum(
        result.harness_status is implementation.expected_harness_status
        for implementation, result in evaluated
    )

    input_artifacts = {name: digest.as_dict() for name, digest in sorted(inputs.digests.items())}
    rendered_traces: list[_RenderedTrace] = []
    for index, (implementation, result) in enumerate(evaluated, start=1):
        trace = FormalEvalTrace(
            schema_version="formal-eval/v1",
            suite_id=suite_id,
            suite_version=inputs.manifest.suite_version,
            run_id=suite_run_id,
            case_id=implementation.implementation_id,
            attempt=1,
            split=EvalSplit.CANARY,
            provider=EvalProvider.FIXTURE,
            status=result.harness_status,
            status_reasons=result.status_reasons,
            started_at=generated_at,
            finished_at=generated_at,
            trajectory=(
                TraceEvent(
                    sequence=0,
                    kind="deterministic_control_adjudication",
                    timestamp=generated_at,
                    payload={
                        "implementation_id": implementation.implementation_id,
                        "expected_harness_status": implementation.expected_harness_status.value,
                        "actual_harness_status": result.harness_status.value,
                    },
                ),
            ),
            payload={
                "artifact_kind": "synthetic_evaluator_harness_control",
                "control_result": result.model_dump(mode="json"),
                "data_partition": "harness_control",
                "declared_kind": implementation.declared_kind.value,
                "fixture_id": fixture.fixture_id,
                "fixture_payload_sha256": fixture.payload_sha256,
                "input_artifacts": input_artifacts,
                "production_path_exercised": False,
                "quality_effects_emitted": False,
                "real_model_used": False,
            },
        )
        content = _finalized_model_bytes(trace, FormalEvalTrace)
        rendered_traces.append(_RenderedTrace(f"{index:03d}.json", trace, content))

    quality_status, quality_reasons = _quality_status(inputs.contract)
    trace_hashes = tuple(_sha256(trace.content) for trace in rendered_traces)
    trace_statuses = [trace.model.status for trace in rendered_traces]
    passed = sum(status is EvalStatus.PASS for status in trace_statuses)
    failed = sum(status is EvalStatus.FAIL for status in trace_statuses)
    quality_status_reasons = (
        tuple(f"BLOCKED_EXTERNAL_DATA:{reason}" for reason in quality_reasons)
        if quality_status is EvalStatus.BLOCKED_EXTERNAL_DATA
        else quality_reasons
    )
    reasons = [
        "HARNESS_ONLY_FIXTURE_CONTROL",
        "NON_REAL_PROVIDER_HAS_NO_QUALITY_EFFECT",
        "PRODUCTION_PATH_NOT_EXERCISED",
        *quality_status_reasons,
    ]
    reasons.append(
        "MATCHED_GOOD_AND_MUTANT_DISCRIMINATED" if harness_ok else "CONTROL_DISCRIMINATION_FAILED"
    )
    report = FormalEvalReport(
        schema_version="formal-eval/v1",
        suite_id=suite_id,
        suite_version=inputs.manifest.suite_version,
        run_id=suite_run_id,
        split=EvalSplit.CANARY,
        provider=EvalProvider.FIXTURE,
        generated_at=generated_at,
        harness_status=EvalStatus.PASS if harness_ok else EvalStatus.FAIL,
        evaluation_status=quality_status,
        status_reasons=tuple(reasons),
        counts=EvaluationCounts(
            total_cases=len(rendered_traces),
            executed_cases=len(rendered_traces),
            passed_cases=passed,
            failed_cases=failed,
            errored_cases=0,
            skipped_cases=0,
        ),
        primary_metric=HARNESS_METRIC_NAME,
        metrics=(
            MetricResult(
                name=HARNESS_METRIC_NAME,
                scope=MetricScope.HARNESS,
                sample_size=len(evaluated),
                value=correct_count / len(evaluated),
                success_count=correct_count,
                unit="ratio",
            ),
        ),
        trace_sha256s=trace_hashes,
    )
    report_bytes = _finalized_model_bytes(report, FormalEvalReport)
    return _SuiteBundle(
        suite_id=suite_id,
        run_id=suite_run_id,
        harness_ok=harness_ok,
        report=report,
        report_bytes=report_bytes,
        traces=tuple(rendered_traces),
    )


def _contained_output_path(root: Path, *parts: str) -> Path:
    root = root.resolve(strict=True)
    for part in parts:
        if part in {"", ".", ".."} or Path(part).name != part or "/" in part or "\\" in part:
            raise FormalRunnerPathError(f"unsafe output path component: {part!r}")
    candidate = root.joinpath(*parts)
    if candidate.exists() and _is_link_like(candidate):
        raise FormalRunnerPathError(f"output symlink is forbidden: {candidate}")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise FormalRunnerPathError(f"output path escapes root: {candidate}") from exc
    return candidate


def _reject_protected_formal_location(candidate: Path, formal_root: Path) -> None:
    try:
        relative_to_formal = candidate.relative_to(formal_root)
    except ValueError:
        return
    if not relative_to_formal.parts or relative_to_formal.parts[0] != "runs":
        raise FormalRunnerPathError(
            "output inside benchmarks/formal is allowed only below benchmarks/formal/runs"
        )


def _prepare_output_root(output_dir: str | Path, formal_root: Path) -> Path:
    candidate = Path(output_dir)
    if candidate.exists() and _is_link_like(candidate):
        raise FormalRunnerPathError(f"output root symlink is forbidden: {candidate}")
    _reject_protected_formal_location(candidate.resolve(strict=False), formal_root)
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise FormalRunnerWriteError(f"cannot create output root {candidate}: {exc}") from exc
    if not root.is_dir():
        raise FormalRunnerPathError(f"output root is not a directory: {root}")
    _reject_protected_formal_location(root, formal_root)
    return root


def _is_link_like(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_check and junction_check())


def _publication_lock_identity_matches(descriptor: int, lock_path: Path) -> bool:
    opened = os.fstat(descriptor)
    current = os.stat(lock_path, follow_symlinks=False)
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and opened.st_nlink == 1
        and current.st_nlink == 1
        and opened.st_ino != 0
        and current.st_ino != 0
        and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
    )


def _try_windows_publication_lock(descriptor: int) -> None:
    """Attempt the publication byte lock once without blocking."""

    windows_lock = _windows_locking_api()
    os.lseek(descriptor, 0, os.SEEK_SET)
    windows_lock.locking(descriptor, windows_lock.LK_NBLCK, 1)


def _release_windows_publication_lock(descriptor: int) -> None:
    """Release the publication byte lock through the typed platform boundary."""

    windows_lock = _windows_locking_api()
    os.lseek(descriptor, 0, os.SEEK_SET)
    windows_lock.locking(descriptor, windows_lock.LK_UNLCK, 1)


@contextmanager
def _publication_root_lock(root: Path) -> Iterator[None]:
    """Take a process-local and OS advisory lock that is released automatically on death."""

    if not root.name or root.parent == root:
        raise FormalRunnerPathError(f"cannot lock a filesystem root as output: {root}")
    lock_path = root.parent / f".{root.name}.formal-eval.lock"
    lock_key = str(lock_path.resolve(strict=False))
    with _PUBLICATION_THREAD_LOCKS_GUARD:
        thread_lock = _PUBLICATION_THREAD_LOCKS.setdefault(lock_key, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise FormalRunnerCollisionError(f"publication root is already locked: {lock_path}")

    descriptor = -1
    os_locked = False
    try:
        if _is_link_like(lock_path):
            raise FormalRunnerPathError(f"publication lock link is forbidden: {lock_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise FormalRunnerWriteError(
                f"cannot open publication lock {lock_path}: {exc}"
            ) from exc
        if _is_link_like(lock_path) or not _publication_lock_identity_matches(
            descriptor, lock_path
        ):
            raise FormalRunnerPathError(
                f"publication lock must be the opened regular non-link file: {lock_path}"
            )
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        try:
            if os.name == "nt":
                _try_windows_publication_lock(descriptor)
            else:
                posix_lock: Any = __import__("fcntl")
                posix_lock.flock(descriptor, posix_lock.LOCK_EX | posix_lock.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise FormalRunnerCollisionError(
                f"publication root is already locked by another process: {lock_path}"
            ) from exc
        os_locked = True
        if _is_link_like(lock_path) or not _publication_lock_identity_matches(
            descriptor, lock_path
        ):
            raise FormalRunnerPathError(
                f"publication lock path changed during acquisition: {lock_path}"
            )
        yield
    finally:
        unlock_error: OSError | None = None
        if descriptor >= 0:
            if os_locked:
                try:
                    if os.name == "nt":
                        _release_windows_publication_lock(descriptor)
                    else:
                        posix_lock = __import__("fcntl")
                        posix_lock.flock(descriptor, posix_lock.LOCK_UN)
                except OSError as exc:
                    unlock_error = exc
                try:
                    os.close(descriptor)
                except OSError as exc:
                    unlock_error = unlock_error or exc
            else:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    unlock_error = exc
        thread_lock.release()
        if unlock_error is not None:
            raise FormalRunnerWriteError(
                f"cannot release publication lock {lock_path}: {unlock_error}"
            )


def _ensure_directory(root: Path, *parts: str) -> Path:
    directory = _contained_output_path(root, *parts)
    if directory.exists() and not directory.is_dir():
        raise FormalRunnerPathError(f"expected output directory, found file: {directory}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FormalRunnerWriteError(f"cannot create output directory {directory}: {exc}") from exc
    if _is_link_like(directory):
        raise FormalRunnerPathError(f"output directory symlink is forbidden: {directory}")
    try:
        directory.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise FormalRunnerPathError(f"output directory escapes root: {directory}") from exc
    return directory


def _reject_unexpected_entries(directory: Path, allowed: set[str]) -> None:
    try:
        unexpected = sorted(
            entry.name for entry in directory.iterdir() if entry.name not in allowed
        )
    except OSError as exc:
        raise FormalRunnerPathError(f"cannot inspect output directory {directory}: {exc}") from exc
    if unexpected:
        raise FormalRunnerPathError(
            f"unexpected entries under dedicated output directory {directory}: {unexpected}"
        )


def _build_cohort_content(
    bundles: tuple[_SuiteBundle, ...],
    *,
    base_run_id: str,
    generated_at: datetime,
    governance_sha256: str,
) -> bytes:
    cohort = FormalEvalCohort(
        schema_version=FORMAL_COHORT_SCHEMA_VERSION,
        base_run_id=base_run_id,
        generated_at=generated_at,
        governance_sha256=governance_sha256,
        suites=tuple(
            FormalCohortSuiteEntry(
                suite_id=bundle.suite_id,  # type: ignore[arg-type]
                run_id=bundle.run_id,
                report_sha256=sha256_bytes(bundle.report_bytes),
                trace_sha256s=tuple(sha256_bytes(trace.content) for trace in bundle.traces),
            )
            for bundle in bundles
        ),
    )
    content = render_formal_cohort(cohort)
    # Reconstruct from final bytes now, rather than discovering a renderer/model mismatch after
    # suite artifacts have been published.
    parse_formal_cohort_bytes(content)
    return content


def _inspect_existing_directory(directory: Path, *, root: Path, allowed: set[str]) -> None:
    if _is_link_like(directory):
        raise FormalRunnerPathError(f"output directory symlink is forbidden: {directory}")
    if not directory.exists():
        return
    if not directory.is_dir():
        raise FormalRunnerPathError(f"expected output directory, found file: {directory}")
    try:
        directory.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise FormalRunnerPathError(f"output directory escapes root: {directory}") from exc
    _reject_unexpected_entries(directory, allowed)


def _inspect_expected_artifact(artifact: _OutputArtifact) -> bool:
    """Return true when missing; reject any non-regular or byte-different existing target."""

    path = artifact.path
    if _is_link_like(path):
        raise FormalRunnerPathError(f"output artifact symlink is forbidden: {path}")
    if not path.exists():
        return True
    if not path.is_file():
        raise FormalRunnerPathError(f"output artifact is not a file: {path}")
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise FormalRunnerPathError(f"cannot inspect output artifact {path}: {exc}") from exc
    if actual != artifact.content:
        raise FormalRunnerCollisionError(
            f"existing output bytes differ from requested cohort: {path}"
        )
    return False


def _recover_atomic_write_orphans(root: Path, expected_targets: tuple[Path, ...]) -> None:
    """Remove only mkstemp names that map exactly to an expected publication target."""

    root = root.resolve(strict=True)
    targets_by_parent: dict[Path, set[str]] = {}
    for target in expected_targets:
        targets_by_parent.setdefault(target.parent, set()).add(target.name)
    for parent, target_names in targets_by_parent.items():
        if not parent.exists() or _is_link_like(parent) or not parent.is_dir():
            continue
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved_parent.relative_to(root)
            entries = tuple(parent.iterdir())
        except (OSError, ValueError) as exc:
            raise FormalRunnerPathError(
                f"cannot safely inspect atomic-write orphans below {parent}: {exc}"
            ) from exc
        patterns = tuple(
            re.compile(rf"^\.{re.escape(target_name)}\.[a-z0-9_]{{8}}\.tmp$")
            for target_name in sorted(target_names)
        )
        for candidate in entries:
            if not any(pattern.fullmatch(candidate.name) for pattern in patterns):
                continue
            if _is_link_like(candidate) or not candidate.is_file():
                # Leave hostile lookalikes in place so the normal unexpected-entry gate rejects.
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise FormalRunnerPathError(
                    f"atomic-write orphan escapes publication root: {candidate}: {exc}"
                ) from exc
            if resolved.parent != resolved_parent:
                raise FormalRunnerPathError(
                    "atomic-write orphan is not a direct child of its target directory: "
                    f"{candidate}"
                )
            try:
                candidate.unlink()
            except OSError as exc:
                raise FormalRunnerWriteError(
                    f"cannot remove verified atomic-write orphan {candidate}: {exc}"
                ) from exc


def _preflight_output(
    root: Path,
    bundles: tuple[_SuiteBundle, ...],
    cohort_content: bytes,
) -> _OutputPlan:
    """Inspect every target before the first artifact write and return a continuation plan."""

    expected_targets = tuple(
        [
            _contained_output_path(root, bundle.suite_id, "traces", trace.filename)
            for bundle in bundles
            for trace in bundle.traces
        ]
        + [_contained_output_path(root, bundle.suite_id, "report.json") for bundle in bundles]
        + [_contained_output_path(root, "cohort.json")]
    )
    _recover_atomic_write_orphans(root, expected_targets)
    _reject_unexpected_entries(root, {*FORMAL_SUITE_IDS, "cohort.json"})
    run_dirs: dict[str, Path] = {}
    artifacts: list[_OutputArtifact] = []
    for bundle in bundles:
        suite_dir = _contained_output_path(root, bundle.suite_id)
        traces_dir = _contained_output_path(root, bundle.suite_id, "traces")
        expected_trace_files = {trace.filename for trace in bundle.traces}
        _inspect_existing_directory(
            suite_dir,
            root=root,
            allowed={"report.json", "traces"},
        )
        _inspect_existing_directory(
            traces_dir,
            root=root,
            allowed=expected_trace_files,
        )
        run_dirs[bundle.suite_id] = suite_dir
        artifacts.extend(
            _OutputArtifact(
                suite_id=bundle.suite_id,
                path=_contained_output_path(root, bundle.suite_id, "traces", trace.filename),
                content=trace.content,
            )
            for trace in bundle.traces
        )
        artifacts.append(
            _OutputArtifact(
                suite_id=bundle.suite_id,
                path=_contained_output_path(root, bundle.suite_id, "report.json"),
                content=bundle.report_bytes,
            )
        )

    cohort_artifact = _OutputArtifact(
        suite_id="cohort",
        path=_contained_output_path(root, "cohort.json"),
        content=cohort_content,
    )
    missing_suite_artifacts = tuple(
        artifact for artifact in artifacts if _inspect_expected_artifact(artifact)
    )
    cohort_missing = _inspect_expected_artifact(cohort_artifact)
    if not cohort_missing and missing_suite_artifacts:
        # A completion record cannot be used as a resumable partial-run marker. This state can
        # arise only after external deletion, because the runner always writes cohort.json last.
        missing = [str(artifact.path) for artifact in missing_suite_artifacts]
        raise FormalRunnerCollisionError(
            f"completed cohort is missing referenced suite artifacts: {missing}"
        )
    return _OutputPlan(
        root=root,
        run_dirs=run_dirs,
        suite_artifacts=tuple(artifacts),
        missing_suite_artifacts=missing_suite_artifacts,
        cohort_path=cohort_artifact.path,
        cohort_content=cohort_content,
        cohort_missing=cohort_missing,
    )


def _write_output_plan(plan: _OutputPlan) -> None:
    """Write missing suite artifacts first and the immutable completion cohort last."""

    for artifact in plan.missing_suite_artifacts:
        relative_parent = artifact.path.parent.relative_to(plan.root)
        _ensure_directory(plan.root, *relative_parent.parts)
        try:
            atomic_write_text(artifact.path, artifact.content.decode("utf-8"))
        except OSError as exc:
            raise FormalRunnerWriteError(
                f"atomic write failed for suite {artifact.suite_id} below {plan.root}: {exc}"
            ) from exc
    if plan.cohort_missing:
        try:
            atomic_write_text(plan.cohort_path, plan.cohort_content.decode("utf-8"))
        except OSError as exc:
            raise FormalRunnerWriteError(
                f"atomic completion write failed below {plan.root}: {exc}"
            ) from exc


def _postflight_expected_publication(plan: _OutputPlan) -> None:
    """Bind the returned result to the exact suite and cohort bytes requested by this call."""

    expected = (
        *plan.suite_artifacts,
        _OutputArtifact(
            suite_id="cohort",
            path=plan.cohort_path,
            content=plan.cohort_content,
        ),
    )
    for artifact in expected:
        try:
            missing = _inspect_expected_artifact(artifact)
        except FormalRunnerPathError as exc:
            raise FormalRunnerCollisionError(
                f"published bytes changed before result finalization: {artifact.path}: {exc}"
            ) from exc
        if missing:
            raise FormalRunnerCollisionError(
                f"published artifact disappeared before result finalization: {artifact.path}"
            )


def _verify_runs_adapter(
    run_dirs: tuple[Path, ...], formal_root: Path
) -> FormalArtifactVerification:
    """Keep the formal_artifacts API dependency isolated in one compatibility seam."""

    try:
        return verify_formal_eval_runs(run_dirs, formal_root=formal_root)
    except (TypeError, AttributeError) as exc:  # pragma: no cover - future API drift guard
        raise FormalRunnerVerifierError(f"formal_artifacts verifier API changed: {exc}") from exc


def run_formal_evals(
    output_dir: str | Path,
    *,
    formal_root: str | Path | None = None,
    run_id: str = DEFAULT_FORMAL_RUN_ID,
    generated_at: str | datetime = DEFAULT_FORMAL_GENERATED_AT,
) -> FormalRunnerResult:
    """Run all four frozen control pairs and emit verifier-compatible protocol artifacts."""

    base_run_id = _validate_run_id(run_id)
    timestamp = _parse_generated_at(generated_at)
    root = _resolve_formal_root(formal_root)
    try:
        verify_frozen_control_fixtures(root / "controls")
    except FrozenControlMismatchError as exc:
        raise FormalRunnerInputError(f"frozen control verification failed: {exc}") from exc
    governance = verify_frozen_formal_artifacts(root)
    if not governance.ok:
        raise FormalRunnerInputError(
            f"frozen formal governance verification failed: {_verification_summary(governance)}"
        )

    inputs = tuple(_load_suite_inputs(root, suite_id) for suite_id in FORMAL_SUITE_IDS)
    bundles = tuple(
        _build_suite_bundle(item, base_run_id=base_run_id, generated_at=timestamp)
        for item in inputs
    )
    try:
        governance_sha256 = compute_formal_governance_sha256(root)
        cohort_content = _build_cohort_content(
            bundles,
            base_run_id=base_run_id,
            generated_at=timestamp,
            governance_sha256=governance_sha256,
        )
    except FormalCohortError as exc:
        raise FormalRunnerInputError(f"cannot freeze formal cohort: {exc}") from exc
    output_root = _prepare_output_root(output_dir, root)
    with _publication_root_lock(output_root):
        output_plan = _preflight_output(output_root, bundles, cohort_content)
        _write_output_plan(output_plan)
        ordered_run_dirs = tuple(output_plan.run_dirs[suite_id] for suite_id in FORMAL_SUITE_IDS)
        verification = _verify_runs_adapter(ordered_run_dirs, root)
        _postflight_expected_publication(output_plan)
    suite_results = tuple(
        FormalSuiteRunResult(
            suite_id=bundle.suite_id,
            run_id=bundle.run_id,
            run_dir=output_plan.run_dirs[bundle.suite_id],
            harness_status=bundle.report.harness_status,
            evaluation_status=bundle.report.evaluation_status,
            trace_count=len(bundle.traces),
            harness_ok=bundle.harness_ok,
        )
        for bundle in bundles
    )
    return FormalRunnerResult(
        output_root=output_root,
        suites=suite_results,
        verification=verification,
    )


__all__ = [
    "DEFAULT_FORMAL_GENERATED_AT",
    "DEFAULT_FORMAL_RUN_ID",
    "FORMAL_SUITE_IDS",
    "HARNESS_METRIC_NAME",
    "FormalRunnerCollisionError",
    "FormalRunnerError",
    "FormalRunnerInputError",
    "FormalRunnerPathError",
    "FormalRunnerResult",
    "FormalRunnerVerifierError",
    "FormalRunnerWriteError",
    "FormalSuiteRunResult",
    "run_formal_evals",
]
